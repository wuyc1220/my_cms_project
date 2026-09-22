import time
import uuid

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload
from sqlalchemy.exc import IntegrityError

from ..models.dict import DictNode
from app.common.core import DictStatus
from app.internal.cms_biz_system.schemas.user_crud import DictNodeCreate, DictNodeListItem, DictNodeUpdate, LanguageOption
from loguru import logger
from app.common.core.i18n import get_msg
from app.common.core.exceptions import ErrorCode, BusinessException

# 字典树内存缓存（无过滤参数时生效，TTL 5 分钟）
_dict_tree_cache: dict = {}
_DICT_TREE_CACHE_TTL = 300

# 字典子节点内存缓存（key=根节点 code，TTL 兜底，正常靠写操作后主动失效）
# get_dict_children_by_code 全后端 20+ 处调用（详情页一次加载可能触发几十条字典 SQL），
# 字典数据变化极少，缓存后单次调用从 2 条串行 SQL 降为 0
_dict_children_cache: dict[str, tuple[float, list[LanguageOption]]] = {}
_DICT_CHILDREN_CACHE_TTL = 300


def _invalidate_dict_caches() -> None:
    """字典写操作（增/改/删/状态切换）提交后调用，清空字典相关内存缓存。

    在 API 层 db.commit() 之后调用，保证数据先落库再清缓存，
    避免并发请求用旧数据回填缓存。
    """
    _dict_tree_cache.clear()
    _dict_children_cache.clear()


async def _fix_sequence(db: AsyncSession) -> None:
    """修复 dict_id_seq 序列（主键冲突回填自增起点）。

    SQL 内联字面量表名/序列名、数值走绑定参数，杜绝动态拼接（Bandit B608）。
    """
    result = await db.execute(text("SELECT MAX(id) FROM dict"))
    max_id = result.scalar() or 0
    await db.execute(text("SELECT setval('dict_id_seq', :new_id, false)"), {"new_id": max_id + 1})
    logger.warning(f"序列 dict_id_seq 已自动修复为 {max_id + 1}")


async def get_node(db: AsyncSession, node_id: int) -> DictNode:
    result = await db.execute(
        select(DictNode)
        .where(DictNode.id == node_id, DictNode.is_deleted == False)
        .options(selectinload(DictNode.children))
    )
    node = result.scalar_one_or_none()
    if not node:
        raise BusinessException(ErrorCode.DICT_NODE_NOT_FOUND, get_msg("DICT_NODE_NOT_FOUND"))
    return node


def _to_response(node: DictNode) -> DictNodeListItem:
    return DictNodeListItem(
        id=node.id,
        parent_id=node.parent_id,
        code=node.code,
        name=node.name,
        sort_order=node.sort_order,
        status=node.status,
        remark=node.remark,
        is_system=node.is_system,
        children=[],
    )


async def _ensure_unique_code(db: AsyncSession, parent_id: int | None, code: str, exclude_id: int | None = None) -> None:
    if parent_id is None:
        query = select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == code, DictNode.is_deleted == False)
    else:
        query = select(DictNode).where(DictNode.parent_id == parent_id, DictNode.code == code, DictNode.is_deleted == False)
    if exclude_id is not None:
        query = query.where(DictNode.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.DICT_CODE_EXISTS, get_msg("DICT_CODE_EXISTS"))


async def _ensure_unique_name(db: AsyncSession, parent_id: int | None, name: str, exclude_id: int | None = None) -> None:
    if parent_id is None:
        query = select(DictNode).where(DictNode.parent_id.is_(None), DictNode.name == name, DictNode.is_deleted == False)
    else:
        query = select(DictNode).where(DictNode.parent_id == parent_id, DictNode.name == name, DictNode.is_deleted == False)
    if exclude_id is not None:
        query = query.where(DictNode.id != exclude_id)
    existing = (await db.execute(query)).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.DICT_NAME_EXISTS, get_msg("DICT_NAME_EXISTS"))


