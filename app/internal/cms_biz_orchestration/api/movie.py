"""
媒资实体 API 路由层。

路由前缀：/movies

接口列表：
    GET    /movies/{movie_id}                        查询单个媒资
    GET    /contents/{content_id}/movies              查询内容关联的媒资列表
    POST   /contents/{content_id}/movies              创建媒资
    PUT    /movies/{movie_id}                         更新媒资
    DELETE /movies/{movie_id}                         删除媒资
    GET    /contents/{content_id}/movies/history      查询内容关联的媒资操作历史
"""

from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values, resolve_option_display_name
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.internal.cms_biz_orchestration.schemas.movie import (
    MovieCreate,
    MovieItem,
    MovieUpdate,
    MovieListItem,
)
from app.internal.cms_biz_orchestration.schemas.movie_history import (
    MovieHistoryListItem,
)
from app.internal.cms_biz_orchestration.services import movie_service
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

router = APIRouter(prefix="/movies", tags=["媒资管理"])


def _movie_log_key(action: str, movie_type: int | None) -> str:
    """生成媒资操作的 i18n 内容编码：log.movie.{action}.{movie_type}（如 log.movie.create.1 → 媒资新增-正片）。

    movie_type 不在 1/2/3 范围时回退到基础编码（如 log.movie.create）。
    """
    base = f"log.movie.{action}"
    if movie_type in (1, 2, 3):
        return f"{base}.{movie_type}"
    return base


