"""
元数据扩展业务逻辑层。

职责：
- Program / Series / Channel / Schedule 四种类型的元数据 CRUD
- 根据 content_type 路由到对应的元数据表
- 内容详情页统一元数据查询
- 创建时自动填充 name（默认同 content.title）
- 创建时自动判定 series_flag
- Series Update Childs 批量同步
- 更新元数据 genre_ids 时同步到 content_genre 中间表
"""

from typing import Optional

from collections import defaultdict

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, ErrorCode, NotFoundException
from app.common.core.transactions import transactional
from app.internal.cms_biz_package.models.package import Content, ContentGenre, ContentCustomTag
from app.internal.cms_biz_metada.models.basic import CustomTag, EntityFieldValue, EntityI18n
from app.internal.cms_biz_orchestration.models.content_metadata import (
    ContentMetadata,
    SeriesMetadata,
    ChannelMetadata,
    ScheduleMetadata,
)
from app.internal.cms_biz_orchestration.schemas.content_metadata import (
    ContentMetadataCreate,
    ContentMetadataItem,
    ContentMetadataUpdate,
    SeriesMetadataCreate,
    SeriesMetadataItem,
    SeriesMetadataUpdate,
    ChannelMetadataCreate,
    ChannelMetadataItem,
    ChannelMetadataUpdate,
    ScheduleMetadataCreate,
    ScheduleMetadataItem,
    ScheduleMetadataUpdate,
    MetadataDetailItem,
)
from app.internal.cms_biz_orchestration.repositories import metadata_repo
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
    rollback_after_published_edit,
    record_status_change,
)
from app.internal.cms_biz_orchestration.services.metadata_validation_service import validate_metadata
from app.common.core.i18n import get_msg


# ═══════════════════════════════════════════════════════════
# 内部辅助
# ═══════════════════════════════════════════════════════════

async def _get_content_or_404(db: AsyncSession, content_id: int) -> Content:
    """查询内容，不存在则抛 404。"""
    c = (
        await db.execute(
            select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
        )
    ).scalar_one_or_none()
    if not c:
        raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))
    return c


# 不参与 Update Childs 同步的字段（系统维护字段）
# cdr_id 为每级内容独立的唯一标识（Series_{id} / Program_{id}），
# 快照同步时必须排除，避免子级被父级 CDR ID 覆盖
_UPDATE_CHILDS_EXCLUDE = {
    "id", "created_at", "updated_at", "created_by", "updated_by",
    "is_deleted", "is_discarded",
    "volume_count", "series_type", "series_ordinal", "show_id",
    "cdr_id",
}


async def _sync_custom_tags(db: AsyncSession, content_id: int, custom_tag_ids: list[int] | None) -> None:
    """
    同步自定义标签到 content_custom_tag 表（父级覆盖策略）。
    
    逻辑：
        1. 删除子级的所有标签关联
        2. 插入父级的标签
    
    参数：
        content_id: 子级内容 ID
        custom_tag_ids: 父级标签 ID 列表
    """
    # 1. 删除子级所有标签关联
    await db.execute(
        ContentCustomTag.__table__.delete().where(ContentCustomTag.content_id == content_id)
    )
    
    # 2. 插入父级标签
    if custom_tag_ids:
        for tag_id in custom_tag_ids:
            # 验证标签是否存在
            tag = await db.execute(
                select(CustomTag).where(CustomTag.id == tag_id, CustomTag.is_deleted.is_(False))
            )
            tag = tag.scalar_one_or_none()
            if tag:
                assoc = ContentCustomTag(content_id=content_id, custom_tag_id=tag_id)
                db.add(assoc)
            else:
                logger.warning("自定义标签 ID={} 不存在或已删除，跳过", tag_id)


def _has_real_changes(metadata, update_data: dict) -> bool:
    """在 setattr 之前逐字段比较当前持久化值与待写入值，判断是否存在实际变更。

    必须在 setattr 循环之前调用：SQLAlchemy 的 inspect(obj).modified
    对同值赋值也会返回 True，无法用于实际变更检测，否则会在
    “创建+同步连调”等场景误写第二条父级流程记录。
    """
    return any(getattr(metadata, key) != value for key, value in update_data.items())


# ═══════════════════════════════════════════════════════════
# ContentMetadata — Program (MOVIE / EPISODE)
# ═══════════════════════════════════════════════════════════

async def get_content_metadata(db: AsyncSession, content_id: int) -> ContentMetadataItem | None:
    """查询 Program 元数据。"""
    metadata = await metadata_repo.get_content_metadata_by_content_id(db, content_id)
    if not metadata:
        return None
    return ContentMetadataItem.model_validate(metadata)


@transactional
async def create_content_metadata(
    db: AsyncSession, data: ContentMetadataCreate, processed_by: str | None = None
) -> ContentMetadataItem:
    """创建 Program 元数据。"""
    content = await _get_content_or_404(db, data.content_id)
    if content.content_type not in ("MOVIE", "EPISODE"):
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTENT_TYPE_INVALID"), 400)

    existing = await metadata_repo.get_content_metadata_by_content_id(db, data.content_id)
    if existing:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTENT_METADATA_EXISTS"), 400)

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="PROGRAM",
    #     data=data,
    #     content_type=content.content_type,
    # )

    # 自动填充 name（若未提供则取 content.title）
    dump = data.model_dump()
    if not dump.get("name"):
        dump["name"] = content.title
    # 自动判定 series_flag: MOVIE→0, EPISODE→1
    dump["series_flag"] = 0 if content.content_type == "MOVIE" else 1
    # 自动生成 CDR ID: Program_{content_id}
    if not dump.get("cdr_id") or dump.get("cdr_id") == "":
        dump["cdr_id"] = f"Program_{data.content_id}"

    # 移除 genre_ids，不保存到元数据表（它存储在 content_genre 中间表）
    genre_ids = dump.pop("genre_ids", None)

    metadata = ContentMetadata(**dump)
    await metadata_repo.add_content_metadata(db, metadata)
    await db.flush()
    await db.refresh(metadata)

    # 保存题材关联到中间表
    if genre_ids:
        for gid in genre_ids:
            db.add(ContentGenre(content_id=data.content_id, genre_id=gid))

    # 用户主动创建元数据弹窗，直接记 Passed（用户确认即完成）
    await complete_process_and_update_status(
        db,
        content_id=data.content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
        info="创建元数据",
    )

    logger.info("创建 Program 元数据 | content_id={} type={}", data.content_id, content.content_type)
    return ContentMetadataItem.model_validate(metadata)


