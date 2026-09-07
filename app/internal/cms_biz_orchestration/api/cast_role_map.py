"""
人物角色关联 API 路由层。

路由前缀：/cast-role-maps

接口列表：
    GET    /cast-role-maps                     分页查询人物角色关联列表
    GET    /cast-role-maps/{map_id}            查询单个人物角色关联
    POST   /cast-role-maps                     创建人物角色关联
    PUT    /cast-role-maps/{map_id}            更新人物角色关联
    DELETE /cast-role-maps/{map_id}            删除人物角色关联（软删除）
"""

from fastapi import APIRouter, Depends, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values, resolve_option_display_name
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.internal.cms_biz_orchestration.schemas.cast_role_map import (
    CastRoleMapCreate,
    CastRoleMapItem,
    CastRoleMapListItem,
    CastRoleMapUpdate,
)
from app.internal.cms_biz_metada.schemas.basic import (
    EntityFieldValueItem,
    EntityFieldValuesPayload,
    EntityI18nItem,
    EntityI18nPayload,
)
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    save_field_values,
    get_i18n_values,
    save_i18n_values,
)
from app.internal.cms_biz_orchestration.services import cast_role_map_service

router = APIRouter(prefix="/cast-role-maps", tags=["人物角色关联"])

ENTITY_TYPE = "cast_role_map"


def _get_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _with_cast_name(val: str | None, cast_name: str | None) -> str | None:
    """updated_value JSON 首键并入人物名称，标识被编辑对象（前端 Details 字段行展示）。"""
    if not val:
        return val
    import json
    try:
        obj = json.loads(val)
        if isinstance(obj, dict):
            return json.dumps({"cast_name": cast_name, **obj}, ensure_ascii=False, default=str)
    except Exception:
        pass
    return val


