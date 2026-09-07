"""
发布管理业务逻辑层。

处理发布/下架任务的创建、修改、取消，以及注入历史的查询。
"""
import asyncio
import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import NotFoundException, ErrorCode, BusinessException
from app.common.core.i18n import get_msg
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_publish.repositories import publish_repository
from app.internal.cms_biz_publish.schemas.publish import (
    ArchivePublishCheckResponse,
    BatchPublishRequest,
    BatchPublishResultItem,
    IngestHistoryItem,
    PublishListItem,
    PublishPlanCreate,
    PublishPlanResponse,
    PublishPlanUpdate,
)
from app.common.schemas import PaginatedResponse
from app.soap.config import soap_settings
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
)


# ═══════════════════════════════════════════════════════════
# 发布任务列表
# ═══════════════════════════════════════════════════════════

async def list_publish_tasks(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    content_name: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    ingest_statuses: Optional[list[str]] = None,
    publish_statuses: Optional[list[str]] = None,
    publish_time_from: Optional[str] = None,
    publish_time_to: Optional[str] = None,
    unpublish_time_from: Optional[str] = None,
    unpublish_time_to: Optional[str] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
) -> PaginatedResponse[PublishListItem]:
    """查询发布任务列表"""
    tasks, total = await publish_repository.list_publish_tasks(
        db,
        page=page,
        page_size=page_size,
        content_name=content_name,
        content_types=content_types,
        ingest_statuses=ingest_statuses,
        publish_statuses=publish_statuses,
        publish_time_from=publish_time_from,
        publish_time_to=publish_time_to,
        unpublish_time_from=unpublish_time_from,
        unpublish_time_to=unpublish_time_to,
        sort_by=sort_by,
        sort_order=sort_order,
    )

    items = []
    for task in tasks:
        # 获取实体的最新信息
        ingest_status = None
        entity_name = task.entity_name
        # 使用局部变量承载 content_type，避免修改持久化对象导致 session 变脏，
        # 触发 autoflush 发出 UPDATE（该行可能被后台发布事务锁定，引发阻塞超时）
        content_type = task.content_type
        if task.entity_type == "Content" and task.entity_id:
            content = await publish_repository.get_content_by_id(db, task.entity_id)
            if content:
                ingest_status = content.status
                entity_name = content.title
                # 从 Content 表同步最新的 content_type（仅用于本次响应，不写库）
                content_type = content.content_type

        items.append(PublishListItem(
            id=task.id,
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            entity_name=entity_name,
            content_type=content_type,
            ingest_status=ingest_status,
            publish_status=task.publish_status,
            task_type=task.task_type,
            publish_time=task.publish_time,
            unpublish_time=task.unpublish_time,
            scheduled_time=task.scheduled_time,
            execution_mode=task.execution_mode,
        ))

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ═══════════════════════════════════════════════════════════
# 发布/下架计划管理
# ═══════════════════════════════════════════════════════════

def _get_initial_publish_status(task_type: str, execution_mode: str) -> str:
    """根据任务类型和执行方式获取初始发布状态"""
    if execution_mode == "now":
        return "publishing" if task_type == "publish" else "closed"
    else:
        return "plan"


async def check_schedule_archive_published(
    db: AsyncSession,
    schedule_id: int,
) -> tuple[bool, Optional[int]]:
    """校验节目单归档产物（元数据 program_id 指向的内容）是否已发布。

    权威规则：以 ObjectPublishStatus.is_published（实际发布状态）为准，
    而不是 Content.status（归档回滚后节目单 status 为 InProgress）。
    无归档产物时视为可发布。

    返回：(can_publish, archive_content_id)
    """
    from app.internal.cms_biz_orchestration.models.content_metadata import ScheduleMetadata
    sched_meta = (await db.execute(
        select(ScheduleMetadata).where(
            ScheduleMetadata.content_id == schedule_id,
            ScheduleMetadata.is_deleted.is_(False),
        )
    )).scalar_one_or_none()
    if not sched_meta or not sched_meta.program_id:
        return True, None
    try:
        archive_content_id = int(sched_meta.program_id)
    except (ValueError, TypeError):
        return True, None
    archive_content = await publish_repository.get_content_by_id(db, archive_content_id)
    # 归档产物已被删除（内容管理页删除归档节目，软删/物理删后查询不到）：
    # 视为"无归档产物"放行，不阻断原始节目单发布——与上方"无归档产物时视为可发布"口径一致
    if not archive_content:
        return True, None
    archive_obj_status = await publish_repository.get_object_publish_status(
        db, "Content", archive_content_id
    )
    if not archive_obj_status or not archive_obj_status.is_published:
        return False, archive_content_id
    return True, archive_content_id


async def check_archive_publish_status(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
) -> ArchivePublishCheckResponse:
    """发布前预检查：节目单归档产物是否已发布（内容详情页点击发布节点时调用）。

    与 create_publish_plan 的归档校验共用同一规则，供前端在状态门槛之前预检，
    使归档场景（节目单 InProgress）下也能给出"归档内容未发布"的明确提示。
    """
    can_publish = True
    archive_content_id: Optional[int] = None
    message: Optional[str] = None
    if entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, entity_id)
        if content and content.content_type == "SCHEDULE":
            can_publish, archive_content_id = await check_schedule_archive_published(
                db, entity_id
            )
            if not can_publish:
                message = get_msg("ARCHIVED_CONTENT_NOT_PUBLISHED")
    return ArchivePublishCheckResponse(
        can_publish=can_publish,
        archive_content_id=archive_content_id,
        message=message,
    )


