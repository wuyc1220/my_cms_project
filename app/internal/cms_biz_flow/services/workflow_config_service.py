"""
工作流配置业务逻辑层。

处理流程配置的 CRUD、发布、版本管理。
"""
import json
from datetime import datetime
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import NotFoundException, ErrorCode, BusinessException
from app.common.core.i18n import get_msg
from app.internal.cms_biz_flow.models.workflow_config import WorkflowConfig, WorkflowNodeConfig
from app.internal.cms_biz_flow.repositories import workflow_config_repo as repo
from app.internal.cms_biz_flow.schemas.workflow_config import (
    WorkflowConfigCreate,
    WorkflowConfigDetail,
    WorkflowConfigListItem,
    WorkflowConfigUpdate,
    WorkflowNodeConfigCreate,
    WorkflowNodeConfigItem,
    WorkflowNodeConfigUpdate,
)
from app.common.schemas import PaginatedResponse


# 可用流程环节定义
AVAILABLE_NODES = [
    {"code": "Materials", "name": "缺失材料", "name_en": "Missing Materials"},
    {"code": "Metadata", "name": "补充元数据", "name_en": "Supplement Metadata"},
    {"code": "Posters", "name": "上传海报", "name_en": "Upload Posters"},
    {"code": "CastRoleMap", "name": "选择演职人员", "name_en": "Cast Role Map"},
    {"code": "Category", "name": "注入栏目", "name_en": "Inject Category"},
    {"code": "Package", "name": "注入服务包", "name_en": "Inject Package"},
    {"code": "ApplicationReview", "name": "申请审核", "name_en": "Application Review"},
    {"code": "ContentReview", "name": "内容审核", "name_en": "Content Review"},
    {"code": "PublishPlan", "name": "发布", "name_en": "Publish"},
    {"code": "InjectSubContent", "name": "注入子内容", "name_en": "Inject Sub Content"},
    {"code": "Trailer", "name": "注入预告片", "name_en": "Trailer"},
    {"code": "MusicEffects", "name": "注入字幕等", "name_en": "Music & Effects File"},
    {"code": "Encoding", "name": "编码转码", "name_en": "Encoding"},
]


