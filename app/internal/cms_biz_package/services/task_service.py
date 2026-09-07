"""
任务管理 - 业务逻辑层。

职责：
- 任务的查询、分配、批量分配
- 任务操作历史记录
- 内容创建时自动创建 arrangement 任务
- 分配任务时自动添加数据权限
"""

from datetime import datetime

from loguru import logger
from sqlalchemy import select, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_package.models.task import Task, TaskHistory
from app.internal.cms_biz_package.models.package import Content
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.models.content_auth import ContentAuth
from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
from app.internal.cms_biz_package.schemas.task import (
    TaskAssignRequest,
    TaskDetail,
    TaskHistoryItem,
    TaskListItem,
)
from app.internal.cms_biz_package.repositories import task_repo
from app.common.schemas import PaginatedResponse
from app.common.core.i18n import get_msg
from app.common.core.exceptions import NotFoundException, BusinessException, ErrorCode


# ─── 内部辅助 ─────────────────────────────────────────────────────────

async def _get_task_or_404(db: AsyncSession, task_id: int) -> Task:
    """查询任务，不存在则抛 404。"""
    task = await task_repo.get_task_by_id(db, task_id)
    if not task:
        raise NotFoundException(ErrorCode.TASK_NOT_FOUND, get_msg("TASK_NOT_FOUND"))
    return task


async def _get_user_name(db: AsyncSession, user_id: int | None) -> str | None:
    """查询用户显示名称：姓名（账号）。"""
    if user_id is None:
        return None
    user = (
        await db.execute(
            select(User.display_name, User.username).where(User.id == user_id)
        )
    ).one_or_none()
    if user:
        name = user.display_name or user.username
        return f"{name}（{user.username}）"
    return None