async def create_publish_plan(
    db: AsyncSession,
    data: PublishPlanCreate,
    user_id: Optional[int] = None,
    processed_by: Optional[str] = None,
    _skip_parent_republish: bool = False,
    _skip_cascade_children: bool = False,
) -> PublishPlanResponse:
    """创建或更新发布/下架计划

    参数：
        _skip_parent_republish: 内部参数，级联发布/父级重新发布时传 True，
                                防止子→父→子→父 死循环
        _skip_cascade_children: 内部参数，父级重新发布时传 True，
                                防止父级级联发布刚触发的子内容（避免重复发布）
    """
    logger.info(
        f"[创建发布计划] entity_type={data.entity_type}, entity_id={data.entity_id}, "
        f"task_type={data.task_type}, execution_mode={data.execution_mode}, "
        f"scheduled_time={data.scheduled_time}, "
        f"skip_parent_republish={_skip_parent_republish}, skip_cascade_children={_skip_cascade_children}"
    )
    
    # 【新增】校验海报是否已发布（只对 SCHEDULE 和 CHANNEL 类型）
    if data.task_type == "publish":
        from app.internal.cms_biz_orchestration.services.picture_check_service import (
            check_pictures_before_publish,
        )
        await check_pictures_before_publish(
            db=db,
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            content_type=data.content_type,
            raise_exception=True,
        )
    
    # 检查实体是否存在
    content = None
    if data.entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, data.entity_id)
        if not content:
            raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))

    # 【业务规则】节目单二次发布时，校验已归档内容（program_id）是否已发布
    # 必须以 ObjectPublishStatus.is_published（实际发布状态）为准，而不是 Content.status
    if (
        data.task_type == "publish"
        and data.entity_type == "Content"
        and data.content_type == "SCHEDULE"
        and content
    ):
        can_publish, _archive_id = await check_schedule_archive_published(db, data.entity_id)
        if not can_publish:
            raise BusinessException(
                ErrorCode.ARCHIVED_CONTENT_NOT_PUBLISHED,
                get_msg("ARCHIVED_CONTENT_NOT_PUBLISHED"),
                400
            )

    # 发布子内容时，校验父内容的发布状态
    if data.task_type == "publish" and data.entity_type == "Content" and data.content_type:
        await _validate_parent_published(db, data.content_type, data.entity_id)

    # 获取现有任务（加行级锁，防止并发更新同一条记录导致死锁）
    existing_task = await publish_repository.get_entity_current_publish_status(
        db, data.entity_type, data.entity_id, for_update=True
    )

    # 检查状态冲突（已取消的任务允许重新设置；pending 状态允许修改计划时间）
    if existing_task:
        if existing_task.publish_status == "plan" and existing_task.status not in ("pending", "cancelled"):
            raise BusinessException(ErrorCode.PUBLISH_PLAN_ALREADY_EXISTS, get_msg("PUBLISH_PLAN_ALREADY_EXISTS"))
        if existing_task.publish_status == "publishing":
            raise BusinessException(ErrorCode.PUBLISH_TASK_IN_PROGRESS, get_msg("PUBLISH_TASK_IN_PROGRESS"))
    if existing_task:
        # 更新现有任务
        logger.info(
            f"[更新发布计划] task_id={existing_task.id}, 原 scheduled_time={existing_task.scheduled_time}, "
            f"新 scheduled_time={data.scheduled_time}"
        )
        existing_task.task_type = data.task_type
        existing_task.execution_mode = data.execution_mode
        existing_task.scheduled_time = data.scheduled_time
        existing_task.status = "pending" if data.execution_mode == "plan" else "processing"
        existing_task.publish_status = _get_initial_publish_status(data.task_type, data.execution_mode)
        # 复用任务时同步更新创建人：重新发布算新一次操作，后续定时执行回溯
        # created_by 才能显示最新操作账号（老任务 created_by 可能为 None）
        existing_task.created_by = user_id
        existing_task.updated_by = user_id
        task = await publish_repository.update_publish_task(db, existing_task)
    else:
        # 创建新任务
        task = PublishTask(
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            entity_name=data.entity_name,
            content_type=data.content_type,
            task_type=data.task_type,
            execution_mode=data.execution_mode,
            scheduled_time=data.scheduled_time,
            status="pending" if data.execution_mode == "plan" else "processing",
            publish_status=_get_initial_publish_status(data.task_type, data.execution_mode),
            created_by=user_id,
        )
        task = await publish_repository.create_publish_task(db, task)

    # 完成 PublishPlan 流程节点（语义为"计划已创建"）：
    # - 计划发布（execution_mode="plan"）：写入 Pending 记录（计划只是未来安排，
    #   不算已处理），任务未来真正执行成功后追加"发布任务执行成功"Passed 记录；
    # - 立即发布（execution_mode="now"）：跳过本记录，只写一条"发布任务执行成功"
    #   Passed 记录——立即发布时两条记录几乎同时写入（相差不到 1 秒），
    #   Pending+Passed 成对出现易被误认为重复记录。
    # skip_status_update=True 保证只创建流程记录，不修改 content.status：
    # - 计划发布不应改变 Ingest Status（计划只是未来安排）
    # - 立即发布由 _execute_publish_task 同步状态，避免此处覆盖
    # 时序说明：必须在 _execute_publish_task 之前写入（计划发布场景）——
    # "创建发布计划"记录先于"发布任务执行成功"记录，processed_before 判定才能
    # 正确区分"本轮发布"与"分界前的历史发布"（重新审核后发布应显示红色，
    # 同任务复用时本轮执行成功记录创建时间晚于创建计划记录，不会被误计为
    # "之前已发布"）。立即发布无创建计划记录，processed_before 由
    # has_successful_publish_before（排除自身任务#ID）判定，不受影响。
    if (
        data.entity_type == "Content"
        and data.content_type
        and data.task_type == "publish"
        and data.execution_mode != "now"
    ):
        await complete_process_and_update_status(
            db,
            content_id=data.entity_id,
            content_type=data.content_type,
            process_name="PublishPlan",
            processed_by=processed_by,
            # 任务#ID 标记：list_processes 计算 processed_before 时排除自身任务，
            # 否则首次发布的记录会把本次发布误计为"之前已发布"（绿勾）
            info=f"创建发布计划: {data.task_type} 任务#{task.id}",
            skip_status_update=True,
        )

    # 如果是立即执行，触发发布流程
    if data.execution_mode == "now":
        logger.info(f"[create_publish_plan] 立即执行模式，准备调用 _execute_publish_task | entity_id={data.entity_id} | task_id={task.id}")
        # 传入当前账号：执行成功记录/状态日志的 Processed By 显示操作人
        await _execute_publish_task(db, task, processed_by=processed_by)
        logger.info(f"[create_publish_plan] _execute_publish_task 执行完毕 | entity_id={data.entity_id}")
    else:
        logger.info(f"[create_publish_plan] 计划发布模式，跳过 _execute_publish_task | entity_id={data.entity_id}")

    # 级联创建子内容的发布/下架计划
    # cascade_ignore_status：发布管理入口传 True（忽略子内容状态），内容详情入口默认 False
    # _skip_cascade_children：父级重新发布时跳过子级级联，避免重复发布刚触发的子内容
    if _skip_cascade_children:
        logger.info(
            f"[级联跳过] 内容 #{data.entity_id} ({data.content_type}) 为父级重新发布场景，"
            f"跳过子级级联（防止重复发布触发源子内容）"
        )
    elif data.entity_type == "Content" and data.content_type:
        await _cascade_create_plan_for_children(
            db,
            content_id=data.entity_id,
            content_type=data.content_type,
            task_type=data.task_type,
            execution_mode=data.execution_mode,
            scheduled_time=data.scheduled_time,
            user_id=user_id,
            ignore_child_status=data.cascade_ignore_status,
            processed_by=processed_by,
        )

    # 发布子内容后，如果父内容已发布过，则重新发布父内容（递归向上）
    # 级联发布/父级重新发布时跳过，防止子→父→子→父 死循环
    if _skip_parent_republish:
        logger.info(
            f"[父级重发跳过] 内容 #{data.entity_id} ({data.content_type}) 为级联/父级重发场景，"
            f"跳过父级重新发布（防止子→父→子循环）"
        )
    elif (
        data.entity_type == "Content"
        and data.task_type == "publish"
        and data.execution_mode == "now"
        and data.content_type
    ):
        await _republish_published_parents(db, data.entity_id, data.content_type, user_id, processed_by=processed_by)

    # 注意：_execute_publish_task 内部已经处理了 content.status 的同步
    # 无需在此处重复同步，避免状态被覆盖
    # - 模拟模式：_execute_publish_task 已同步为 Published/Closed
    # - 真实模式：SOAP 回调后会自动同步为 Published/Closed

    return PublishPlanResponse.model_validate(task)


async def get_current_plan(
    db: AsyncSession,
    entity_type: str,
    entity_id: int
) -> Optional[PublishPlanResponse]:
    """获取实体当前的发布任务信息（含已发布/已下架）"""
    task = await publish_repository.get_entity_current_publish_status(
        db, entity_type, entity_id
    )
    if not task or task.status == "cancelled":
        return None
    return PublishPlanResponse.model_validate(task)


async def update_publish_plan(
    db: AsyncSession,
    task_id: int,
    data: PublishPlanUpdate,
    processed_by: Optional[str] = None,
) -> PublishPlanResponse:
    """修改发布/下架计划"""
    task = await publish_repository.get_publish_task_by_id(db, task_id)
    if not task:
        raise NotFoundException(ErrorCode.PUBLISH_TASK_NOT_FOUND, get_msg("PUBLISH_TASK_NOT_FOUND"))

    # 只能修改待执行或已取消的计划任务
    if task.status not in ("pending", "cancelled"):
        raise BusinessException(ErrorCode.PUBLISH_TASK_CANNOT_MODIFY, get_msg("PUBLISH_TASK_CANNOT_MODIFY"))

    if data.execution_mode == "now":
        # 从计划升级为立即执行：复用已有记录，触发发布/下架流程
        task.execution_mode = "now"
        task.scheduled_time = None
        task.status = "processing"
        task.publish_status = "publishing" if task.task_type == "publish" else "closed"
        await publish_repository.update_publish_task(db, task)
        # 传入当前账号：执行成功记录/状态日志的 Processed By 显示操作人
        await _execute_publish_task(db, task, processed_by=processed_by)

        if task.task_type == "publish" and task.entity_type == "Content" and task.content_type:
            # 修改计划入口仅发布管理页使用，级联忽略子内容状态
            await _cascade_create_plan_for_children(
                db,
                content_id=task.entity_id,
                content_type=task.content_type,
                task_type=task.task_type,
                execution_mode="now",
                ignore_child_status=True,
            )
    else:
        # 修改计划时间
        task.execution_mode = data.execution_mode
        task.scheduled_time = data.scheduled_time
        await publish_repository.update_publish_task(db, task)

    return PublishPlanResponse.model_validate(task)


async def cancel_publish_plan(db: AsyncSession, task_id: int) -> bool:
    """取消发布/下架计划"""
    task = await publish_repository.get_publish_task_by_id(db, task_id)
    if not task:
        raise NotFoundException(ErrorCode.PUBLISH_TASK_NOT_FOUND, get_msg("PUBLISH_TASK_NOT_FOUND"))

    if task.status != "pending":
        raise BusinessException(ErrorCode.PUBLISH_TASK_CANNOT_CANCEL, get_msg("PUBLISH_TASK_CANNOT_CANCEL"))

    # 确定回退的 publish_status：根据实际 content.status 判断
    # 取消计划后应回退到计划前的状态，而不是一律变为 "Not Published"
    # - content.status 为 Closed → 回退为 "closed"（已下架状态）
    # - 其他 → "none"（Not Published）
    revert_status = "none"
    if task.entity_type == "Content":
        # 先检查 object_publish_status 中的 is_published 和最后操作时间
        obj_status = await publish_repository.get_object_publish_status(
            db, "Content", task.entity_id
        )
        if obj_status and obj_status.is_published:
            # 对象标记为已发布过，判断最后是发布还是下架
            if (
                obj_status.last_unpublish_time
                and obj_status.last_publish_time
                and obj_status.last_unpublish_time >= obj_status.last_publish_time
            ):
                revert_status = "closed"  # 最后操作是下架，当前为已下架状态
            else:
                revert_status = "success"  # 无下架记录或最后操作是发布，当前为已发布状态
        else:
            # is_published=False 时，进一步检查 Content 表的实际状态
            # 可能从未发布过（status=InProgress），也可能曾经发布后被下架（status=Closed）
            content = await publish_repository.get_content_by_id(db, task.entity_id)
            if content and content.status == "Closed":
                revert_status = "closed"

    success = await publish_repository.cancel_publish_task(db, task_id, revert_status)
    return success


