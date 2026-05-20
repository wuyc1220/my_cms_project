"""
注入历史 API 路由层。

路由前缀：/ingest-histories
"""

from urllib.parse import quote

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, get_db
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail
from app.internal.cms_biz_orchestration.schemas.ingest_history import IngestHistoryItem, IngestHistoryDetailItem
from app.internal.cms_biz_system.models.user import User

router = APIRouter(prefix="/ingest-histories")


def _build_ingest_history_item(history: IngestHistory) -> IngestHistoryItem:
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
    # 添加下载 URL（不含 /api/v1 前缀，由前端 axios baseURL 自动添加）
    if history.ingest_xml_path:
        data["ingest_xml_url"] = f"/attachments/download?path={quote(history.ingest_xml_path, safe='')}"
    if history.result_xml_path:
        data["result_xml_url"] = f"/attachments/download?path={quote(history.result_xml_path, safe='')}"
    return IngestHistoryItem(**data)


@router.get("/", response_model=PaginatedResponse[IngestHistoryItem])
async def list_ingest_histories(
    entity_type: str | None = None,
    entity_id: int | None = None,
    action: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """查询注入历史列表（分页 + 筛选）。"""
    query = select(IngestHistory).where(IngestHistory.is_deleted.is_(False))

    if entity_type:
        query = query.where(IngestHistory.entity_type == entity_type)
    if entity_id is not None:
        query = query.where(IngestHistory.entity_id == entity_id)
    if action:
        query = query.where(IngestHistory.action == action)
    if status:
        query = query.where(IngestHistory.status == status)

    # 总条数
    total_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = total_result.scalar_one()

    # 分页数据
    result = await db.execute(
        query.order_by(IngestHistory.create_date.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    items = result.scalars().all()

    return PaginatedResponse[IngestHistoryItem](
        total=total,
        page=page,
        page_size=page_size,
        items=[_build_ingest_history_item(item) for item in items],
    )


@router.get("/details/", response_model=PaginatedResponse[IngestHistoryDetailItem])
async def list_ingest_history_details(
    entity_type: str,
    entity_id: int,
    page: int = 1,
    page_size: int = 10,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    查询关联对象的注入历史明细（分页）。

    通过 ingest_history_detail 表查询 Cast/Category/Package 等对象的发布历史，
    JOIN ingest_history 获取触发发布的 Content 名称和 XML 路径。
    """
    base_query = (
        select(IngestHistoryDetail, IngestHistory)
        .join(IngestHistory, IngestHistoryDetail.history_id == IngestHistory.id)
        .where(
            IngestHistoryDetail.entity_type == entity_type,
            IngestHistoryDetail.entity_id == entity_id,
            IngestHistoryDetail.is_deleted.is_(False),
            IngestHistory.is_deleted.is_(False),
        )
    )

    # 总条数
    count_query = select(func.count()).select_from(base_query.subquery())
    total_result = await db.execute(count_query)
    total = total_result.scalar_one()

    # 分页数据
    result = await db.execute(
        base_query.order_by(IngestHistoryDetail.created_at.desc())
        .offset((page - 1) * page_size)
        .limit(page_size)
    )
    rows = result.all()

    items = []
    for detail, history in rows:
        data = {
            "id": detail.id,
            "history_id": detail.history_id,
            "entity_type": detail.entity_type,
            "entity_id": detail.entity_id,
            "entity_name": detail.entity_name,
            "action": detail.action,
            "created_at": detail.created_at,
            "trigger_content_id": history.entity_id,
            "trigger_content_name": history.entity_name,
            "status": history.status,
            "create_date": history.create_date,
            "send_date": history.send_date,
            "end_date": history.end_date,
            "ingest_xml_path": history.ingest_xml_path,
            "result_xml_path": history.result_xml_path,
        }
        if history.ingest_xml_path:
            data["ingest_xml_url"] = f"/attachments/download?path={quote(history.ingest_xml_path, safe='')}"
        if history.result_xml_path:
            data["result_xml_url"] = f"/attachments/download?path={quote(history.result_xml_path, safe='')}"
        items.append(IngestHistoryDetailItem(**data))

    return PaginatedResponse[IngestHistoryDetailItem](
        total=total,
        page=page,
        page_size=page_size,
        items=items,
    )