@transactional
async def update_content_metadata(
    db: AsyncSession, content_id: int, data: ContentMetadataUpdate, processed_by: str | None = None
) -> ContentMetadataItem:
    """更新 Program 元数据。"""
    metadata = await metadata_repo.get_content_metadata_by_content_id(db, content_id)
    if not metadata:
        raise NotFoundException(ErrorCode.CONTENT_METADATA_NOT_FOUND, get_msg("CONTENT_METADATA_NOT_FOUND"))

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # content = await _get_content_or_404(db, content_id)
    # await validate_metadata(
    #     db,
    #     entity_type="PROGRAM",
    #     data=data,
    #     content_type=content.content_type,
    #     content_id=content_id,  # 编辑模式：传入 content_id 用于合并数据
    # )

    update_data = data.model_dump(exclude_unset=True)
    # 在 setattr 前检测实际变更（inspect().modified 对同值赋值也返回 True）
    has_metadata_changes = _has_real_changes(metadata, update_data)
    for key, value in update_data.items():
        setattr(metadata, key, value)

    await db.flush()
    await db.refresh(metadata)

    # 自动生成 CDR ID: Program_{content_id}（与创建路径 data.content_id 口径一致）
    if not metadata.cdr_id:
        metadata.cdr_id = f"Program_{content_id}"
        await db.flush()

    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(db, content_id, content.content_type, processed_by or "system", "修改元数据")

    # 用户主动保存元数据弹窗（含主表/自定义/多语言任一变更），每次都写一条 Passed 记录。
    # 流程记录与状态判断解耦：保存即记录，状态固定 Passed（用户主动确认即完成），
    # 不再以"主表是否有变更"或"是否已有 Passed 记录"作为是否写入的条件。
    await complete_process_and_update_status(
        db,
        content_id=content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
        info="更新元数据" if has_metadata_changes else "确认元数据（仅自定义/多语言变更）",
    )

    logger.info("更新 Program 元数据 | content_id={}", content_id)

    # 刷新后再转换为 Pydantic 模型，避免 flush 后 ORM 属性过期触发绿色线程错误
    await db.refresh(metadata)
    return ContentMetadataItem.model_validate(metadata)


@transactional
async def delete_content_metadata(db: AsyncSession, content_id: int) -> None:
    """软删除 Program 元数据。"""
    await metadata_repo.delete_content_metadata_by_content_id(db, content_id)
    logger.info("删除 Program 元数据 | content_id={}", content_id)
    # 删除元数据属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(
        db, content_id, content.content_type, "system", "删除元数据"
    )


# ═══════════════════════════════════════════════════════════
# SeriesMetadata — Series (SERIES / SEASON)
# ═══════════════════════════════════════════════════════════

async def get_series_metadata(db: AsyncSession, content_id: int) -> SeriesMetadataItem | None:
    """查询 Series 元数据。"""
    metadata = await metadata_repo.get_series_metadata_by_content_id(db, content_id)
    if not metadata:
        return None
    return SeriesMetadataItem.model_validate(metadata)


@transactional
async def create_series_metadata(
    db: AsyncSession, data: SeriesMetadataCreate, processed_by: str | None = None
) -> SeriesMetadataItem:
    """创建 Series 元数据。"""
    content = await _get_content_or_404(db, data.content_id)
    if content.content_type not in ("SERIES", "SEASON", "SEASON_SERIES"):
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTENT_TYPE_INVALID"), 400)

    existing = await metadata_repo.get_series_metadata_by_content_id(db, data.content_id)
    if existing:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("SERIES_METADATA_EXISTS"), 400)

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="SERIES",
    #     data=data,
    #     content_type=content.content_type,
    #     content_id=data.content_id,  # 传入 content_id 用于获取 genre_id
    # )

    dump = data.model_dump()
    if not dump.get("name"):
        dump["name"] = content.title
    # 自动生成 CDR ID: Series_{content_id}
    if not dump.get("cdr_id") or dump.get("cdr_id") == "":
        dump["cdr_id"] = f"Series_{data.content_id}"

    # 移除 genre_ids，不保存到元数据表（它存储在 content_genre 中间表）
    genre_ids = dump.pop("genre_ids", None)

    metadata = SeriesMetadata(**dump)
    await metadata_repo.add_series_metadata(db, metadata)
    await db.flush()
    await db.refresh(metadata)

    # 保存题材关联到中间表
    if genre_ids:
        for gid in genre_ids:
            db.add(ContentGenre(content_id=data.content_id, genre_id=gid))

    # 用户主动创建元数据弹窗，直接记 Passed（用户确认即完成）
    await complete_process_and_update_status(
        db,
        content_id=data.content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
        info="创建 Series 元数据",
    )

    logger.info("创建 Series 元数据 | content_id={} type={}", data.content_id, content.content_type)
    return SeriesMetadataItem.model_validate(metadata)


