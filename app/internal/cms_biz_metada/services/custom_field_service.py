from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import CustomField, CustomFieldBelonging, CustomFieldOption
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import CustomFieldCreate, CustomFieldListItem, CustomFieldUpdate
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode

async def list_custom_fields(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    field_name: str | None = None,
    field_type: str | None = None,
    belongings: list[str] | None = None,
    mandatory: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> dict:
    query = select(CustomField).where(CustomField.is_deleted.is_(False))
    if field_name:
        query = query.where(CustomField.field_name.ilike(f"%{field_name}%"))
    if field_type:
        query = query.where(CustomField.field_type == field_type)
    if mandatory is not None:
        query = query.where(CustomField.mandatory == mandatory)
    if belongings:
        effective_belongings = set(belongings)
        if effective_belongings - {'ALL'}:
            effective_belongings.add('ALL')
        subq = (
            select(CustomFieldBelonging.custom_field_id)
            .where(CustomFieldBelonging.belonging.in_(effective_belongings))
            .distinct()
        )
        query = query.where(CustomField.id.in_(subq))

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(CustomField, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, CustomField.id.desc())
        else:
            query = query.order_by(CustomField.id.desc())
    else:
        query = query.order_by(CustomField.id.desc())

    total = (await db.execute(select(func.count()).select_from(query.subquery()))).scalar_one()
    rows = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return {
        "total": total,
        "page": page,
        "page_size": page_size,
        "items": [CustomFieldListItem.from_orm(r) for r in rows],
    }

async def _get_belongings(db: AsyncSession, field_id: int) -> list[str]:
    rows = (await db.execute(
        select(CustomFieldBelonging.belonging).where(CustomFieldBelonging.custom_field_id == field_id)
    )).scalars().all()
    return list(rows)


async def get_custom_field(db: AsyncSession, field_id: int) -> CustomField:
    cf = (await db.execute(select(CustomField).where(CustomField.id == field_id, CustomField.is_deleted.is_(False)))).scalar_one_or_none()
    if not cf:
        raise NotFoundException(ErrorCode.CUSTOM_FIELD_NOT_FOUND, get_msg("CUSTOM_FIELD_NOT_FOUND"))
    return cf

def _validate_option_codes(options: list) -> None:
    """校验下拉框选项编码不可重复"""
    if not options:
        return
    codes = [opt.code for opt in options if opt.code]
    if len(codes) != len(set(codes)):
        raise BusinessException(ErrorCode.CUSTOM_FIELD_OPTION_CODE_DUPLICATE, get_msg("CUSTOM_FIELD_OPTION_CODE_DUPLICATE"))


async def create_custom_field(db: AsyncSession, data: CustomFieldCreate) -> CustomField:
    existing_belonging_ids = (
        await db.execute(
            select(CustomFieldBelonging.custom_field_id)
            .where(CustomFieldBelonging.belonging.in_(data.belongings))
            .distinct()
        )
    ).scalars().all()
    if existing_belonging_ids:
        duplicate = (
            await db.execute(
                select(CustomField.id)
                .where(
                    CustomField.id.in_(existing_belonging_ids),
                    CustomField.field_name == data.field_name,
                    CustomField.is_deleted.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if duplicate:
            raise BusinessException(ErrorCode.CUSTOM_FIELD_NAME_EXISTS, get_msg("CUSTOM_FIELD_NAME_EXISTS"))
    _validate_option_codes(data.options)
    cf = CustomField(
        field_name=data.field_name,
        field_code="cf_tmp",
        field_type=data.field_type,
        mandatory=data.mandatory,
        multi_language=data.multi_language,
        tip=data.tip,
    )
    db.add(cf)
    await db.flush()
    cf.field_code = f"cf_{cf.id}"
    for b in data.belongings:
        db.add(CustomFieldBelonging(custom_field_id=cf.id, belonging=b))
    for idx, opt in enumerate(data.options):
        db.add(CustomFieldOption(
            custom_field_id=cf.id,
            code=opt.code,
            names=opt.names,
            sort_order=opt.sort_order if opt.sort_order else idx,
        ))
    await db.commit()
    await db.refresh(cf)
    return cf

async def update_custom_field(db: AsyncSession, field_id: int, data: CustomFieldUpdate) -> CustomField:
    cf = await get_custom_field(db, field_id)
    new_field_name = data.field_name if data.field_name is not None else cf.field_name
    new_belongings = data.belongings if data.belongings is not None else (await _get_belongings(db, field_id))

    if new_field_name != cf.field_name or (data.belongings is not None and set(data.belongings) != set(await _get_belongings(db, field_id))):
        existing_belonging_ids = (
            await db.execute(
                select(CustomFieldBelonging.custom_field_id)
                .where(CustomFieldBelonging.belonging.in_(new_belongings))
                .distinct()
            )
        ).scalars().all()
        if existing_belonging_ids:
            duplicate = (
                await db.execute(
                    select(CustomField.id)
                    .where(
                        CustomField.id.in_(existing_belonging_ids),
                        CustomField.field_name == new_field_name,
                        CustomField.id != field_id,
                        CustomField.is_deleted.is_(False),
                    )
                    .limit(1)
                )
            ).scalar_one_or_none()
            if duplicate:
                raise BusinessException(ErrorCode.CUSTOM_FIELD_NAME_EXISTS, get_msg("CUSTOM_FIELD_NAME_EXISTS"))

    if data.field_name is not None:
        cf.field_name = data.field_name
    if data.field_type is not None:
        cf.field_type = data.field_type
    if data.mandatory is not None:
        cf.mandatory = data.mandatory
    if data.multi_language is not None:
        cf.multi_language = data.multi_language
    if data.tip is not None:
        cf.tip = data.tip

    if data.belongings is not None:
        await db.execute(
            CustomFieldBelonging.__table__.delete().where(CustomFieldBelonging.custom_field_id == cf.id)
        )
        for b in data.belongings:
            db.add(CustomFieldBelonging(custom_field_id=cf.id, belonging=b))

    if data.options is not None:
        _validate_option_codes(data.options)
        await db.execute(
            CustomFieldOption.__table__.delete().where(CustomFieldOption.custom_field_id == cf.id)
        )
        for idx, opt in enumerate(data.options):
            db.add(CustomFieldOption(
                custom_field_id=cf.id,
                code=opt.code,
                names=opt.names,
                sort_order=opt.sort_order if opt.sort_order else idx,
            ))

    await db.commit()
    await db.refresh(cf)
    return cf

async def delete_custom_field(db: AsyncSession, field_id: int) -> None:
    cf = await get_custom_field(db, field_id)
    cf.is_deleted = True
    await db.commit()

async def batch_delete_custom_fields(db: AsyncSession, req: BatchDeleteRequest) -> int:
    cfs = (await db.execute(select(CustomField).where(CustomField.id.in_(req.ids), CustomField.is_deleted.is_(False)))).scalars().all()
    for cf in cfs:
        cf.is_deleted = True
    await db.commit()
    return len(cfs)
