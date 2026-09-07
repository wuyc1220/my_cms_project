"""
内容工作流服务层。

职责：
- 状态变更日志记录（Status Logs）
- 内容处理流程管理（Processes）
- 内容创建时初始化预设流程节点
"""

from datetime import datetime
import re

from loguru import logger
from app.common.core.i18n import get_msg
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
    from app.internal.cms_biz_system.models.user import User

    logs = await status_log_repo.list_status_logs_by_content_id(db, content_id)

    usernames = {log.processed_by for log in logs if log.processed_by}
    display_name_map: dict[str, str] = {}
    if usernames:
        u_result = await db.execute(
            select(User.username, User.display_name).where(User.username.in_(usernames))
        )
        display_name_map = {uname: dn for uname, dn in u_result.all() if dn}

    items = []
    for log in logs:
        item = StatusLogListItem.model_validate(log)
        item.processed_by_display_name = display_name_map.get(log.processed_by) if log.processed_by else None
        items.append(item)
    return items


async def list_processes(db: AsyncSession, content_id: int, current_status: str | None = None) -> list[ProcessListItem]:
    """查询内容处理流程列表。

    直接返回已有的流程操作记录，不自动初始化，不自动推进。
    计算 processed_before 字段：判断该节点之前是否已完成过。

    PublishPlan 节点语义特殊：仅创建发布计划不算"处理过"，
    只有发布任务真正执行成功（publish_task 存在成功发布记录）才算；
    内容被编辑回滚后（存在 ApplicationReview/Pending 记录）发布历史重置，
    仅统计该回滚时间点之后的成功发布。
    """
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
    from app.internal.cms_biz_publish.repositories import publish_repository
    from app.internal.cms_biz_system.models.user import User
    from sqlalchemy import select, and_
    
    processes = await process_repo.list_processes_by_content_id(db, content_id)

    usernames = {p.assigned for p in processes if p.assigned}
    display_name_map: dict[str, str] = {}
    if usernames:
        u_result = await db.execute(
            select(User.username, User.display_name).where(User.username.in_(usernames))
        )
        display_name_map = {uname: dn for uname, dn in u_result.all() if dn}
    
    # 为每个流程记录计算 processed_before
    result = []
    for process in processes:
        # 查询该 content_id + node_code 组合是否有之前已完成的记录
        # 条件：创建时间早于当前记录，且状态为 Passed/Finished/Failed（已完成）
        match_code = process.node_code or process.name

        if match_code == "PublishPlan":
            # PublishPlan 记录语义特殊（创建发布计划时写入一条，执行发布不产生新记录）：
            # - 仅创建计划不算"处理过"，只有发布任务真正执行成功
            #   （publish_task 中 task_type=publish 且 publish_status=success）才算；
            # - 内容流程重新开始后发布历史重置：以当前记录之前最近一条
            #   ApplicationReview 记录为分界点（不限状态，覆盖两种场景：
            #   ① 编辑回滚写入的 Pending 记录；② 发布后重新提交/发起审核写入的
            #   Passed 记录——live_service 发起审核即打勾），仅统计该时间点之后
            #   的成功发布（对应"重新审核后需重新发布"的红色语义）。
            latest_rollback = (
                await db.execute(
                    select(ContentProcess).where(
                        and_(
                            ContentProcess.content_id == content_id,
                            ContentProcess.node_code == "ApplicationReview",
                            ContentProcess.created_at < process.created_at,
                            ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
                        )
                    ).order_by(ContentProcess.created_at.desc()).limit(1)
                )
            ).scalar_one_or_none()

            rollback_at = latest_rollback.created_at if latest_rollback is not None else None
            # 排除本记录自身对应的发布任务（info 中带"任务#ID"标记）：
            # 立即发布时流程记录在任务执行成功后写入，publish_time 早于记录 created_at，
            # 若不排除，首次发布的 Processed Before 会把自身计为"之前已发布"而误显示绿色。
            own_task_ids = {
                int(m) for m in re.findall(r"任务#(\d+)", process.info or "")
            }
            published_before = await publish_repository.has_successful_publish_before(
                db, content_id, before=process.created_at, after=rollback_at,
                exclude_task_ids=own_task_ids or None,
            )
            if not published_before:
                # 补充：同一发布任务被复用时（更新计划后再次执行，publish_time 被覆盖、
                # publish_task 中上次执行证据丢失），更早的"发布任务执行成功"Passed
                # 记录本身就是一次真实发布，计入 Processed Before（同样受回滚边界约束）。
                earlier_executed = (
                    await db.execute(
                        select(ContentProcess.id).where(
                            and_(
                                ContentProcess.content_id == content_id,
                                ContentProcess.node_code == "PublishPlan",
                                ContentProcess.status == "Passed",
                                ContentProcess.info.like("发布任务执行成功%"),
                                ContentProcess.created_at < process.created_at,
                                ContentProcess.is_deleted.is_(False),
                                ContentProcess.is_discarded.is_(False),
                                *(
                                    [ContentProcess.created_at > rollback_at]
                                    if rollback_at is not None else []
                                ),
                            )
                        ).limit(1)
                    )
                ).scalar_one_or_none()
                published_before = earlier_executed is not None
            process.processed_before = published_before
        else:
            # 删除记录自身即分界点：其 processed_before 反映"删除前该节点是否已处理过"
            # （如必填海报全部上传后删除一张，删除前节点已完整处理，应显示绿色）。
            # 注意：分界清零只对"删除之后的再次操作"生效（见下方 latest_delete 逻辑），
            # 删除记录自身不套用分界，否则上传→删除的场景会误显示为首次处理（红色）。
            if process.info and process.info.startswith("删除"):
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
                process.processed_before = previous_completed is not None
            else:
                # 删除操作会使该节点的处理历史清零：删除之后的再次操作应视为"首次处理"（红色）。
                # 因此先定位当前记录之前最近的一次"删除操作记录"作为历史分界点，
                # 仅统计该分界点之后的已完成记录。删除操作记录通过 info 以"删除"开头识别
                # （删除子内容/物理频道/演员角色/材料等入口均以"删除…"开头写入 info）。
                latest_delete = (
                    await db.execute(
                        select(ContentProcess).where(
                            and_(
                                ContentProcess.content_id == content_id,
                                ContentProcess.node_code == match_code,
                                ContentProcess.created_at < process.created_at,
                                ContentProcess.info.like("删除%"),
                                ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
                            )
                        ).order_by(ContentProcess.created_at.desc()).limit(1)
                    )
                ).scalar_one_or_none()

                history_conditions = [
                    ContentProcess.content_id == content_id,
                    ContentProcess.node_code == match_code,
                    ContentProcess.created_at < process.created_at,
                    ContentProcess.status.in_(["Passed", "Finished", "Failed"]),
                    ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
                ]
                # 存在删除分界点时，只统计该删除之后的已完成记录；
                # 删除分界点本身及其之前的历史不再计入，从而"删除后再添加"判定为首次处理（红色）。
                if latest_delete is not None:
                    history_conditions.append(ContentProcess.created_at > latest_delete.created_at)

                previous_completed = (
                    await db.execute(
                        select(ContentProcess).where(and_(*history_conditions))
                        .order_by(ContentProcess.created_at.desc()).limit(1)
                    )
                ).scalar_one_or_none()

                # 如果之前没有已完成的记录，说明是首次处理（红色）
                # 如果之前有已完成的记录，说明是重复处理（蓝色）
                process.processed_before = previous_completed is not None
        item = ProcessListItem.model_validate(process)
        item.assigned_display_name = display_name_map.get(process.assigned) if process.assigned else None
        result.append(item)
    
    return result