@transactional
async def update_series_metadata(
    db: AsyncSession, content_id: int, data: SeriesMetadataUpdate,
    processed_by: str | None = None,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> SeriesMetadataItem:
    """更新 Series 元数据。"""
    metadata = await metadata_repo.get_series_metadata_by_content_id(db, content_id)
    if not metadata:
        raise NotFoundException(ErrorCode.SERIES_METADATA_NOT_FOUND, get_msg("SERIES_METADATA_NOT_FOUND"))

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # content = await _get_content_or_404(db, content_id)
    # await validate_metadata(
    #     db,
    #     entity_type="SERIES",
    #     data=data,
    #     content_type=content.content_type,
    #     content_id=content_id,  # 编辑模式：传入 content_id 用于合并数据
    # )

    update_data = data.model_dump(exclude_unset=True)
    # Update Childs 控制开关（各标签页独立控制）
    update_childs_main = update_data.pop("update_childs_main", False)
    update_childs_custom_fields = update_data.pop("update_childs_custom_fields", False)
    update_childs_i18n = update_data.pop("update_childs_i18n", False)
    logger.info(
        "更新 Series 元数据 | content_id={} update_childs_main={} update_childs_custom_fields={} update_childs_i18n={}",
        content_id, update_childs_main, update_childs_custom_fields, update_childs_i18n
    )
    # 提取 genre_ids 用于同步（它存储在 content_genre 中间表，不保存到元数据表）
    genre_ids_for_sync = update_data.get("genre_ids")
    update_data.pop("genre_ids", None)

    # 在 setattr 前检测实际变更：inspect().modified 对同值赋值也返回 True，
    # 会在“创建+同步连调”场景误写第二条父级流程记录
    has_metadata_changes = _has_real_changes(metadata, update_data)
    for key, value in update_data.items():
        setattr(metadata, key, value)

    # 同步 genre_ids 到 content_genre 中间表
    if genre_ids_for_sync is not None:
        await db.execute(
            ContentGenre.__table__.delete().where(ContentGenre.content_id == content_id)
        )
        for gid in genre_ids_for_sync:
            db.add(ContentGenre(content_id=content_id, genre_id=gid))

    # Update Childs: 各标签页独立控制
    if update_childs_main or update_childs_custom_fields or update_childs_i18n:
        logger.info(
            "Update Childs 条件满足，开始调用同步函数 | content_id={} main={} custom={} i18n={}",
            content_id, update_childs_main, update_childs_custom_fields, update_childs_i18n
        )
        await _sync_series_to_children(
            db, content_id,
            update_childs_main, update_childs_custom_fields, update_childs_i18n,
            processed_by,
            actor_id=actor_id,
            ip_address=ip_address,
        )
        logger.info("Update Childs 同步函数执行完成 | content_id={}", content_id)
    else:
        logger.info(
            "Update Childs 全部未开启，跳过同步 | content_id={}",
            content_id
        )

    await db.flush()
    await db.refresh(metadata)

    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(db, content_id, content.content_type, processed_by or "system", "修改元数据")

    # 检查是否有实际变更，避免仅保存自定义字段时也记录 Metadata 流程
    has_genre_changes = genre_ids_for_sync is not None

    # 用户主动保存元数据弹窗（含主表/自定义/多语言任一变更），每次都写一条 Passed 记录。
    # 流程记录与状态判断解耦：保存即记录，状态固定 Passed（用户主动确认即完成），
    # 不再以"主表是否有变更"或"是否已有 Passed 记录"作为是否写入的条件。
    await complete_process_and_update_status(
        db,
        content_id=content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
        info=("更新 Series 元数据" if (has_metadata_changes or has_genre_changes)
              else "确认 Series 元数据（仅自定义/多语言变更）"),
    )

    logger.info(
        "更新 Series 元数据完成 | content_id={} update_childs_main={} update_childs_custom_fields={} update_childs_i18n={}",
        content_id, update_childs_main, update_childs_custom_fields, update_childs_i18n
    )
    return SeriesMetadataItem.model_validate(metadata)


async def _sync_series_to_children(
    db: AsyncSession, parent_id: int,
    update_childs_main: bool = False,
    update_childs_custom_fields: bool = False,
    update_childs_i18n: bool = False,
    processed_by: str | None = None,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> None:
    """将 Series 元数据批量同步至子内容节点（递归同步所有层级）。

    同步策略：
        1. 从父级 content 表获取 title、genre_id
        2. 从父级元数据表读取全量字段快照（而非本次提交的字段 diff），
           确保子级缺失的必填字段（type_id/vod_type/rating_level 等）能一次补齐
        3. 从 content_custom_tag 表获取自定义标签
        4. 递归同步到所有子级（SEASON → SEASON_SERIES → EPISODE）
        5. 强制覆盖子级数据
        6. 记录操作日志

    排除字段：
        - volume_count, series_type, series_ordinal, show_id（系统维护）
        - cdr_id（每级内容独立生成的唯一标识，不可被父级覆盖）
        - 审计字段（created_at, updated_at 等）

    参数：
        parent_id: 父级内容 ID
    """
    # 1. 获取父级内容
    parent = await _get_content_or_404(db, parent_id)
    parent_type = parent.content_type

    logger.info(
        "Update Childs 开始同步 | parent_id={} parent_type={} parent_title={} main={} custom={} i18n={}",
        parent_id, parent_type, parent.title,
        update_childs_main, update_childs_custom_fields, update_childs_i18n,
    )

    if parent_type not in ("SERIES", "SEASON_SERIES", "SEASON"):
        logger.warning("Update Childs 仅支持 SERIES/SEASON 类型 | parent_id={} type={}", parent_id, parent_type)
        return

    # 2. 构建同步数据包（从 content_genre 中间表获取 genre_ids）
    genre_rows = await db.execute(
        select(ContentGenre.genre_id).where(ContentGenre.content_id == parent_id)
    )
    parent_genre_ids = [r[0] for r in genre_rows.all()]
    sync_content_fields = {
        "genre_ids": parent_genre_ids,
    }
    
    # 3. 根据开关按需获取父级数据
    parent_custom_tag_ids = []
    parent_field_values = {}
    parent_multilang_values = defaultdict(dict)
    parent_i18n_metadata = defaultdict(dict)
    
    if update_childs_custom_fields:
        # 获取父级自定义标签
        parent_tags_result = await db.execute(
            select(ContentCustomTag.custom_tag_id).where(
                ContentCustomTag.content_id == parent_id
            )
        )
        parent_custom_tag_ids = [row[0] for row in parent_tags_result.all()]
        
        # 获取父级自定义字段值
        parent_fields_result = await db.execute(
            select(EntityFieldValue).where(
                EntityFieldValue.entity_type == "Content",
                EntityFieldValue.entity_id == parent_id,
                EntityFieldValue.is_deleted.is_(False),
            )
        )
        parent_field_values = {
            row.custom_field_id: row.value
            for row in parent_fields_result.scalars().all()
            if row.value is not None
        }
        logger.info(
            "Update Childs 父级自定义字段 | parent_id={} field_count={} fields={}",
            parent_id, len(parent_field_values), parent_field_values
        )
        
        # 获取父级多语言自定义字段值（entity_type="Content", field_name='cf_' + field_code）
        parent_i18n_result = await db.execute(
            select(EntityI18n).where(
                EntityI18n.entity_type == "Content",
                EntityI18n.entity_id == parent_id,
                EntityI18n.field_name.startswith("cf_"),
                EntityI18n.is_deleted.is_(False),
            )
        )
        for row in parent_i18n_result.scalars().all():
            # field_name 即 field_code（如 "cf_3"）
            field_code = row.field_name
            parent_multilang_values[field_code][row.language] = row.value
        if parent_multilang_values:
            logger.info(
                "Update Childs 父级多语言自定义字段 | parent_id={} fields={}",
                parent_id, dict(parent_multilang_values)
            )
    
    if update_childs_i18n:
        # 获取父级元数据多语言字段值（名称、排序名、短标题、简介等）
        parent_content_i18n_result = await db.execute(
            select(EntityI18n).where(
                EntityI18n.entity_type == "Content",
                EntityI18n.entity_id == parent_id,
                EntityI18n.is_deleted.is_(False),
            )
        )
        for row in parent_content_i18n_result.scalars().all():
            parent_i18n_metadata[row.field_name][row.language] = row.value
        if parent_i18n_metadata:
            logger.info(
                "Update Childs 父级元数据多语言字段 | parent_id={} fields={}",
                parent_id, dict(parent_i18n_metadata)
            )
    
    
    # 4. 构建父级元数据完整快照（Main 标签）
    #    使用父级元数据表的全量列（同一事务内本次 setattr 更新已应用到该对象），
    #    而非本次提交的字段 diff，保证子级能拿到父级的所有字段值、缺失必填一次补齐
    sync_metadata_fields = {}
    if update_childs_main:
        from sqlalchemy import inspect as sa_inspect
        parent_meta = await metadata_repo.get_series_metadata_by_content_id(db, parent_id)
        if parent_meta is None:
            logger.warning("Update Childs 父级元数据不存在，跳过 Main 同步 | parent_id={}", parent_id)
        else:
            sync_metadata_fields = {
                attr.key: getattr(parent_meta, attr.key)
                for attr in sa_inspect(parent_meta).mapper.column_attrs
                if attr.key not in _UPDATE_CHILDS_EXCLUDE and attr.key != "content_id"
            }

    # 5. 递归同步到所有子级（各标签独立控制）
    synced_count = await _sync_recursive(
        db, parent_id, sync_content_fields, sync_metadata_fields,
        parent_custom_tag_ids, parent_field_values, parent_multilang_values, parent_i18n_metadata,
        parent.title, update_childs_main, update_childs_custom_fields, update_childs_i18n,
        processed_by=processed_by,
        actor_id=actor_id,
        ip_address=ip_address,
    )
    
    logger.info(
        "Update Childs 同步完成 | parent_id={} parent_type={} synced_children={}",
        parent_id, parent_type, synced_count
    )


async def _sync_recursive(
    db: AsyncSession,
    parent_id: int,
    sync_content_fields: dict,
    sync_metadata_fields: dict,
    parent_custom_tag_ids: list[int],
    parent_field_values: dict[int, str],
    parent_multilang_values: dict[str, dict[str, str]],
    parent_i18n_metadata: dict[str, dict[str, str]],
    parent_title: str,
    update_childs_main: bool = False,
    update_childs_custom_fields: bool = False,
    update_childs_i18n: bool = False,
    depth: int = 0,
    processed_by: str | None = None,
    actor_id: int | None = None,
    ip_address: str | None = None,
) -> int:
    """递归同步元数据到所有子级内容（各标签独立控制）。
    
    参数：
        parent_id: 父级内容 ID
        sync_content_fields: content 主表字段（genre_id）
        sync_metadata_fields: 元数据表字段（Main 标签）
        parent_custom_tag_ids: 父级自定义标签 ID 列表
        parent_field_values: 父级自定义字段值（Custom Fields 标签）
        parent_multilang_values: 父级多语言自定义字段值（Custom Fields 标签）
        parent_i18n_metadata: 父级元数据多语言字段（Multi Languages 标签）
        parent_title: 父级 title，用于同步到子级 metadata.name
        update_childs_main: 是否同步 Main 标签
        update_childs_custom_fields: 是否同步 Custom Fields 标签
        update_childs_i18n: 是否同步 Multi Languages 标签
        depth: 当前递归深度（用于日志）
    
    返回：
        同步的子级数量
    """
    # 查询直接子级
    children = (
        await db.execute(
            select(Content).where(
                Content.parent_id == parent_id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
    ).scalars().all()
    
    logger.info(
        "Update Childs 查询到子级 | parent_id={} children_count={} children=[{}]",
        parent_id, len(children), 
        ", ".join([f"{c.id}({c.content_type})" for c in children])
    )
    
    if not children:
        logger.warning("Update Childs 没有找到子级 | parent_id={}", parent_id)
        return 0
    
    synced_count = 0
    
    for child in children:
        # 为每个子级构建包含 name 的元数据同步字段
        sync_metadata_fields_with_name = {**sync_metadata_fields, "name": child.title}
        
        # Main 标签：同步 content 主表字段 + 元数据表字段
        if update_childs_main:
            # 1. 同步 content_genre 中间表（父级覆盖子级策略）
            parent_genre_ids = sync_content_fields.get("genre_ids", [])
            await db.execute(
                ContentGenre.__table__.delete().where(ContentGenre.content_id == child.id)
            )
            for gid in parent_genre_ids:
                db.add(ContentGenre(content_id=child.id, genre_id=gid))
        
        # Custom Fields 标签：同步自定义标签 + 自定义字段
        if update_childs_custom_fields:
            # 2. 同步自定义标签（删除旧标签，插入新标签）
            await _sync_custom_tags(db, child.id, parent_custom_tag_ids)
            # 2.5 同步自定义字段
            await _sync_custom_fields(db, child.id, child.content_type, parent_field_values, parent_multilang_values)
        
        # Multi Languages 标签：同步元数据多语言字段
        if update_childs_i18n:
            # 2.6 同步元数据多语言字段（名称、排序名、短标题、简介等）
            await _sync_i18n_metadata(db, child.id, parent_i18n_metadata)
        
        # Main 标签：同步元数据表字段
        child_meta_synced = False
        if update_childs_main:
            child_type = child.content_type
            if child_type in ("MOVIE", "EPISODE"):
                # EPISODE 使用 program_metadata 表
                child_meta = await metadata_repo.get_content_metadata_by_content_id(db, child.id)
                if child_meta:
                    # 元数据存在，更新
                    for key, value in sync_metadata_fields_with_name.items():
                        if hasattr(child_meta, key):
                            setattr(child_meta, key, value)
                    synced_count += 1
                    child_meta_synced = True
                    logger.info(
                        "Update Childs 同步 EPISODE | child_id={} synced_fields={}",
                        child.id, list(sync_metadata_fields_with_name.keys())
                    )
                else:
                    # 元数据不存在，创建新记录
                    logger.info("Update Childs EPISODE 元数据不存在，自动创建 | child_id={}", child.id)
                    from app.internal.cms_biz_orchestration.schemas.content_metadata import ContentMetadataCreate
                    metadata_create = ContentMetadataCreate(
                        content_id=child.id,
                        **{k: v for k, v in sync_metadata_fields_with_name.items() if v is not None}
                    )
                    # 排除 genre_ids 和 custom_tag_ids（它们在 content 主表和中间表，不在元数据表）
                    metadata_dict = metadata_create.model_dump(exclude={'genre_ids', 'custom_tag_ids'})
                    # 自动生成 CDR ID（如果未提供）
                    if not metadata_dict.get("cdr_id") or metadata_dict.get("cdr_id") == "":
                        metadata_dict["cdr_id"] = f"Program_{child.id}"
                    child_meta = ContentMetadata(**metadata_dict)
                    # 系统维护字段：同步创建的单集为连续剧成员（0=VOD(MOVIE), 1=Series(EPISODE)）
                    child_meta.series_flag = 1
                    db.add(child_meta)
                    synced_count += 1
                    child_meta_synced = True
            elif child_type in ("SERIES", "SEASON_SERIES", "SEASON"):
                # SERIES/SEASON 使用 series_metadata 表
                child_meta = await metadata_repo.get_series_metadata_by_content_id(db, child.id)
                if child_meta:
                    # 元数据存在，更新
                    for key, value in sync_metadata_fields_with_name.items():
                        if hasattr(child_meta, key):
                            setattr(child_meta, key, value)
                    synced_count += 1
                    child_meta_synced = True
                    logger.info(
                        "Update Childs 同步 SERIES/SEASON | child_id={} synced_fields={}",
                        child.id, list(sync_metadata_fields_with_name.keys())
                    )
                else:
                    # 元数据不存在，创建新记录
                    logger.info("Update Childs SERIES/SEASON 元数据不存在，自动创建 | child_id={}", child.id)
                    from app.internal.cms_biz_orchestration.schemas.content_metadata import SeriesMetadataCreate
                    metadata_create = SeriesMetadataCreate(
                        content_id=child.id,
                        **{k: v for k, v in sync_metadata_fields_with_name.items() if v is not None}
                    )
                    # 排除 genre_ids 和 custom_tag_ids（它们在 content 主表和中间表，不在元数据表）
                    metadata_dict = metadata_create.model_dump(exclude={'genre_ids', 'custom_tag_ids'})
                    # 自动生成 CDR ID（如果未提供）
                    if not metadata_dict.get("cdr_id") or metadata_dict.get("cdr_id") == "":
                        metadata_dict["cdr_id"] = f"Series_{child.id}"
                    child_meta = SeriesMetadata(**metadata_dict)
                    # 系统维护字段：按子级类型设置连续剧类型（1=SERIES, 2=单季, 3=总季），集/季数量初始化为 0
                    child_meta.series_type = {"SERIES": 1, "SEASON_SERIES": 2, "SEASON": 3}.get(child_type)
                    child_meta.volume_count = 0
                    db.add(child_meta)
                    synced_count += 1
                    child_meta_synced = True
        
        # 同步完成后按真实必填完备性评估流程状态（check_metadata 判定元数据记录存在即 Passed，
        # 与前端 Metadata 红绿勾口径一致）；skip_status_update 避免干扰下方子节点状态流转
        if child_meta_synced:
            await complete_process_and_update_status(
                db,
                content_id=child.id,
                content_type=child.content_type,
                process_name="Metadata",
                processed_by=processed_by,
                skip_status_update=True,
                info="Update Childs 同步元数据",
            )
            # 活动日志：归属子级（content_id=child.id），保证单季/单集详情页 Activity Log 可见；
            # 复用现有 PROGRAM/SERIES_METADATA_UPDATE 枚举，前端映射零改动
            from app.internal.cms_biz_system.services.operation_log_service import write_log, OperationType
            op_type = (
                OperationType.PROGRAM_METADATA_UPDATE
                if child.content_type in ("MOVIE", "EPISODE")
                else OperationType.SERIES_METADATA_UPDATE
            )
            await write_log(
                db,
                user_id=actor_id,
                user_name=processed_by,
                operation_type=op_type,
                operation_object_code="OBJ_CONTENT", operation_object_params={"name": child.title},
                operation_content_code="log.metadata.sync",
                operation_content_params={"title": parent_title},
                content_id=child.id,
                entity_type="content",
                entity_id=child.id,
                updated_value=f"由「{parent_title}」同步元数据",
                ip_address=ip_address,
                result="success",
            )
        
        # 4. 递归同步子级的子级（传递标签控制开关）
        child_synced = await _sync_recursive(
            db, child.id, sync_content_fields, sync_metadata_fields,
            parent_custom_tag_ids, parent_field_values, parent_multilang_values, parent_i18n_metadata,
            parent_title, update_childs_main, update_childs_custom_fields, update_childs_i18n,
            depth + 1, processed_by=processed_by,
            actor_id=actor_id,
            ip_address=ip_address,
        )
        synced_count += child_synced

        # 5. 子内容状态为 None 时，根据素材情况决定新状态：
        #    - 无素材 → WaitingForMaterials
        #    - 有素材 → InProgress
        #    其他状态（Published/ReadyForPublish 等）保持不变
        if child.status == "None":
            from app.internal.cms_biz_orchestration.services.content_status_service import ContentStatusService
            should_waiting = await ContentStatusService.should_be_waiting_for_materials(
                db, child.id, child.content_type
            )
            new_status = "WaitingForMaterials" if should_waiting else "InProgress"
            child.status = new_status
            await record_status_change(
                db,
                content_id=child.id,
                before_status="None",
                after_status=new_status,
                processed_by=processed_by or "system",
            )

    return synced_count


async def _sync_custom_fields(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    parent_field_values: dict[int, str],
    parent_multilang_values: dict[str, dict[str, str]],
) -> None:
    """同步自定义字段到子级（包含普通字段和多语言字段）。
    
    策略：
    1. 查询子级内容类型可用的自定义字段（通过 custom_field_belonging）
    2. 普通字段：从 entity_field_value 表同步
    3. 多语言字段：从 entity_i18n 表同步
    4. 子级已有则更新，没有则创建
    
    参数：
        db: 数据库会话
        content_id: 子级内容 ID
        content_type: 子级内容类型（EPISODE、SERIES 等）
        parent_field_values: 父级普通自定义字段值 {custom_field_id: value}
        parent_multilang_values: 父级多语言自定义字段值 {field_code: {language: value}}
    """
    if not parent_field_values and not parent_multilang_values:
        logger.info("Update Childs 父级无自定义字段，跳过同步 | child_id={}", content_id)
        return
    
    # content_type 到 CustomFieldBelonging.belonging 的映射
    # 统一转为小写比较，避免大小写和命名别名不一致
    from sqlalchemy import func
    _CONTENT_TYPE_TO_BELONGING: dict[str, list[str]] = {
        "MOVIE": ["program", "movie"],
        "EPISODE": ["program"],
        "SERIES": ["series"],
        "SEASON_SERIES": ["series"],
        "SEASON": ["series"],
        "CHANNEL": ["channel"],
        "SCHEDULE": ["schedule"],
    }
    belonging_values = _CONTENT_TYPE_TO_BELONGING.get(content_type, [content_type.lower()])
    belonging_values.append("all")
    
    from app.internal.cms_biz_metada.models.basic import CustomFieldBelonging, CustomField
    
    # 1. 查询子级内容类型可用的自定义字段
    available_fields_result = await db.execute(
        select(CustomFieldBelonging.custom_field_id).where(
            func.lower(CustomFieldBelonging.belonging).in_(belonging_values),
            CustomFieldBelonging.is_deleted.is_(False),
        )
    )
    available_field_ids = set(row[0] for row in available_fields_result.all())
    
    if not available_field_ids:
        logger.info("Update Childs 子级类型无可用的自定义字段 | child_id={} content_type={}", content_id, content_type)
        return
    
    # 2. 获取可用字段的详细配置（field_code、multi_language）
    fields_config_result = await db.execute(
        select(CustomField).where(
            CustomField.id.in_(available_field_ids),
            CustomField.is_deleted.is_(False),
        )
    )
    fields_config = {cf.id: cf for cf in fields_config_result.scalars().all()}
    
    logger.info(
        "Update Childs 自定义字段配置 | child_id={} fields_config={}",
        content_id, {fid: (cf.field_code, cf.multi_language) for fid, cf in fields_config.items()}
    )
    
    synced_count = 0
    created_count = 0
    
    # 3. 同步普通自定义字段（非多语言）
    if parent_field_values:
        # 过滤出父级中适用于子级的非多语言字段
        normal_fields_to_sync = {
            fid: value for fid, value in parent_field_values.items()
            if fid in available_field_ids
        }
        
        if normal_fields_to_sync:
            # 3.1 查询子级现有的普通自定义字段
            child_fields_result = await db.execute(
                select(EntityFieldValue).where(
                    EntityFieldValue.entity_type == "Content",
                    EntityFieldValue.entity_id == content_id,
                    EntityFieldValue.is_deleted.is_(False),
                )
            )
            child_fields = child_fields_result.scalars().all()
            child_field_map = {field.custom_field_id: field for field in child_fields}
            
            for field_id, parent_value in normal_fields_to_sync.items():
                if field_id in child_field_map:
                    child_field = child_field_map[field_id]
                    child_field.value = parent_value
                    synced_count += 1
                else:
                    new_field = EntityFieldValue(
                        entity_type="Content",
                        entity_id=content_id,
                        custom_field_id=field_id,
                        value=parent_value,
                    )
                    db.add(new_field)
                    created_count += 1
    
    # 4. 同步多语言自定义字段
    if parent_multilang_values:
        # 找出可用字段中的多语言字段，建立 field_code -> field_id 映射
        multilang_field_map = {
            cf.field_code: cf.id
            for cf in fields_config.values()
            if cf.multi_language and cf.field_code in parent_multilang_values
        }
        
        if multilang_field_map:
            for field_code, field_id in multilang_field_map.items():
                lang_values = parent_multilang_values[field_code]
                
                # 查询子级现有的多语言字段值
                child_i18n_result = await db.execute(
                    select(EntityI18n).where(
                        EntityI18n.entity_type == "Content",
                        EntityI18n.entity_id == content_id,
                        EntityI18n.field_name == field_code,
                        EntityI18n.is_deleted.is_(False),
                    )
                )
                child_i18n_rows = child_i18n_result.scalars().all()
                child_i18n_map = {row.language: row for row in child_i18n_rows}
                
                for lang, value in lang_values.items():
                    if lang in child_i18n_map:
                        child_i18n_map[lang].value = value
                        synced_count += 1
                    else:
                        new_i18n = EntityI18n(
                            entity_type="Content",
                            entity_id=content_id,
                            field_name=field_code,
                            language=lang,
                            value=value,
                        )
                        db.add(new_i18n)
                        created_count += 1
    
    if synced_count > 0 or created_count > 0:
        logger.info(
            "Update Childs 同步自定义字段完成 | child_id={} synced={} created={}",
            content_id, synced_count, created_count
        )
    else:
        logger.info(
            "Update Childs 父级字段不适用于子级类型，跳过 | child_id={} content_type={}",
            content_id, content_type
        )


async def _sync_i18n_metadata(
    db: AsyncSession,
    content_id: int,
    parent_i18n_metadata: dict[str, dict[str, str]],
) -> None:
    """同步元数据多语言字段到子级。
    
    同步 entity_i18n 表中 entity_type="Content" 的多语言值
    （包括名称、排序名、短标题、简介等字段的各语言翻译）。
    
    策略：子级存在则更新，不存在则创建。
    """
    if not parent_i18n_metadata:
        return
    
    # 查询子级已有的 EntityI18n（Content 类型）
    child_i18n_result = await db.execute(
        select(EntityI18n).where(
            EntityI18n.entity_type == "Content",
            EntityI18n.entity_id == content_id,
            EntityI18n.is_deleted.is_(False),
        )
    )
    child_i18n_rows = child_i18n_result.scalars().all()
    child_i18n_map = {(row.field_name, row.language): row for row in child_i18n_rows}
    
    synced_count = 0
    created_count = 0
    
    for field_name, lang_values in parent_i18n_metadata.items():
        for language, value in lang_values.items():
            key = (field_name, language)
            if key in child_i18n_map:
                child_i18n_map[key].value = value
                synced_count += 1
            else:
                new_i18n = EntityI18n(
                    entity_type="Content",
                    entity_id=content_id,
                    field_name=field_name,
                    language=language,
                    value=value,
                )
                db.add(new_i18n)
                created_count += 1
    
    if synced_count > 0 or created_count > 0:
        logger.info(
            "Update Childs 同步元数据多语言字段 | child_id={} synced={} created={}",
            content_id, synced_count, created_count
        )


@transactional
async def delete_series_metadata(db: AsyncSession, content_id: int) -> None:
    """软删除 Series 元数据。"""
    await metadata_repo.delete_series_metadata_by_content_id(db, content_id)
    logger.info("删除 Series 元数据 | content_id={}", content_id)
    # 删除元数据属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(
        db, content_id, content.content_type, "system", "删除元数据"
    )


# ═══════════════════════════════════════════════════════════
# ChannelMetadata — Channel (CHANNEL)
# ═══════════════════════════════════════════════════════════

async def get_channel_metadata(db: AsyncSession, content_id: int) -> ChannelMetadataItem | None:
    """查询 Channel 元数据。"""
    metadata = await metadata_repo.get_channel_metadata_by_content_id(db, content_id)
    if not metadata:
        return None
    return ChannelMetadataItem.model_validate(metadata)


@transactional
async def create_channel_metadata(
    db: AsyncSession, data: ChannelMetadataCreate, processed_by: str | None = None
) -> ChannelMetadataItem:
    """创建 Channel 元数据。"""
    content = await _get_content_or_404(db, data.content_id)
    if content.content_type != "CHANNEL":
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTENT_TYPE_INVALID"), 400)

    existing = await metadata_repo.get_channel_metadata_by_content_id(db, data.content_id)
    if existing:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CHANNEL_METADATA_EXISTS"), 400)

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="CHANNEL",
    #     data=data,
    #     content_type=content.content_type,
    # )

    dump = data.model_dump()
    if not dump.get("name"):
        dump["name"] = content.title

    metadata = ChannelMetadata(**dump)
    await metadata_repo.add_channel_metadata(db, metadata)
    await db.flush()
    await db.refresh(metadata)

    # 用户主动创建元数据弹窗，直接记 Passed（用户确认即完成）
    await complete_process_and_update_status(
        db,
        content_id=data.content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
    )

    logger.info("创建 Channel 元数据 | content_id={}", data.content_id)
    return ChannelMetadataItem.model_validate(metadata)


@transactional
async def update_channel_metadata(
    db: AsyncSession, content_id: int, data: ChannelMetadataUpdate, processed_by: str | None = None
) -> ChannelMetadataItem:
    """更新 Channel 元数据。"""
    metadata = await metadata_repo.get_channel_metadata_by_content_id(db, content_id)
    if not metadata:
        raise NotFoundException(ErrorCode.CHANNEL_METADATA_NOT_FOUND, get_msg("CHANNEL_METADATA_NOT_FOUND"))

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="CHANNEL",
    #     data=data,
    #     content_id=content_id,  # 编辑模式：传入 content_id 用于合并数据
    # )

    update_data = data.model_dump(exclude_unset=True)
    # 在 setattr 前检测实际变更（inspect().modified 对同值赋值也返回 True）
    has_metadata_changes = _has_real_changes(metadata, update_data)
    for key, value in update_data.items():
        setattr(metadata, key, value)

    await db.flush()
    await db.refresh(metadata)

    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(db, content_id, content.content_type, processed_by or "system", "修改元数据")

    # 用户主动保存元数据弹窗（含主表/自定义/多语言任一变更），每次都写一条 Passed 记录。
    # 流程记录与状态判断解耦：保存即记录，状态固定 Passed（用户主动确认即完成），
    # 不再以"主表是否有变更"或"是否已有 Passed 记录"作为是否写入的条件。
    await complete_process_and_update_status(
        db,
        content_id=content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
        info="更新 Channel 元数据" if has_metadata_changes else "确认 Channel 元数据（仅自定义/多语言变更）",
    )

    logger.info("更新 Channel 元数据 | content_id={}", content_id)
    return ChannelMetadataItem.model_validate(metadata)


@transactional
async def delete_channel_metadata(db: AsyncSession, content_id: int) -> None:
    """软删除 Channel 元数据。"""
    await metadata_repo.delete_channel_metadata_by_content_id(db, content_id)
    logger.info("删除 Channel 元数据 | content_id={}", content_id)
    # 删除元数据属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(
        db, content_id, content.content_type, "system", "删除元数据"
    )


# ═══════════════════════════════════════════════════════════
# ScheduleMetadata — Schedule (SCHEDULE)
# ═══════════════════════════════════════════════════════════

async def get_schedule_metadata(db: AsyncSession, content_id: int) -> ScheduleMetadataItem | None:
    """查询 Schedule 元数据。"""
    metadata = await metadata_repo.get_schedule_metadata_by_content_id(db, content_id)
    if not metadata:
        return None
    return ScheduleMetadataItem.model_validate(metadata)


@transactional
async def create_schedule_metadata(
    db: AsyncSession, data: ScheduleMetadataCreate, processed_by: str | None = None
) -> ScheduleMetadataItem:
    """创建 Schedule 元数据。"""
    content = await _get_content_or_404(db, data.content_id)
    if content.content_type != "SCHEDULE":
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("CONTENT_TYPE_INVALID"), 400)

    existing = await metadata_repo.get_schedule_metadata_by_content_id(db, data.content_id)
    if existing:
        raise BusinessException(ErrorCode.VALIDATION_ERROR, get_msg("SCHEDULE_METADATA_EXISTS"), 400)

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # await validate_metadata(
    #     db,
    #     entity_type="SCHEDULE",
    #     data=data,
    #     content_type=content.content_type,
    # )

    dump = data.model_dump()
    if not dump.get("name"):
        dump["name"] = content.title
    # 自动生成 CDR ID: Schedule_{content_id}
    if not dump.get("cdr_id") or dump.get("cdr_id") == "":
        dump["cdr_id"] = f"Schedule_{data.content_id}"

    metadata = ScheduleMetadata(**dump)
    await metadata_repo.add_schedule_metadata(db, metadata)
    await db.flush()
    await db.refresh(metadata)

    # 同步 Schedule 专属字段到 content 主表，保证列表查询一致
    if data.cutv_enable is not None:
        content.cutv_enable = data.cutv_enable

    # 用户主动创建元数据弹窗，直接记 Passed（用户确认即完成）
    await complete_process_and_update_status(
        db,
        content_id=data.content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
    )

    logger.info("创建 Schedule 元数据 | content_id={}", data.content_id)
    return ScheduleMetadataItem.model_validate(metadata)


@transactional
async def update_schedule_metadata(
    db: AsyncSession, content_id: int, data: ScheduleMetadataUpdate, processed_by: str | None = None
) -> ScheduleMetadataItem:
    """更新 Schedule 元数据。"""
    metadata = await metadata_repo.get_schedule_metadata_by_content_id(db, content_id)
    if not metadata:
        raise NotFoundException(ErrorCode.SCHEDULE_METADATA_NOT_FOUND, get_msg("SCHEDULE_METADATA_NOT_FOUND"))

    # ⚠️ 实时校验已禁用，改为定时任务执行
    # content = await _get_content_or_404(db, content_id)
    # await validate_metadata(
    #     db,
    #     entity_type="SCHEDULE",
    #     data=data,
    #     content_type=content.content_type,
    #     content_id=content_id,  # 编辑模式：传入 content_id 用于合并数据
    # )

    update_data = data.model_dump(exclude_unset=True)

    # 在 setattr 前检测实际变更（inspect().modified 对同值赋值也返回 True）
    has_metadata_changes = _has_real_changes(metadata, update_data)
    for key, value in update_data.items():
        setattr(metadata, key, value)

    # 同步回 content 主表的字段：cutv_enable
    # genre_id / begin_time / end_time 已移至 content 主表，不再从 metadata 同步
    sync_keys = {"cutv_enable"}
    content = await _get_content_or_404(db, content_id)
    if sync_keys & update_data.keys():
        for k in sync_keys:
            if k in update_data:
                setattr(content, k, update_data[k])

    await db.flush()
    await db.refresh(metadata)

    if content.status == "Published":
        await rollback_after_published_edit(db, content_id, content.content_type, processed_by or "system", "修改元数据")

    # 用户主动保存元数据弹窗（含主表/自定义/多语言任一变更），每次都写一条 Passed 记录。
    # 流程记录与状态判断解耦：保存即记录，状态固定 Passed（用户主动确认即完成），
    # 不再以"主表是否有变更"或"是否已有 Passed 记录"作为是否写入的条件。
    await complete_process_and_update_status(
        db,
        content_id=content_id,
        content_type=content.content_type,
        process_name="Metadata",
        processed_by=processed_by,
        record_status="Passed",
        info="更新 Schedule 元数据" if has_metadata_changes else "确认 Schedule 元数据（仅自定义/多语言变更）",
    )

    logger.info("更新 Schedule 元数据 | content_id={}", content_id)
    return ScheduleMetadataItem.model_validate(metadata)


@transactional
async def delete_schedule_metadata(db: AsyncSession, content_id: int) -> None:
    """软删除 Schedule 元数据。"""
    await metadata_repo.delete_schedule_metadata_by_content_id(db, content_id)
    logger.info("删除 Schedule 元数据 | content_id={}", content_id)
    # 删除元数据属于节点数据变更，回退内容状态（已发布/准备发布等 → InProgress）
    content = await _get_content_or_404(db, content_id)
    await rollback_after_published_edit(
        db, content_id, content.content_type, "system", "删除元数据"
    )


# ═══════════════════════════════════════════════════════════
# 统一查询接口（内容详情页使用）
# ═══════════════════════════════════════════════════════════

async def get_metadata_detail(db: AsyncSession, content_id: int) -> MetadataDetailItem:
    """
    查询内容详情页元数据。

    根据 content.content_type 自动路由到对应的元数据表，
    返回统一结构的 MetadataDetailItem。
    """
    content = await _get_content_or_404(db, content_id)
    content_type = content.content_type

    result = MetadataDetailItem(
        content_type=content_type,
        content_name=content.title,
        content_id=content_id,
    )

    if content_type in ("MOVIE", "EPISODE"):
        result.program = await get_content_metadata(db, content_id)
    elif content_type in ("SERIES", "SEASON", "SEASON_SERIES"):
        result.series = await get_series_metadata(db, content_id)
    elif content_type == "CHANNEL":
        result.channel = await get_channel_metadata(db, content_id)
    elif content_type == "SCHEDULE":
        result.schedule = await get_schedule_metadata(db, content_id)

    logger.info("查询元数据详情 | content_id={} type={}", content_id, content_type)
    return result
