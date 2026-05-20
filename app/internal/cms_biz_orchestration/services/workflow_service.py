"""
内容工作流服务层。

职责：
- 状态变更日志记录（Status Logs）
- 内容处理流程管理（Processes）
- 内容创建时初始化预设流程节点
"""

from datetime import datetime
from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.models.content_status_log import ContentStatusLog
from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
from app.internal.cms_biz_orchestration.repositories import status_log_repo
from app.internal.cms_biz_orchestration.repositories import process_repo
from app.internal.cms_biz_orchestration.schemas.live import (
    StatusLogListItem,
    ProcessListItem,
)
from app.internal.cms_biz_system.models.dict import DictNode


# 预设内容处理流程节点（顺序号、code）
# 对应字典表 Content_Process_Names 中的 code 字段
DEFAULT_PROCESS_TEMPLATES = [
    (1, "MissingMaterials"),
    (2, "SupplementMetadata"),
    (3, "UploadPosters"),
    (4, "ContentReview"),
]

# 状态变更到流程节点的映射
# 当内容状态变为某状态时，标记对应的流程节点为 Passed
# 对应字典表 Content_Process_Names 中的 code 字段
STATUS_TO_PROCESS_MAP = {
    "WaitingForMaterials": "MissingMaterials",
    "InProgress": "SupplementMetadata",
    "ReadyForPublish": "UploadPosters",
    "Published": "ContentReview",
}

# 默认状态流转顺序（当字典表不可用时使用）
# 实际应从字典表 Ingest_Status 的 sort_order 获取
DEFAULT_STATUS_FLOW_ORDER = ["None", "WaitingForMaterials", "InProgress", "ReadyForPublish", "Publishing", "Published"]


async def get_status_flow_order(db: AsyncSession) -> list[str]:
    """从字典表获取 Ingest 状态流转顺序。
    
    查询 Ingest_Status 字典的所有子节点，按 sort_order 排序返回 code 列表。
    如果字典表未配置或查询失败，返回默认顺序。
    """
    try:
        # 查询 Ingest_Status 根节点
        root = (
            await db.execute(
                select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == "Ingest_Status", DictNode.is_deleted == False)
            )
        ).scalar_one_or_none()
        
        if not root:
            logger.warning("字典表 Ingest_Status 未找到，使用默认状态流转顺序")
            return DEFAULT_STATUS_FLOW_ORDER
        
        # 查询所有子节点，按 sort_order 排序
        children = (
            await db.execute(
                select(DictNode)
                .where(DictNode.parent_id == root.id, DictNode.status == "active", DictNode.is_deleted == False)
                .order_by(DictNode.sort_order, DictNode.id)
            )
        ).scalars().all()
        
        if not children:
            logger.warning("字典表 Ingest_Status 没有子节点，使用默认状态流转顺序")
            return DEFAULT_STATUS_FLOW_ORDER
        
        # 返回所有子节点的 code 列表
        return [child.code for child in children]
    except Exception as e:
        logger.warning(f"从字典表获取状态流转顺序失败: {e}，使用默认顺序")
        return DEFAULT_STATUS_FLOW_ORDER


async def record_status_change(
    db: AsyncSession,
    content_id: int,
    before_status: str,
    after_status: str,
    processed_by: str | None = None,
) -> None:
    """记录内容状态变更日志。"""
    if before_status == after_status:
        return

    log = ContentStatusLog(
        content_id=content_id,
        before_status=before_status,
        after_status=after_status,
        processed_by=processed_by,
        processed_at=datetime.now(),
    )
    await status_log_repo.add_status_log(db, log)
    logger.info(
        "记录状态变更日志 | content_id={} {} → {} by={}",
        content_id, before_status, after_status, processed_by,
    )


async def list_status_logs(db: AsyncSession, content_id: int) -> list[StatusLogListItem]:
    """查询内容状态变更日志列表。"""
    logs = await status_log_repo.list_status_logs_by_content_id(db, content_id)
    return [StatusLogListItem.model_validate(log) for log in logs]