# ═══════════════════════════════════════════════════════════
# 立即发布/下架
# ═══════════════════════════════════════════════════════════

async def _upgrade_plan_to_now(
    db: AsyncSession,
    existing: PublishTask,
    task_type: str,
    user_id: Optional[int] = None,
    cascade_ignore_status: bool = False,
    processed_by: Optional[str] = None,
) -> PublishPlanResponse:
    """将已有 plan 任务"升级"为立即执行：复用同一条记录，避免列表出现重复行。"""
    existing.task_type = task_type
    existing.execution_mode = "now"
    existing.scheduled_time = None
    existing.status = "processing"
    existing.publish_status = "publishing" if task_type == "publish" else "closed"
    await publish_repository.update_publish_task(db, existing)
    await _execute_publish_task(db, existing, processed_by=processed_by)

    if task_type == "publish" and existing.entity_type == "Content" and existing.content_type:
        await _cascade_create_plan_for_children(
            db,
            content_id=existing.entity_id,
            content_type=existing.content_type,
            task_type=task_type,
            execution_mode="now",
            user_id=user_id,
            ignore_child_status=cascade_ignore_status,
            processed_by=processed_by,
        )
        # 发布子内容后，如果父内容已发布过，则重新发布父内容（递归向上）
        await _republish_published_parents(
            db, existing.entity_id, existing.content_type, user_id, processed_by=processed_by
        )

    # 完成 PublishPlan 流程节点
    # 与 create_publish_plan 保持一致：立即发布（升级模式）也需要记录流程进度
    if existing.entity_type == "Content" and existing.content_type and task_type == "publish":
        await complete_process_and_update_status(
            db,
            content_id=existing.entity_id,
            content_type=existing.content_type,
            process_name="PublishPlan",
            processed_by=processed_by,
            info=f"立即发布（升级计划）: {task_type} 任务#{existing.id}",
            skip_status_update=True,
        )

    return PublishPlanResponse.model_validate(existing)


async def publish_now(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    user_id: Optional[int] = None,
    processed_by: Optional[str] = None,
) -> PublishPlanResponse:
    """立即发布"""
    # 获取实体信息
    entity_name = None
    content_type = None
    if entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, entity_id)
        if not content:
            raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))
        entity_name = content.title
        content_type = content.content_type

    # 校验海报是否已发布（只对 SCHEDULE 和 CHANNEL 类型）
    from app.internal.cms_biz_orchestration.services.picture_check_service import (
        check_pictures_before_publish,
    )
    await check_pictures_before_publish(
        db=db,
        entity_type=entity_type,
        entity_id=entity_id,
        content_type=content_type,
        raise_exception=True,
    )

    # 立即发布场景：若已存在 plan 任务且未取消，复用同一条记录升级为立即执行
    # 加行级锁，防止与定时任务或重复请求并发导致死锁
    existing = await publish_repository.get_entity_current_publish_status(
        db, entity_type, entity_id, for_update=True
    )
    if existing and existing.publish_status == "plan" and existing.status != "cancelled":
        # 发布管理入口：级联发布忽略子内容状态
        result = await _upgrade_plan_to_now(
            db, existing, task_type="publish", user_id=user_id, cascade_ignore_status=True, processed_by=processed_by
        )
    elif existing and existing.publish_status == "publishing":
        # 正在发布中，拒绝重复提交
        raise BusinessException(ErrorCode.PUBLISH_TASK_IN_PROGRESS, get_msg("PUBLISH_TASK_IN_PROGRESS"))
    else:
        data = PublishPlanCreate(
            entity_type=entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            content_type=content_type,
            task_type="publish",
            execution_mode="now",
            cascade_ignore_status=True,  # 发布管理入口：级联发布忽略子内容状态
        )
        result = await create_publish_plan(db, data, user_id, processed_by=processed_by)

    return result


async def unpublish_now(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    user_id: Optional[int] = None,
    processed_by: Optional[str] = None,
) -> PublishPlanResponse:
    """立即下架"""
    # 获取实体信息
    entity_name = None
    content_type = None
    if entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, entity_id)
        if not content:
            raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))
        entity_name = content.title
        content_type = content.content_type

        # 级联下架子内容
        await _cascade_unpublish_children(db, content.id, user_id)

    # 立即下架场景：若已存在 plan 任务且未取消，复用同一条记录升级为立即执行
    # 加行级锁，防止与定时任务或重复请求并发导致死锁
    existing = await publish_repository.get_entity_current_publish_status(
        db, entity_type, entity_id, for_update=True
    )
    if existing and existing.publish_status == "plan" and existing.status != "cancelled":
        return await _upgrade_plan_to_now(db, existing, task_type="unpublish", user_id=user_id, processed_by=processed_by)

    data = PublishPlanCreate(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        content_type=content_type,
        task_type="unpublish",
        execution_mode="now",
    )

    return await create_publish_plan(db, data, user_id, processed_by=processed_by)


# ═══════════════════════════════════════════════════════════
# 批量操作
# ═══════════════════════════════════════════════════════════

async def batch_publish(
    db: AsyncSession,
    data: BatchPublishRequest,
    user_id: Optional[int] = None,
    processed_by: Optional[str] = None,
) -> list[BatchPublishResultItem]:
    """批量发布/下架"""
    results: list[BatchPublishResultItem] = []
    logger.info(f"[batch_publish] 开始批量{data.task_type} | entity_type={data.entity_type} | ids={data.entity_ids} | execution_mode={data.execution_mode}")

    # 【优化】批量发布时去重：如果同批中已包含某内容的祖先（父级/祖父级），
    # 则跳过该内容，因为父级发布时会级联发布子级，单独发布子级会因父级发布任务尚未完成
    # 导致 _validate_parent_published 校验失败。
    entity_ids_to_process = list(data.entity_ids)
    if data.entity_type == "Content" and data.task_type == "publish":
        entity_ids_to_process = await _filter_out_descendants_in_batch(db, list(data.entity_ids))
        if len(entity_ids_to_process) < len(data.entity_ids):
            skipped_ids = set(data.entity_ids) - set(entity_ids_to_process)
            logger.info(f"[batch_publish] 批量发布去重：跳过 {len(skipped_ids)} 个子内容（会被父级级联发布）| skipped_ids={skipped_ids}")

    for entity_id in entity_ids_to_process:
        entity_name = None
        content_type = None

        if data.entity_type == "Content":
            content = await publish_repository.get_content_by_id(db, entity_id)
            if content:
                entity_name = content.title
                content_type = content.content_type
                logger.info(f"[batch_publish] 查询到内容 | entity_id={entity_id} | content_type={content_type} | current_status={content.status}")
            else:
                logger.warning(f"[batch_publish] 未查询到内容 | entity_id={entity_id}")

        # 批量发布前与单行发布保持一致：校验海报是否已发布
        if data.task_type == "publish":
            try:
                from app.internal.cms_biz_orchestration.services.picture_check_service import (
                    check_pictures_before_publish,
                )
                await check_pictures_before_publish(
                    db=db,
                    entity_type=data.entity_type,
                    entity_id=entity_id,
                    content_type=content_type,
                    raise_exception=True,
                )
            except BusinessException as e:
                results.append(BatchPublishResultItem(
                    entity_id=entity_id,
                    entity_name=entity_name,
                    success=False,
                    message=e.message,
                ))
                logger.warning(f"批量{data.task_type} 失败: entity_type={data.entity_type}, entity_id={entity_id}, reason={e.message}")
                continue

        plan_data = PublishPlanCreate(
            entity_type=data.entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            content_type=content_type,
            task_type=data.task_type,
            execution_mode=data.execution_mode,
            scheduled_time=data.scheduled_time,
            cascade_ignore_status=data.cascade_ignore_status,
        )
        logger.info(f"[batch_publish] 准备创建发布计划 | entity_id={entity_id} | content_type={content_type} | execution_mode={data.execution_mode}")

        try:
            result = await create_publish_plan(db, plan_data, user_id, processed_by=processed_by)
            results.append(BatchPublishResultItem(
                entity_id=entity_id,
                entity_name=entity_name,
                success=True,
                data=result,
            ))
            logger.info(f"批量{data.task_type} 成功: entity_type={data.entity_type}, entity_id={entity_id}")
        except BusinessException as e:
            results.append(BatchPublishResultItem(
                entity_id=entity_id,
                entity_name=entity_name,
                success=False,
                message=e.message,
            ))
            logger.warning(f"批量{data.task_type} 失败: entity_type={data.entity_type}, entity_id={entity_id}, reason={e.message}")

    return results


