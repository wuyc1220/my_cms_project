"""
Category 同步构建器。

独立于内容发布流程，专门将 Category（栏目）+ Picture（海报）及其 Mapping
生成符合 C2 规范的 ADI XML，用于"Category同步给业务系统"功能。

同步策略（基于 object_publish_status 表跟踪）：
    - 从未同步过 → REGIST（输出完整 Object）
    - 已同步过且无变更（updated_at <= last_publish_time）→ SKIP（仅输出 Mapping）
    - 已同步过且有变更 → UPDATE
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from datetime import datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Category, Picture, PosterSize
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_publish.repositories import publish_repository
from app.soap.config import soap_settings
from app.config import app_tz

from . import objects as obj_builders
from .constants import Action, ElementType, PICTURE_ENTITY_TYPE_TO_ELEMENT
from .mappings import add_mapping

from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_publish.models.object_publish_status import ObjectPublishStatus


# ═══════════════════════════════════════════════════════════
# 同步上下文
# ═══════════════════════════════════════════════════════════

@dataclass
class CategorySyncContext:
    """Category 同步上下文，承载从数据库加载的全部业务数据"""

    categories: list[Category] = field(default_factory=list)
    pictures: list[Picture] = field(default_factory=list)

    # 多语言
    languages: list[str] = field(default_factory=list)
    primary_language: str = "en"

    # Category 多语言数据 {category_id: {field_name: {language: value}}}
    category_i18n_data: dict = field(default_factory=dict)

    # PosterSize 名称 {picture_id: poster_size_name}
    poster_size_names: dict = field(default_factory=dict)

    # 自定义字段 {category_id: {"extendinfo": json_str}}
    # 无自定义字段值的实体不包含 extendinfo key
    category_custom_fields: dict = field(default_factory=dict)

    # Picture 自定义字段 {poster_size_id: {"extendinfo": json_str}}
    # Picture 的扩展字段挂在其所属的海报规格(PosterSize)上
    picture_custom_fields: dict = field(default_factory=dict)

    # PosterSize mapping_type 映射 {picture_id: mapping_type}
    poster_size_mapping_types: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# 数据加载器
# ═══════════════════════════════════════════════════════════

async def load_category_sync_context(
    db: AsyncSession,
    category_ids: list[int],
) -> CategorySyncContext:
    """
    以 category_ids 为入口，加载同步所需的全部数据。

    :param db: 数据库会话
    :param category_ids: 要同步的 Category ID 列表
    :return: 聚合完成的 CategorySyncContext
    """
    ctx = CategorySyncContext()

    # ── 1. 加载 Category ───────────────────────────────
    stmt = select(Category).where(
        Category.id.in_(category_ids),
        Category.is_deleted.is_(False),
    )
    ctx.categories = list((await db.execute(stmt)).scalars().all())

    if not ctx.categories:
        return ctx

    # ── 2. 多语言 ──────────────────────────────────────
    ctx.languages, ctx.primary_language = await _load_languages(db)

    # ── 3. Category 多语言数据 ──────────────────────────
    ctx.category_i18n_data = await _load_category_i18n_data(
        db, ctx.categories, ctx.languages, ctx.primary_language,
    )

    # ── 4. 加载关联的 Picture ──────────────────────────
    # Picture 通过 entity_type="category" + entity_id 关联
    all_pictures: list[Picture] = []
    for cat in ctx.categories:
        pics = await _get_pictures_for_entity(db, "category", cat.id)
        all_pictures.extend(pics)
    ctx.pictures = all_pictures

    # ── 5. PosterSize 名称映射 ─────────────────────────
    ctx.poster_size_names, ctx.poster_size_mapping_types = await _load_poster_size_names(db, ctx.pictures)

    # ── 6. Category 自定义字段 ────────────────────────
    ctx.category_custom_fields = await _load_category_custom_fields(
        db, [cat.id for cat in ctx.categories], ctx.languages, ctx.primary_language,
    )

    # ── 7. Picture 自定义字段（按所属海报规格加载，entity_type="poster_size"） ──
    from .loader import _load_custom_fields_for_entities
    if ctx.pictures:
        ctx.picture_custom_fields = await _load_custom_fields_for_entities(
            db, "poster_size",
            list({p.poster_size_id for p in ctx.pictures if p.poster_size_id}),
            ctx.languages, ctx.primary_language,
        )

    return ctx


# ═══════════════════════════════════════════════════════════
# Category 同步构建器
# ═══════════════════════════════════════════════════════════

class CategorySyncBuilder:
    """
    Category 同步 ADI XML 构建器。

    使用示例::

        builder = CategorySyncBuilder(db)
        result = await builder.build_sync(category_ids=[1, 2, 3])
        # result = {"xml_str": "...", "file_path": "c2/xxx.xml",
        #           "stats": {"regist": 2, "update": 1, "skip": 0}}
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_sync(
        self,
        category_ids: list[int],
    ) -> dict:
        """
        构建 Category 同步 ADI XML。

        1. 加载数据 → 2. 判断每个对象的 Action → 3. 构建 XML → 4. 上传 SFTP
        → 5. 更新 object_publish_status → 6. 返回结果

        :param category_ids: 要同步的 Category ID 列表
        :return: {
            "xml_str": str,         # 生成的 XML 字符串
            "file_path": str,       # SFTP 路径
            "stats": dict,          # {regist: N, update: N, skip: N}
            "synced_ids": list[int] # 实际输出的 Category ID（非 SKIP）
        }
        """
        ctx = await load_category_sync_context(self.db, category_ids)
        if not ctx.categories:
            logger.warning(f"未找到待同步的 Category: {category_ids}")
            return {
                "xml_str": "",
                "file_path": "",
                "stats": {"regist": 0, "update": 0, "skip": 0},
                "synced_ids": [],
            }

        # ── 构建 XML ──────────────────────────────────
        root = self._build_root()
        objects_el = SubElement(root, "Objects")
        mappings_el = SubElement(root, "Mappings")

        stats = {"regist": 0}
        synced_entity_statuses: list[tuple[str, int, str]] = []

        category_actions: dict[int, str] = {}
        picture_actions: dict[int, str] = {}

        # bug 32017：PosterType / JumpCategoryCode 字段先保留传空（由
        # build_category_object 内部统一输出空 Property），无需再从首张海报
        # 预计算 mapping_type；待 C2 规范明确后可在此重新接入。

        # ── 处理 Category ──────────────────────────────
        for cat in ctx.categories:
            category_actions[cat.id] = Action.REGIST.value
            cat_i18n = ctx.category_i18n_data.get(cat.id)

            logger.info(f"[CategorySync] Category#{cat.id} → REGIST")
            objects_el.append(
                obj_builders.build_category_object(
                    cat, Action.REGIST,
                    i18n_data=cat_i18n,
                    languages=ctx.languages,
                    primary_language=ctx.primary_language,
                    custom_fields=ctx.category_custom_fields.get(cat.id),
                )
            )
            stats["regist"] += 1
            synced_entity_statuses.append(("Category", cat.id, "REGIST"))

        # ── 处理 Picture（Category 关联的海报） ────────
        # 记录需要更新状态的海报
        pictures_to_update = []
        
        for pic in ctx.pictures:
            picture_actions[pic.id] = Action.REGIST.value

            poster_size_name = ctx.poster_size_names.get(pic.id)
            objects_el.append(
                obj_builders.build_picture_object(
                    pic, Action.REGIST,
                    poster_size_name=poster_size_name,
                    custom_fields=ctx.picture_custom_fields.get(pic.poster_size_id, {}),
                )
            )
            stats["regist"] += 1
            synced_entity_statuses.append(("Picture", pic.id, "REGIST"))

            pic_target_et = PICTURE_ENTITY_TYPE_TO_ELEMENT.get(
                pic.entity_type.lower(), ElementType.CATEGORY,
            )
            pic_mapping_type = ctx.poster_size_mapping_types.get(pic.id)
            add_mapping(
                self._maps_list(mappings_el),
                ElementType.PICTURE, pic.id,
                pic_target_et, pic.entity_id,
                Action.REGIST,
                mapping_type=pic_mapping_type,
            )
            
            # 记录需要更新状态的海报
            pictures_to_update.append(pic)
        
        # 更新海报的发布状态（设置为 processing，等待后续更新为 success/failed）
        if pictures_to_update:
            await self._update_pictures_ingest_status(pictures_to_update, "processing")

        xml_str = _serialize(root)

        # ── 上传到 SFTP ────────────────────────────────
        file_path = self._write_to_file(xml_str)

        # ── 生成 CorrelateID 与 CmdFileURL ─────────────
        correlate_id = uuid.uuid4().hex
        cmd_file_url = self._build_cmd_file_url(file_path)

        # ── 创建注入历史记录（初始 status="failure"，等待 LSP 回调更新）──
        await self._create_ingest_history_records(
            ctx, file_path, category_actions, picture_actions,
            correlate_id=correlate_id,
        )

        # ── 创建 PublishTask ───────────────────────────
        # 取首个 Category 作为任务实体
        first_cat = ctx.categories[0]
        now = datetime.now()
        publish_task = PublishTask(
            entity_type="Category",
            entity_id=first_cat.id,
            entity_name=first_cat.name,
            task_type="publish",
            execution_mode="now",
            status="processing",
            publish_status="publishing",
            correlate_id=correlate_id,
            ingest_xml_path=file_path,
        )
        await publish_repository.create_publish_task(self.db, publish_task)

        # ── 调用 SOAP ExecCmdReq 通知 LSP ──────────────
        soap_success = False
        soap_error = None
        if soap_settings.enabled:
            try:
                from app.soap.client import SOAPClient
                soap_client = SOAPClient()
                soap_result = soap_client.send_exec_cmd_req(
                    cmd_file_url=cmd_file_url,
                    correlate_id=correlate_id,
                )
                soap_success = soap_result["success"]
                soap_error = soap_result.get("error_description")
                logger.info(
                    f"[CategorySync] SOAP ExecCmd 结果 - success={soap_success}, "
                    f"CorrelateID={correlate_id}"
                )
            except Exception as e:
                soap_error = str(e)
                logger.error(f"[CategorySync] SOAP 调用异常: {e}")
        else:
            # SOAP 未启用，模拟成功
            logger.warning(f"[CategorySync] SOAP 未启用，模拟通知成功 - CorrelateID={correlate_id}")
            soap_success = True

        # ── 根据 SOAP 结果更新状态 ─────────────────────
        if soap_success and soap_settings.enabled:
            from sqlalchemy import update as sa_update
            ingest_stmt = (
                sa_update(Category)
                .where(Category.id.in_(category_ids))
                .values(ingest_status="processing", updated_at=Category.updated_at)
            )
            await self.db.execute(ingest_stmt)
            logger.info(
                f"[CategorySync] SOAP 通知已发送，等待 LSP 回调 | "
                f"CorrelateID={correlate_id}"
            )
        elif soap_success and not soap_settings.enabled:
            # SOAP 未启用，直接标记为成功
            from sqlalchemy import update as sa_update
            ingest_stmt = (
                sa_update(Category)
                .where(Category.id.in_(category_ids))
                .values(ingest_status="success", updated_at=Category.updated_at)
            )
            await self.db.execute(ingest_stmt)
            publish_task.status = "success"
            publish_task.publish_status = "success"
            await publish_repository.update_publish_task(self.db, publish_task)
            # 模拟模式无 LSP 回调，同步终态 IngestHistory，避免列表 success 而历史弹窗 failure
            await self._finalize_ingest_histories(correlate_id, "success")
            logger.info(
                f"[CategorySync] SOAP 未启用，直接标记同步成功 | "
                f"CorrelateID={correlate_id}"
            )
        else:
            from sqlalchemy import update as sa_update
            ingest_stmt = (
                sa_update(Category)
                .where(Category.id.in_(category_ids))
                .values(ingest_status="failure", updated_at=Category.updated_at)
            )
            await self.db.execute(ingest_stmt)
            publish_task.status = "failure"
            publish_task.publish_status = "failure"
            publish_task.error_message = f"SOAP 调用失败: {soap_error}"
            await publish_repository.update_publish_task(self.db, publish_task)
            # SOAP 调用失败不会收到 LSP 回调，同步终态 IngestHistory 并补齐结束时间
            await self._finalize_ingest_histories(correlate_id, "failure")
            logger.error(
                f"[CategorySync] SOAP 调用失败，任务标记为失败 | "
                f"CorrelateID={correlate_id}, Error: {soap_error}"
            )

        # ── 更新 object_publish_status（不含 commit）───
        await self._update_publish_statuses(synced_entity_statuses)

        # ── 统一提交 ──────────────────────────────────
        await self.db.commit()

        logger.info(
            f"[CategorySync] 同步完成 | categories={len(ctx.categories)} "
            f"pictures={len(ctx.pictures)} | stats={stats} | "
            f"soap_success={soap_success} CorrelateID={correlate_id}"
        )

        return {
            "xml_str": xml_str,
            "file_path": file_path,
            "stats": stats,
            "synced_ids": [cat.id for cat in ctx.categories],
            "correlate_id": correlate_id,
            "soap_success": soap_success,
            "soap_error": soap_error,
        }

    # ═══════════════════════════════════════════════════
    # 内部方法
    # ═══════════════════════════════════════════════════

    def _build_root(self) -> Element:
        """生成 <ADI> 根节点（含 xmlns 与消息头）"""
        root = Element(
            "ADI",
            {
                "xmlns": "http://www.ChinaDTV.cn/CDTVStandard/ADI",
                "Version": "1.0",
            },
        )
        header = SubElement(root, "Header")
        SubElement(header, "MsgID").text = str(uuid.uuid4())
        SubElement(header, "CSPID").text = soap_settings.csp_id
        SubElement(header, "LSPID").text = soap_settings.lsp_id
        SubElement(header, "Timestamp").text = datetime.now(app_tz).replace(tzinfo=None).isoformat()
        return root

    def _write_to_file(self, xml_str: str) -> str:
        """
        将 XML 字符串上传到 SFTP 的 c2 目录。

        文件名格式: ``c2/YYYYMM/category_sync_{uuid8}_{timestamp}.xml``

        :return: SFTP 相对路径（c2/YYYYMM/xxx.xml）
        """
        from .file_utils import generate_c2_filename, upload_c2_xml

        # Category 同步没有特定的 entity，使用通用的 category_sync 前缀
        filename = generate_c2_filename(
            prefix="category_sync",
            entity_type="category",
            entity_id=0,
        )

        return upload_c2_xml(xml_str, filename, storage_service)

    @staticmethod
    def _build_cmd_file_url(file_path: str) -> str:
        """
        根据 file_path 构建 LSP 可访问的 CmdFileURL。

        优先级：SOAP_CMD_FILE_URL_PREFIX > FTP/SFTP 自动拼接
        """
        from pathlib import Path
        from app.config import settings

        if soap_settings.cmd_file_url_prefix:
            return f"{soap_settings.cmd_file_url_prefix.rstrip('/')}/{file_path}"
        elif settings.storage_type == "ftp" and settings.file_host:
            ftp_prefix = (
                f"ftp://{settings.file_username}:{settings.file_password}"
                f"@{settings.file_host}:{settings.file_port}"
                f"{settings.file_base_path.rstrip('/')}"
            )
            return f"{ftp_prefix}/{file_path}"
        elif settings.storage_type == "sftp" and settings.file_host:
            sftp_prefix = (
                f"sftp://{settings.file_username}:{settings.file_password}"
                f"@{settings.file_host}:{settings.file_port}"
                f"{settings.file_base_path.rstrip('/')}"
            )
            return f"{sftp_prefix}/{file_path}"
        else:
            raise RuntimeError(
                "未配置 FTP/SFTP 连接信息，无法构建 XML 文件访问 URL。"
                "请配置 storage_type、file_host 等参数，或设置 SOAP_CMD_FILE_URL_PREFIX。"
            )

    async def _update_pictures_ingest_status(
        self,
        pictures: list,
        status: str,
    ) -> None:
        """
        更新海报的注入状态（仅更新 Picture 表的 ingest_status 字段）。
        详细的发布历史（XML 路径等）由 _create_ingest_history_records 方法保存到 IngestHistory 表。

        Args:
            pictures: 海报列表
            status: 状态 (none/processing/success/failed)
        """
        from datetime import datetime, timezone
        from sqlalchemy import update
        from app.internal.cms_biz_metada.models.basic import Picture

        current_time = datetime.now(timezone.utc)
        
        for pic in pictures:
            await self.db.execute(
                update(Picture)
                .where(Picture.id == pic.id)
                .values(
                    ingest_status=status,
                    updated_at=current_time,
                )
            )
        
        await self.db.commit()
        logger.info(f"已更新 {len(pictures)} 张海报的注入状态为: {status}")

    async def _update_publish_statuses(
        self,
        statuses: list[tuple[str, int, str]],
    ) -> None:
        """
        批量更新 object_publish_status 表。

        调用方负责 commit。
        """
        from sqlalchemy import update as sa_update, func as sa_func
        for entity_type, entity_id, action in statuses:
            status = await publish_repository.get_or_create_object_publish_status(
                self.db, entity_type, entity_id, content_id=None,
            )
            status.mark_as_published(action)
            await publish_repository.update_object_publish_status(self.db, status)
            await self.db.execute(
                sa_update(ObjectPublishStatus)
                .where(ObjectPublishStatus.id == status.id)
                .values(last_publish_time=sa_func.now())
            )

    async def _create_ingest_history_records(
        self,
        ctx: CategorySyncContext,
        file_path: str,
        category_actions: dict[int, str],
        picture_actions: dict[int, str],
        correlate_id: str = "",
    ) -> None:
        """
        为本次同步创建 IngestHistory + IngestHistoryDetail 记录。

        按 Category 粒度：每个 Category 创建一条 IngestHistory，
        关联的 Picture 创建 IngestHistoryDetail。

        初始 status="failure"，等待 LSP 回调后更新为 success/failure。
        """
        now = datetime.now()

        # Picture 按 category 分组（pic.entity_id 指向 category.id）
        pictures_by_cat: dict[int, list[Picture]] = {}
        for pic in ctx.pictures:
            pictures_by_cat.setdefault(pic.entity_id, []).append(pic)

        for cat in ctx.categories:
            cat_action = category_actions.get(cat.id, "REGIST")

            history = IngestHistory(
                entity_type="Category",
                entity_id=cat.id,
                entity_name=cat.name,
                action=cat_action,
                status="failure",  # 等待 LSP 回调更新
                create_date=now,
                send_date=now,
                ingest_xml_path=file_path,
                correlate_id=correlate_id,
            )
            self.db.add(history)
            await self.db.flush()

            # Category 自身明细
            self.db.add(IngestHistoryDetail(
                history_id=history.id,
                entity_type="Category",
                entity_id=cat.id,
                entity_name=cat.name,
                action=cat_action,
            ))

            # 关联 Picture 明细
            for pic in pictures_by_cat.get(cat.id, []):
                pic_action = picture_actions.get(pic.id, "REGIST")
                self.db.add(IngestHistoryDetail(
                    history_id=history.id,
                    entity_type="Picture",
                    entity_id=pic.id,
                    entity_name=pic.file_name,
                    action=pic_action,
                ))

        logger.info(
            f"[CategorySync] 注入历史记录创建完成 | "
            f"categories={len(ctx.categories)} pictures={len(ctx.pictures)}"
        )

    async def _finalize_ingest_histories(self, correlate_id: str, status: str) -> None:
        """
        按 correlate_id 终态化 IngestHistory 记录（无 LSP 回调场景使用）。

        模拟模式 / SOAP 调用失败时不会再有 LSP 回调回写历史，
        需在此写入最终 status 与 end_date，保证历史弹窗与列表状态一致。
        调用方负责 commit。
        """
        from datetime import timezone
        now = datetime.now(timezone.utc)
        histories = await publish_repository.get_ingest_histories_by_correlate_id(
            self.db, correlate_id
        )
        for h in histories:
            h.status = status
            h.end_date = now
        if histories:
            logger.info(
                f"[CategorySync] IngestHistory 终态化 | "
                f"CorrelateID={correlate_id} status={status} count={len(histories)}"
            )

    @staticmethod
    def _maps_list(maps_el: Element) -> list[Element]:
        """直接操作 ElementTree 的子节点列表"""
        return maps_el  # type: ignore[return-value]


