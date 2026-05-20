"""
媒资实体业务逻辑层。

职责：
- Movie / Trailer / Subtitle 三种媒资的 CRUD
- 按 content_id 查询媒资列表（内容详情页 Media File Tab）
- 媒资操作历史记录（Materials 弹框 Material History Tab）
- 创建/删除媒资时联动更新内容 Ingest 状态
"""

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, ErrorCode, NotFoundException

from app.internal.cms_biz_orchestration.models.movie import Movie
from app.internal.cms_biz_orchestration.models.movie_history import MovieHistory
from app.internal.cms_biz_orchestration.schemas.movie import (
    MovieCreate,
    MovieItem,
    MovieUpdate,
    MovieListItem,
)
from app.internal.cms_biz_orchestration.schemas.movie_history import (
    MovieHistoryItem,
    MovieHistoryListItem,
)
from app.internal.cms_biz_orchestration.repositories import movie_repo
from app.internal.cms_biz_orchestration.repositories import movie_history_repo
from app.internal.cms_biz_orchestration.repositories import get_content_by_id
from app.internal.cms_biz_orchestration.services import workflow_service
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
)
from app.internal.cms_biz_orchestration.services.metadata_validation_service import validate_metadata
from app.common.core.i18n import get_msg


async def get_movie(db: AsyncSession, movie_id: int) -> MovieItem:
    """查询单个媒资实体。"""
    movie = await movie_repo.get_movie_by_id(db, movie_id)
    if not movie:
        raise NotFoundException(ErrorCode.MOVIE_NOT_FOUND, get_msg("MOVIE_NOT_FOUND"))
    return MovieItem.model_validate(movie)


async def list_movies(
    db: AsyncSession, content_id: int, movie_type: int | None = None
) -> MovieListItem:
    """查询内容关联的媒资列表。"""
    movies = await movie_repo.list_movies_by_content_id(db, content_id, movie_type)
    items = [MovieItem.model_validate(m) for m in movies]
    return MovieListItem(items=items, total=len(items))


async def create_movie(
    db: AsyncSession,
    data: MovieCreate,
    processed_by: str | None = None,
    processed_by_id: int | None = None,
) -> MovieItem:
    """创建媒资实体，并记录操作历史；同时更新内容 Ingest 状态。"""
    # 归档内容不允许注入 Movie（正片）类型材料
    content = await get_content_by_id(db, data.content_id)
    if content and bool(getattr(content, "is_archived", False)) and int(data.movie_type) == 1:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            "归档内容不允许注入正片（Movie）材料",
            422,
        )

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="MOVIE",
    #     data=data,
    # )

    movie = Movie(**data.model_dump())
    await movie_repo.add_movie(db, movie)

    # 完成流程节点并更新内容状态
    if content:
        movie_type_to_process = {
            1: "Materials",
            2: "Trailer",
            3: "MusicEffects",
        }
        process_name = movie_type_to_process.get(int(data.movie_type), "Materials")
        await complete_process_and_update_status(
            db,
            content_id=data.content_id,
            content_type=content.content_type,
            process_name=process_name,
            processed_by=processed_by,
            info=f"注入材料: {data.file_name}",
        )

    # 记录 Add 历史
    history = MovieHistory(
        content_id=data.content_id,
        file_name=data.file_name,
        movie_type=data.movie_type,
        file_size=data.file_size,
        processed_by=processed_by,
        processed_type="Add",
    )
    await movie_history_repo.add_movie_history(db, history)

    logger.info(
        "创建媒资实体 | content_id={} movie_type={} file_name={} by={}",
        data.content_id, data.movie_type, data.file_name, processed_by
    )
    return MovieItem.model_validate(movie)


async def update_movie(db: AsyncSession, movie_id: int, data: MovieUpdate) -> MovieItem:
    """更新媒资实体。"""
    movie = await movie_repo.get_movie_by_id(db, movie_id)
    if not movie:
        raise NotFoundException(ErrorCode.MOVIE_NOT_FOUND, get_msg("MOVIE_NOT_FOUND"))

    update_data = data.model_dump(exclude_unset=True)
    for key, value in update_data.items():
        setattr(movie, key, value)

    logger.info("更新媒资实体 | movie_id={}", movie_id)
    return MovieItem.model_validate(movie)


async def delete_movie(
    db: AsyncSession, movie_id: int, processed_by: str | None = None
) -> None:
    """删除媒资实体，并记录操作历史；同时更新内容状态。"""
    movie = await movie_repo.get_movie_by_id(db, movie_id)
    if not movie:
        raise NotFoundException(ErrorCode.MOVIE_NOT_FOUND, get_msg("MOVIE_NOT_FOUND"))

    content_id = movie.content_id
    file_name = movie.file_name

    # 记录 Delete 历史（在软删除前记录）
    history = MovieHistory(
        content_id=content_id,
        file_name=file_name,
        movie_type=movie.movie_type,
        file_size=movie.file_size,
        processed_by=processed_by,
        processed_type="Delete",
    )
    await movie_history_repo.add_movie_history(db, history)

    # 执行软删除
    movie.is_deleted = True

    # 检查是否还有正片(movie_type=1)素材 - 与前端判断逻辑保持一致
    remaining_movies = await movie_repo.list_movies_by_content_id(db, content_id, movie_type=1)
    logger.info(
        "删除素材后检查剩余正片 | content_id={} movie_id={} remaining_main_movie_count={}",
        content_id, movie_id, len(remaining_movies)
    )
    
    # 如果没有正片素材，回退内容状态到 WaitingForMaterials
    if not remaining_movies:
        content = await get_content_by_id(db, content_id)
        logger.info(
            "检查内容状态 | content_id={} current_status={}",
            content_id, content.status if content else "None"
        )
        if content and content.status != "WaitingForMaterials":
            # 创建流程记录表示 Materials 节点被回退
            from datetime import datetime
            from app.internal.cms_biz_orchestration.models.content_process import ContentProcess
            from app.internal.cms_biz_orchestration.repositories import process_repo
            from app.internal.cms_biz_orchestration.services.workflow_service import record_status_change
            
            now = datetime.now()
            process = ContentProcess(
                content_id=content_id,
                name="Materials",
                node_code="Materials",
                sequence=0,
                start_dt=now,
                status="Pending",  # 回退到待处理状态
                end_dt=None,
                assigned=processed_by,
                info=f"删除材料后回退: {file_name}",
            )
            await process_repo.add_process(db, process)
            
            # 记录旧状态
            old_status = content.status
            
            # 更新内容状态为 WaitingForMaterials
            content.status = "WaitingForMaterials"
            
            # 记录状态变更日志
            await record_status_change(
                db,
                content_id=content_id,
                before_status=old_status,
                after_status="WaitingForMaterials",
                processed_by=processed_by or "system",
            )
            
            logger.info(
                "删除最后一个素材，回退内容状态 | content_id={} movie_id={} {} → {} by={}",
                content_id, movie_id, old_status, "WaitingForMaterials", processed_by
            )

    logger.info("删除媒资实体 | movie_id={} file_name={} by={}", movie_id, file_name, processed_by)


async def list_movie_history(
    db: AsyncSession, content_id: int
) -> MovieHistoryListItem:
    """查询内容关联的媒资操作历史列表。"""
    histories = await movie_history_repo.list_movie_history_by_content_id(db, content_id)
    items = [MovieHistoryItem.model_validate(h) for h in histories]
    return MovieHistoryListItem(items=items, total=len(items))