async def _filter_out_descendants_in_batch(
    db: AsyncSession,
    entity_ids: list[int],
) -> list[int]:
    """
    批量发布去重：从 ID 列表中移除那些"祖先也在列表中"的内容。

    业务场景：批量发布勾选了总季+单季+单集时，单季和单集会被总季级联发布，
    无需单独处理，否则会因为父级发布任务尚未完成导致校验失败。

    返回：去重后的 ID 列表（保留祖先，移除后代），保持原顺序。
    """
    if not entity_ids:
        return entity_ids

    # 查询所有内容的 parent_id
    from app.internal.cms_biz_package.models.package import Content
    id_set = set(entity_ids)
    rows = (
        await db.execute(
            select(Content.id, Content.parent_id).where(
                Content.id.in_(id_set),
                Content.is_deleted.is_(False),
            )
        )
    ).all()
    parent_map: dict[int, int | None] = {row[0]: row[1] for row in rows}

    # 自底向上遍历祖先链，如果链上某个祖先也在批次列表里，则跳过该内容
    result: list[int] = []
    for eid in entity_ids:
        if eid not in parent_map:
            result.append(eid)
            continue
        ancestor_id = parent_map[eid]
        has_ancestor_in_batch = False
        visited: set[int] = set()
        while ancestor_id and ancestor_id not in visited:
            visited.add(ancestor_id)
            if ancestor_id in id_set:
                has_ancestor_in_batch = True
                break
            # 继续向上找
            if ancestor_id not in parent_map:
                # 需要查数据库获取更高层祖先
                ancestor_row = (
                    await db.execute(
                        select(Content.parent_id).where(Content.id == ancestor_id)
                    )
                ).scalar_one_or_none()
                if not ancestor_row:
                    break
                parent_map[ancestor_id] = ancestor_row
                ancestor_id = ancestor_row
            else:
                ancestor_id = parent_map[ancestor_id]

        if not has_ancestor_in_batch:
            result.append(eid)
        else:
            logger.info(f"[batch_publish] 跳过 #{eid}（祖先在批次中，会被级联发布）")

    return result


# ═══════════════════════════════════════════════════════════
# 注入历史
# ═══════════════════════════════════════════════════════════


def _build_ingest_history_item(history) -> IngestHistoryItem:
    """将 ORM 模型转换为 Pydantic 模型，并添加下载 URL。"""
    # 先获取字典数据
    data = {
        "id": history.id,
        "entity_type": history.entity_type,
        "entity_id": history.entity_id,
        "entity_name": history.entity_name,
        "action": history.action,
        "status": history.status,
        "create_date": history.create_date,
        "send_date": history.send_date,
        "end_date": history.end_date,
        "ingest_xml_path": history.ingest_xml_path,
        "result_xml_path": history.result_xml_path,
    }
    # 添加下载 URL（带 /api/v1 前缀，前端 fetch 直接使用）
    if history.ingest_xml_path:
        data["ingest_xml_url"] = storage_service.get_file_url(history.ingest_xml_path)
    if history.result_xml_path:
        data["result_xml_url"] = storage_service.get_file_url(history.result_xml_path)
    return IngestHistoryItem(**data)


async def list_ingest_histories(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
) -> PaginatedResponse[IngestHistoryItem]:
    """查询注入历史"""
    histories, total = await publish_repository.list_ingest_histories(
        db,
        page=page,
        page_size=page_size,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        status=status,
    )

    items = [_build_ingest_history_item(h) for h in histories]

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ═══════════════════════════════════════════════════════════
# 内部辅助方法
# ═══════════════════════════════════════════════════════════

async def write_plan_execute_log(db: AsyncSession, task: PublishTask) -> None:
    """定时计划（发布/下架）执行成功后补写操作日志。

    背景：操作日志原先只写在 API 层，定时任务（ContentPublish /
    ContentScheduledOffline）和 SOAP 真实模式回调不经过 API 层，
    导致计划到时执行成功后内容详情 Activity Log 无记录。

    归属口径：
    - 操作人取计划创建人（task.created_by 反查 User）；
    - content_id/entity_id 指向目标内容，保证内容详情 Activity Log 可见；
    - 仅 Content 类型实体写日志（其他实体无内容详情页）；
    - 调用方需保证仅对 execution_mode='plan' 的任务调用（立即发布
      已在 API 层写 PUBLISH_NOW/UNPUBLISH_NOW 日志，勿重复记录）。
    """
    try:
        if task.entity_type != "Content" or not task.entity_id:
            return
        from app.internal.cms_biz_system.services.operation_log_service import (
            OperationType,
            write_log,
        )
        from app.internal.cms_biz_system.models.user import User

        is_publish = task.task_type == "publish"
        user_id = task.created_by
        user_name: str | None = None
        if user_id:
            user_name = (await db.execute(
                select(User.username).where(User.id == user_id)
            )).scalar_one_or_none()

        await write_log(
            db,
            user_id=user_id,
            user_name=user_name or "system",
            operation_type=(
                OperationType.PUBLISH_PLAN_EXECUTE
                if is_publish else OperationType.UNPUBLISH_PLAN_EXECUTE
            ),
            operation_object_code="OBJ_CONTENT",
            operation_object_params={
                "name": task.entity_name or f"Content #{task.entity_id}"
            },
            operation_content_code=(
                "log.publish.plan.execute"
                if is_publish else "log.unpublish.plan.execute"
            ),
            content_id=task.entity_id,
            entity_type="content",
            entity_id=task.entity_id,
            updated_value=(
                f"计划时间: {task.scheduled_time}" if task.scheduled_time else None
            ),
            result="success",
        )
        logger.info(
            f"[write_plan_execute_log] 补写计划执行日志 | task_id={task.id} | "
            f"entity_id={task.entity_id} | task_type={task.task_type}"
        )
    except Exception as exc:  # noqa: BLE001
        # 日志失败不影响发布主流程
        logger.warning(f"[write_plan_execute_log] 写入计划执行日志失败: {exc}")


async def _resolve_task_operator(db: AsyncSession, task: PublishTask, processed_by: Optional[str]) -> str:
    """解析发布任务的操作账号。

    优先级：显式传入的 processed_by（页面当前账号，立即发布/修改计划升级执行时传入）
    > 任务创建者（task.created_by，定时任务到点执行时无请求上下文，回溯计划创建人）
    > 兜底 "system"。
    用于"发布任务执行成功"流程记录与 Ingest 状态同步的 Processed By 展示。
    """
    if processed_by:
        return processed_by
    if task.created_by:
        from sqlalchemy import select as _select
        from app.internal.cms_biz_system.models.user import User
        row = (await db.execute(
            _select(User.username).where(User.id == task.created_by)
        )).first()
        if row and row.username:
            return row.username
    return "system"


