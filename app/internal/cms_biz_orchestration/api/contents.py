"""
内容（Content）API 路由层 — 交易管理视角。

路由前缀：/contents

接口列表：
    GET    /                         查询内容列表（分页 + 多维过滤）
    POST   /                         新建内容（含 SERIES/SEASON 子节点自动创建）
    GET    /without-license-count    统计无许可证内容数量（快捷按钮数据）
    GET    /series-simple            获取 SERIES 简要列表（EPISODE 父级下拉）
    GET    /channels-simple          获取 CHANNEL 简要列表（SCHEDULE 频道下拉）
    GET    /{content_id}             查询单个内容详情
    PUT    /{content_id}             编辑内容
    DELETE /{content_id}             软删除内容（级联删除子节点）
    GET    /{content_id}/licenses    查询内容关联许可证列表
    GET    /{content_id}/adjacent    查询上一条/下一条内容 ID
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_package.models.package import Content
from app.internal.cms_biz_orchestration.schemas.content import (
    AdjacentContentResponse,
    BatchImportRequest,
    BatchImportResponse,
    ContentCreate,
    ContentListItem,
    ContentSimpleItem,
    ContentUpdate,
    ContentLicenseRef,
    ContentDetailResponse,
)
from app.common.schemas import PaginatedResponse, BatchDeleteRequest
from app.internal.cms_biz_orchestration.services import content_service
from app.internal.cms_biz_orchestration.services import episode_history_service
from app.internal.cms_biz_orchestration.services import batch_content_service
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.internal.cms_biz_package.models.enums import ContentType
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    save_field_values,
    get_i18n_values,
    save_i18n_values,
)
from app.internal.cms_biz_metada.schemas.basic import (
    EntityFieldValueItem,
    EntityFieldValuesPayload,
    EntityI18nItem,
    EntityI18nPayload,
)

router = APIRouter(prefix="/contents")


@router.get("/", response_model=PaginatedResponse[ContentListItem])
async def get_content_list(
    page: int = 1,
    page_size: int = 10,
    content_id: int | None = None,
    title: str | None = None,
    content_types: list[str] | None = Query(default=None),
    statuses: list[str] | None = Query(default=None),
    genre_ids: list[int] | None = Query(default=None),
    custom_tag_ids: list[int] | None = Query(default=None),
    parent_id: int | None = None,
    created_from: str | None = None,
    created_to: str | None = None,
    without_license: bool = False,
    license_start_from: str | None = None,
    license_start_to: str | None = None,
    license_end_from: str | None = None,
    license_end_to: str | None = None,
    is_discarded: bool | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await content_service.list_contents(
        db, page, page_size, content_id, title,
        content_types, statuses, genre_ids, custom_tag_ids, parent_id,
        created_from, created_to,
        without_license,
        license_start_from, license_start_to,
        license_end_from, license_end_to,
        is_discarded=is_discarded,
        sort_by=sort_by, sort_order=sort_order,
        current_user=current_user,
    )


@router.post("/", response_model=ContentListItem)
async def create_content_api(
    body: ContentCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content = await content_service.create_content(db, body)

    # 如果创建的是 EPISODE 或 SERIES 类型，记录操作历史
    if content.content_type in ("EPISODE", "SERIES") and content.parent_id:
        processed_by = f"{current_user.username}({current_user.id})"
        series_ordinal = content.series_ordinal if content.content_type == "SERIES" else None

        await episode_history_service.add_episode_history(
            db,
            parent_id=content.parent_id,
            content_id=content.id,
            content_name=content.title,
            content_type=content.content_type,
            series_ordinal=series_ordinal,
            processed_by=processed_by,
            processed_type="Add",
            created_by=current_user.id,
        )

    if content.content_type == ContentType.EPISODE.value:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.EPISODE_INJECT,
            operation_object=f"单集注入 {content.title}",
            operation_content=f"log.episode.inject:{content.title}",
            content_id=content.id,
            ip_address=_get_ip(request),
            result="success",
        )
    else:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_CREATE,
            operation_object=f"内容 {content.title}",
            operation_content=f"Created content: title={content.title}, type={content.content_type}",
            content_id=content.id,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return content


@router.post("/batch", response_model=BatchImportResponse)
async def batch_create_contents_api(
    body: BatchImportRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量导入子内容（EPISODE/SERIES），单个事务控制。"""
    try:
        results, parent_ctype = await batch_content_service.batch_create_sub_contents(
            db, parent_id=body.parent_id, items=body.items, current_user=current_user,
        )

        success_count = sum(1 for r in results if r.success)
        failed_count = sum(1 for r in results if not r.success)

        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_CREATE,
            operation_object=f"批量导入 {success_count + failed_count} 个子内容",
            operation_content=f"批量导入: parent_id={body.parent_id}, 成功={success_count}, 失败={failed_count}",
            content_id=body.parent_id,
            ip_address=_get_ip(request),
            result="success" if failed_count == 0 else "partial",
        )

        # 统一提交：内容 + 历史 + 日志 都在同一个事务中
        await db.commit()

        return BatchImportResponse(
            success_count=success_count,
            failed_count=failed_count,
            details=results,
        )
    except Exception:
        await db.rollback()
        raise