@router.get("/{movie_id}", response_model=MovieItem)
async def get_movie(
    movie_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询单个媒资实体。"""
    return await movie_service.get_movie(db, movie_id)


@router.get("/contents/{content_id}/movies", response_model=MovieListItem)
async def list_content_movies(
    content_id: int,
    movie_type: int | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询内容关联的媒资列表。"""
    return await movie_service.list_movies(db, content_id, movie_type)


@router.post("/contents/{content_id}/movies", response_model=MovieItem)
async def create_movie(
    content_id: int,
    data: MovieCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建媒资实体。"""
    data_dict = data.model_dump()
    data_dict["content_id"] = content_id
    result = await movie_service.create_movie(
        db, MovieCreate(**data_dict), processed_by=current_user.username, processed_by_id=current_user.id
    )
    new_data = orm_to_dict(result, "movie")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "movie", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MOVIE_CREATE,
        operation_object_code="OBJ_MOVIE", operation_object_params={"name": result.file_name},
        operation_content_code=_movie_log_key("create", result.movie_type),
        content_id=content_id,
        entity_type="movie",
        entity_id=result.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return result


@router.put("/{movie_id}", response_model=MovieItem)
async def update_movie(
    movie_id: int,
    data: MovieUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新媒资实体。"""
    import copy
    old = await movie_service.get_movie(db, movie_id)
    old_data = copy.deepcopy(orm_to_dict(old, "movie")) if old else {}
    result = await movie_service.update_movie(db, movie_id, data, processed_by=current_user.username)
    new_data = orm_to_dict(result, "movie")
    prev_val, upd_val, raw_val = await prepare_log_values(db, "movie", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.MOVIE_UPDATE,
            operation_object_code="OBJ_MOVIE", operation_object_params={"name": result.file_name},
            operation_content_code=_movie_log_key("edit", result.movie_type),
            content_id=result.content_id,
            entity_type="movie",
            entity_id=result.id,
            previous_value=prev_val,
            updated_value=upd_val,
            updated_value_json=raw_val,
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


@router.delete("/{movie_id}")
async def delete_movie(
    movie_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除媒资实体。"""
    existing = await movie_service.get_movie(db, movie_id)
    old_data = orm_to_dict(existing, "movie") if existing else {}
    await movie_service.delete_movie(db, movie_id, processed_by=current_user.username)
    prev_val, upd_val, raw_val = await prepare_log_values(db, "movie", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.MOVIE_DELETE,
        operation_object_code="OBJ_MOVIE", operation_object_params={"name": existing.file_name},
        operation_content_code=_movie_log_key("delete", existing.movie_type),
        content_id=existing.content_id,
        entity_type="movie",
        entity_id=existing.id,
        previous_value=prev_val,
        updated_value=upd_val,
        updated_value_json=raw_val,
        ip_address=_get_ip(request),
        result="success",
    )
    await db.commit()
    return {"success": True}


@router.get("/contents/{content_id}/movies/history", response_model=MovieHistoryListItem)
async def list_content_movie_history(
    content_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询内容关联的媒资操作历史列表。"""
    return await movie_service.list_movie_history(db, content_id)


# ── 自定义字段值 ────────────────────────────────────────

ENTITY_TYPE = "movie"


@router.get("/{movie_id}/field-values", response_model=list[EntityFieldValueItem])
async def get_movie_field_values(
    movie_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询媒资实体的自定义字段值。"""
    await movie_service.get_movie(db, movie_id)
    return await get_field_values(db, ENTITY_TYPE, movie_id)


@router.put("/{movie_id}/field-values", response_model=list[EntityFieldValueItem])
async def save_movie_field_values(
    movie_id: int,
    body: EntityFieldValuesPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """保存媒资实体的自定义字段值（独立写 MOVIE_FIELD_UPDATE 操作日志）。"""
    import json
    from sqlalchemy import select
    from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption

    movie = await movie_service.get_movie(db, movie_id)
    # 记录旧值用于 diff，仅变更字段写日志
    old_fv = await get_field_values(db, ENTITY_TYPE, movie_id)
    old_fv_map = {r.custom_field_id: r.value for r in old_fv}
    result = await save_field_values(db, ENTITY_TYPE, movie_id, body)

    # 编辑素材自定义字段属于节点数据变更，回退内容状态
    if movie.content_id:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
        content = await get_content_by_id(db, movie.content_id)
        if content:
            await rollback_after_published_edit(
                db, movie.content_id, content.content_type,
                current_user.username, "编辑素材自定义字段",
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
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.MOVIE_FIELD_UPDATE,
            operation_object_code="log.field.edit",
            operation_content_code="log.field.edit",
            content_id=movie.content_id,
            entity_type=ENTITY_TYPE,
            entity_id=movie_id,
            previous_value=None,
            updated_value=json.dumps(field_data, ensure_ascii=False, default=str),
            # 原始数据快照（自定义字段变更映射）
            updated_value_json=json.dumps(field_data, ensure_ascii=False, default=str),
            ip_address=_get_ip(request),
            result="success",
        )
    await db.commit()
    return result


# ── 多语言值 ────────────────────────────────────────────


@router.get("/{movie_id}/i18n", response_model=list[EntityI18nItem])
async def get_movie_i18n(
    movie_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询媒资实体的多语言值。"""
    await movie_service.get_movie(db, movie_id)
    return await get_i18n_values(db, ENTITY_TYPE, movie_id)


@router.put("/{movie_id}/i18n", response_model=list[EntityI18nItem])
async def save_movie_i18n(
    movie_id: int,
    body: EntityI18nPayload,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """保存媒资实体的多语言值（一次传一种语言的所有字段）。"""
    import json
    from sqlalchemy import select
    from app.internal.cms_biz_metada.models.basic import CustomField, CustomFieldOption

    movie = await movie_service.get_movie(db, movie_id)
    # 记录旧值用于 diff，仅变更字段写日志
    old_i18n = await get_i18n_values(db, ENTITY_TYPE, movie_id)
    old_map = {r.field_name: r.value for r in old_i18n if r.language == body.language}
    changed_fields = {
        k: v for k, v in body.fields.items()
        if v is not None and v != "" and old_map.get(k) != v
    }

    # 编辑素材多语言信息属于节点数据变更，回退内容状态
    if movie.content_id:
        from app.internal.cms_biz_orchestration.services.workflow_service import rollback_after_published_edit
        from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
        content = await get_content_by_id(db, movie.content_id)
        if content:
            await rollback_after_published_edit(
                db, movie.content_id, content.content_type,
                current_user.username, "编辑素材多语言信息",
            )

    # cf_ 前缀的自定义字段变更写「自定义字段编辑」日志，下拉框类型翻译为显示名称
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
            await write_log(
                db,
                user_id=current_user.id,
                user_name=current_user.username,
                operation_type=OperationType.MOVIE_FIELD_UPDATE,
                operation_object_code="log.field.edit",
                # 带语言标识，显示"自定义字段编辑（en）"，区分各语言页签的变更（bug 32055）
                operation_content_code="log.field.edit.lang", operation_content_params={"lang_code": body.language},
                content_id=movie.content_id,
                entity_type=ENTITY_TYPE,
                entity_id=movie_id,
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
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.MOVIE_I18N_UPDATE,
            operation_object_code="log.i18n.edit",
            operation_content_code="log.i18n.edit.lang", operation_content_params={"lang_code": body.language},
            content_id=movie.content_id,
            entity_type=ENTITY_TYPE,
            entity_id=movie_id,
            previous_value=None,
            updated_value=json.dumps(translated_fields, ensure_ascii=False, default=str),
            # 原始数据快照（多语言字段变更映射）
            updated_value_json=json.dumps(translated_fields, ensure_ascii=False, default=str),
            ip_address=_get_ip(request),
            result="success",
        )
    return await save_i18n_values(db, ENTITY_TYPE, movie_id, body)