async def _execute_publish_task(db: AsyncSession, task: PublishTask, processed_by: Optional[str] = None) -> None:
    """
    执行发布/下架任务：生成 XML → 发送 SOAP → 创建注入历史 → 等待 LSP 回调

    流程：
        1. 判断 SOAP 是否启用，未启用则模拟成功
        2. 生成 C2 规范 Ingest XML 文件（TODO: 待对接 C2 规范）
        3. 创建 IngestHistory 记录
        4. 调用 SOAP ExecCmdReq 通知接口机
        5. 更新任务状态为 publishing（等待 LSP 回调更新最终状态）
    """
    logger.info(f"[_execute_publish_task] 开始执行 | task_id={task.id} | entity_id={task.entity_id} | task_type={task.task_type} | soap_enabled={soap_settings.enabled}")
    # 操作账号：显式传入（页面当前账号）优先，否则回溯任务创建者，兜底 system
    operator = await _resolve_task_operator(db, task, processed_by)
    # ── SOAP 未启用时走模拟逻辑 ──
    if not soap_settings.enabled:
        logger.warning(f"SOAP 未启用，模拟发布成功 - TaskID: {task.id}")
        # 即使 SOAP 未启用，也生成 XML 文件供调试查看
        xml_filepath = None
        try:
            xml_filepath, xml_url = await _generate_ingest_xml(db, task)
            logger.info(f"[模拟模式] 生成 Ingest XML - TaskID: {task.id}, Path: {xml_filepath}, URL: {xml_url}")
        except Exception as e:
            logger.error(f"[模拟模式] 生成 XML 失败 - TaskID: {task.id}, Error: {e}")

        # 创建 IngestHistory 记录（模拟模式下默认成功）
        correlate_id = uuid.uuid4().hex
        action = "DELETE" if task.task_type == "unpublish" else "REGIST"
        now = datetime.now(timezone.utc)
        logger.info(f"[模拟模式] 创建 IngestHistory - TaskID: {task.id}, xml_filepath={xml_filepath}")
        history = IngestHistory(
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            entity_name=task.entity_name,
            action=action,
            status="success",  # 模拟模式下直接标记为成功
            create_date=now,
            send_date=now,
            end_date=now,  # 模拟模式下立即完成，设置结束时间
            ingest_xml_path=xml_filepath,
            correlate_id=correlate_id,
        )
        await publish_repository.create_ingest_history(db, history)
        logger.info(f"[模拟模式] 创建 IngestHistory - TaskID: {task.id}, HistoryID: {history.id}, ingest_xml_path={history.ingest_xml_path}")

        task.status = "success"
        task.correlate_id = correlate_id
        task.ingest_xml_path = xml_filepath
        if task.task_type == "publish":
            task.publish_status = "success"
            task.publish_time = datetime.now(timezone.utc)
        else:
            task.publish_status = "closed"
            task.unpublish_time = datetime.now(timezone.utc)
        await publish_repository.update_publish_task(db, task)

        # 发布执行成功后补写 PublishPlan 流程记录（Passed）：
        # 创建计划（尤其是未来时间的计划）时流程记录为 Pending（计划只是未来安排，不算已处理），
        # 只有任务真正执行成功才补记 Passed（与 check_publish_plan 检查器口径一致）。
        # Processed By 显示操作账号（立即发布=页面当前账号，定时发布=计划创建者）
        if task.entity_type == "Content" and task.task_type == "publish" and task.content_type:
            await complete_process_and_update_status(
                db,
                content_id=task.entity_id,
                content_type=task.content_type,
                process_name="PublishPlan",
                processed_by=operator,
                info=f"发布任务执行成功: 任务#{task.id}",
                skip_status_update=True,
            )

        # 模拟模式下同步 content.status
        if task.entity_type == "Content":
            if task.task_type == "publish":
                logger.info(
                    f"[模拟模式-发布] 准备同步 content.status 为 Published | "
                    f"task_id={task.id}, content_id={task.entity_id}, "
                    f"entity_name={task.entity_name}"
                )
                await _sync_content_ingest_status(
                    db, task.entity_id, "Published", processed_by=operator
                )
                
                # 模拟模式下同步更新 object_publish_status
                try:
                    from app.soap.c2.loader import load_build_context
                    from app.internal.cms_biz_publish.services.object_publish_status_service import (
                        batch_mark_objects_from_context,
                    )
                    
                    ctx = await load_build_context(db, task.entity_id)
                    if ctx:
                        await batch_mark_objects_from_context(
                            db, task.entity_id, ctx, "REGIST",
                            ingest_history_id=history.id,
                        )
                        logger.info(
                            f"[模拟模式] 已更新 object_publish_status | content_id={task.entity_id}"
                        )
                    else:
                        logger.warning(
                            f"[模拟模式] BuildContext加载失败 | content_id={task.entity_id}"
                        )
                except Exception as e:
                    logger.error(f"[模拟模式] 更新 object_publish_status 失败 | content_id={task.entity_id} error={e}")
                    raise
                    
                # 发布成功，将 arrangement 任务标记为已完成
                try:
                    from app.internal.cms_biz_package.models.task import Task, TaskHistory
                    
                    arrangement_task = (
                        await db.execute(
                            select(Task).where(
                                Task.content_id == task.entity_id,
                                Task.task_type == "arrangement",
                                Task.is_deleted.is_(False),
                            )
                        )
                    ).scalar_one_or_none()
                    
                    if arrangement_task and arrangement_task.task_status != "Completed":
                        old_status = arrangement_task.task_status
                        arrangement_task.task_status = "Completed"
                        arrangement_task.end_time = datetime.now(timezone.utc)
                        db.add(
                            TaskHistory(
                                task_id=arrangement_task.id,
                                processed_type="Complete",
                                processed_by=operator,
                                previous_value=old_status,
                                updated_value="任务完成: 内容已发布",
                            )
                        )
                        logger.info(f"[模拟模式] 发布成功，arrangement任务标记为已完成 | content_id={task.entity_id}")
                except Exception as e:
                    logger.error(f"[模拟模式] 更新 arrangement 任务状态失败 | content_id={task.entity_id} error={e}")
                    
            elif task.task_type == "unpublish":
                await _sync_content_ingest_status(
                    db, task.entity_id, "Closed", processed_by=operator
                )
                
                # 模拟模式下同步更新 object_publish_status（下架）
                try:
                    status = await publish_repository.get_or_create_object_publish_status(
                        db, "Content", task.entity_id, task.entity_id
                    )
                    status.mark_as_unpublished()
                    await publish_repository.update_object_publish_status(db, status)
                    logger.info(
                        f"[模拟模式] 已更新 object_publish_status（下架） | content_id={task.entity_id}"
                    )
                except Exception as e:
                    logger.error(f"[模拟模式] 更新 object_publish_status（下架）失败 | content_id={task.entity_id} error={e}")
                
                # 下架成功，arrangement 任务恢复为待处理（PRD 3.7.2.4）
                try:
                    from app.internal.cms_biz_package.services import task_service
                    await task_service.reopen_arrangement_task(
                        db,
                        content_id=task.entity_id,
                        processed_by=operator,
                        reason="内容已下架",
                    )
                except Exception as e:
                    logger.error(f"[模拟模式] 恢复 arrangement 任务状态失败 | content_id={task.entity_id} error={e}")
        return

    try:
        # ── 1. 生成 C2 规范 XML 文件 ──
        # TODO: 待对接 C2 规范后实现，目前使用占位 XML
        xml_filepath, xml_url = await _generate_ingest_xml(db, task)
        logger.info(f"生成 Ingest XML - TaskID: {task.id}, Path: {xml_filepath}, URL: {xml_url}")

        # ── 2. 生成 CorrelateID ──
        correlate_id = uuid.uuid4().hex

        # ── 3. 创建 IngestHistory 记录 ──
        action = "DELETE" if task.task_type == "unpublish" else "REGIST"
        history = IngestHistory(
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            entity_name=task.entity_name,
            action=action,
            status="failure",  # 初始状态，LSP 回调后更新
            create_date=datetime.now(timezone.utc),
            send_date=datetime.now(timezone.utc),
            ingest_xml_path=xml_filepath,
            correlate_id=correlate_id,
        )
        await publish_repository.create_ingest_history(db, history)
        logger.info(
            f"创建 IngestHistory - TaskID: {task.id}, "
            f"HistoryID: {history.id}, CorrelateID: {correlate_id}"
        )

        # ── 4. 更新 PublishTask：写入 correlate_id 和 XML 路径 ──
        task.correlate_id = correlate_id
        task.ingest_xml_path = xml_filepath
        task.status = "processing"
        task.publish_status = "publishing" if task.task_type == "publish" else "closed"
        await publish_repository.update_publish_task(db, task)

        # 同步 content.status 为 Publishing（计划发布执行时）
        if task.entity_type == "Content" and task.task_type == "publish":
            await _sync_content_ingest_status(
                db, task.entity_id, "Publishing", processed_by=operator
            )

        # ── 5. 调用 SOAP 发送 ExecCmdReq ──
        soap_result = await _send_soap_exec_cmd(
            correlate_id=correlate_id,
            cmd_file_url=xml_url
        )

        # ── 记录 SOAP ExecCmdReq 返回结果到 IngestHistory ──
        history.soap_csp_id = soap_settings.csp_id
        history.soap_lsp_id = soap_settings.lsp_id
        history.soap_cmd_result = soap_result.get("result")
        history.soap_error_description = soap_result.get("error_description")

        if not soap_result["success"]:
            # SOAP 调用本身失败（网络异常等），任务标记为失败
            task.status = "failure"
            task.publish_status = "failure"
            task.error_message = f"SOAP 调用失败: {soap_result['error_description']}"
            task.retry_attempts = (task.retry_attempts or 0) + 1
            history.status = "failure"
            await publish_repository.update_publish_task(db, task)
            logger.error(
                f"SOAP 调用失败 - TaskID: {task.id}, "
                f"Error: {soap_result['error_description']}"
            )
            # 同步 content.status 为 PublishFailed
            if task.entity_type == "Content" and task.task_type == "publish":
                await _sync_content_ingest_status(
                    db, task.entity_id, "PublishFailed", processed_by=operator
                )
            await publish_repository.update_ingest_history(db, history)
            return

        # ── 6. SOAP 调用成功，等待 LSP 异步回调更新状态 ──
        logger.info(
            f"SOAP 调用成功，等待 LSP 回调 - TaskID: {task.id}, "
            f"CorrelateID: {correlate_id}"
        )
        # ExecCmdReq 成功，写入到 ingest_history
        await publish_repository.update_ingest_history(db, history)

    except Exception as e:
        logger.error(f"执行发布任务异常 - TaskID: {task.id}, Error: {e}")
        task.status = "failure"
        task.publish_status = "failure"
        task.error_message = str(e)
        task.retry_attempts = (task.retry_attempts or 0) + 1
        await publish_repository.update_publish_task(db, task)
        # 同步 content.status 为 PublishFailed
        if task.entity_type == "Content" and task.task_type == "publish":
            await _sync_content_ingest_status(
                db, task.entity_id, "PublishFailed", processed_by=operator
            )