@router.get("/without-license-count")
async def get_without_license_count(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = await content_service.get_without_license_count(db, current_user=current_user)
    return {"count": count}


@router.get("/series-simple", response_model=list[ContentSimpleItem])
async def get_series_simple(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await content_service.get_series_simple(db, current_user=current_user)


@router.get("/channels-simple", response_model=list[ContentSimpleItem])
async def get_channels_simple(
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    return await content_service.get_channels_simple(db, current_user=current_user)


@router.get("/{content_id}", response_model=ContentDetailResponse)
async def get_content_detail(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await content_service.get_content(db, content_id)


@router.put("/{content_id}", response_model=ContentListItem)
async def update_content_api(
    content_id: int,
    body: ContentUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_detail = await content_service.get_content(db, content_id)
    old = old_detail.content
    content = await content_service.update_content(db, content_id, body)
    from app.internal.cms_biz_package.models.enums import ContentType
    if old.content_type in (ContentType.CHANNEL.value, ContentType.SCHEDULE.value):
        await db.commit()
        return content
    old_data = orm_to_dict(old, "content")
    new_data = orm_to_dict(content, "content")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "content", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_EDIT,
        operation_object=f"内容 {old.title}",
        operation_content=f"编辑内容 {old.title}",
        content_id=content_id,
        entity_type="content",
        entity_id=content_id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return content


@router.delete("/{content_id}")
async def delete_content_api(
    content_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    content_detail = await content_service.get_content(db, content_id)
    content_title = content_detail.content.title
    content_type = content_detail.content.content_type
    parent_id = content_detail.content.parent_id

    # 如果删除的是 EPISODE 或 SERIES 类型，记录操作历史（在删除前记录）
    if content_type in ("EPISODE", "SERIES") and parent_id:
        processed_by = f"{current_user.username}({current_user.id})"

        # 如果是 SERIES 类型，需要获取 series_ordinal
        series_ordinal = None
        if content_type == "SERIES":
            series_ordinal = getattr(content_detail.content, 'series_ordinal', None)

        await episode_history_service.add_episode_history(
            db,
            parent_id=parent_id,
            content_id=content_id,
            content_name=content_title,
            content_type=content_type,
            series_ordinal=series_ordinal,
            processed_by=processed_by,
            processed_type="Delete",
            created_by=current_user.id,
        )

    await content_service.delete_content(db, content_id)
    if content_type == ContentType.EPISODE.value:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.EPISODE_REMOVE,
            operation_object=f"单集移除 {content_title}",
            operation_content=f"log.episode.remove:{content_title}",
            content_id=content_id,
            ip_address=_get_ip(request),
            result="success",
        )
    else:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_DELETE,
            operation_object=f"内容 {content_title}",
            operation_content=f"Deleted content: ID={content_id}, title={content_title}",
            content_id=content_id,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return {"success": True}


@router.post("/batch-delete")
async def batch_delete_contents_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    rows = (await db.execute(select(Content.title).where(Content.id.in_(body.ids)))).scalars().all()
    content_names = ", ".join(rows) if rows else str(body.ids)
    deleted = await content_service.batch_delete_contents(db, body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONTENT_BATCH_DELETE,
        operation_object=f"内容 {content_names}",
        operation_content=f"批量删除内容: {content_names}",
        ip_address=_get_ip(request),
        result="success",
        entity_type="content",
    )
    await db.commit()
    return {"deleted": deleted}


@router.get("/{content_id}/licenses", response_model=list[ContentLicenseRef])
async def get_content_licenses(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await content_service.get_content_licenses(db, content_id)


@router.get("/{content_id}/adjacent", response_model=AdjacentContentResponse)
async def get_adjacent_content(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """查询上一条/下一条内容 ID（用于详情页记录导航）。"""
    return await content_service.get_adjacent_content(db, content_id, current_user)


# ---------- Custom Field Values ----------

@router.get("/{content_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_content_field_values(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_field_values(db, "Content", content_id)


@router.put("/{content_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_content_field_values(
    content_id: int,
    body: EntityFieldValuesPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.internal.cms_biz_package.models.enums import ContentType
    content_obj = (await db.execute(select(Content.content_type).where(Content.id == content_id))).scalar_one_or_none()
    if content_obj in (ContentType.CHANNEL.value, ContentType.SCHEDULE.value):
        import json
        from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption
        from app.internal.cms_biz_metada.services.entity_data_service import get_field_values
        old_fv = await get_field_values(db, "Content", content_id)
        old_fv_map = {r.custom_field_id: r.value for r in old_fv}
    result = await save_field_values(db, "Content", content_id, body)
    if content_obj in (ContentType.CHANNEL.value, ContentType.SCHEDULE.value):
        field_ids = [v.custom_field_id for v in body.values if v.value is not None and v.value != ""]
        if field_ids:
            cf_rows = (await db.execute(select(CustomField).where(CustomField.id.in_(field_ids), CustomField.is_deleted.is_(False)))).scalars().all()
            field_info = {cf.id: cf for cf in cf_rows}
            dropdown_ids = [cf.id for cf in cf_rows if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select')]
            option_map: dict[int, dict[str, str]] = {}
            if dropdown_ids:
                opt_rows = (await db.execute(select(CustomFieldOption).where(CustomFieldOption.custom_field_id.in_(dropdown_ids)))).scalars().all()
                for opt in opt_rows:
                    if opt.custom_field_id not in option_map:
                        option_map[opt.custom_field_id] = {}
                    names = opt.names or {}
                    display_name = names.get('cn') or names.get('zh-CN') or next(iter(names.values()), None) or opt.code
                    option_map[opt.custom_field_id][opt.code] = display_name
            field_data = {}
            for v in body.values:
                if v.value is not None and v.value != "" and v.custom_field_id in field_info:
                    cf = field_info[v.custom_field_id]
                    if old_fv_map.get(v.custom_field_id) == v.value:
                        continue
                    raw_value = v.value
                    if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select') and v.custom_field_id in option_map:
                        codes = raw_value.split(',')
                        translated = [option_map[v.custom_field_id].get(c.strip(), c.strip()) for c in codes]
                        field_data[cf.field_name] = ','.join(translated)
                    else:
                        field_data[cf.field_name] = raw_value
            if field_data:
                upd_val = json.dumps(field_data, ensure_ascii=False, default=str)
                op_type = OperationType.CHANNEL_FIELD_UPDATE if content_obj == ContentType.CHANNEL.value else OperationType.SCHEDULE_FIELD_UPDATE
                await write_log(
                    db,
                    user_id=current_user.id,
                    user_name=current_user.username,
                    operation_type=op_type,
                    operation_object="log.field.edit",
                    operation_content="log.field.edit",
                    content_id=content_id,
                    entity_type="content",
                    entity_id=content_id,
                    previous_value=None,
                    updated_value=upd_val,
                    updated_value_json=None,
                    ip_address=_get_ip(request),
                    result="success",
                )
    elif content_obj:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CONTENT_EDIT,
            operation_object=f"内容 ID={content_id} 自定义字段",
            operation_content=f"编辑自定义字段",
            content_id=content_id,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


# ---------- I18n Values ----------

@router.get("/{content_id}/i18n", response_model=list[EntityI18nItem])
async def get_content_i18n(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await get_i18n_values(db, "Content", content_id)


@router.put("/{content_id}/i18n", response_model=list[EntityI18nItem])
async def save_content_i18n(
    content_id: int,
    body: EntityI18nPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    from app.internal.cms_biz_package.models.enums import ContentType
    content_obj = (await db.execute(select(Content.content_type).where(Content.id == content_id))).scalar_one_or_none()
    if content_obj in (ContentType.CHANNEL.value, ContentType.SCHEDULE.value):
        import json
        from app.internal.cms_biz_metada.services.entity_data_service import get_i18n_values
        from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption
        old_i18n = await get_i18n_values(db, "Content", content_id)
        old_map = {r.field_name: r.value for r in old_i18n if r.language == body.language}
        changed_fields = {}
        for k, v in body.fields.items():
            if v is not None and v != "" and old_map.get(k) != v:
                changed_fields[k] = v
        cf_field_codes = [k[3:] for k in changed_fields if k.startswith('cf_')]
        cf_info: dict[str, CustomField] = {}
        option_map: dict[str, dict[str, str]] = {}
        if cf_field_codes:
            cf_rows = (await db.execute(select(CustomField).where(CustomField.field_code.in_(cf_field_codes), CustomField.is_deleted.is_(False)))).scalars().all()
            dropdown_ids = []
            for cf in cf_rows:
                cf_info[cf.field_code] = cf
                if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select'):
                    dropdown_ids.append(cf.id)
            if dropdown_ids:
                opt_rows = (await db.execute(select(CustomFieldOption).where(CustomFieldOption.custom_field_id.in_(dropdown_ids)))).scalars().all()
                id_to_code = {cf.id: cf.field_code for cf in cf_rows if cf.id in dropdown_ids}
                for opt in opt_rows:
                    fc = id_to_code.get(opt.custom_field_id)
                    if fc and fc not in option_map:
                        option_map[fc] = {}
                    if fc:
                        names = opt.names or {}
                        display_name = names.get('cn') or names.get('zh-CN') or next(iter(names.values()), None) or opt.code
                        option_map[fc][opt.code] = display_name
        translated_fields = {}
        for k, v in changed_fields.items():
            if k.startswith('cf_') and k[3:] in cf_info:
                cf = cf_info[k[3:]]
                if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select') and cf.field_code in option_map:
                    codes = v.split(',')
                    translated = [option_map[cf.field_code].get(c.strip(), c.strip()) for c in codes]
                    translated_fields[cf.field_name] = ','.join(translated)
                else:
                    translated_fields[cf.field_name] = v
            else:
                translated_fields[k] = v
        fk_fields = {'tag_ids': ('tag', '标签'), 'genre_ids': ('genre', '题材')}
        for fk_key, (table, label) in fk_fields.items():
            if fk_key in translated_fields:
                from app.database import Base
                ids_str = translated_fields.pop(fk_key)
                ids = [int(x.strip()) for x in ids_str.split(',') if x.strip()]
                if ids:
                    model_cls = None
                    for mapper in Base.registry.mappers:
                        cls = mapper.class_
                        if getattr(cls, "__tablename__", None) == table:
                            model_cls = cls
                            break
                    if model_cls:
                        rows = (await db.execute(select(model_cls).where(model_cls.id.in_(ids), model_cls.language == body.language, model_cls.is_deleted.is_(False)))).scalars().all()
                        name_map = {r.id: r.name for r in rows}
                        names = [name_map.get(i, str(i)) for i in ids]
                        translated_fields[label] = ','.join(names)
                    else:
                        translated_fields[label] = ids_str
        if translated_fields:
            upd_val = json.dumps(translated_fields, ensure_ascii=False, default=str)
            op_type = OperationType.CHANNEL_I18N_UPDATE if content_obj == ContentType.CHANNEL.value else OperationType.SCHEDULE_I18N_UPDATE
            await write_log(
                db,
                user_id=current_user.id,
                user_name=current_user.username,
                operation_type=op_type,
                operation_object="log.i18n.edit",
                operation_content=f"log.i18n.edit:{body.language}",
                content_id=content_id,
                entity_type="content",
                entity_id=content_id,
                previous_value=None,
                updated_value=upd_val,
                updated_value_json=None,
                ip_address=_get_ip(request),
                result="success",
            )
    result = await save_i18n_values(db, "Content", content_id, body)
    if content_obj and content_obj not in (ContentType.CHANNEL.value, ContentType.SCHEDULE.value):
        import json
        from app.internal.cms_biz_metada.services.entity_data_service import get_i18n_values
        from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption
        old_i18n = await get_i18n_values(db, "Content", content_id)
        old_map = {r.field_name: r.value for r in old_i18n if r.language == body.language}
        changed_fields = {}
        for k, v in body.fields.items():
            if v is not None and v != "" and old_map.get(k) != v:
                changed_fields[k] = v
        cf_field_codes = [k[3:] for k in changed_fields if k.startswith('cf_')]
        cf_info: dict[str, CustomField] = {}
        option_map: dict[str, dict[str, str]] = {}
        if cf_field_codes:
            cf_rows = (await db.execute(select(CustomField).where(CustomField.field_code.in_(cf_field_codes)))).scalars().all()
            dropdown_ids = []
            for cf in cf_rows:
                cf_info[cf.field_code] = cf
                if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select'):
                    dropdown_ids.append(cf.id)
            if dropdown_ids:
                opt_rows = (await db.execute(select(CustomFieldOption).where(CustomFieldOption.custom_field_id.in_(dropdown_ids)))).scalars().all()
                id_to_code = {cf.id: cf.field_code for cf in cf_rows if cf.id in dropdown_ids}
                for opt in opt_rows:
                    fc = id_to_code.get(opt.custom_field_id)
                    if fc and fc not in option_map:
                        option_map[fc] = {}
                    if fc:
                        names = opt.names or {}
                        display_name = names.get('cn') or names.get('zh-CN') or next(iter(names.values()), None) or opt.code
                        option_map[fc][opt.code] = display_name
        translated_fields = {}
        for k, v in changed_fields.items():
            if k.startswith('cf_') and k[3:] in cf_info:
                cf = cf_info[k[3:]]
                if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select') and cf.field_code in option_map:
                    codes = v.split(',')
                    translated = [option_map[cf.field_code].get(c.strip(), c.strip()) for c in codes]
                    translated_fields[cf.field_name] = ','.join(translated)
                else:
                    translated_fields[cf.field_name] = v
            else:
                translated_fields[k] = v
        fk_fields = {'tag_ids': ('tag', '标签'), 'genre_ids': ('genre', '题材')}
        for fk_key, (table, label) in fk_fields.items():
            if fk_key in translated_fields:
                from app.database import Base
                ids_str = translated_fields.pop(fk_key)
                ids = [int(x.strip()) for x in ids_str.split(',') if x.strip()]
                if ids:
                    model_cls = None
                    for mapper in Base.registry.mappers:
                        cls = mapper.class_
                        if getattr(cls, "__tablename__", None) == table:
                            model_cls = cls
                            break
                    if model_cls:
                        rows = (await db.execute(select(model_cls).where(model_cls.id.in_(ids), model_cls.language == body.language, model_cls.is_deleted.is_(False)))).scalars().all()
                        name_map = {r.id: r.name for r in rows}
                        names = [name_map.get(i, str(i)) for i in ids]
                        translated_fields[label] = ','.join(names)
                    else:
                        translated_fields[label] = ids_str
        if translated_fields:
            upd_val = json.dumps(translated_fields, ensure_ascii=False, default=str)
            await write_log(
                db,
                user_id=current_user.id,
                user_name=current_user.username,
                operation_type=OperationType.VOD_I18N_UPDATE,
                operation_object="log.i18n.edit",
                operation_content=f"log.i18n.edit:{body.language}",
                content_id=content_id,
                entity_type="content",
                entity_id=content_id,
                previous_value=None,
                updated_value=upd_val,
                updated_value_json=None,
                ip_address=_get_ip(request),
                result="success",
            )
    await db.commit()
    return result