async def _ensure_content_auth(db: AsyncSession, content_id: int, user_id: int) -> None:
    """确保指定内容已授权给指定用户（如未授权则添加）。"""
    existing = (
        await db.execute(
            select(ContentAuth).where(
                ContentAuth.content_id == content_id,
                ContentAuth.user_id == user_id,
                ContentAuth.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if not existing:
        db.add(
            ContentAuth(
                content_id=content_id,
                user_id=user_id,
                role_id=None,
                is_deleted=False,
            )
        )
        await db.flush()
        logger.info(
            "自动添加数据权限 | content_id={} user_id={}", content_id, user_id
        )


async def _revoke_content_auth_for_user(
    db: AsyncSession, content_id: int, user_id: int
) -> None:
    """删除指定内容对指定用户的授权记录（仅删除任务指派自动添加的用户授权，不影响角色授权）。"""
    await db.execute(
        delete(ContentAuth).where(
            ContentAuth.content_id == content_id,
            ContentAuth.user_id == user_id,
            ContentAuth.role_id.is_(None),
            ContentAuth.is_deleted.is_(False),
        )
    )
    await db.flush()
    logger.info(
        "删除任务指派产生的数据权限 | content_id={} user_id={}", content_id, user_id
    )


async def _update_content_process_assigned(
    db: AsyncSession,
    content_id: int,
    task_type: str,
    assignee_name: str | None,
) -> None:
    """同步更新 content_process 表的 assigned 字段。

    当审批任务（review L1/L2/L3）的负责人变更时，同步更新流程记录中的处理人。
    """
    # 查询对应的流程记录（Pending 状态的 ContentReview）
    process = (
        await db.execute(
            select(ContentProcess).where(
                ContentProcess.content_id == content_id,
                ContentProcess.node_code == "ContentReview",
                ContentProcess.status == "Pending",
                ContentProcess.is_deleted.is_(False), ContentProcess.is_discarded.is_(False),
            )
        )
    ).scalar_one_or_none()

    if process:
        process.assigned = assignee_name
        # 更新 info 字段，记录审批级别
        level = task_type.replace("review ", "")
        process.info = f"{level}审批: {assignee_name or '未分配'}"
        await db.flush()
        logger.info(
            "同步更新流程记录处理人 | content_id={} task_type={} assigned={}",
            content_id, task_type, assignee_name
        )


def _build_list_item(
    task: Task, content: Content, user: User | None, has_children: bool = False
) -> TaskListItem:
    """将 ORM 对象转换为列表项 Schema。"""
    assignee_name = None
    if user:
        name = user.display_name or user.username
        assignee_name = f"{name}（{user.username}）"
    return TaskListItem(
        id=task.id,
        content_id=task.content_id,
        content_name=content.title,
        content_type=content.content_type,
        ingest_status=content.status,
        task_type=task.task_type,
        assignee_id=task.assignee_id,
        assignee_name=assignee_name,
        task_status=task.task_status,
        start_time=task.start_time,
        end_time=task.end_time,
        created_at=task.created_at,
        has_children=has_children,
    )


def _build_detail_item(
    task: Task, content: Content, user: User | None, has_children: bool = False
) -> TaskDetail:
    """将 ORM 对象转换为详情 Schema。"""
    assignee_name = None
    if user:
        name = user.display_name or user.username
        assignee_name = f"{name}（{user.username}）"
    return TaskDetail(
        id=task.id,
        content_id=task.content_id,
        content_name=content.title,
        content_type=content.content_type,
        task_type=task.task_type,
        assignee_id=task.assignee_id,
        assignee_name=assignee_name,
        task_status=task.task_status,
        start_time=task.start_time,
        end_time=task.end_time,
        created_at=task.created_at,
        updated_at=task.updated_at,
        has_children=has_children,
    )


# ─── 任务查询 ─────────────────────────────────────────────────────────

async def list_tasks(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    task_types: list[str] | None = None,
    task_statuses: list[str] | None = None,
    assignee_keyword: str | None = None,
    assignee_id: int | None = None,
    assignee_is_null: bool = False,
    content_name: str | None = None,
    content_types: list[str] | None = None,
    content_id: int | None = None,
    time_start: datetime | None = None,
    time_end: datetime | None = None,
    end_time_start: datetime | None = None,
    end_time_end: datetime | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    sort_mode: str | None = None,
) -> PaginatedResponse[TaskListItem]:
    """查询任务列表（分页 + 过滤）。"""
    total = await task_repo.count_tasks(
        db,
        task_types=task_types,
        task_statuses=task_statuses,
        assignee_keyword=assignee_keyword,
        assignee_id=assignee_id,
        assignee_is_null=assignee_is_null,
        content_name=content_name,
        content_types=content_types,
        content_id=content_id,
        time_start=time_start,
        time_end=time_end,
        end_time_start=end_time_start,
        end_time_end=end_time_end,
    )

    query = await task_repo.list_tasks_query(
        db,
        task_types=task_types,
        task_statuses=task_statuses,
        assignee_keyword=assignee_keyword,
        assignee_id=assignee_id,
        assignee_is_null=assignee_is_null,
        content_name=content_name,
        content_types=content_types,
        content_id=content_id,
        time_start=time_start,
        time_end=time_end,
        end_time_start=end_time_start,
        end_time_end=end_time_end,
        sort_by=sort_by,
        sort_order=sort_order,
        sort_mode=sort_mode,
    )

    results = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).all()

    items = [
        _build_list_item(task, content, user, bool(has_children))
        for task, content, user, has_children in results
    ]

    return PaginatedResponse(items=items, total=total, page=page, page_size=page_size)


async def get_task_detail(db: AsyncSession, task_id: int) -> TaskDetail:
    """查询任务详情（含操作历史）。"""
    task = await _get_task_or_404(db, task_id)

    content = (
        await db.execute(select(Content).where(Content.id == task.content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False)))
    ).scalar_one_or_none()

    user = None
    if task.assignee_id:
        user = (
            await db.execute(select(User).where(User.id == task.assignee_id, User.is_deleted.is_(False)))
        ).scalar_one_or_none()

    has_children = bool(await task_repo.get_child_content_ids(db, task.content_id))

    return _build_detail_item(task, content, user, has_children)


async def list_task_history(db: AsyncSession, task_id: int) -> list[TaskHistoryItem]:
    """查询任务操作历史。"""
    histories = await task_repo.list_task_history_by_task_id(db, task_id)
    return [TaskHistoryItem.model_validate(h) for h in histories]


# ─── 任务分配 ─────────────────────────────────────────────────────────

async def assign_task(
    db: AsyncSession,
    task_id: int,
    data: TaskAssignRequest,
    processed_by: str | None = None,
) -> TaskDetail:
    """分配任务负责人。

    规则：
    1. 已完成的任务不能修改负责人。
    2. 更新负责人后状态自动变为 Pending。
    3. 自动给负责人添加该内容的数据权限。
    4. update_childs=true 时同步更新子内容任务。
    """
    task = await _get_task_or_404(db, task_id)

    if task.task_status == "Completed":
        raise BusinessException(ErrorCode.TASK_COMPLETED_CANNOT_MODIFY, get_msg("TASK_COMPLETED_CANNOT_MODIFY"))

    # 记录旧值
    old_assignee_id = task.assignee_id
    old_assignee_name = await _get_user_name(db, task.assignee_id)
    new_assignee_name = await _get_user_name(db, data.assignee_id)

    # 更新任务
    task.assignee_id = data.assignee_id
    if task.task_status == "Not Assigned":
        task.task_status = "Pending"
    await db.flush()

    # 记录操作历史
    history = TaskHistory(
        task_id=task.id,
        processed_type="Assign",
        processed_by=processed_by,
        previous_value=f"Assignee: {old_assignee_name or ''}",
        updated_value=f"Assignee: {new_assignee_name or ''}",
    )
    await task_repo.add_task_history(db, history)

    # 自动添加数据权限
    await _ensure_content_auth(db, task.content_id, data.assignee_id)
    # 移除旧负责人的任务指派授权（仅限 role_id 为空的用户授权，不影响角色授权）
    if old_assignee_id is not None and old_assignee_id != data.assignee_id:
        await _revoke_content_auth_for_user(db, task.content_id, old_assignee_id)

    # 同步更新 content_process 表的 assigned 字段（审批任务）
    if task.task_type in ["review L1", "review L2", "review L3"]:
        await _update_content_process_assigned(
            db, task.content_id, task.task_type, new_assignee_name
        )

    # 更新子内容任务
    if data.update_childs:
        child_ids = await task_repo.get_child_content_ids(db, task.content_id)
        if child_ids:
            child_tasks = await task_repo.get_tasks_by_content_ids_and_type(
                db, child_ids, task.task_type
            )
            for child_task in child_tasks:
                if child_task.task_status != "Completed":
                    child_old_assignee_id = child_task.assignee_id
                    child_old = await _get_user_name(db, child_task.assignee_id)
                    child_task.assignee_id = data.assignee_id
                    if child_task.task_status == "Not Assigned":
                        child_task.task_status = "Pending"
                    await db.flush()
                    await task_repo.add_task_history(
                        db,
                        TaskHistory(
                            task_id=child_task.id,
                            processed_type="Assign",
                            processed_by=processed_by,
                            previous_value=f"Assignee: {child_old or ''}",
                            updated_value=f"Assignee: {new_assignee_name or ''}",
                        ),
                    )
                    await _ensure_content_auth(db, child_task.content_id, data.assignee_id)
                    if child_old_assignee_id is not None and child_old_assignee_id != data.assignee_id:
                        await _revoke_content_auth_for_user(db, child_task.content_id, child_old_assignee_id)
            logger.info(
                "同步更新子内容任务 | parent_task={} child_count={}",
                task_id, len(child_tasks),
            )

    logger.info(
        "分配任务 | task_id={} assignee_id={} by={}",
        task_id, data.assignee_id, processed_by,
    )

    return await get_task_detail(db, task_id)


async def batch_assign_tasks(
    db: AsyncSession,
    task_ids: list[int],
    assignee_id: int,
    update_childs: bool = False,
    processed_by: str | None = None,
) -> int:
    """批量分配任务，返回成功分配数量（自动跳过已完成任务）。"""
    tasks = await task_repo.get_tasks_by_ids(db, task_ids)
    assigned_count = 0

    for task in tasks:
        if task.task_status == "Completed":
            continue

        old_assignee_id = task.assignee_id
        old_assignee_name = await _get_user_name(db, task.assignee_id)
        new_assignee_name = await _get_user_name(db, assignee_id)

        task.assignee_id = assignee_id
        if task.task_status == "Not Assigned":
            task.task_status = "Pending"
        await db.flush()

        await task_repo.add_task_history(
            db,
            TaskHistory(
                task_id=task.id,
                processed_type="Assign",
                processed_by=processed_by,
                previous_value=f"Assignee: {old_assignee_name or ''}",
                updated_value=f"Assignee: {new_assignee_name or ''}",
            ),
        )
        await _ensure_content_auth(db, task.content_id, assignee_id)
        # 移除旧负责人的任务指派授权（仅限 role_id 为空的用户授权，不影响角色授权）
        if old_assignee_id is not None and old_assignee_id != assignee_id:
            await _revoke_content_auth_for_user(db, task.content_id, old_assignee_id)

        # 同步更新 content_process 表的 assigned 字段（审批任务）
        if task.task_type in ["review L1", "review L2", "review L3"]:
            await _update_content_process_assigned(
                db, task.content_id, task.task_type, new_assignee_name
            )

        if update_childs:
            child_ids = await task_repo.get_child_content_ids(db, task.content_id)
            if child_ids:
                child_tasks = await task_repo.get_tasks_by_content_ids_and_type(
                    db, child_ids, task.task_type
                )
                for child_task in child_tasks:
                    if child_task.task_status != "Completed":
                        child_old_assignee_id = child_task.assignee_id
                        child_old = await _get_user_name(db, child_task.assignee_id)
                        child_task.assignee_id = assignee_id
                        if child_task.task_status == "Not Assigned":
                            child_task.task_status = "Pending"
                        await db.flush()
                        await task_repo.add_task_history(
                            db,
                            TaskHistory(
                                task_id=child_task.id,
                                processed_type="Assign",
                                processed_by=processed_by,
                                previous_value=f"Assignee: {child_old or ''}",
                                updated_value=f"Assignee: {new_assignee_name or ''}",
                            ),
                        )
                        await _ensure_content_auth(db, child_task.content_id, assignee_id)
                        if child_old_assignee_id is not None and child_old_assignee_id != assignee_id:
                            await _revoke_content_auth_for_user(db, child_task.content_id, child_old_assignee_id)

        assigned_count += 1

    logger.info(
        "批量分配任务 | task_ids={} assignee_id={} success={} by={}",
        task_ids, assignee_id, assigned_count, processed_by,
    )
    return assigned_count


# ─── 自动创建任务 ─────────────────────────────────────────────────────

async def create_arrangement_task(
    db: AsyncSession,
    content_id: int,
    assignee_id: int | None = None,
    processed_by: str | None = None,
) -> Task:
    """为指定内容创建 arrangement 任务（内容创建时调用）。"""
    now = datetime.now()
    task_status = "Pending" if assignee_id else "Not Assigned"
    task = Task(
        content_id=content_id,
        task_type="arrangement",
        assignee_id=assignee_id,
        task_status=task_status,
        start_time=now,
        end_time=None,
    )
    await task_repo.add_task(db, task)

    # 如果有负责人，自动添加数据权限
    if assignee_id:
        await _ensure_content_auth(db, content_id, assignee_id)

    await task_repo.add_task_history(
        db,
        TaskHistory(
            task_id=task.id,
            processed_type="Add",
            processed_by=processed_by or "system",
            previous_value=None,
            updated_value=f"Task created, assignee_id={assignee_id}" if assignee_id else "Task created",
        ),
    )

    logger.info(
        "自动创建 arrangement 任务 | content_id={} task_id={} assignee_id={}",
        content_id, task.id, assignee_id,
    )
    return task


async def create_review_task(
    db: AsyncSession, content_id: int, review_level: str
) -> Task:
    """为指定内容创建指定级别的审核任务。"""
    now = datetime.now()
    task = Task(
        content_id=content_id,
        task_type=f"review {review_level}",
        task_status="Not Assigned",
        start_time=now,
        end_time=None,
    )
    await task_repo.add_task(db, task)

    await task_repo.add_task_history(
        db,
        TaskHistory(
            task_id=task.id,
            processed_type="Add",
            processed_by="system",
            previous_value=None,
            updated_value="Task created",
        ),
    )

    logger.info(
        "自动创建 review 任务 | content_id={} level={} task_id={}",
        content_id, review_level, task.id,
    )
    return task


# ─── 任务状态恢复 ─────────────────────────────────────────────────────

async def reopen_arrangement_task(
    db: AsyncSession,
    content_id: int,
    processed_by: str | None = None,
    reason: str = "",
) -> None:
    """内容下架成功后，将 arrangement 任务由 Completed 恢复为待处理。

    PRD 3.7.2.4：内容下架成功，则对应的内容编排任务状态，会自动恢复成待处理。
    发布/下架两条执行路径（模拟模式与 LSP 回调）统一调用本函数。

    处理要素：
        1. 仅当任务当前为 Completed 时恢复（Not Assigned / Pending 保持不变）
        2. 清空 end_time
        3. start_time 重置为当前时间（对齐首页看板 Start Time 本轮处理周期语义）
        4. 写入 TaskHistory "Reopen" 记录
    """
    arrangement_task = (
        await db.execute(
            select(Task).where(
                Task.content_id == content_id,
                Task.task_type == "arrangement",
                Task.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()

    if arrangement_task is None:
        logger.info("下架恢复：无存活的 arrangement 任务，跳过 | content_id={}", content_id)
        return

    if arrangement_task.task_status != "Completed":
        logger.info(
            "下架恢复：arrangement 任务非 Completed（当前 {}），跳过 | content_id={} task_id={}",
            arrangement_task.task_status, content_id, arrangement_task.id,
        )
        return

    now = datetime.now()
    old_status = arrangement_task.task_status
    arrangement_task.task_status = "Pending"
    arrangement_task.end_time = None
    arrangement_task.start_time = now
    await task_repo.add_task_history(
        db,
        TaskHistory(
            task_id=arrangement_task.id,
            processed_type="Reopen",
            processed_by=processed_by or "system",
            previous_value=old_status,
            updated_value=f"任务恢复待处理: {reason}" if reason else "任务恢复待处理",
        ),
    )

    logger.info(
        "下架成功，arrangement 任务恢复为待处理 | content_id={} task_id={}",
        content_id, arrangement_task.id,
    )
