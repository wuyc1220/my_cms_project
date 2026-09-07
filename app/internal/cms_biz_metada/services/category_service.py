from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from ..models.basic import Category
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_metada.schemas.basic import CategoryCreate, CategoryListItem, CategoryUpdate
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode
from app.internal.cms_biz_package.models.package import ContentCategory, ContentGenre
from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_system.models.dict import DictNode

def _build_category_tree(nodes: list[Category]) -> list[CategoryListItem]:
    items: dict[int, CategoryListItem] = {
        node.id: CategoryListItem(
            id=node.id,
            parent_id=node.parent_id,
            platform=node.platform,
            name=node.name,
            sequence=node.sequence,
            category_type=node.category_type,
            vod_count=node.vod_count,
            description=node.description,
            jump_category_code=node.jump_category_code,
            status=node.status,
            ingest_status=node.ingest_status,
            children=[],
            created_at=node.created_at,
        )
        for node in nodes
    }
    roots: list[CategoryListItem] = []
    for node in nodes:
        item = items[node.id]
        if node.parent_id and node.parent_id in items:
            items[node.parent_id].children.append(item)
        else:
            roots.append(item)

    def sort_nodes(tree_nodes: list[CategoryListItem]) -> None:
        tree_nodes.sort(key=lambda n: (n.sequence, n.id))
        for child in tree_nodes:
            sort_nodes(child.children)

    sort_nodes(roots)
    return roots

def _match_category(
    node: CategoryListItem,
    name: str | None,
    platforms: list[str] | None,
    category_types: list[str] | None,
    ingest_statuses: list[str] | None,
) -> bool:
    return (
        (not name or name.lower() in node.name.lower())
        and (not platforms or node.platform in platforms)
        and (not category_types or node.category_type in (category_types or []))
        and (not ingest_statuses or node.ingest_status in ingest_statuses)
    )

def _filter_category_tree(
    nodes: list[CategoryListItem],
    name: str | None,
    platforms: list[str] | None,
    category_types: list[str] | None,
    ingest_statuses: list[str] | None,
) -> list[CategoryListItem]:
    if not any([name, platforms, category_types, ingest_statuses]):
        return nodes
    result: list[CategoryListItem] = []
    for node in nodes:
        filtered_children = _filter_category_tree(node.children, name, platforms, category_types, ingest_statuses)
        if _match_category(node, name, platforms, category_types, ingest_statuses):
            result.append(node)
        elif filtered_children:
            result.append(node.model_copy(update={"children": filtered_children}))
    return result

async def _get_valid_platforms(db: AsyncSession) -> set[str]:
    platform_root = (
        await db.execute(
            select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == "Platform", DictNode.is_deleted == False)
        )
    ).scalar_one_or_none()
    if platform_root is None:
        return set()
    children = (
        await db.execute(
            select(DictNode.code).where(
                DictNode.parent_id == platform_root.id,
                DictNode.status == "active",
                DictNode.is_deleted == False,
            )
        )
    ).scalars().all()
    return set(children)


async def get_category_tree(
    db: AsyncSession,
    name: str | None = None,
    platforms: list[str] | None = None,
    category_types: list[str] | None = None,
    ingest_statuses: list[str] | None = None,
) -> list[CategoryListItem]:
    valid_platforms = await _get_valid_platforms(db)
    nodes = (
        await db.execute(
            select(Category).where(
                Category.is_deleted.is_(False),
                Category.platform.in_(valid_platforms) if valid_platforms else Category.platform.is_not(None),
            ).order_by(Category.sequence, Category.id)
        )
    ).scalars().all()
    tree = _build_category_tree(nodes)
    return _filter_category_tree(tree, name, platforms, category_types, ingest_statuses)


async def _get_category_or_404(db: AsyncSession, category_id: int) -> Category:
    """获取分类，不存在则404"""
    cat = (await db.execute(
        select(Category).where(Category.id == category_id, Category.is_deleted.is_(False))
    )).scalar_one_or_none()
    if not cat:
        raise NotFoundException(ErrorCode.CATEGORY_NOT_FOUND, get_msg("CATEGORY_NOT_FOUND"))
    return cat


async def get_category(db: AsyncSession, category_id: int) -> Category:
    return await _get_category_or_404(db, category_id)