async def _generate_ingest_xml(db: AsyncSession, task: PublishTask) -> tuple[str, str]:
    """
    生成 C2 规范的 Ingest XML 文件。

    仅支持 entity_type='Content' 的任务；其他类型暂降级为占位 XML。

    :param db: 当前事务会话，复用避免连接池耗尽
    :returns: (xml_filepath, xml_url) 本地文件绝对路径和 LSP 可访问的 URL
    """
    # 仅 Content 类型走 C2 规范完整流程
    if task.entity_type != "Content":
        logger.warning(f"entity_type={task.entity_type} 暂不支持 C2 规范，回退到占位 XML")
        return await _generate_placeholder_xml(task)

    from app.soap.c2 import ADIBuilder

    builder = ADIBuilder(db)
    if task.task_type == "publish":
        xml_str = await builder.build_publish_xml(content_id=task.entity_id)
    else:
        xml_str = await builder.build_unpublish_xml(content_id=task.entity_id)

    # SFTP 上传是同步阻塞 IO，放到线程池避免阻塞事件循环
    filepath = await asyncio.to_thread(
        builder.write_to_file,
        xml_str,
        correlate_id=task.correlate_id or uuid.uuid4().hex,
        entity_id=task.entity_id,
    )

    # 计算 CmdFileURL
    # 优先 SOAP_CMD_FILE_URL_PREFIX，其次从 SFTP 配置自动拼接，最后回退 HTTP
    from pathlib import Path
    from app.soap.config import soap_settings
    from app.config import settings

    if soap_settings.cmd_file_url_prefix:
        # 显式配置了前缀：直接拼接
        xml_url = f"{soap_settings.cmd_file_url_prefix.rstrip('/')}/{filepath}"
    elif settings.storage_type == "ftp" and settings.file_host:
        # 从 FTP 配置自动拼接：ftp://user:pass@host:port/base_path
        ftp_prefix = (
            f"ftp://{settings.file_username}:{settings.file_password}"
            f"@{settings.file_host}:{settings.file_port}"
            f"{settings.file_base_path.rstrip('/')}"
        )
        xml_url = f"{ftp_prefix}/{filepath}"
    elif settings.storage_type == "sftp" and settings.file_host:
        # 从 SFTP 配置自动拼接：sftp://user:pass@host:port/base_path
        sftp_prefix = (
            f"sftp://{settings.file_username}:{settings.file_password}"
            f"@{settings.file_host}:{settings.file_port}"
            f"{settings.file_base_path.rstrip('/')}"
        )
        xml_url = f"{sftp_prefix}/{filepath}"
    else:
        # HTTP 下载模式（兼容旧配置）
        filename = Path(filepath).name
        xml_url = f"{soap_settings.xml_base_url.rstrip('/')}/{filename}"
    return filepath, xml_url


async def _generate_placeholder_xml(task: PublishTask) -> tuple[str, str]:
    """非 Content 类型的降级占位 XML。"""
    from app.soap.xml_generator import XMLCommandGenerator

    xml_gen = XMLCommandGenerator()
    if task.task_type == "publish":
        xml_filepath = xml_gen.generate_content_publish_xml(
            content_id=str(task.entity_id),
            content_type=task.content_type or "unknown",
            title=task.entity_name or "",
            file_url="",
        )
    else:
        xml_filepath = xml_gen.generate_content_unpublish_xml(
            content_id=str(task.entity_id),
            content_type=task.content_type or "unknown",
            reason="unpublish",
        )
    xml_url = xml_gen.get_xml_url(xml_filepath)
    return xml_filepath, xml_url


async def _send_soap_exec_cmd(
    correlate_id: str,
    cmd_file_url: str
) -> dict:
    """
    调用 SOAP ExecCmdReq 发送指令

    Returns:
        {"success": bool, "correlate_id": str, "result": int, "error_description": str}
    """
    try:
        from app.soap.client import SOAPClient

        soap_client = SOAPClient()
        return soap_client.send_exec_cmd_req(
            cmd_file_url=cmd_file_url,
            correlate_id=correlate_id
        )
    except Exception as e:
        logger.error(f"SOAP 客户端初始化/调用失败: {e}")
        return {
            "success": False,
            "correlate_id": correlate_id,
            "result": -1,
            "error_description": str(e)
        }


# 内容详情入口级联发布时允许的子内容状态白名单
# ReadyForPublish：具备发布条件；PublishFailed：发布失败可重试；Published：已发布随父级再次发布
DETAIL_CASCADE_CHILD_STATUSES = ("ReadyForPublish", "PublishFailed", "Published")


async def _cascade_publish_children(
    db: AsyncSession,
    parent_id: int,
    user_id: Optional[int] = None,
    ignore_child_status: bool = False,
    processed_by: Optional[str] = None,
    _visited: Optional[set[int]] = None,
    _depth: int = 0,
) -> None:
    """
    级联发布子内容（立即发布场景）。
    
    层级关系：
        SEASON (总季) → SERIES (单季) → EPISODE (单集)

    发布顺序：先发布父级，再发布子级（Season → Series → Episode），
    确保 LSP 侧先收到父对象再收到子对象。

    业务规则：
        - 发布管理入口（ignore_child_status=True）：不管子内容什么状态，
          只要子内容在发布任务表里有记录，都随父级再次发布
        - 内容详情入口（ignore_child_status=False）：仅级联状态在
          DETAIL_CASCADE_CHILD_STATUSES 白名单内且任务未取消的子内容
        - 若子内容尚无发布任务记录，跳过该子内容及其子树（不创建新任务）

    防死循环（硬性防护，任何场景生效）：
        - _visited：访问路径集合，子内容已在路径中说明出现环，立即终止
        - _depth：递归深度上限，超过强制终止
    """
    # ── 硬性防护1：深度上限 ──
    if _depth > _MAX_PUBLISH_RECURSION_DEPTH:
        logger.error(
            f"[级联发布] 递归深度超上限 {_MAX_PUBLISH_RECURSION_DEPTH}，"
            f"疑似环状脏数据，强制终止 | parent_id={parent_id} | visited={_visited}"
        )
        return

    # ── 硬性防护2：访问路径去重（防 parent_id 成环） ──
    if _visited is None:
        _visited = set()
    if parent_id in _visited:
        logger.error(
            f"[级联发布] 内容 #{parent_id} 已在访问路径中，检测到环状引用，强制终止"
        )
        return
    _visited.add(parent_id)

    children = await publish_repository.get_child_contents(db, parent_id)

    for child in children:
        # 检查子内容是否在发布任务表里有记录，没有则跳过整棵子树（不创建新任务）
        existing_task = await publish_repository.get_entity_current_publish_status(
            db, "Content", child.id
        )
        if not existing_task:
            logger.info(
                f"[级联发布] 子内容 #{child.id} ({child.content_type}) 尚无发布任务，跳过"
            )
            continue

        # 内容详情入口：仅级联状态在白名单内且任务未取消的子内容
        if not ignore_child_status:
            if child.status not in DETAIL_CASCADE_CHILD_STATUSES:
                logger.info(
                    f"[级联发布] 子内容 #{child.id} ({child.content_type}) 状态为 {child.status}，跳过"
                )
                continue
            if existing_task.status == "cancelled":
                logger.info(
                    f"[级联发布] 子内容 #{child.id} ({child.content_type}) 任务已取消，跳过"
                )
                continue

        # 先发布当前子内容，再递归发布其子内容
        # 保证 Season → Series → Episode 的顺序
        data = PublishPlanCreate(
            entity_type="Content",
            entity_id=child.id,
            entity_name=child.title,
            content_type=child.content_type,
            task_type="publish",
            execution_mode="now",
            cascade_ignore_status=ignore_child_status,
        )
        try:
            await create_publish_plan(db, data, user_id, processed_by=processed_by, _skip_parent_republish=True)
        except BusinessException:
            pass

        # 递归发布子内容的子内容（传递访问路径与深度，防环状脏数据死循环）
        await _cascade_publish_children(
            db, child.id, user_id, ignore_child_status, processed_by=processed_by,
            _visited=_visited, _depth=_depth + 1,
        )


