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
from app.common.utils.log_enricher import orm_to_dict, prepare_log_values
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
)
from app.internal.cms_biz_metada.services.entity_data_service import (
    get_field_values,
    save_field_values,
)
from app.internal.cms_biz_orchestration.services import cast_role_map_service

router = APIRouter(prefix="/cast-role-maps", tags=["人物角色关联"])

ENTITY_TYPE = "cast_role_map"


def _get_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


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
        operation_object=f"人物角色 {result.role_name}",
        operation_content="log.metadata.create",
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
    prev_val, upd_val, raw_val = await prepare_log_values(db, "cast_role_map", old_data, new_data)
    if prev_val is not None or upd_val is not None:
        await write_log(
            db,
            user_id=current_user.id,
            user_name=current_user.username,
            operation_type=OperationType.CAST_ROLE_MAP_UPDATE,
            operation_object=f"人物角色 {result.role_name}",
            operation_content="log.metadata.edit",
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
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除人物角色关联（软删除）。"""
    await cast_role_map_service.delete_cast_role_map(db, map_id, processed_by=current_user.username)
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
        operation_object="演职人员关联",
        operation_content="log.cast.link",
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
        operation_object="取消关联演职人员",
        operation_content="log.cast.unlink",
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
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """保存单个人物角色关联的自定义字段值。"""
    await cast_role_map_service.get_cast_role_map(db, map_id)
    return await save_field_values(db, ENTITY_TYPE, map_id, body)