async def list_workflow_configs(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    process_code: Optional[str] = None,
    process_name: Optional[str] = None,
    belonging: Optional[list[str]] = None,
    status: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
) -> PaginatedResponse[WorkflowConfigListItem]:
    """查询流程配置列表。"""
    configs, total = await repo.list_workflow_configs(
        db,
        process_code=process_code,
        process_name=process_name,
        belonging=belonging,
        status=status,
        page=page,
        page_size=page_size,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    items = [WorkflowConfigListItem.model_validate(c) for c in configs]

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


async def get_workflow_config(db: AsyncSession, config_id: int) -> WorkflowConfigDetail:
    """获取流程配置详情（含节点）。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    nodes = await repo.list_workflow_nodes(db, config_id)
    node_items = [WorkflowNodeConfigItem.model_validate(n) for n in nodes]

    # 手动构造详情，避免 SQLAlchemy 关系懒加载问题
    detail = WorkflowConfigDetail(
        id=config.id,
        process_code=config.process_code,
        process_name=config.process_name,
        belonging=config.belonging,
        status=config.status,
        version=config.version,
        published_version=config.published_version,
        config_json=config.config_json,
        nodes=node_items,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )
    return detail


async def create_workflow_config(
    db: AsyncSession, data: WorkflowConfigCreate, user_id: Optional[int] = None
) -> WorkflowConfigDetail:
    """创建流程配置。"""
    existing_draft = await repo.get_draft_by_code(db, data.process_code)
    if existing_draft:
        raise BusinessException(ErrorCode.WORKFLOW_CONFIG_DRAFT_EXISTS, get_msg("WORKFLOW_CONFIG_DRAFT_EXISTS"))

    max_version = await repo.get_max_version_by_code(db, data.process_code)

    config = WorkflowConfig(
        process_code=data.process_code,
        process_name=data.process_name,
        belonging=data.belonging,
        status="draft",
        version=max_version + 1,
        created_by=user_id,
    )
    config = await repo.create_workflow_config(db, config)

    # 手动构造详情，避免 SQLAlchemy 关系懒加载问题
    detail = WorkflowConfigDetail(
        id=config.id,
        process_code=config.process_code,
        process_name=config.process_name,
        belonging=config.belonging,
        status=config.status,
        version=config.version,
        published_version=config.published_version,
        config_json=config.config_json,
        nodes=[],
        created_at=config.created_at,
        updated_at=config.updated_at,
    )
    return detail


async def update_workflow_config(
    db: AsyncSession, config_id: int, data: WorkflowConfigUpdate, user_id: Optional[int] = None
) -> WorkflowConfigDetail:
    """更新流程配置。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    if config.status == "published":
        raise BusinessException(ErrorCode.WORKFLOW_CONFIG_PUBLISHED_CANNOT_EDIT, get_msg("WORKFLOW_CONFIG_PUBLISHED_CANNOT_EDIT"))

    if data.process_code is not None:
        config.process_code = data.process_code

    if data.process_name is not None:
        config.process_name = data.process_name

    if data.belonging is not None:
        config.belonging = data.belonging

    if data.config_json is not None:
        config.config_json = data.config_json
        await _sync_nodes_from_json(db, config, data.config_json, user_id)

    config.updated_by = user_id
    config = await repo.update_workflow_config(db, config)

    return await get_workflow_config(db, config_id)


async def delete_workflow_config(db: AsyncSession, config_id: int) -> bool:
    """删除流程配置。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    return await repo.delete_workflow_config(db, config_id)


async def publish_workflow_config(db: AsyncSession, config_id: int, user_id: Optional[int] = None) -> WorkflowConfigDetail:
    """发布流程配置。同一模块同时只能有一个已发布版本。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    if not config.config_json:
        raise BusinessException(ErrorCode.WORKFLOW_CONFIG_EMPTY, get_msg("WORKFLOW_CONFIG_EMPTY"))

    current_published = await repo.get_published_workflow_by_belonging(db, config.belonging)
    if current_published and current_published.id != config_id:
        current_published.status = "unpublished"
        current_published.published_version = None
        logger.info(
            "取消旧版本发布 | config_id={} process_code={}",
            current_published.id, current_published.process_code,
        )

    config = await repo.publish_workflow_config(db, config_id)
    logger.info("发布流程配置 | config_id={} process_code={} version={}", config_id, config.process_code, config.version)

    return await get_workflow_config(db, config_id)


async def unpublish_workflow_config(db: AsyncSession, config_id: int, user_id: Optional[int] = None) -> WorkflowConfigDetail:
    """取消发布流程配置。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    if config.status != "published":
        raise BusinessException(ErrorCode.WORKFLOW_CONFIG_ONLY_UNPUBLISH_PUBLISHED, get_msg("WORKFLOW_CONFIG_ONLY_UNPUBLISH_PUBLISHED"))

    config = await repo.unpublish_workflow_config(db, config_id)
    logger.info("取消发布流程配置 | config_id={} process_code={}", config_id, config.process_code)

    return await get_workflow_config(db, config_id)


async def batch_publish(db: AsyncSession, config_ids: list[int], user_id: Optional[int] = None) -> list[WorkflowConfigDetail]:
    """批量发布流程配置。"""
    results = []
    for config_id in config_ids:
        try:
            result = await publish_workflow_config(db, config_id, user_id)
            results.append(result)
        except BusinessException as e:
            logger.warning(f"批量发布跳过: config_id={config_id}, reason={e.message}")
            continue
    return results


async def get_available_nodes() -> list[dict]:
    """获取可用的流程环节列表。"""
    return AVAILABLE_NODES


async def get_published_workflow_for_belonging(db: AsyncSession, belonging: str) -> WorkflowConfigDetail | None:
    """获取指定模块的已发布流程配置。"""
    config = await repo.get_published_workflow_by_belonging(db, belonging)
    if not config:
        return None

    nodes = await repo.list_workflow_nodes(db, config.id)
    node_items = [WorkflowNodeConfigItem.model_validate(n) for n in nodes]

    # 手动构造详情，避免 SQLAlchemy 关系懒加载问题
    detail = WorkflowConfigDetail(
        id=config.id,
        process_code=config.process_code,
        process_name=config.process_name,
        belonging=config.belonging,
        status=config.status,
        version=config.version,
        published_version=config.published_version,
        config_json=config.config_json,
        nodes=node_items,
        created_at=config.created_at,
        updated_at=config.updated_at,
    )
    return detail


async def create_new_version(db: AsyncSession, config_id: int, user_id: Optional[int] = None) -> WorkflowConfigDetail:
    """基于当前配置创建新草稿版本。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    existing_draft = await repo.get_draft_by_code(db, config.process_code)
    if existing_draft:
        raise BusinessException(
            ErrorCode.WORKFLOW_CONFIG_DRAFT_EXISTS,
            get_msg("WORKFLOW_CONFIG_DRAFT_EXISTS", code=config.process_code, version=existing_draft.version)
        )

    max_version = await repo.get_max_version_by_code(db, config.process_code)

    new_config = WorkflowConfig(
        process_code=config.process_code,
        process_name=config.process_name,
        belonging=config.belonging,
        status="draft",
        version=max_version + 1,
        config_json=config.config_json,
        created_by=user_id,
    )
    new_config = await repo.create_workflow_config(db, new_config)

    nodes = await repo.list_workflow_nodes(db, config_id)

    # 建立旧ID到新ID的映射
    id_mapping: dict[str, int] = {}
    created_nodes: list[WorkflowNodeConfig] = []

    for node in nodes:
        old_id = str(node.id)
        new_node = WorkflowNodeConfig(
            workflow_config_id=new_config.id,
            node_code=node.node_code,
            node_name=node.node_name,
            node_type=node.node_type,
            mandatory=node.mandatory,
            parallel_rule=node.parallel_rule,
            bind_status_before=node.bind_status_before,
            bind_status_after=node.bind_status_after,
            position_x=node.position_x,
            position_y=node.position_y,
            width=node.width,
            height=node.height,
            sequence=node.sequence,
            parent_node_id=node.parent_node_id,
            created_by=user_id,
        )
        new_node = await repo.create_workflow_node(db, new_node)
        id_mapping[old_id] = new_node.id
        created_nodes.append(new_node)

    # 更新数据库中节点的 parent_node_id 为新ID
    for node in created_nodes:
        if node.parent_node_id is not None:
            old_parent_id_str = str(node.parent_node_id)
            if old_parent_id_str in id_mapping:
                node.parent_node_id = id_mapping[old_parent_id_str]
                await db.merge(node)

    # 更新 config_json 中的节点ID、parent_node_id 和 edges 引用
    if config.config_json:
        try:
            data = json.loads(config.config_json)

            # 更新 nodes 中的 id 和 parent_node_id
            for node_data in data.get("nodes", []):
                node_id_str = str(node_data.get("id", ""))
                if node_id_str in id_mapping:
                    node_data["id"] = id_mapping[node_id_str]
                parent_id = node_data.get("parent_node_id")
                if parent_id is not None:
                    old_parent_id_str = str(parent_id)
                    if old_parent_id_str in id_mapping:
                        node_data["parent_node_id"] = id_mapping[old_parent_id_str]

            # 更新 edges 中的 source/target
            for edge in data.get("edges", []):
                source = str(edge.get("source", ""))
                target = str(edge.get("target", ""))
                if source in id_mapping:
                    edge["source"] = id_mapping[source]
                if target in id_mapping:
                    edge["target"] = id_mapping[target]

            new_config.config_json = json.dumps(data, ensure_ascii=False)
        except json.JSONDecodeError:
            logger.warning("create_new_version: config_json 解析失败，跳过ID更新")

    return await get_workflow_config(db, new_config.id)


async def get_version_history(db: AsyncSession, config_id: int) -> list[dict]:
    """获取流程配置的版本历史。"""
    config = await repo.get_workflow_config_by_id(db, config_id)
    if not config:
        raise NotFoundException(ErrorCode.WORKFLOW_CONFIG_NOT_FOUND, get_msg("WORKFLOW_CONFIG_NOT_FOUND"))

    versions = await repo.get_version_history(db, config.process_code)
    return [
        {
            "id": v.id,
            "version": v.version,
            "status": v.status,
            "published_version": v.published_version,
            "created_at": v.created_at.isoformat() if v.created_at else None,
            "updated_at": v.updated_at.isoformat() if v.updated_at else None,
            "created_by": v.created_by,
        }
        for v in versions
    ]


async def _sync_nodes_from_json(
    db: AsyncSession, config: WorkflowConfig, config_json: str, user_id: Optional[int] = None
) -> None:
    """从 JSON 配置同步节点数据，并更新 config_json 中的节点 ID 和 edges 引用。"""
    try:
        data = json.loads(config_json)
    except json.JSONDecodeError:
        raise BusinessException(ErrorCode.WORKFLOW_CONFIG_JSON_INVALID, get_msg("WORKFLOW_CONFIG_JSON_INVALID"))

    nodes_data = data.get("nodes", [])
    edges_data = data.get("edges", [])

    await repo.delete_workflow_nodes_by_config_id(db, config.id)

    id_mapping: dict[str, int] = {}
    new_nodes_data = []
    created_nodes = []

    # 第一遍：创建所有节点，建立 ID 映射
    for node_data in nodes_data:
        old_id = str(node_data.get("id", ""))
        node_type = node_data.get("node_type", "process")

        # start 和 end 节点不保存到数据库，只保留在 config_json 中
        if node_type in ("start", "end"):
            new_nodes_data.append({**node_data})
            continue

        node = WorkflowNodeConfig(
            workflow_config_id=config.id,
            node_code=node_data.get("node_code", ""),
            node_name=node_data.get("node_name", ""),
            node_type=node_type,
            mandatory=node_data.get("mandatory", True),
            parallel_rule=node_data.get("parallel_rule"),
            bind_status_before=node_data.get("bind_status_before"),
            bind_status_after=node_data.get("bind_status_after"),
            position_x=node_data.get("position_x"),
            position_y=node_data.get("position_y"),
            width=node_data.get("width"),
            height=node_data.get("height"),
            sequence=node_data.get("sequence", 0),
            parent_node_id=node_data.get("parent_node_id"),
            created_by=user_id,
        )
        new_node = await repo.create_workflow_node(db, node)
        if old_id:
            id_mapping[old_id] = new_node.id
        new_nodes_data.append({**node_data, "id": new_node.id})
        created_nodes.append(new_node)

    # 第二遍：更新数据库中节点的 parent_node_id 为新的节点 ID
    for node in created_nodes:
        old_parent_id = node.parent_node_id
        if old_parent_id is not None:
            old_parent_id_str = str(old_parent_id)
            if old_parent_id_str in id_mapping:
                new_parent_id = id_mapping[old_parent_id_str]
                node.parent_node_id = new_parent_id
                await db.merge(node)

    # 第三遍：更新 edges 中的 source/target 为新的节点 ID
    new_edges_data = []
    for edge in edges_data:
        new_edge = dict(edge)
        source = str(edge.get("source", ""))
        target = str(edge.get("target", ""))
        if source in id_mapping:
            new_edge["source"] = id_mapping[source]
        if target in id_mapping:
            new_edge["target"] = id_mapping[target]
        new_edges_data.append(new_edge)

    # 第四遍：更新 nodes JSON 中的 parent_node_id 为新的节点 ID
    for node_data in new_nodes_data:
        parent_id = node_data.get("parent_node_id")
        if parent_id is not None:
            parent_id_str = str(parent_id)
            if parent_id_str in id_mapping:
                node_data["parent_node_id"] = id_mapping[parent_id_str]

    # 更新 config_json
    data["nodes"] = new_nodes_data
    data["edges"] = new_edges_data
    config.config_json = json.dumps(data, ensure_ascii=False)