@router.get("/", response_model=CastRoleMapListItem)
async def list_cast_role_maps(
    program_id: int | None = Query(None, description="节目ID"),
    movie_id: int | None = Query(None, description="影片ID"),
    cast_id: int | None = Query(None, description="演职人员ID"),
    content_id: int | None = Query(None, description="内容ID"),
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页条数"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """分页查询人物角色关联列表。"""
    return await cast_role_map_service.list_cast_role_maps(
        db,
        program_id=program_id,
        movie_id=movie_id,
        cast_id=cast_id,
        content_id=content_id,
        page=page,
        page_size=page_size,
    )


@router.get("/{map_id}", response_model=CastRoleMapItem)
async def get_cast_role_map(
    map_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单个人物角色关联。"""
    return await cast_role_map_service.get_cast_role_map(db, map_id)


@router.post("/", response_model=CastRoleMapItem)
async def create_cast_role_map(
    data: CastRoleMapCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建人物角色关联。"""
    result = await cast_role_map_service.create_cast_role_map(db, data, processed_by=current_user.username)
    new_data = orm_to_dict(result, "cast_role_map")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "cast_role_map", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_ROLE_MAP_CREATE,
        operation_object_code="OBJ_CAST", operation_object_params={"name": result.role_name},
        operation_content_code="log.metadata.create",
        content_id=data.content_id,
        entity_type="cast_role_map",
        entity_id=result.map_id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{map_id}", response_model=CastRoleMapItem)
async def update_cast_role_map(
    map_id: int,
    data: CastRoleMapUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新人物角色关联。"""
    import copy
    old = await cast_role_map_service.get_cast_role_map(db, map_id)
    old_data = copy.deepcopy(orm_to_dict(old, "cast_role_map")) if old else {}
    result = await cast_role_map_service.update_cast_role_map(db, map_id, data)
    new_data = orm_to_dict(result, "cast_role_map")
    # cast_name/cast_poster_url 为富化派生字段（old 快照经 get_cast_role_map 富化有值，
    # new 快照直接 model_validate ORM 恒为 None）。不剔除会产生伪 diff，
    # 导致仅编辑自定义字段（主表无任何变化）也误写日志。
    # role_code 为 CastRole 下拉的内部编码，前端仅要求展示 Cast 与 Role Name，一并剔除
    for derived in ("cast_name", "cast_poster_url", "role_code"):
        old_data.pop(derived, None)
        new_data.pop(derived, None)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "cast_role_map", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        cast_label = old.cast_name if old else None
        upd_val = _with_cast_name(upd_val, cast_label)
        raw_val = _with_cast_name(raw_val, cast_label)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CAST_ROLE_MAP_UPDATE,
            operation_object_code="OBJ_CAST", operation_object_params={"name": result.role_name},
            operation_content_code="log.cast.role.map.edit",
            content_id=result.content_id,
            entity_type="cast_role_map",
            entity_id=result.map_id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{map_id}")
async def delete_cast_role_map(
    map_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除人物角色关联（软删除）。"""
    # 删除前先查询关联信息，用于日志记录
    old = await cast_role_map_service.get_cast_role_map(db, map_id)
    old_data = orm_to_dict(old, "cast_role_map") if old else {}
    content_id = old.content_id if old else None
    role_name = old.role_name if old else None

    await cast_role_map_service.delete_cast_role_map(db, map_id, processed_by=current_user.username)

    # 写入内容详情页活动日志（与 create/update 保持一致）
    if content_id:
        prev_val, _upd_val, raw_val = await prepare_log_values(db, "cast_role_map", old_data, None)
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CAST_ROLE_MAP_DELETE,
            operation_object_code="OBJ_CAST", operation_object_params={"name": role_name},
            operation_content_code="log.metadata.delete",
            content_id=content_id,
            entity_type="cast_role_map",
            entity_id=map_id,
            previous_value=prev_val,
            updated_value=None,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return {"success": True}


@router.post("/batch", response_model=list[CastRoleMapItem])
async def batch_create_cast_role_maps(
    items: list[CastRoleMapCreate],
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量创建人物角色关联，只记录一次流程。"""
    import json
    from sqlalchemy import select
    from app.internal.cms_biz_metada.models.basic import Cast

    results = await cast_role_map_service.batch_create_cast_role_maps(
        db, items, processed_by=current_user.username
    )

    cast_ids = list(set(r.cast_id for r in results if r.cast_id))
    cast_map = {}
    if cast_ids:
        cast_rows = (await db.execute(select(Cast).where(Cast.id.in_(cast_ids)))).scalars().all()
        cast_map = {c.id: c.name for c in cast_rows}

    role_details = []
    for r in results:
        cast_name = cast_map.get(r.cast_id, str(r.cast_id))
        role_details.append(f"{r.role_name}：{cast_name}")

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_ROLE_MAP_LINK,
        operation_object_code="OBJ_CAST_ROLE_LINK",
        operation_content_code="LOG_CAST_LINK", operation_content_params={"count": len(role_details)},
        content_id=items[0].content_id if items else None,
        entity_type="cast_role_map",
        entity_id=results[0].map_id if results else None,
        updated_value=json.dumps({"roles": role_details}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return results


@router.post("/batch-delete")
async def batch_delete_cast_role_maps(
    request: Request,
    body: dict,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量删除人物角色关联，只记录一次流程。"""
    import json
    from sqlalchemy import select
    from app.internal.cms_biz_metada.models.basic import Cast
    from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap

    map_ids = body.get("map_ids", [])
    if not map_ids:
        return {"success": True}

    cast_role_maps = (await db.execute(select(CastRoleMap).where(CastRoleMap.map_id.in_(map_ids)))).scalars().all()

    cast_ids = list(set(m.cast_id for m in cast_role_maps if m.cast_id))
    cast_map = {}
    if cast_ids:
        cast_rows = (await db.execute(select(Cast).where(Cast.id.in_(cast_ids)))).scalars().all()
        cast_map = {c.id: c.name for c in cast_rows}

    role_details = []
    for m in cast_role_maps:
        cast_name = cast_map.get(m.cast_id, str(m.cast_id))
        role_details.append(f"{m.role_name}：{cast_name}")

    for map_id in map_ids:
        await cast_role_map_service.delete_cast_role_map(db, map_id, processed_by=current_user.username)

    content_id = cast_role_maps[0].content_id if cast_role_maps else None

    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CAST_ROLE_MAP_UNLINK,
        operation_object_code="OBJ_CAST_ROLE_UNLINK",
        operation_content_code="LOG_CAST_UNLINK", operation_content_params={"count": len(role_details)},
        content_id=content_id,
        entity_type="cast_role_map",
        entity_id=map_ids[0] if map_ids else None,
        updated_value=json.dumps({"roles": role_details}, ensure_ascii=False),
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/{map_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_cast_role_map_field_values(
    map_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单个人物角色关联的自定义字段值。"""
    await cast_role_map_service.get_cast_role_map(db, map_id)
    return await get_field_values(db, ENTITY_TYPE, map_id)


@router.put("/{map_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_cast_role_map_field_values(
    map_id: int,
    body: EntityFieldValuesPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """保存单个人物角色关联的自定义字段值（独立写 CAST_ROLE_MAP_FIELD_UPDATE 操作日志）。"""
    import json
    from sqlalchemy import select
    from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption

    map_item = await cast_role_map_service.get_cast_role_map(db, map_id)
    # 记录旧值用于 diff，仅变更字段写日志
    old_fv = await get_field_values(db, ENTITY_TYPE, map_id)
    old_fv_map = {r.custom_field_id: r.value for r in old_fv}
    result = await save_field_values(db, ENTITY_TYPE, map_id, body)

    # 编辑人物角色关联自定义字段属于节点数据变更，回退内容状态
    if map_item.content_id:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
        content = await get_content_by_id(db, map_item.content_id)
        if content:
            await rollback_after_published_edit(
                db, map_item.content_id, content.content_type,
                current_user.username, "编辑人物角色自定义字段",
            )

    # 仅变更的非空字段写「自定义字段编辑」日志，下拉框类型翻译为显示名称
    field_ids = [v.custom_field_id for v in body.values if v.value is not None and v.value != ""]
    field_data = {}
    if field_ids:
        cf_rows = (
            await db.execute(
                select(CustomField).where(
                    CustomField.id.in_(field_ids),
                    CustomField.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        field_info = {cf.id: cf for cf in cf_rows}
        dropdown_ids = [cf.id for cf in cf_rows if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select')]
        option_map: dict[int, dict[str, str]] = {}
        if dropdown_ids:
            opt_rows = (
                await db.execute(
                    select(CustomFieldOption).where(CustomFieldOption.custom_field_id.in_(dropdown_ids))
                )
            ).scalars().all()
            for opt in opt_rows:
                if opt.custom_field_id not in option_map:
                    option_map[opt.custom_field_id] = {}
                names = opt.names or {}
                display_name = resolve_option_display_name(names, opt.code)
                option_map[opt.custom_field_id][opt.code] = display_name

        for v in body.values:
            if v.value is not None and v.value != "" and v.custom_field_id in field_info:
                if old_fv_map.get(v.custom_field_id) == v.value:
                    continue
                cf = field_info[v.custom_field_id]
                raw_value = v.value
                if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select') and v.custom_field_id in option_map:
                    codes = raw_value.split(',')
                    translated = [option_map[v.custom_field_id].get(c.strip(), c.strip()) for c in codes]
                    field_data[cf.field_name] = ','.join(translated)
                else:
                    field_data[cf.field_name] = raw_value

    if field_data:
        # 首键并入人物名称，标识被编辑对象（前端 Details 字段行展示）
        field_data = {"cast_name": map_item.cast_name, **field_data}
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CAST_ROLE_MAP_FIELD_UPDATE,
            operation_object_code="log.field.edit",
            operation_content_code="log.cast.role.field.edit",
            content_id=map_item.content_id,
            entity_type=ENTITY_TYPE,
            entity_id=map_id,
            previous_value=None,
            updated_value=json.dumps(field_data, ensure_ascii=False, default=str),
            # 原始数据快照（自定义字段变更映射）
            updated_value_json=json.dumps(field_data, ensure_ascii=False, default=str),
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.get("/{map_id}/i18n", response_model=list[EntityI18nItem])
async def get_cast_role_map_i18n(
    map_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单个人物角色关联的多语言字段值。"""
    await cast_role_map_service.get_cast_role_map(db, map_id)
    return await get_i18n_values(db, ENTITY_TYPE, map_id)


@router.put("/{map_id}/i18n", response_model=list[EntityI18nItem])
async def save_cast_role_map_i18n(
    map_id: int,
    body: EntityI18nPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """保存单个人物角色关联的多语言字段值（写操作日志，与 movie i18n 保存口径一致）。"""
    import json
    from sqlalchemy import select
    from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption

    map_item = await cast_role_map_service.get_cast_role_map(db, map_id)
    # 记录旧值用于 diff，仅变更字段写日志
    old_i18n = await get_i18n_values(db, ENTITY_TYPE, map_id)
    old_map = {r.field_name: r.value for r in old_i18n if r.language == body.language}
    changed_fields = {
        k: v for k, v in body.fields.items()
        if v is not None and v != "" and old_map.get(k) != v
    }

    # 编辑人物角色关联多语言字段属于节点数据变更，回退内容状态
    if map_item.content_id:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
        content = await get_content_by_id(db, map_item.content_id)
        if content:
            await rollback_after_published_edit(
                db, map_item.content_id, content.content_type,
                current_user.username, "编辑人物角色多语言字段",
            )

    # cf_ 前缀的自定义字段变更写「自定义字段编辑」日志，下拉框类型按页签语言翻译为显示名称
    cf_changed = {k: v for k, v in changed_fields.items() if k.startswith('cf_')}
    if cf_changed:
        cf_rows = (
            await db.execute(
                select(CustomField).where(
                    CustomField.field_code.in_(list(cf_changed.keys())),
                    CustomField.is_deleted.is_(False),
                )
            )
        ).scalars().all()
        cf_info = {cf.field_code: cf for cf in cf_rows}
        dropdown_ids = [cf.id for cf in cf_rows if cf.field_type in ('DropList', 'DropList_multiple', 'multi_select')]
        option_map: dict[str, dict[str, str]] = {}
        if dropdown_ids:
            opt_rows = (
                await db.execute(
                    select(CustomFieldOption).where(CustomFieldOption.custom_field_id.in_(dropdown_ids))
                )
            ).scalars().all()
            id_to_code = {cf.id: cf.field_code for cf in cf_rows if cf.id in dropdown_ids}
            for opt in opt_rows:
                fc = id_to_code.get(opt.custom_field_id)
                if fc:
                    names = opt.names or {}
                    # 选项名称与前端弹窗保持一致：按多语言编辑页签语言解析（bug 32055/32420）
                    display_name = resolve_option_display_name(names, opt.code, language=body.language)
                    option_map.setdefault(fc, {})[opt.code] = display_name
        cf_translated: dict[str, str] = {}
        for k, v in cf_changed.items():
            cf = cf_info.get(k)
            if cf and cf.field_type in ('DropList', 'DropList_multiple', 'multi_select') and k in option_map:
                codes = v.split(',')
                cf_translated[cf.field_name] = ','.join(
                    option_map[k].get(c.strip(), c.strip()) for c in codes
                )
            elif cf:
                cf_translated[cf.field_name] = v
            else:
                cf_translated[k] = v
        if cf_translated:
            # 首键并入人物名称，标识被编辑对象（前端 Details 字段行展示）
            cf_translated = {"cast_name": map_item.cast_name, **cf_translated}
            await write_log(
                db,
                user_id=current_user.id,
                user_name=current_user.username,
                operation_type=OperationType.CAST_ROLE_MAP_FIELD_UPDATE,
                operation_object_code="log.field.edit",
                # 带语言标识，显示"自定义字段编辑（en）"，区分各语言页签的变更（bug 32055）
                operation_content_code="log.cast.role.field.edit.lang", operation_content_params={"lang_code": body.language},
                content_id=map_item.content_id,
                entity_type=ENTITY_TYPE,
                entity_id=map_id,
                previous_value=None,
                updated_value=json.dumps(cf_translated, ensure_ascii=False, default=str),
                # 原始数据快照（自定义字段多语言变更映射）
                updated_value_json=json.dumps(cf_translated, ensure_ascii=False, default=str),
                ip_address=_get_ip(request),
                result="success",
            )

    # 其余多语言字段变更写「多语言编辑」日志
    translated_fields = {k: v for k, v in changed_fields.items() if not k.startswith('cf_')}
    if translated_fields:
        # 首键并入人物名称，标识被编辑对象（前端 Details 字段行展示）
        translated_fields = {"cast_name": map_item.cast_name, **translated_fields}
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CAST_ROLE_MAP_I18N_UPDATE,
            operation_object_code="log.i18n.edit",
            operation_content_code="log.cast.role.i18n.edit.lang", operation_content_params={"lang_code": body.language},
            content_id=map_item.content_id,
            entity_type=ENTITY_TYPE,
            entity_id=map_id,
            previous_value=None,
            updated_value=json.dumps(translated_fields, ensure_ascii=False, default=str),
            # 原始数据快照（多语言字段变更映射）
            updated_value_json=json.dumps(translated_fields, ensure_ascii=False, default=str),
            ip_address=_get_ip(request),
            result="success",
        )
    # 先写日志后保存（与 save_content_i18n 口径一致）：save_i18n_values 内部会 commit，
    # 日志随同一事务持久化；save 之后再补一次提交，防止日志挂在新事务中被丢弃
    result = await save_i18n_values(db, ENTITY_TYPE, map_id, body)
    await db.commit()
    return result