def _build_tree(nodes: list[DictNode]) -> list[DictNodeListItem]:
    items = {
        node.id: DictNodeListItem(
            id=node.id,
            parent_id=node.parent_id,
            code=node.code,
            name=node.name,
            sort_order=node.sort_order,
            status=node.status,
            remark=node.remark,
            is_system=node.is_system,
            children=[],
        )
        for node in nodes
    }
    roots: list[DictNodeListItem] = []
    for node in nodes:
        item = items[node.id]
        if node.parent_id and node.parent_id in items:
            items[node.parent_id].children.append(item)
        else:
            roots.append(item)

    def sort_nodes(tree_nodes: list[DictNodeListItem]) -> None:
        tree_nodes.sort(key=lambda n: (n.sort_order, n.id))
        for child in tree_nodes:
            sort_nodes(child.children)

    sort_nodes(roots)
    return roots


def _match(node: DictNodeListItem, name: str | None, code: str | None, remark: str | None) -> bool:
    return (
        (not name or name.lower() in node.name.lower())
        and (not code or code.lower() in node.code.lower())
        and (not remark or remark.lower() in (node.remark or "").lower())
    )


def _filter_tree(
    nodes: list[DictNodeListItem],
    name: str | None,
    code: str | None,
    remark: str | None,
) -> list[DictNodeListItem]:
    if not any([name, code, remark]):
        return nodes

    result: list[DictNodeListItem] = []
    for node in nodes:
        filtered_children = _filter_tree(node.children, name, code, remark)
        if _match(node, name, code, remark):
            result.append(node)
        elif filtered_children:
            result.append(node.model_copy(update={"children": filtered_children}))
    return result