# 递归深度硬上限：正常层级最多3层（EPISODE→SERIES→SEASON），
# 超过视为数据异常（环状脏数据或逻辑错误），强制终止防止死循环
_MAX_PUBLISH_RECURSION_DEPTH = 10


async def _republish_published_parents(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    user_id: Optional[int] = None,
    processed_by: Optional[str] = None,
    _visited: Optional[set[int]] = None,
    _depth: int = 0,
) -> None:
    """
    发布子内容后，如果父内容已经发布过，则重新发布父内容。

    层级关系：
        EPISODE → 父级 SERIES/SEASON_SERIES
        SEASON_SERIES → 父级 SEASON
        SCHEDULE → 父级 CHANNEL

    业务规则：
        - 仅对 publish 任务触发
        - 仅当父内容的 ObjectPublishStatus.is_published == True 时才重新发布
        - 递归向上：父内容重新发布后，祖父内容也需检查

    防死循环（硬性防护，任何场景生效）：
        - _visited：访问路径集合，父级已在路径中说明出现环，立即终止
        - _depth：递归深度上限，超过强制终止
    """
    # ── 硬性防护1：深度上限 ──
    if _depth > _MAX_PUBLISH_RECURSION_DEPTH:
        logger.error(
            f"[父级重新发布] 递归深度超上限 {_MAX_PUBLISH_RECURSION_DEPTH}，"
            f"疑似环状脏数据，强制终止 | content_id={content_id} | visited={_visited}"
        )
        return

    # ── 硬性防护2：访问路径去重（防 parent_id 成环） ──
    if _visited is None:
        _visited = set()
    if content_id in _visited:
        logger.error(
            f"[父级重新发布] 内容 #{content_id} 已在访问路径中，检测到环状引用，强制终止"
        )
        return
    _visited.add(content_id)

    _CHILD_PARENT_MAP = {
        "EPISODE": ("SERIES", "SEASON_SERIES"),
        "SEASON_SERIES": ("SEASON",),
        "SCHEDULE": ("CHANNEL",),
    }

    valid_parent_types = _CHILD_PARENT_MAP.get(content_type)
    if not valid_parent_types:
        logger.info(
            f"[父级重新发布] 内容 #{content_id} ({content_type}) 类型无父级映射，跳过"
        )
        return

    content = await publish_repository.get_content_by_id(db, content_id)
    if not content or not content.parent_id:
        logger.info(
            f"[父级重新发布] 内容 #{content_id} ({content_type}) 无父级，跳过"
        )
        return

    parent = await publish_repository.get_content_by_id(db, content.parent_id)
    if not parent or parent.content_type not in valid_parent_types:
        logger.info(
            f"[父级重新发布] 内容 #{content_id} ({content_type}) 父级类型不匹配"
            f"（parent_type={parent.content_type if parent else None}, "
            f"要求={valid_parent_types}），跳过"
        )
        return

    # 检查父内容是否已发布过（ObjectPublishStatus）
    parent_publish_status = await publish_repository.get_object_publish_status(
        db, "Content", parent.id
    )
    if not parent_publish_status or not parent_publish_status.is_published:
        logger.info(
            f"[父级重新发布] 父内容 #{parent.id} ({parent.content_type}) 未发布过，"
            f"无需重新发布（is_published={parent_publish_status.is_published if parent_publish_status else None}）"
        )
        return

    # 检查父内容是否正在发布中，避免并发重复发布导致死锁
    parent_task = await publish_repository.get_entity_current_publish_status(
        db, "Content", parent.id
    )
    if parent_task and parent_task.publish_status == "publishing":
        logger.info(
            f"[父级重新发布] 父内容 #{parent.id} 正在发布中，跳过"
        )
        return

    logger.info(
        f"[父级重新发布] 子内容 #{content_id} ({content_type}) 发布后，"
        f"父内容 #{parent.id} ({parent.content_type}) 已发布过，重新发布"
    )

    # 重新发布父内容（立即执行，忽略子内容状态）
    data = PublishPlanCreate(
        entity_type="Content",
        entity_id=parent.id,
        entity_name=parent.title,
        content_type=parent.content_type,
        task_type="publish",
        execution_mode="now",
        cascade_ignore_status=True,
    )
    try:
        await create_publish_plan(
            db, data, user_id, processed_by=processed_by,
            _skip_parent_republish=True,
            _skip_cascade_children=True,
        )
        logger.info(f"[父级重新发布] 父内容 #{parent.id} 重新发布完成")
    except BusinessException as e:
        logger.warning(f"[父级重新发布] 父内容 #{parent.id} 重新发布失败: {e.message}")

    # 递归向上检查祖父内容（传递访问路径与深度，防环状脏数据死循环）
    await _republish_published_parents(
        db, parent.id, parent.content_type, user_id, processed_by=processed_by,
        _visited=_visited, _depth=_depth + 1,
    )


async def _cascade_unpublish_children(
    db: AsyncSession,
    parent_id: int,
    user_id: Optional[int] = None
) -> None:
    """
    级联下架子内容（立即下架场景）。

    层级关系：
        SEASON (总季) → SERIES (单季) → EPISODE (单集)
        CHANNEL (频道) → SCHEDULE (节目单)

    业务规则：
        - SEASON/SERIES 下架时，不级联下架其子内容
        - CHANNEL 下架时，不级联下架其节目单（Schedule）
        - 子内容（SERIES/EPISODE/SCHEDULE）需要单独下架
    """
    # 根据业务需求，下架时不级联下架子内容
    # 子内容（SERIES/EPISODE/SCHEDULE）需要单独手动下架
    pass


async def _cascade_create_plan_for_children(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    task_type: str,
    execution_mode: str,
    scheduled_time: Optional[datetime] = None,
    user_id: Optional[int] = None,
    ignore_child_status: bool = False,
    processed_by: Optional[str] = None,
    _visited: Optional[set[int]] = None,
    _depth: int = 0,
) -> None:
    """
    级联更新子内容的发布/下架计划（仅更新，不创建）。

    层级关系：
        SEASON (总季, series_type=3) → SERIES (单季, series_type=2) → EPISODE (单集)
        CHANNEL (频道) → SCHEDULE (节目单)

    业务规则：
        - 发布管理入口（ignore_child_status=True）：不管子内容什么状态，
          只要子内容在发布任务表里有记录，都随父级再次发布
        - 内容详情入口（ignore_child_status=False）：仅级联状态在
          DETAIL_CASCADE_CHILD_STATUSES 白名单内且任务未取消的子内容
        - 若子内容尚无发布任务记录，跳过该子内容及其子树（不创建新任务）
        - 下架计划：不级联处理子内容，子内容需要单独下架
        - CHANNEL 的节目单（Schedule）也遵循上述规则

    防死循环（硬性防护，任何场景生效）：
        - _visited：访问路径集合，子内容已在路径中说明出现环，立即终止
        - _depth：递归深度上限，超过强制终止
    """
    # ── 硬性防护1：深度上限 ──
    if _depth > _MAX_PUBLISH_RECURSION_DEPTH:
        logger.error(
            f"[级联计划] 递归深度超上限 {_MAX_PUBLISH_RECURSION_DEPTH}，"
            f"疑似环状脏数据，强制终止 | content_id={content_id} | visited={_visited}"
        )
        return

    # ── 硬性防护2：访问路径去重（防 parent_id 成环，如 A.parent=B 且 B.parent=A） ──
    if _visited is None:
        _visited = set()
    if content_id in _visited:
        logger.error(
            f"[级联计划] 内容 #{content_id} 已在访问路径中，检测到环状引用，强制终止"
        )
        return
    _visited.add(content_id)

    # 只有 SEASON/SERIES/CHANNEL 才有子内容
    # CHANNEL 的子内容（SCHEDULE）在此处同样不级联处理
    if content_type not in ("SEASON", "SEASON_SERIES", "SERIES"):
        return

    # 下架计划：不级联创建子内容的下架计划
    if task_type == "unpublish":
        logger.info(
            f"[级联计划] 父内容 #{content_id} ({content_type}) 下架计划不级联子内容"
        )
        return

    children = await publish_repository.get_child_contents(db, content_id)

    if not children:
        return

    logger.info(
        f"[级联计划] 父内容 #{content_id} ({content_type}) | "
        f"task_type={task_type} | execution_mode={execution_mode} | "
        f"子内容数量={len(children)}"
    )

    for child in children:
        # 检查子内容是否在发布任务表里有记录，没有则跳过整棵子树（不创建新任务）
        existing_task = await publish_repository.get_entity_current_publish_status(
            db, "Content", child.id
        )
        if not existing_task:
            logger.info(
                f"[级联计划] 子内容 #{child.id} ({child.content_type}) 尚无发布任务，跳过"
            )
            continue

        # 内容详情入口：仅级联状态在白名单内且任务未取消的子内容
        if not ignore_child_status:
            if child.status not in DETAIL_CASCADE_CHILD_STATUSES:
                logger.info(
                    f"[级联计划] 子内容 #{child.id} ({child.content_type}) 状态为 {child.status}，跳过"
                )
                continue
            if existing_task.status == "cancelled":
                logger.info(
                    f"[级联计划] 子内容 #{child.id} ({child.content_type}) 任务已取消，跳过"
                )
                continue

        # 先更新当前子内容的发布任务，再递归处理其子内容
        # 保证 Season → Series → Episode 的顺序
        # 定时发布时，子内容的 scheduled_time 比父内容晚 1 秒，
        # 确保定时扫描器先执行父内容再执行子内容
        child_scheduled_time = scheduled_time
        if execution_mode == "plan" and scheduled_time is not None:
            from datetime import timedelta
            child_scheduled_time = scheduled_time + timedelta(seconds=1)
        data = PublishPlanCreate(
            entity_type="Content",
            entity_id=child.id,
            entity_name=child.title,
            content_type=child.content_type,
            task_type=task_type,
            execution_mode=execution_mode,
            scheduled_time=child_scheduled_time,
            cascade_ignore_status=ignore_child_status,
        )
        try:
            await create_publish_plan(db, data, user_id, processed_by=processed_by, _skip_parent_republish=True)
            logger.info(
                f"[级联计划] 为子内容 #{child.id} ({child.content_type}) 更新{task_type}计划 | "
                f"execution_mode={execution_mode}"
            )
        except BusinessException as e:
            logger.warning(
                f"[级联计划] 为子内容 #{child.id} 更新计划失败: {e.message}"
            )

        # 注意：此处不再显式递归处理子内容的子内容。
        # create_publish_plan 内部（级联创建子内容的发布/下架计划分支）已会对
        # 每个子内容递归级联其下级，若此处再次递归会导致孙级内容被发布两次。
        # 发布顺序 Season → Series → Episode 由 create_publish_plan 内部级联保证；
        # 定时发布时子级 scheduled_time 逐级 +1 秒同样由内部级联传递。