async def list_processes(db: AsyncSession, content_id: int, current_status: str | None = None) -> list[ProcessListItem]:
    """查询内容处理流程列表。

    直接返回已有的流程操作记录，不自动初始化，不自动推进。
    计算 processed_before 字段：判断该节点之前是否已完成过。
    """
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
    from sqlalchemy import select, and_
    
    processes = await process_repo.list_processes_by_content_id(db, content_id)
    
    # 为每个流程记录计算 processed_before
    result = []
    for process in processes:
        # 查询该 content_id + node_code 组合是否有之前已完成的记录
        # 条件：创建时间早于当前记录，且状态为 Passed/Finished/Failed（已完成）
        match_code = process.node_code or process.name
        previous_completed = (
            await db.execute(
                select(ContentProcess).where(
                    and_(
                        ContentProcess.content_id == content_id,
                        ContentProcess.node_code == match_code,
                        ContentProcess.created_at < process.created_at,
                        ContentProcess.status.in_(["Passed", "Finished", "Failed"]),
                        ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
                    )
                ).order_by(ContentProcess.created_at.desc()).limit(1)
            )
        ).scalar_one_or_none()
        
        # 如果之前没有已完成的记录，说明是首次处理（红色）
        # 如果之前有已完成的记录，说明是重复处理（蓝色）
        process.processed_before = previous_completed is not None
        result.append(ProcessListItem.model_validate(process))
    
    return result


# 内容类型到流程配置所属模块的映射
CONTENT_TYPE_TO_BELONGING = {
    "MOVIE": "PROGRAM",
    "EPISODE": "PROGRAM",
    "SERIES": "SERIES",
    "SEASON": "SEASON",
    "CHANNEL": "CHANNEL",
    "SCHEDULE": "SCHEDULE",
}


async def update_content_status_by_process_completion(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    process_name: str,
    processed_by: str | None = None,
) -> str | None:
    """
    当流程节点处理完成时，根据流程配置更新内容状态。
    
    参数：
        db: 数据库会话
        content_id: 内容ID
        content_type: 内容类型（MOVIE/EPISODE/SERIES等）
        process_name: 流程节点名称
    
    返回：
        新的内容状态，如果没有配置则返回None
    """
    from app.internal.cms_biz_flow.repositories.workflow_config_repo import (
        get_published_workflow_by_belonging,
        list_workflow_nodes,
    )
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    
    # 获取内容所属模块
    belonging = CONTENT_TYPE_TO_BELONGING.get(content_type)
    if not belonging:
        logger.warning(f"未知内容类型: {content_type}, 无法获取流程配置")
        return None
    
    # 获取已发布的流程配置
    workflow_config = await get_published_workflow_by_belonging(db, belonging)
    if not workflow_config:
        logger.info(f"模块 {belonging} 没有已发布的流程配置")
        return None
    
    # 获取流程节点配置
    nodes = await list_workflow_nodes(db, workflow_config.id)
    
    # 找到当前处理的节点
    current_node = None
    for node in nodes:
        if node.node_code == process_name or node.node_name == process_name:
            current_node = node
            break
    
    if not current_node:
        logger.warning(f"流程配置中未找到节点: {process_name}")
        return None
    
    # 获取节点配置的 bind_status_after
    new_status = current_node.bind_status_after
    if not new_status:
        logger.info(f"节点 {process_name} 没有配置 bind_status_after，不更新内容状态")
        return None
    
    # 获取当前内容
    content = await get_content_by_id(db, content_id)
    if not content:
        logger.warning(f"内容不存在: {content_id}")
        return None

    # 判断是否应该变为待上传素材状态
    # PROGRAM (MOVIE/EPISODE): 没有上传素材 → WaitingForMaterials
    # SERIES/SEASON: 没有绑定子内容 → WaitingForMaterials
    # CHANNEL: 没有绑定物理频道 → WaitingForMaterials
    # SCHEDULE/ARCHIVED: 不存在待上传素材状态，直接使用节点配置的状态
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    should_be_waiting = await ContentStatusService.should_be_waiting_for_materials(db, content_id, content_type)
    if should_be_waiting:
        new_status = "WaitingForMaterials"

    # 状态优先级定义（用于防止状态回退）
    STATUS_PRIORITY = {
        "None": 0,
        "WaitingForMaterials": 1,
        "InProgress": 2,
        "ReadyForPublish": 3,
        "Publishing": 4,
        "PublishFailed": 5,
        "Published": 6,
        "NoActiveLicense": 7,
        "Closed": 8,
    }
    
    current_priority = STATUS_PRIORITY.get(content.status, 0)
    new_priority = STATUS_PRIORITY.get(new_status, 0)
    
    # 核心逻辑：防止状态回退
    # 特殊情况允许更新：
    # 1. 待上传素材状态（删除素材导致），无论当前状态如何，都要更新
    # 2. 已下架状态（Closed），允许恢复到正常流程节点状态
    # 3. 否则，只有当新状态优先级 >= 当前状态优先级时才更新
    is_closed_state = content.status == "Closed"
    if new_status != "WaitingForMaterials" and not is_closed_state and new_priority < current_priority:
        logger.info(
            "跳过状态更新（防止回退）| content_id={} process={} 当前状态={} (优先级{}) 目标状态={} (优先级{})",
            content_id, process_name, content.status, current_priority, new_status, new_priority,
        )
        return content.status

    # 如果状态没有变化，不更新
    if content.status == new_status:
        return new_status
    
    # 记录状态变更前的状态
    old_status = content.status
    content.status = new_status

    if new_status != "Closed" and hasattr(content, 'previous_status') and content.previous_status is not None:
        content.previous_status = None

    # 记录状态变更日志
    await record_status_change(
        db,
        content_id=content_id,
        before_status=old_status,
        after_status=new_status,
        processed_by=processed_by or "system",
    )
    
    logger.info(
        "根据流程节点完成更新内容状态 | content_id={} process={} {} → {}",
        content_id, process_name, old_status, new_status,
    )
    
    return new_status