# ═══════════════════════════════════════════════════════════
# 序列化工具
# ═══════════════════════════════════════════════════════════

def _serialize(root: Element) -> str:
    """将 Element 序列化为带 XML 声明、缩进美化的字符串"""
    raw = tostring(root, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    # bug 32017：JumpCategoryCode / PosterType 保留传空，需输出显式开闭标签
    return obj_builders.expand_empty_properties(pretty.decode("utf-8"))


# ═══════════════════════════════════════════════════════════
# 私有加载函数（复用 loader.py 的相同逻辑）
# ═══════════════════════════════════════════════════════════

async def _load_languages(db: AsyncSession) -> tuple[list[str], str]:
    """从 Multi_Languages 字典加载所有支持的语言代码列表"""
    from app.internal.cms_biz_system.models.dict import DictNode

    root = (
        await db.execute(
            select(DictNode).where(
                DictNode.parent_id.is_(None),
                DictNode.code == "Multi_Languages",
                DictNode.is_deleted.is_(False),
            )
        )
    ).scalar_one_or_none()
    if root is None:
        return ["en"], "en"

    children = (
        await db.execute(
            select(DictNode)
            .where(
                DictNode.parent_id == root.id,
                DictNode.status == "active",
                DictNode.is_deleted.is_(False),
            )
            .order_by(DictNode.sort_order, DictNode.id)
        )
    ).scalars().all()

    if not children:
        return ["en"], "en"

    languages = [c.code for c in children]
    primary_language = languages[0] if languages else "en"
    return languages, primary_language


async def _load_category_i18n_data(
    db: AsyncSession,
    categories: list[Category],
    languages: list[str],
    primary_language: str,
) -> dict:
    """批量加载 Category 的多语言数据，含回退逻辑"""
    if not categories:
        return {}

    from app.internal.cms_biz_metada.models.basic import EntityI18n

    cat_ids = [c.id for c in categories]
    stmt = select(EntityI18n).where(
        EntityI18n.entity_type == "category",
        EntityI18n.entity_id.in_(cat_ids),
        EntityI18n.is_deleted.is_(False),
    )
    rows = list((await db.execute(stmt)).scalars().all())

    raw: dict[int, dict[str, dict[str, str]]] = {}
    for row in rows:
        if row.value and row.field_name and row.language:
            raw.setdefault(row.entity_id, {}).setdefault(row.field_name, {})[row.language] = row.value

    result: dict[int, dict[str, dict[str, str]]] = {}
    for cid in cat_ids:
        if cid not in raw:
            continue
        result[cid] = {}
        for field_name, lang_values in raw[cid].items():
            primary_value = lang_values.get(primary_language)
            result[cid][field_name] = {}
            for lang in languages:
                if lang in lang_values:
                    result[cid][field_name][lang] = lang_values[lang]
                elif primary_value:
                    result[cid][field_name][lang] = primary_value

    return result


async def _get_pictures_for_entity(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
) -> list[Picture]:
    """按多态键查询关联图片"""
    if not entity_type:
        return []
    stmt = select(Picture).where(
        Picture.entity_type == entity_type,
        Picture.entity_id == entity_id,
        Picture.is_deleted.is_(False),
        Picture.is_discarded.is_(False),
    )
    return list((await db.execute(stmt)).scalars().all())


async def _load_poster_size_names(db: AsyncSession, pictures: list[Picture]) -> tuple[dict, dict]:
    """
    加载 Picture 关联的 PosterSize 名称和 mapping_type。

    返回 (names_dict, mapping_types_dict)：
        names_dict:        {picture_id: poster_size_name}
        mapping_types_dict: {picture_id: mapping_type}（仅 mapping_type 非空）
    """
    if not pictures:
        return {}, {}
    ps_ids = list({p.poster_size_id for p in pictures if p.poster_size_id})
    if not ps_ids:
        return {}, {}
    stmt = select(PosterSize).where(
        PosterSize.id.in_(ps_ids),
        PosterSize.is_deleted.is_(False),
    )
    sizes = list((await db.execute(stmt)).scalars().all())
    size_map = {s.id: s.name for s in sizes if s.name}
    mapping_type_map = {s.id: s.mapping_type for s in sizes if s.mapping_type is not None}
    names = {
        p.id: size_map.get(p.poster_size_id, None)
        for p in pictures
        if p.poster_size_id and p.poster_size_id in size_map
    }
    mapping_types = {
        p.id: mapping_type_map.get(p.poster_size_id, None)
        for p in pictures
        if p.poster_size_id and p.poster_size_id in mapping_type_map
    }
    return names, mapping_types


async def _load_category_custom_fields(
    db: AsyncSession,
    category_ids: list[int],
    languages: list[str],
    primary_language: str = "en",
) -> dict:
    """
    加载 Category 的自定义字段。

    从 EntityFieldValue + EntityI18n 表查询 entity_type="category" 的记录，
    按 category_id 分组组装为 C2 规范的 extendinfo 格式。

    返回 {category_id: {"extendinfo": json_str}}，格式：
      非多语言字段 → {"field_name": "value"}
      多语言字段   → {"field_name": {"en": "val", "zh": "val"}}
    没有自定义字段值的实体不包含 extendinfo key。
    """
    from app.internal.cms_biz_metada.models.basic import CustomField, EntityFieldValue, EntityI18n
    from app.soap.c2.loader import _format_custom_field_value
    from collections import defaultdict
    import json

    if not category_ids:
        return {}

    # 1. 从 EntityFieldValue 加载非多语言字段值
    stmt = select(EntityFieldValue).where(
        EntityFieldValue.entity_type == "category",
        EntityFieldValue.entity_id.in_(category_ids),
        EntityFieldValue.is_deleted.is_(False),
    )
    values = list((await db.execute(stmt)).scalars().all())

    cf_ids = list({v.custom_field_id for v in values if v.custom_field_id})
    cf_map: dict[int, CustomField] = {}
    if cf_ids:
        for cf in (await db.execute(select(CustomField).where(CustomField.id.in_(cf_ids)))).scalars().all():
            cf_map[cf.id] = cf

    field_parts: dict[int, dict] = defaultdict(dict)
    for ev in values:
        if ev.custom_field_id and ev.custom_field_id in cf_map:
            cf = cf_map[ev.custom_field_id]
            if not cf.multi_language:
                field_parts[ev.entity_id][cf.field_name] = _format_custom_field_value(ev.value, cf.field_type)

    # 2. 从 EntityI18n 加载多语言字段（所有语言）
    i18n_stmt = select(EntityI18n).where(
        EntityI18n.entity_type == "category",
        EntityI18n.entity_id.in_(category_ids),
        EntityI18n.field_name.startswith("cf_"),
        EntityI18n.is_deleted.is_(False),
    )
    i18n_rows = list((await db.execute(i18n_stmt)).scalars().all())

    i18n_field_codes = list({r.field_name for r in i18n_rows})
    cf_i18n_name_map: dict[str, str] = {}
    cf_i18n_type_map: dict[str, str] = {}
    if i18n_field_codes:
        for cf in (await db.execute(
            select(CustomField).where(
                CustomField.field_code.in_(i18n_field_codes),
                CustomField.multi_language.is_(True),
                CustomField.is_deleted.is_(False),
            )
        )).scalars().all():
            cf_i18n_name_map[cf.field_code] = cf.field_name
            cf_i18n_type_map[cf.field_code] = cf.field_type

    entity_i18n_values: dict[int, dict[str, dict[str, str]]] = defaultdict(lambda: defaultdict(dict))
    for row in i18n_rows:
        display_name = cf_i18n_name_map.get(row.field_name)
        if display_name:
            entity_i18n_values[row.entity_id][row.field_name][row.language] = row.value

    for eid, field_lang_map in entity_i18n_values.items():
        for field_code, lang_vals in field_lang_map.items():
            display_name = cf_i18n_name_map.get(field_code)
            if not display_name:
                continue
            ft = cf_i18n_type_map.get(field_code, "")
            lang_obj = {}
            for lang in languages:
                val = lang_vals.get(lang) or lang_vals.get(primary_language)
                if val:
                    lang_obj[lang] = _format_custom_field_value(val, ft)
            if lang_obj:
                field_parts[eid][display_name] = lang_obj

    # 3. 组装结果
    result = {}
    for cat_id in category_ids:
        parts = field_parts.get(cat_id, {})
        if parts:
            result[cat_id] = {"extendinfo": json.dumps(parts, ensure_ascii=False)}

    return result
