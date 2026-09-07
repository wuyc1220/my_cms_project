"""
任务管理 Repository 层。

封装 task 和 task_history 表的 SQLAlchemy 查询操作。
"""

from datetime import datetime

from sqlalchemy import func, select, exists, case
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.internal.cms_biz_package.models.task import Task, TaskHistory
from app.internal.cms_biz_package.models.package import Content
from app.internal.cms_biz_system.models.user import User


# ─── Task CRUD ─────────────────────────────────────────────────────────

async def add_task(db: AsyncSession, task: Task) -> None:
    """新增任务。"""
    db.add(task)
    await db.flush()


async def get_task_by_id(db: AsyncSession, task_id: int) -> Task | None:
    """按 ID 查询任务。"""
    return (
        await db.execute(select(Task).where(Task.id == task_id, Task.is_deleted.is_(False)))
    ).scalar_one_or_none()


async def get_tasks_by_ids(db: AsyncSession, task_ids: list[int]) -> list[Task]:
    """按 ID 列表批量查询任务。"""
    return (
        await db.execute(select(Task).where(Task.id.in_(task_ids), Task.is_deleted.is_(False)))
    ).scalars().all()


async def list_tasks_query(
    db: AsyncSession,
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
):
    """
    构建任务列表查询（join content + cms_user）。
    返回 SQLAlchemy Query 对象（未执行）。
    """
    ChildContent = aliased(Content)
    has_children_subq = exists().where(
        ChildContent.parent_id == Content.id,
        ChildContent.is_deleted.is_(False),
        ChildContent.is_discarded.is_(False),
    )
    query = (
        select(Task, Content, User, has_children_subq.label("has_children"))
        .join(Content, Content.id == Task.content_id)
        .outerjoin(User, User.id == Task.assignee_id)
        .where(Task.is_deleted.is_(False), Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )

    if task_types:
        query = query.where(Task.task_type.in_(task_types))
    if task_statuses:
        query = query.where(Task.task_status.in_(task_statuses))
    if assignee_is_null:
        query = query.where(Task.assignee_id.is_(None))
    elif assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)
    elif assignee_keyword:
        query = query.where(
            (User.is_deleted.is_(False)) &
            ((User.display_name.ilike(f"%{assignee_keyword}%")) |
            (User.username.ilike(f"%{assignee_keyword}%")))
        )
    if content_name:
        query = query.where(Content.title.ilike(f"%{content_name}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if content_id is not None:
        query = query.where(Task.content_id == content_id)
    if time_start:
        query = query.where(Task.start_time >= time_start)
    if time_end:
        query = query.where(Task.start_time <= time_end)
    if end_time_start:
        query = query.where(Task.end_time >= end_time_start)
    if end_time_end:
        query = query.where(Task.end_time <= end_time_end)

    # 排序
    if sort_mode == "assigned_to_me":
        task_status_order = case(
            (Task.task_status == "Pending", 0),
            (Task.task_status == "Completed", 1),
            else_=2,
        )
        task_type_order = case(
            (Task.task_type == "review L3", 0),
            (Task.task_type == "review L2", 1),
            (Task.task_type == "review L1", 2),
            (Task.task_type == "arrangement", 3),
            else_=4,
        )
        content_type_order = case(
            (Content.content_type == "CHANNEL", 0),
            (Content.content_type == "MOVIE", 1),
            (Content.content_type == "SEASON", 2),
            (Content.content_type == "SERIES", 3),
            (Content.content_type == "SEASON_SERIES", 3),
            (Content.content_type == "EPISODE", 4),
            (Content.content_type == "SCHEDULE", 5),
            else_=6,
        )
        query = query.order_by(
            task_status_order,
            task_type_order,
            content_type_order,
            Task.start_time.desc(),
            Task.id.desc(),
        )
    elif sort_mode == "not_assigned":
        task_type_order = case(
            (Task.task_type == "review L3", 0),
            (Task.task_type == "review L2", 1),
            (Task.task_type == "review L1", 2),
            (Task.task_type == "arrangement", 3),
            else_=4,
        )
        content_type_order = case(
            (Content.content_type == "CHANNEL", 0),
            (Content.content_type == "MOVIE", 1),
            (Content.content_type == "SEASON", 2),
            (Content.content_type == "SERIES", 3),
            (Content.content_type == "SEASON_SERIES", 3),
            (Content.content_type == "EPISODE", 4),
            (Content.content_type == "SCHEDULE", 5),
            else_=6,
        )
        query = query.order_by(
            task_type_order,
            content_type_order,
            Task.start_time.desc(),
            Task.id.desc(),
        )
    elif sort_by and sort_order:
        if sort_by == "content_name":
            sort_column = Content.title
        elif sort_by == "content_type":
            sort_column = Content.content_type
        elif sort_by == "assignee_name":
            sort_column = User.display_name
        else:
            sort_column = getattr(Task, sort_by, None)
        
        if sort_column is not None:
            query = query.order_by(
                sort_column.asc() if sort_order == "asc" else sort_column.desc(),
                Task.id.desc(),
            )
        else:
            query = query.order_by(Task.created_at.desc(), Task.id.desc())
    else:
        query = query.order_by(Task.created_at.desc(), Task.id.desc())

    return query


async def count_tasks(
    db: AsyncSession,
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
) -> int:
    """查询任务总数。"""
    query = (
        select(func.count(Task.id))
        .join(Content, Content.id == Task.content_id)
        .outerjoin(User, User.id == Task.assignee_id)
        .where(Task.is_deleted.is_(False), Content.is_deleted.is_(False), Content.is_discarded.is_(False))
    )

    if task_types:
        query = query.where(Task.task_type.in_(task_types))
    if task_statuses:
        query = query.where(Task.task_status.in_(task_statuses))
    if assignee_is_null:
        query = query.where(Task.assignee_id.is_(None))
    elif assignee_id is not None:
        query = query.where(Task.assignee_id == assignee_id)
    elif assignee_keyword:
        query = query.where(
            (User.is_deleted.is_(False)) &
            ((User.display_name.ilike(f"%{assignee_keyword}%")) |
            (User.username.ilike(f"%{assignee_keyword}%")))
        )
    if content_name:
        query = query.where(Content.title.ilike(f"%{content_name}%"))
    if content_types:
        query = query.where(Content.content_type.in_(content_types))
    if content_id is not None:
        query = query.where(Task.content_id == content_id)
    if time_start:
        query = query.where(Task.start_time >= time_start)
    if time_end:
        query = query.where(Task.start_time <= time_end)
    if end_time_start:
        query = query.where(Task.end_time >= end_time_start)
    if end_time_end:
        query = query.where(Task.end_time <= end_time_end)

    return (await db.execute(query)).scalar_one()


async def update_task_assignee(
    db: AsyncSession, task_id: int, assignee_id: int | None
) -> None:
    """更新任务分配人。"""
    await db.execute(
        select(Task).where(Task.id == task_id)
    )
    task = await get_task_by_id(db, task_id)
    if task:
        task.assignee_id = assignee_id


async def update_task_status(
    db: AsyncSession, task_id: int, task_status: str
) -> None:
    """更新任务状态。"""
    task = await get_task_by_id(db, task_id)
    if task:
        task.task_status = task_status


# ─── TaskHistory ───────────────────────────────────────────────────────

async def add_task_history(db: AsyncSession, history: TaskHistory) -> None:
    """新增任务操作历史。"""
    db.add(history)
    await db.flush()


async def list_task_history_by_task_id(
    db: AsyncSession, task_id: int
) -> list[TaskHistory]:
    """按 task_id 查询操作历史，按操作时间倒序。"""
    return (
        await db.execute(
            select(TaskHistory)
            .where(TaskHistory.task_id == task_id, TaskHistory.is_deleted.is_(False))
            .order_by(TaskHistory.processed_at.desc())
        )
    ).scalars().all()


# ─── Content 子内容查询 ────────────────────────────────────────────────

async def get_child_content_ids(
    db: AsyncSession, content_id: int
) -> list[int]:
    """递归获取指定内容的所有后代内容 ID 列表（含子、孙等多层）。"""
    all_ids: list[int] = []
    current_ids = [content_id]
    visited: set[int] = set()
    while current_ids:
        next_ids: list[int] = []
        for cid in current_ids:
            if cid in visited:
                continue
            visited.add(cid)
            rows = (
                await db.execute(
                    select(Content.id).where(
                        Content.parent_id == cid,
                        Content.is_deleted.is_(False),
                        Content.is_discarded.is_(False),
                    )
                )
            ).scalars().all()
            next_ids.extend(rows)
        all_ids.extend(next_ids)
        current_ids = next_ids
    return all_ids


async def get_tasks_by_content_ids_and_type(
    db: AsyncSession, content_ids: list[int], task_type: str
) -> list[Task]:
    """按内容 ID 列表和任务类型查询任务。"""
    return (
        await db.execute(
            select(Task).where(
                Task.content_id.in_(content_ids),
                Task.task_type == task_type,
                Task.is_deleted.is_(False),
            )
        )
    ).scalars().all()
