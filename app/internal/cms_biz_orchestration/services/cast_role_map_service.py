"""
CastRoleMap 业务逻辑层。

职责：
- CastRoleMap 的 CRUD
- 按 content_id 查询关联的 CastRoleMap（通过 program_id / movie_id）
"""

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import ErrorCode, NotFoundException
from app.common.core.i18n import get_msg
from app.common.core.transactions import transactional
from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap
from app.internal.cms_biz_orchestration.repositories import cast_role_map_repo
from app.internal.cms_biz_orchestration.schemas.cast_role_map import (
    CastRoleMapCreate,
    CastRoleMapItem,
    CastRoleMapListItem,
    CastRoleMapUpdate,
)
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
)


async def get_cast_role_map(db: AsyncSession, map_id: int) -> CastRoleMapItem:
    """查询单个 CastRoleMap。"""
    from app.internal.cms_biz_metada.models.basic import Picture
    from app.internal.cms_biz_orchestration.services.storage import storage_service
    
    cast_role_map = await cast_role_map_repo.get_cast_role_map_by_id(db, map_id)
    if not cast_role_map:
        raise NotFoundException(ErrorCode.CAST_ROLE_MAP_NOT_FOUND, get_msg("CAST_ROLE_MAP_NOT_FOUND"))
    item = CastRoleMapItem.model_validate(cast_role_map)
    if hasattr(cast_role_map, "cast") and cast_role_map.cast:
        if item.cast_name is None:
            item.cast_name = cast_role_map.cast.name
        # 查询 Cast 的海报
        pic = (
            await db.execute(
                select(Picture.file_path)
                .where(
                    Picture.entity_type.in_(["cast", "Cast"]),
                    Picture.entity_id == cast_role_map.cast_id,
                    Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
                )
                .order_by(Picture.created_at)
                .limit(1)
            )
        ).scalar_one_or_none()
        if pic:
            item.cast_poster_url = storage_service.get_file_url(pic)
    return item


async def list_cast_role_maps(
    db: AsyncSession,
    *,
    program_id: int | None = None,
    movie_id: int | None = None,
    cast_id: int | None = None,
    content_id: int | None = None,
    page: int = 1,
    page_size: int = 10,
) -> CastRoleMapListItem:
    """分页查询 CastRoleMap 列表。"""
    from app.internal.cms_biz_metada.models.basic import Picture
    from app.internal.cms_biz_orchestration.services.storage import storage_service
    items, total = await cast_role_map_repo.list_cast_role_maps(
        db,
        program_id=program_id,
        movie_id=movie_id,
        cast_id=cast_id,
        content_id=content_id,
        page=page,
        page_size=page_size,
    )
    result_items = []
    for i in items:
        item = CastRoleMapItem.model_validate(i)
        if hasattr(i, "cast") and i.cast:
            if item.cast_name is None:
                item.cast_name = i.cast.name
            # 查询 Cast 的海报
            pic = (
                await db.execute(
                    select(Picture.file_path)
                    .where(
                        Picture.entity_type.in_(["cast", "Cast"]),
                        Picture.entity_id == i.cast_id,
                        Picture.is_deleted.is_(False), Picture.is_discarded.is_(False),
                    )
                    .order_by(Picture.created_at)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if pic:
                item.cast_poster_url = storage_service.get_file_url(pic)
        result_items.append(item)
    return CastRoleMapListItem(
        items=result_items,
        total=total,
    )


@transactional
async def create_cast_role_map(
    db: AsyncSession, 
    data: CastRoleMapCreate, 
    processed_by: str | None = None,
    skip_process_record: bool = False
) -> CastRoleMapItem:
    """创建 CastRoleMap。
    
    参数：
        skip_process_record: 是否跳过流程记录（用于批量创建时只记录一次）
    """
    dump = data.model_dump()
    cast_role_map = CastRoleMap(**dump)
    await cast_role_map_repo.add_cast_role_map(db, cast_role_map)
    await db.flush()
    await db.refresh(cast_role_map)

    # 完成 CastRoleMap 流程节点并更新内容状态
    if data.content_id and not skip_process_record:
        from sqlalchemy import select
        from app.internal.cms_biz_package.models.package import Content
        content = (await db.execute(select(Content).where(Content.id == data.content_id))).scalar_one_or_none()
        if content:
            await complete_process_and_update_status(
                db,
                content_id=data.content_id,
                content_type=content.content_type,
                process_name="CastRoleMap",
                processed_by=processed_by,
                info=f"添加演员角色: cast_id={data.cast_id}, role={data.role_name}",
            )

    return CastRoleMapItem.model_validate(cast_role_map)


@transactional
async def batch_create_cast_role_maps(
    db: AsyncSession,
    items: list[CastRoleMapCreate],
    processed_by: str | None = None
) -> list[CastRoleMapItem]:
    """批量创建 CastRoleMap，只记录一次流程。
    
    用于前端批量保存演员角色映射，避免创建多条流程记录。
    """
    from sqlalchemy import select
    from app.internal.cms_biz_package.models.package import Content
    
    results = []
    content_id = None
    content_type = None
    
    # 批量创建，跳过流程记录
    for data in items:
        result = await create_cast_role_map(db, data, processed_by, skip_process_record=True)
        results.append(result)
        if content_id is None and data.content_id:
            content_id = data.content_id
    
    # 只记录一次流程
    if content_id:
        content = (await db.execute(select(Content).where(Content.id == content_id))).scalar_one_or_none()
        if content:
            content_type = content.content_type
            await complete_process_and_update_status(
                db,
                content_id=content_id,
                content_type=content_type,
                process_name="CastRoleMap",
                processed_by=processed_by,
                info=f"批量添加演员角色: 共{len(items)}条",
            )
    
    return results


@transactional
async def update_cast_role_map(db: AsyncSession, map_id: int, data: CastRoleMapUpdate) -> CastRoleMapItem:
    """更新 CastRoleMap。"""
    cast_role_map = await cast_role_map_repo.get_cast_role_map_by_id(db, map_id)
    if not cast_role_map:
        raise NotFoundException(ErrorCode.CAST_ROLE_MAP_NOT_FOUND, get_msg("CAST_ROLE_MAP_NOT_FOUND"))

    update_data = data.model_dump(exclude_unset=True)
    await cast_role_map_repo.update_cast_role_map(db, cast_role_map, **update_data)
    return CastRoleMapItem.model_validate(cast_role_map)


@transactional
async def delete_cast_role_map(db: AsyncSession, map_id: int, processed_by: str | None = None) -> None:
    """删除 CastRoleMap（软删除）。"""
    cast_role_map = await cast_role_map_repo.get_cast_role_map_by_id(db, map_id)
    if not cast_role_map:
        raise NotFoundException(ErrorCode.CAST_ROLE_MAP_NOT_FOUND, get_msg("CAST_ROLE_MAP_NOT_FOUND"))
    cast_role_map.is_deleted = True
    await db.flush()

    # 完成 CastRoleMap 流程节点并更新内容状态
    if cast_role_map.content_id:
        from sqlalchemy import select
        from app.internal.cms_biz_package.models.package import Content
        content = (await db.execute(select(Content).where(Content.id == cast_role_map.content_id))).scalar_one_or_none()
        if content:
            await complete_process_and_update_status(
                db,
                content_id=cast_role_map.content_id,
                content_type=content.content_type,
                process_name="CastRoleMap",
                processed_by=processed_by,
                info=f"删除演员角色: cast_id={cast_role_map.cast_id}, role={cast_role_map.role_name}",
            )