async def complete_process_and_update_status(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    process_name: str,
    processed_by: str | None = None,
    info: str | None = None,
) -> tuple[bool, str | None]:
    """
    完成流程节点并更新内容状态。
    
    当用户完成某个操作（如上传海报、注入材料等）时调用此函数。
    
    参数：
        db: 数据库会话
        content_id: 内容ID
        content_type: 内容类型
        process_name: 流程节点名称（如 "Posters", "Materials" 等）
        processed_by: 处理人
        info: 备注信息
    
    返回：
        (是否成功, 新的内容状态)
    """
    from datetime import datetime
    from sqlalchemy import select
    from app.internal.cms_biz_flow.models.workflow_config import WorkflowConfig, WorkflowNodeConfig

    now = datetime.now()

    # 根据 content_type 获取对应的 workflow_config
    # 不同内容类型的 InjectSubContent 节点有不同的显示名称
    # SERIES -> 'Episodes', SEASON -> 'Season Series', CHANNEL -> 'Physical Channel'
    workflow_config = (
        await db.execute(
            select(WorkflowConfig).where(
                WorkflowConfig.belonging == content_type,
                WorkflowConfig.status == 'published',
                WorkflowConfig.is_deleted.is_(False),
            ).order_by(
                WorkflowConfig.version.desc()
            ).limit(1)
        )
    ).scalar_one_or_none()
    
    # 从 workflow_node_config 表查询节点的显示名称
    # 需要同时匹配 workflow_config_id 和 node_code
    node_config = None
    if workflow_config:
        node_config = (
            await db.execute(
                select(WorkflowNodeConfig).where(
                    WorkflowNodeConfig.workflow_config_id == workflow_config.id,
                    WorkflowNodeConfig.node_code == process_name,
                    WorkflowNodeConfig.is_deleted.is_(False),
                ).limit(1)
            )
        ).scalar_one_or_none()
    
    node_display_name = node_config.node_name if node_config else process_name

    # 每次操作直接创建一条新的流程记录
    process = ContentProcess(
        content_id=content_id,
        name=node_display_name,  # ✅ 使用显示名称（如 'Episodes'）
        node_code=process_name,  # ✅ 使用节点编码（如 'InjectSubContent'）
        sequence=0,
        start_dt=now,
        status="Passed",
        end_dt=now,
        assigned=processed_by,
        info=info,
    )
    await process_repo.add_process(db, process)
    logger.info(f"创建流程记录 | content_id={content_id} process={process_name}")
    
    # 2. 根据流程配置更新内容状态
    new_status = await update_content_status_by_process_completion(
        db, content_id, content_type, process_name, processed_by
    )

    # 3. 如果流程配置没有返回新状态，使用默认状态流转规则
    if new_status is None:
        new_status = await _update_status_by_default_rules(
            db, content_id, content_type, process_name, processed_by
        )

    # 4. 同步父内容状态（EPISODE → SERIES → SEASON 递归向上）
    if new_status is not None:
        from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
        await ContentStatusService.sync_parent_status(db, content_id, content_type)

    return True, new_status