async def create_category(db: AsyncSession, data: CategoryCreate) -> Category:
    if data.parent_id is not None:
        await get_category(db, data.parent_id)
    existing = (
        await db.execute(
            select(Category.id).where(
                Category.parent_id == data.parent_id,
                Category.platform == data.platform,
                Category.name == data.name,
                Category.is_deleted.is_(False),
            ).limit(1)
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise BusinessException(ErrorCode.CATEGORY_NAME_EXISTS_UNDER_PARENT, get_msg("CATEGORY_NAME_EXISTS_UNDER_PARENT"))
    cat = Category(
        parent_id=data.parent_id,
        platform=data.platform,
        name=data.name,
        sequence=data.sequence,
        category_type=data.category_type,
        vod_count=data.vod_count,
        description=data.description,
        jump_category_code=data.jump_category_code,
        status=data.status,
    )
    db.add(cat)
    await db.commit()
    await db.refresh(cat)
    return cat


async def update_category(db: AsyncSession, category_id: int, data: CategoryUpdate) -> Category:
    cat = await _get_category_or_404(db, category_id)
    if data.name is not None and data.name != cat.name:
        existing = (
            await db.execute(
                select(Category.id).where(
                    Category.id != category_id,
                    Category.parent_id == cat.parent_id,
                    Category.platform == cat.platform,
                    Category.name == data.name,
                    Category.is_deleted.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()
        if existing is not None:
            raise BusinessException(ErrorCode.CATEGORY_NAME_EXISTS_UNDER_PARENT, get_msg("CATEGORY_NAME_EXISTS_UNDER_PARENT"))
    if data.platform is not None:
        cat.platform = data.platform
    if data.name is not None:
        cat.name = data.name
    if data.sequence is not None:
        cat.sequence = data.sequence
    if data.category_type is not None:
        cat.category_type = data.category_type
    if data.vod_count is not None:
        cat.vod_count = data.vod_count
    if data.description is not None:
        cat.description = data.description
    if data.jump_category_code is not None:
        cat.jump_category_code = data.jump_category_code
    if data.status is not None:
        cat.status = data.status
    if data.ingest_status is not None:
        cat.ingest_status = data.ingest_status
    await db.commit()
    await db.refresh(cat)
    return cat


async def delete_category(db: AsyncSession, category_id: int) -> None:
    cat = await _get_category_or_404(db, category_id)
    child_exists = (
        await db.execute(
            select(Category.id).where(Category.parent_id == cat.id, Category.is_deleted.is_(False)).limit(1)
        )
    ).scalar_one_or_none()
    if child_exists is not None:
        raise BusinessException(ErrorCode.CATEGORY_HAS_CHILDREN, get_msg("CATEGORY_HAS_CHILDREN"))
    content_exists = (
        await db.execute(select(ContentCategory.id).where(ContentCategory.category_id == cat.id).limit(1))
    ).scalar_one_or_none()
    if content_exists is not None:
        raise BusinessException(ErrorCode.CATEGORY_HAS_CONTENTS, get_msg("CATEGORY_HAS_CONTENTS"))
    cat.is_deleted = True
    await db.commit()


async def batch_delete_categories(db: AsyncSession, req: BatchDeleteRequest) -> int:
    cats = (await db.execute(
        select(Category).where(Category.id.in_(req.ids), Category.is_deleted.is_(False))
    )).scalars().all()
    for cat in cats:
        child_exists = (
            await db.execute(
                select(Category.id).where(Category.parent_id == cat.id, Category.is_deleted.is_(False)).limit(1)
            )
        ).scalar_one_or_none()
        if child_exists is not None:
            raise BusinessException(
                ErrorCode.CATEGORY_HAS_CHILDREN,
                get_msg("CATEGORY_HAS_CHILDREN_NAME", name=cat.name),
            )
        content_exists = (
            await db.execute(select(ContentCategory.id).where(ContentCategory.category_id == cat.id).limit(1))
        ).scalar_one_or_none()
        if content_exists is not None:
            raise BusinessException(
                ErrorCode.CATEGORY_HAS_CONTENTS,
                get_msg("CATEGORY_HAS_CONTENTS_NAME", name=cat.name),
            )
    for cat in cats:
        cat.is_deleted = True
    await db.commit()
    return len(cats)


async def get_category_contents(db: AsyncSession, category_id: int) -> list[dict]:
    await get_category(db, category_id)
    links = (
        await db.execute(
            select(ContentCategory)
            .where(ContentCategory.category_id == category_id, ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False))
            .order_by(ContentCategory.sequence, ContentCategory.id)
            .options(selectinload(ContentCategory.content))
        )
    ).scalars().all()

    # 从 content_genre 中间表批量查询所有 genre_id
    content_ids = [link.content.id for link in links]
    genre_rows = (await db.execute(
        select(ContentGenre.content_id, ContentGenre.genre_id)
        .where(ContentGenre.content_id.in_(content_ids), ContentGenre.is_deleted.is_(False))
    )).all()

    content_genre_map: dict[int, list[int]] = {}
    all_genre_ids: set[int] = set()
    for row in genre_rows:
        content_genre_map.setdefault(row.content_id, []).append(row.genre_id)
        all_genre_ids.add(row.genre_id)

    genre_map = {}
    if all_genre_ids:
        genres = (await db.execute(select(Genre).where(Genre.id.in_(all_genre_ids), Genre.is_deleted.is_(False)))).scalars().all()
        genre_map = {g.id: g.name for g in genres}

    result = []
    for link in links:
        content = link.content
        gids = content_genre_map.get(content.id, [])
        genre_names = [genre_map[gid] for gid in gids if gid in genre_map]
        result.append({
            "id": content.id,
            "sequence": link.sequence if link.sequence is not None else 0,
            "content_name": content.title,
            "content_type": content.content_type,
            "genre": ", ".join(genre_names) if genre_names else "",
            "status": content.status,
        })
    return result


async def reorder_category_contents(db: AsyncSession, category_id: int, content_ids: list[int]) -> None:
    await get_category(db, category_id)
    links = (
        await db.execute(
            select(ContentCategory).where(
                ContentCategory.category_id == category_id,
                ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False)
            )
        )
    ).scalars().all()

    link_map = {link.content_id: link for link in links}

    for idx, content_id in enumerate(content_ids):
        if content_id in link_map:
            link_map[content_id].sequence = idx + 1

    await db.commit()


async def remove_category_content(db: AsyncSession, category_id: int, content_id: int) -> None:
    await get_category(db, category_id)
    link = (
        await db.execute(
            select(ContentCategory).where(
                ContentCategory.category_id == category_id,
                ContentCategory.content_id == content_id,
                ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()

    if link:
        await db.delete(link)
        await db.commit()