async def get_tree(
    db: AsyncSession,
    name: str | None = None,
    code: str | None = None,
    remark: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> list[DictNodeListItem]:
    # 无过滤参数时使用内存缓存（详情页常见场景，字典数据变化极少）
    use_cache = all(v is None for v in [name, code, remark, sort_by, sort_order])

    if use_cache:
        now = time.time()
        if _dict_tree_cache and _dict_tree_cache.get("expires", 0) > now:
            return [DictNodeListItem.model_validate(item) for item in _dict_tree_cache["data"]]

    # 过滤掉已逻辑删除的节点（status='deleted' 或 is_deleted=True），仅返回可见数据
    query = select(DictNode).where(DictNode.status != "deleted", DictNode.is_deleted == False)

    # 动态排序
    if sort_by and sort_order:
        sort_column = getattr(DictNode, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == 'asc' else sort_column.desc()
            query = query.order_by(order_func, DictNode.id)
        else:
            query = query.order_by(DictNode.sort_order, DictNode.id)
    else:
        query = query.order_by(DictNode.sort_order, DictNode.id)

    nodes = (await db.execute(query)).scalars().all()
    tree = _build_tree(nodes)
    result = _filter_tree(tree, name, code, remark)

    # 无过滤参数时写入内存缓存
    if use_cache:
        _dict_tree_cache["data"] = [item.model_dump() for item in result]
        _dict_tree_cache["expires"] = time.time() + _DICT_TREE_CACHE_TTL

    return result


async def get_multi_language_options(db: AsyncSession) -> list[LanguageOption]:
    # 与 get_dict_children_by_code("Multi_Languages") 逻辑一致，复用以获得内存缓存
    return await get_dict_children_by_code(db, "Multi_Languages")


async def get_dict_children_by_code(db: AsyncSession, code: str) -> list[LanguageOption]:
    """
    根据根节点 code 获取其所有活跃子节点，返回 LanguageOption 列表。

    带内存缓存（TTL 兜底）；字典写操作提交后由 _invalidate_dict_caches 主动清空，
    正常路径不会读到过期数据。返回列表浅拷贝，防止调用方增删元素污染缓存
    （元素本身仅被只读使用）。
    """
    now = time.time()
    cached = _dict_children_cache.get(code)
    if cached is not None and cached[0] > now:
        return list(cached[1])

    root = (
        await db.execute(
            select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == code, DictNode.is_deleted == False)
        )
    ).scalar_one_or_none()
    if root is None:
        return []

    children = (
        await db.execute(
            select(DictNode)
            .where(DictNode.parent_id == root.id, DictNode.status == "active", DictNode.is_deleted == False)
            .order_by(DictNode.sort_order, DictNode.id)
        )
    ).scalars().all()
    result = [LanguageOption(code=item.code, name=item.name) for item in children]
    _dict_children_cache[code] = (time.time() + _DICT_CHILDREN_CACHE_TTL, result)
    return list(result)


async def create_node(db: AsyncSession, data: DictNodeCreate) -> DictNode:
    if data.parent_id is not None:
        await get_node(db, data.parent_id)
    node_code = (data.code or "").strip() or uuid.uuid4().hex[:8].upper()
    await _ensure_unique_code(db, data.parent_id, node_code)
    await _ensure_unique_name(db, data.parent_id, data.name)
    node = DictNode(
        parent_id=data.parent_id,
        code=node_code,
        name=data.name,
        sort_order=data.sort_order,
        remark=data.remark,
        status=DictStatus.ACTIVE.value,
        is_system=False,
    )
    db.add(node)
    try:
        await db.flush()
    except IntegrityError as e:
        if "dict_pkey" in str(e) or "UniqueViolation" in str(e):
            await db.rollback()
            await _fix_sequence(db)
            db.add(node)
            await db.flush()
        else:
            raise
    await db.refresh(node)
    return _to_response(node)


async def update_node(db: AsyncSession, node_id: int, data: DictNodeUpdate) -> DictNodeListItem:
    node = await get_node(db, node_id)
    if data.name is not None and data.name != node.name:
        await _ensure_unique_name(db, node.parent_id, data.name, exclude_id=node_id)
        node.name = data.name
    if data.sort_order is not None:
        node.sort_order = data.sort_order
    if data.remark is not None:
        node.remark = data.remark
    await db.commit()
    await db.refresh(node)
    return _to_response(node)


async def _collect_descendant_ids(db: AsyncSession, parent_id: int) -> list[int]:
    ids: list[int] = []
    children = (await db.execute(select(DictNode.id).where(DictNode.parent_id == parent_id, DictNode.is_deleted == False))).scalars().all()
    for child_id in children:
        ids.append(child_id)
        ids.extend(await _collect_descendant_ids(db, child_id))
    return ids


async def toggle_status(db: AsyncSession, node_id: int, new_status: str) -> DictNode:
    """
    更新节点状态。
    当 new_status='deleted' 时执行逻辑删除：
      - 系统内置节点不允许删除
      - 级联将所有子节点状态也设为 'deleted' 且 is_deleted=True
    """
    node = await get_node(db, node_id)
    if new_status == "deleted":
        if node.is_system:
            raise BusinessException(ErrorCode.SYSTEM_DICT_CANNOT_DELETE, get_msg("SYSTEM_DICT_CANNOT_DELETE"))
        descendant_ids = await _collect_descendant_ids(db, node.id)
        if descendant_ids:
            await db.execute(
                DictNode.__table__.update()
                .where(DictNode.id.in_(descendant_ids))
                .values(status="deleted", is_deleted=True)
            )
        node.is_deleted = True
    node.status = new_status
    await db.commit()
    await db.refresh(node)
    return node


async def delete_node(db: AsyncSession, node_id: int) -> None:
    node = await get_node(db, node_id)
    if node.is_system:
        raise BusinessException(ErrorCode.SYSTEM_DICT_CANNOT_DELETE, get_msg("SYSTEM_DICT_CANNOT_DELETE"))
    descendant_ids = await _collect_descendant_ids(db, node.id)
    if descendant_ids:
        await db.execute(DictNode.__table__.delete().where(DictNode.id.in_(descendant_ids)))
    await db.delete(node)
    await db.commit()