# 内容类型到流程配置所属模块的映射
CONTENT_TYPE_TO_BELONGING = {
    "MOVIE": "MOVIE",
    "EPISODE": "EPISODE",
    "SERIES": "SERIES",
    "SEASON_SERIES": "SEASON_SERIES",
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
    logger.info(f"[update_status] content_id={content_id} process={process_name} bind_status_after={new_status} should_be_waiting={should_be_waiting} current_status={content.status}")
    if should_be_waiting:
        new_status = "WaitingForMaterials"

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


async def _evaluate_process_status(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    process_name: str,
) -> str:
    """
    根据 process_name 调用对应的检查方法，判断流程节点是否真正完成。

    返回:
        "Passed"  — 节点全部完成
        "Pending" — 节点尚未完成（部分完成或未完成）
    """
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService

    try:
        checker = PROCESS_STATUS_CHECKERS.get(process_name)
        if checker is None:
            # 没有对应检查器的节点（如 ApplicationReview、ContentReview 等）默认记为 Passed
            return "Passed"

        done = await checker(db, content_id, content_type)
        return "Passed" if done else "Pending"
    except Exception as e:
        logger.warning(f"评估流程节点状态失败，默认 Passed | process={process_name} error={e}")
        return "Passed"


async def _check_posters_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_posters(db, content_id, content_type)


async def _check_sub_content_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    # CHANNEL 没有 Content 子内容（物理频道存于 physical_channel 表），check_sub_content
    # 查 parent_id 恒为 False，导致添加物理频道后流程记录恒为 Pending；
    # InjectSubContent 节点对 CHANNEL 对应物理频道绑定检查（与 should_be_waiting_for_materials 口径一致）
    if content_type == "CHANNEL":
        return await ContentStatusService.check_physical_channel(db, content_id)
    # check_sub_content 只接受 (db, content_id)，多传 content_type 会触发 TypeError，
    # 被 _evaluate_process_status 的异常兜底吞掉后默认记 Passed，导致删除唯一子内容后流程记录状态错误
    return await ContentStatusService.check_sub_content(db, content_id)


async def _check_physical_channel_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    # check_physical_channel 只接受 (db, content_id)，同上
    return await ContentStatusService.check_physical_channel(db, content_id)


async def _check_materials_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_materials(db, content_id)


async def _check_metadata_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    # Metadata 节点 Status 判定必须与详情页元数据红绿勾同一口径：
    # check_metadata_complete 基于 metadata_validation_rule 必填规则 + 必填自定义字段。
    # 原先用 ContentStatusService.check_metadata（元数据记录存在即 Passed），
    # 总季同步元数据给单季/单集后，即使子级缺必填自定义字段（顶部红叉），
    # Processes 的 Status 也显示 Passed，两处口径自相矛盾。
    from app.internal.cms_biz_orchestration.services.metadata_validation_service import (
        check_metadata_complete,
    )
    ok, _missing = await check_metadata_complete(db, content_id, content_type or "")
    return ok


async def _check_cast_role_map_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_cast_role_map(db, content_id)


async def _check_trailer_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_trailer(db, content_id)


async def _check_music_effects_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_music_effects(db, content_id)


async def _check_package_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_package(db, content_id)


async def _check_category_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
    return await ContentStatusService.check_category(db, content_id)


async def _check_publish_plan_wrapper(db: AsyncSession, content_id: int, content_type: str) -> bool:
    # PublishPlan 只有发布任务真正执行成功（task_type=publish 且 publish_status=success）
    # 才算完成；创建未来时间的计划不算（计划只是未来安排）。
    # 否则计划创建后流程记录恒为 Passed，与详情页"未发布红叉"的口径自相矛盾。
    # publish_time 为 timestamptz，必须用带时区的当前时间比较
    from datetime import datetime, timezone
    from app.internal.cms_biz_publish.repositories import publish_repository
    return await publish_repository.has_successful_publish_before(
        db, content_id, before=datetime.now(timezone.utc)
    )


# 流程节点编码 → 检查函数映射
# 未列出的节点默认记为 Passed（如 ApplicationReview、ContentReview 等由专门流程控制的节点）
PROCESS_STATUS_CHECKERS: dict[str, callable] = {
    "Posters": _check_posters_wrapper,
    "UploadPosters": _check_posters_wrapper,
    "Materials": _check_materials_wrapper,
    "MissingMaterials": _check_materials_wrapper,
    "Metadata": _check_metadata_wrapper,
    "SupplementMetadata": _check_metadata_wrapper,
    "CastRoleMap": _check_cast_role_map_wrapper,
    "Trailer": _check_trailer_wrapper,
    "MusicEffects": _check_music_effects_wrapper,
    "Package": _check_package_wrapper,
    "Category": _check_category_wrapper,
    "InjectSubContent": _check_sub_content_wrapper,
    "PhysicalChannel": _check_physical_channel_wrapper,
    "PublishPlan": _check_publish_plan_wrapper,
}


async def complete_process_and_update_status(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    process_name: str,
    processed_by: str | None = None,
    info: str | None = None,
    skip_status_update: bool = False,
    record_status: str | None = None,
) -> tuple[bool, str | None]:
    """
    完成流程节点并更新内容状态。
    
    当用户完成某个操作（如上传海报、注入材料等）时调用此函数。
    
    参数：
        skip_status_update: 为True时仅创建流程记录，不更新content.status。
            用于“创建发布计划”场景，计划不应改变Ingest状态。
        record_status: 指定时直接作为流程记录状态，跳过节点完成度评估。
            用于“归档带过元数据”等场景：先记 Pending，待用户编辑确认后再置 Passed。
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

    # 根据 process_name 检查节点是否真正完成，决定流程记录状态
    # 全部完成 → Passed，未完成 → Pending
    # record_status 允许调用方显式指定记录状态（如归档带过元数据先记 Pending，用户编辑确认后 Passed）
    if record_status is not None:
        process_record_status = record_status
    else:
        process_record_status = await _evaluate_process_status(db, content_id, content_type, process_name)

    # 每次操作直接创建一条新的流程记录
    # end_dt 记录本次操作时间；Pending 状态仅表示节点尚未全部完成
    process = ContentProcess(
        content_id=content_id,
        name=node_display_name,  # ✅ 使用显示名称（如 'Episodes'）
        node_code=process_name,  # ✅ 使用节点编码（如 'InjectSubContent'）
        sequence=0,
        start_dt=now,
        status=process_record_status,
        end_dt=now,
        assigned=processed_by,
        info=info,
    )
    await process_repo.add_process(db, process)
    logger.info(f"创建流程记录 | content_id={content_id} process={process_name} record_status={process_record_status}")
    
    # skip_status_update=True 时，仅创建流程记录（用于发布计划场景），
    # 不更新 content.status，计划不应改变 Ingest 状态
    if skip_status_update:
        return True, None
    
    # 2. 根据流程配置更新内容状态
    new_status = await update_content_status_by_process_completion(
        db, content_id, content_type, process_name, processed_by
    )
    logger.info(f"[complete_process] content_id={content_id} process={process_name} workflow_status={new_status}")

    # 3. 如果流程配置没有返回新状态，使用默认状态流转规则
    if new_status is None:
        new_status = await _update_status_by_default_rules(
            db, content_id, content_type, process_name, processed_by
        )
        logger.info(f"[complete_process] content_id={content_id} process={process_name} default_status={new_status}")

    # 4. 同步父内容状态（EPISODE → SERIES → SEASON 递归向上）
    if new_status is not None:
        from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
        await ContentStatusService.sync_parent_status(db, content_id, content_type)

    return True, new_status


async def rollback_after_published_edit(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    edited_by: str,
    edit_info: str,
) -> None:
    from datetime import datetime
    from sqlalchemy import select, update
    from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
    from app.internal.cms_biz_orchestration.models.content_review import ContentReview
    from app.internal.cms_biz_package.models.task import Task
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id

    content = await get_content_by_id(db, content_id)
    if not content:
        logger.warning(f"内容不存在: {content_id}")
        return

    if content.status in ("Published", "Closed", "ReadyForPublish", "Publishing", "PublishFailed"):
        # 已发布/已下架等状态 → 全量回滚逻辑
        old_status = content.status
        content.status = "InProgress"

        if hasattr(content, "previous_status") and content.previous_status is not None:
            content.previous_status = None

        await record_status_change(
            db,
            content_id=content_id,
            before_status=old_status,
            after_status="InProgress",
            processed_by=edited_by,
        )

        now = datetime.now()
        pending_review = ContentProcess(
            content_id=content_id,
            name="ApplicationReview",
            node_code="ApplicationReview",
            sequence=3,
            start_dt=now,
            status="Pending",
            end_dt=None,
            assigned=edited_by,
            info=get_msg("PROCESS_ROLLBACK_AFTER_EDIT", edit_info=edit_info),
        )
        db.add(pending_review)

        await db.execute(
            update(Task)
            .where(
                Task.content_id == content_id,
                Task.task_type.in_(["review L1", "review L2", "review L3"]),
                Task.is_deleted == False,
            )
            .values(is_deleted=True)
        )

        arrangement_tasks = (
            await db.execute(
                select(Task).where(
                    Task.content_id == content_id,
                    Task.task_type == "arrangement",
                    Task.is_deleted == False,
                )
            )
        ).scalars().all()

        for task in arrangement_tasks:
            task.task_status = "Pending"
            task.end_time = None

        # 软删除 ContentReview 审批记录（避免 initiate_content_review 查到 Pending 记录而报错）
        content_review_record = (
            await db.execute(
                select(ContentReview)
                .where(
                    ContentReview.content_id == content_id,
                    ContentReview.is_deleted.is_(False),
                )
                .order_by(ContentReview.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()

        if content_review_record:
            content_review_record.is_deleted = True

        logger.info(
            "已发布内容编辑后回滚 | content_id={} {} → InProgress, 新增ApplicationReview Pending记录",
            content_id, old_status,
        )
    elif content.status == "InProgress":
        # InProgress 状态下有待审批的 ContentReview → 直接清理评审记录，让用户重新发起
        existing_review = (
            await db.execute(
                select(ContentReview).where(
                    ContentReview.content_id == content_id,
                    ContentReview.is_deleted.is_(False),
                    ContentReview.final_status == "Pending"
                ).limit(1)
            )
        ).scalar_one_or_none()

        if existing_review:
            # 软删除 ContentReview（让 initiate_content_review 可以新建）
            existing_review.is_deleted = True

            # 软删除 review L1/L2/L3 任务
            await db.execute(
                update(Task)
                .where(
                    Task.content_id == content_id,
                    Task.task_type.in_(["review L1", "review L2", "review L3"]),
                    Task.is_deleted == False,
                )
                .values(is_deleted=True)
            )

            logger.info(
                "InProgress 内容编辑后清理评审记录 | content_id={}",
                content_id,
            )

        # 已发布过的内容再次编辑，且当前没有待审批的 ContentReview 时，
        # 重置 ApplicationReview 为 Pending，避免提交审核入口仍显示绿色✓。
        # 存在 Pending ContentReview 时按规范只清理审核记录，不重置提交审核节点。
        if not existing_review:
            from app.internal.cms_biz_publish.repositories import publish_repository

            obj_status = await publish_repository.get_object_publish_status(db, "Content", content_id)
            if obj_status and obj_status.is_published:
                latest_app_review = (
                    await db.execute(
                        select(ContentProcess).where(
                            ContentProcess.content_id == content_id,
                            ContentProcess.node_code == "ApplicationReview",
                            ContentProcess.is_deleted.is_(False),
                        ).order_by(ContentProcess.created_at.desc()).limit(1)
                    )
                ).scalar_one_or_none()
                if latest_app_review is None or latest_app_review.status != "Pending":
                    now = datetime.now()
                    pending_review = ContentProcess(
                        content_id=content_id,
                        name="ApplicationReview",
                        node_code="ApplicationReview",
                        sequence=3,
                        start_dt=now,
                        status="Pending",
                        end_dt=None,
                        assigned=edited_by,
                        info=get_msg("PROCESS_ROLLBACK_AFTER_EDIT", edit_info=edit_info),
                    )
                    db.add(pending_review)
                    logger.info(
                        "已发布内容再次编辑，重置 ApplicationReview 为 Pending | content_id={}",
                        content_id,
                    )


# 祖先回退递归深度硬上限：正常层级最多3层（EPISODE→SERIES→SEASON），
# 超过视为数据异常（环状脏数据或逻辑错误），强制终止防止死循环
_MAX_ANCESTOR_ROLLBACK_DEPTH = 10


async def rollback_ancestors_after_child_change(
    db: AsyncSession,
    start_parent_id: int,
    edited_by: str,
    edit_info: str,
) -> None:
    """子内容新增/删除后，从其直接父级开始递归向上回退所有祖先的发布状态。

    场景：
        - 删除/新增 EPISODE → 回退父级 SERIES/SEASON_SERIES → 递归回退祖父 SEASON
        - 删除/新增 SEASON_SERIES → 回退父级 SEASON
        - 删除/新增 SCHEDULE → 回退父级 CHANNEL

    注意：start_parent_id 必须由调用方传入（删除场景子内容已软删，无法反查 parent_id）。

    防死循环（硬性防护，任何场景生效）：
        - visited 集合：父级已在访问路径中说明 parent_id 成环，立即终止
        - 深度上限：超过 _MAX_ANCESTOR_ROLLBACK_DEPTH 强制终止
    """
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id

    visited: set[int] = set()
    current_id = start_parent_id
    for depth in range(_MAX_ANCESTOR_ROLLBACK_DEPTH + 1):
        if current_id in visited:
            logger.error(
                f"[祖先回退] 检测到环状引用（#{current_id} 已在访问路径中），强制终止 | "
                f"start_parent_id={start_parent_id} | visited={visited}"
            )
            return
        visited.add(current_id)

        ancestor = await get_content_by_id(db, current_id)
        if not ancestor:
            return

        logger.info(
            f"[祖先回退] 子内容变更（{edit_info}），回退祖先 #{current_id} "
            f"({ancestor.content_type}) | depth={depth}"
        )
        await rollback_after_published_edit(
            db, current_id, ancestor.content_type, edited_by, edit_info
        )

        if not ancestor.parent_id:
            return
        current_id = ancestor.parent_id

    logger.error(
        f"[祖先回退] 递归深度超上限 {_MAX_ANCESTOR_ROLLBACK_DEPTH}，"
        f"疑似环状脏数据，强制终止 | start_parent_id={start_parent_id} | visited={visited}"
    )


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