async def _validate_parent_published(
    db: AsyncSession,
    content_type: str,
    content_id: int,
) -> None:
    """
    发布子内容时，校验父内容必须已发布。

    规则：
        EPISODE → 父级 SERIES/SEASON_SERIES 必须已发布或正在发布中
        SEASON_SERIES → 父级 SEASON 必须已发布或正在发布中
        SCHEDULE → 父级 CHANNEL 必须已发布或正在发布中

    注意：此处校验的是 ObjectPublishStatus.is_published（真正的发布状态），
    而不是 Content.status（内容生命周期状态），避免已发布但许可证过期
    （NoActiveLicense）的父级被误判为未发布。
    父级正在发布中（publishing）也视为通过，避免父级发布任务尚未完成时
    子级无法发布的问题。
    """
    _CHILD_PARENT_MAP = {
        "EPISODE": ("SERIES", "SEASON_SERIES"),
        "SEASON_SERIES": ("SEASON",),
        "SCHEDULE": ("CHANNEL",),
    }

    valid_parent_types = _CHILD_PARENT_MAP.get(content_type)
    if not valid_parent_types:
        logger.info(
            f"[_validate_parent_published] 跳过校验 | content_type={content_type} 不在子父映射中 | content_id={content_id}"
        )
        return

    content = await publish_repository.get_content_by_id(db, content_id)
    if not content or not content.parent_id:
        logger.info(
            f"[_validate_parent_published] 跳过校验 | content_id={content_id} | "
            f"content存在={content is not None} | parent_id={content.parent_id if content else None}"
        )
        return

    parent = await publish_repository.get_content_by_id(db, content.parent_id)
    if not parent:
        logger.info(
            f"[_validate_parent_published] 跳过校验 | content_id={content_id} | "
            f"parent_id={content.parent_id} 父内容不存在"
        )
        return

    logger.info(
        f"[_validate_parent_published] 校验开始 | content_id={content_id} | content_type={content_type} | "
        f"parent_id={parent.id} | parent.content_type={parent.content_type} | "
        f"parent.title={parent.title} | valid_parent_types={valid_parent_types}"
    )

    if parent.content_type not in valid_parent_types:
        logger.info(
            f"[_validate_parent_published] 跳过校验 | parent.content_type={parent.content_type} "
            f"不在 {valid_parent_types} 中 | content_id={content_id} | parent_id={parent.id}"
        )
        return

    # 校验父级真正的发布状态，而非内容生命周期状态
    publish_status = await publish_repository.get_object_publish_status(
        db, "Content", parent.id
    )
    logger.info(
        f"[_validate_parent_published] ObjectPublishStatus查询结果 | parent_id={parent.id} | "
        f"publish_status存在={publish_status is not None} | "
        f"is_published={publish_status.is_published if publish_status else None} | "
        f"full_record={publish_status.__dict__ if publish_status else None}"
    )
    if publish_status and publish_status.is_published:
        logger.info(
            f"[_validate_parent_published] 校验通过（父级已发布）| parent_id={parent.id} | content_id={content_id}"
        )
        return

    # 父级正在发布中也视为通过（publishing 状态）
    parent_task = await publish_repository.get_entity_current_publish_status(
        db, "Content", parent.id
    )
    logger.info(
        f"[_validate_parent_published] 当前发布任务查询结果 | parent_id={parent.id} | "
        f"task存在={parent_task is not None} | "
        f"publish_status={parent_task.publish_status if parent_task else None} | "
        f"task_id={parent_task.id if parent_task else None}"
    )
    if parent_task and parent_task.publish_status == "publishing":
        logger.info(
            f"[_validate_parent_published] 校验通过（父级正在发布中）| parent_id={parent.id} | content_id={content_id}"
        )
        return

    logger.warning(
        f"[_validate_parent_published] 校验失败，准备抛异常 | content_id={content_id} | content_type={content_type} | "
        f"parent_id={parent.id} | parent.title={parent.title} | "
        f"ObjectPublishStatus.is_published={publish_status.is_published if publish_status else None} | "
        f"parent_task.publish_status={parent_task.publish_status if parent_task else None}"
    )

    if content_type == "SCHEDULE":
        raise BusinessException(
            ErrorCode.CHANNEL_NOT_PUBLISHED,
            get_msg("CHANNEL_NOT_PUBLISHED", channel_name=parent.title or str(parent.id)),
        )
    else:
        raise BusinessException(
            ErrorCode.PARENT_NOT_PUBLISHED,
            get_msg("PARENT_NOT_PUBLISHED", name=parent.title or str(parent.id)),
        )


async def _sync_content_ingest_status(
    db: AsyncSession,
    content_id: int,
    new_status: str,
    processed_by: Optional[str] = None,
) -> None:
    """
    同步内容的 Ingest 状态（content.status）。

    用于发布/下架流程中保持 publish_task.publish_status 与 content.status 一致。
    """
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    from app.internal.cms_biz_orchestration.services.workflow_service import record_status_change

    logger.info(f"[_sync_content_ingest_status] 开始同步 | content_id={content_id} | 目标状态={new_status}")
    content = await get_content_by_id(db, content_id)
    if not content:
        logger.warning(f"[同步Ingest状态] 内容不存在: content_id={content_id}, 可能已被删除或标记为is_discarded")
        # 尝试查询不考虑is_discarded的内容,用于诊断
        from app.internal.cms_biz_package.models.package import Content
        from sqlalchemy import select
        content_with_discarded = (
            await db.execute(
                select(Content).where(
                    Content.id == content_id,
                    Content.is_deleted.is_(False),
                )
            )
        ).scalar_one_or_none()
        if content_with_discarded:
            logger.warning(
                f"[同步Ingest状态] 诊断: content_id={content_id} 存在但 is_discarded={content_with_discarded.is_discarded}, "
                f"status={content_with_discarded.status}"
            )
        else:
            logger.error(f"[同步Ingest状态] 严重: content_id={content_id} 在数据库中完全不存在（包括is_discarded）")
        return

    logger.info(
        f"[同步Ingest状态] content_id={content_id}, title={content.title}, "
        f"当前状态={content.status}, 目标状态={new_status}, "
        f"is_discarded={content.is_discarded}"
    )

    if content.status == new_status:
        logger.info(f"[同步Ingest状态] content_id={content_id} 状态已是 {new_status},跳过更新")
        return

    old_status = content.status
    content.status = new_status
    logger.info(f"[同步Ingest状态] 已设置 content.status = {new_status} (content_id={content_id})")

    if new_status != "Closed" and hasattr(content, 'previous_status') and content.previous_status is not None:
        content.previous_status = None

    await record_status_change(
        db,
        content_id=content_id,
        before_status=old_status,
        after_status=new_status,
        processed_by=processed_by or "system",
    )
    logger.info(
        "[_sync_content_ingest_status] 同步完成 | content_id={} {} → {}",
        content_id, old_status, new_status,
    )