# 流程节点到默认后置状态的映射（当流程配置未设置时使用）
PROCESS_TO_DEFAULT_STATUS = {
    "Materials": "InProgress",
    "MissingMaterials": "InProgress",
    "Metadata": "InProgress",
    "SupplementMetadata": "InProgress",
    "Posters": "ReadyForPublish",
    "UploadPosters": "ReadyForPublish",
    "ContentReview": "Published",
    "ApplicationReview": "ReadyForPublish",
    "PublishPlan": "Published",
    "Category": "InProgress",
    "Package": "InProgress",
    "CastRoleMap": "InProgress",
    "Trailer": "InProgress",
    "MusicEffects": "InProgress",
    "Encoding": "ReadyForPublish",
    "InjectSubContent": "InProgress",
    "PhysicalChannel": "InProgress",
}


async def _update_status_by_default_rules(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    process_name: str,
    processed_by: str | None = None,
) -> str | None:
    """
    当流程配置未设置 bind_status_after 时，使用默认规则更新内容状态。

    参数：
        db: 数据库会话
        content_id: 内容ID
        content_type: 内容类型
        process_name: 流程节点名称

    返回：
        新的内容状态，如果没有匹配规则则返回None
    """
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id

    default_status = PROCESS_TO_DEFAULT_STATUS.get(process_name)
    if not default_status:
        logger.info(f"节点 {process_name} 没有默认状态规则，不更新内容状态")
        return None

    content = await get_content_by_id(db, content_id)
    if not content:
        logger.warning(f"内容不存在: {content_id}")
        return None

    # 判断是否应该变为待上传素材状态
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    should_be_waiting = await ContentStatusService.should_be_waiting_for_materials(db, content_id, content_type)
    if should_be_waiting:
        default_status = "WaitingForMaterials"

    # 状态优先级定义（用于防止状态回退）
    STATUS_PRIORITY = {
        "None": 0,
        "WaitingForMaterials": 1,
        "InProgress": 2,
        "ReadyForPublish": 3,
        "Publishing": 4,
        "PublishFailed": 5,
        "Published": 6,
        "NoActiveLicense": 7,
        "Closed": 8,
    }
    
    current_priority = STATUS_PRIORITY.get(content.status, 0)
    new_priority = STATUS_PRIORITY.get(default_status, 0)
    
    # 核心逻辑：防止状态回退
    # 特殊情况允许更新：
    # 1. 待上传素材状态（删除素材导致），无论当前状态如何，都要更新
    # 2. 已下架状态（Closed），允许恢复到正常流程节点状态
    # 3. 否则，只有当新状态优先级 >= 当前状态优先级时才更新
    is_closed_state = content.status == "Closed"
    if default_status != "WaitingForMaterials" and not is_closed_state and new_priority < current_priority:
        logger.info(
            "跳过状态更新（防止回退）| content_id={} process={} 当前状态={} (优先级{}) 目标状态={} (优先级{})",
            content_id, process_name, content.status, current_priority, default_status, new_priority,
        )
        return content.status

    # 如果状态没有变化，不更新
    if content.status == default_status:
        return default_status

    old_status = content.status
    content.status = default_status

    if default_status != "Closed" and hasattr(content, 'previous_status') and content.previous_status is not None:
        content.previous_status = None

    # 记录状态变更日志
    await record_status_change(
        db,
        content_id=content_id,
        before_status=old_status,
        after_status=default_status,
        processed_by=processed_by or "system",
    )

    logger.info(
        "根据默认规则更新内容状态 | content_id={} process={} {} → {}",
        content_id, process_name, old_status, default_status,
    )

    return default_status
