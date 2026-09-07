from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession
from loguru import logger

from ..models.basic import CustomField, CustomFieldBelonging, CustomFieldOption, EntityFieldValue, EntityI18n
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import CustomFieldCreate, CustomFieldListItem, CustomFieldUpdate
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode
from app.internal.cms_biz_system.services.dict_service import get_multi_language_options

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
    existing = (
        await db.execute(
            select(CustomField.id)
            .where(
                CustomField.field_name == data.field_name,
                CustomField.is_deleted.is_(False),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    if existing:
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

async def _migrate_field_values_on_multi_language_change(
    db: AsyncSession, cf: CustomField, new_multi_language: bool
) -> None:
    """
    multi_language 开关切换时迁移存量字段值，保证存储轨道与字段定义一致。

    多语言字段值存 entity_i18n（每语言一行），非多语言字段值存 entity_field_value。
    若不迁移，切换开关后已有值将无法被读取（编辑/详情回显为空、C2 同步丢字段）。

    - True→False：将 entity_i18n 存量值（默认语言优先，回退语言顺序第一个非空值）
      迁移到 entity_field_value，并清理该字段全部 entity_i18n 行；
    - False→True：将 entity_field_value 存量值复制到 entity_i18n 的所有语言，
      并清理该字段全部 entity_field_value 行。

    注意：先清理目标轨道旧数据再插入，保证迁移幂等（来回切换不产生重复行）。
    """
    lang_options = await get_multi_language_options(db)
    languages = [opt.code for opt in lang_options] or ["en"]
    default_lang = languages[0]

    if new_multi_language:
        # False→True：单值复制到所有语言
        await db.execute(
            EntityI18n.__table__.delete().where(EntityI18n.field_name == cf.field_code)
        )
        rows = (await db.execute(
            select(EntityFieldValue).where(
                EntityFieldValue.custom_field_id == cf.id,
                EntityFieldValue.is_deleted.is_(False),
            )
        )).scalars().all()
        for row in rows:
            if not row.value:
                continue
            for lang in languages:
                db.add(EntityI18n(
                    entity_type=row.entity_type,
                    entity_id=row.entity_id,
                    language=lang,
                    field_name=cf.field_code,
                    value=row.value,
                ))
        await db.execute(
            EntityFieldValue.__table__.delete().where(EntityFieldValue.custom_field_id == cf.id)
        )
        logger.info(
            f"自定义字段 {cf.field_code}({cf.field_name}) 切换为多语言，已将 {len(rows)} 条单值迁移至各语言"
        )
    else:
        # True→False：多语言值收敛为单值（默认语言优先，回退语言顺序第一个非空值）
        await db.execute(
            EntityFieldValue.__table__.delete().where(EntityFieldValue.custom_field_id == cf.id)
        )
        rows = (await db.execute(
            select(EntityI18n).where(
                EntityI18n.field_name == cf.field_code,
                EntityI18n.is_deleted.is_(False),
            )
        )).scalars().all()
        grouped: dict[tuple[str, int], dict[str, str]] = {}
        for row in rows:
            if not row.value:
                continue
            grouped.setdefault((row.entity_type, row.entity_id), {})[row.language] = row.value
        for (entity_type, entity_id), lang_vals in grouped.items():
            value = next((lang_vals[lang] for lang in languages if lang_vals.get(lang)), None)
            if value is None:
                value = next(iter(lang_vals.values()))
            db.add(EntityFieldValue(
                entity_type=entity_type,
                entity_id=entity_id,
                custom_field_id=cf.id,
                value=value,
            ))
        await db.execute(
            EntityI18n.__table__.delete().where(EntityI18n.field_name == cf.field_code)
        )
        logger.info(
            f"自定义字段 {cf.field_code}({cf.field_name}) 切换为非多语言（默认语言 {default_lang}），"
            f"已将 {len(grouped)} 个实体的多语言值迁移为单值"
        )


async def update_custom_field(db: AsyncSession, field_id: int, data: CustomFieldUpdate) -> CustomField:
    cf = await get_custom_field(db, field_id)
    old_multi_language = cf.multi_language
    new_field_name = data.field_name if data.field_name is not None else cf.field_name

    if new_field_name != cf.field_name:
        existing = (
            await db.execute(
                select(CustomField.id)
                .where(
                    CustomField.field_name == new_field_name,
                    CustomField.id != field_id,
                    CustomField.is_deleted.is_(False),
                )
                .limit(1)
            )
        ).scalar_one_or_none()
        if existing:
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

    # multi_language 开关变化时，与字段定义更新在同一事务内迁移存量值，保证原子性
    if data.multi_language is not None and data.multi_language != old_multi_language:
        await _migrate_field_values_on_multi_language_change(db, cf, data.multi_language)

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
