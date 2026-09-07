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

from app.common.core.exceptions import BusinessException, ErrorCode, NotFoundException, ValidationException

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
    rollback_after_published_edit,
)
from app.internal.cms_biz_orchestration.services.metadata_validation_service import validate_metadata
from app.common.core.i18n import get_msg
from app.common.crypto import encrypt_storage_url

async def _validate_duration_definition(movie_type: int, duration: int | None, definition: str | None) -> None:
    """非字幕类型（Movie/Trailer）的 Duration/Definition 为 C2 规范必填；字幕类型允许为空。"""
    if int(movie_type) == 3:
        return
    if duration is None:
        raise ValidationException(get_msg("MOVIE_DURATION_REQUIRED"))
    if not definition:
        raise ValidationException(get_msg("MOVIE_DEFINITION_REQUIRED"))


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
            get_msg("ARCHIVED_CONTENT_NO_MOVIE"),
            422,
        )

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="MOVIE",
    #     data=data,
    # )

    # Trailer (movie_type=2) 固定 publish_flag=True
    movie_data = data.model_dump()
    if int(movie_data.get("movie_type", 0)) == 2:
        movie_data["publish_flag"] = True

    # 非字幕类型必填校验（字幕类型 Duration/Definition 可为空）
    await _validate_duration_definition(
        int(movie_data.get("movie_type", 0)),
        movie_data.get("duration"),
        movie_data.get("definition"),
    )

    # file_path 可能是：
    # 1. storage_url（本地上传，已由 /attachments/upload 加密）
    # 2. 用户输入的 sftp:// 完整 URL（外部区域，需要加密）
    # 3. 相对路径（旧数据兼容）
    fp = movie_data.get("file_path", "")
    if fp and (fp.lower().startswith("sftp://") or fp.lower().startswith("ftp://")):
        # 用户输入的完整 FTP/SFTP URL → 加密存储
        movie_data["file_path"] = encrypt_storage_url(fp)
        # 外部FTP场景：relative_path 为空，下载时解密 file_path 连接外部服务器
        movie_data["relative_path"] = None

    movie = Movie(**movie_data)
    await movie_repo.add_movie(db, movie)

    # 完成流程节点并更新内容状态
    if content:
        movie_type_to_process = {
            1: "Materials",
            2: "Trailer",
            3: "MusicEffects",
        }
        process_name = movie_type_to_process.get(int(data.movie_type), "Materials")
        await rollback_after_published_edit(db, data.content_id, content.content_type, processed_by or "system", "上传素材")
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


async def update_movie(
    db: AsyncSession, movie_id: int, data: MovieUpdate, processed_by: str | None = None
) -> MovieItem:
    """更新媒资实体。"""
    movie = await movie_repo.get_movie_by_id(db, movie_id)
    if not movie:
        raise NotFoundException(ErrorCode.MOVIE_NOT_FOUND, get_msg("MOVIE_NOT_FOUND"))

    update_data = data.model_dump(exclude_unset=True)

    # 按合并后的有效值做必填校验（兼容类型切换、字段置空场景）
    await _validate_duration_definition(
        int(update_data.get("movie_type", movie.movie_type)),
        update_data.get("duration", movie.duration),
        update_data.get("definition", movie.definition),
    )

    # 如果更新了 file_path 且是用户输入的完整 FTP/SFTP URL → 加密
    fp = update_data.get("file_path", "")
    if fp and (fp.lower().startswith("sftp://") or fp.lower().startswith("ftp://")):
        update_data["file_path"] = encrypt_storage_url(fp)
        # 外部FTP场景：relative_path 为空，下载时解密 file_path 连接外部服务器
        update_data["relative_path"] = None

    for key, value in update_data.items():
        setattr(movie, key, value)

    # 更新素材属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    content = await get_content_by_id(db, movie.content_id)
    if content:
        await rollback_after_published_edit(
            db,
            content_id=movie.content_id,
            content_type=content.content_type,
            edited_by=processed_by or "system",
            edit_info="更新素材",
        )

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

    # 已发布/已下架内容编辑后回滚状态并新建提交审核记录（与上传素材行为一致）
    content = await get_content_by_id(db, content_id)
    if content:
        await rollback_after_published_edit(
            db,
            content_id=content_id,
            content_type=content.content_type,
            edited_by=processed_by or "system",
            edit_info="删除素材",
        )

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
        # 无条件写入 Materials 分界记录（info 以"删除"开头）：
        # 保证 Processes 可见删除操作，且"删除→再保存"的 processed_before 判定为首次处理（红色）。
        # 注意不能放在下方 status 条件内——状态为 WaitingForMaterials 时跳过写入会导致
        # 再保存时误命中更早的 Passed 历史而显示绿勾。
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
            status="Pending",  # 删除后节点回到待处理状态
            end_dt=None,
            assigned=processed_by,
            info=f"删除材料后回退: {file_name}",
        )
        await process_repo.add_process(db, process)

        if content and content.status != "WaitingForMaterials":
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
