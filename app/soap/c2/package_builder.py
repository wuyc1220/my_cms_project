"""
Package 同步构建器。

独立于内容发布流程，专门将 Package（服务包）生成符合 C2 规范的 ADI XML，
用于"Package同步给业务系统"功能。

同步策略：
    - 所有 Package 统一使用 REGIST 操作
    - 仅输出 Package Object（Name、Description、Extendinfo），无 Mapping
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

from app.internal.cms_biz_package.models.package import Package
from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.internal.cms_biz_publish.repositories import publish_repository
from app.soap.config import soap_settings
from app.config import app_tz

from . import objects as obj_builders
from .constants import Action, ElementType

from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_publish.models.object_publish_status import ObjectPublishStatus


# ═══════════════════════════════════════════════════════════
# 同步上下文
# ═══════════════════════════════════════════════════════════

@dataclass
class PackageSyncContext:
    """Package 同步上下文，承载从数据库加载的全部业务数据"""

    packages: list[Package] = field(default_factory=list)

    # 自定义字段 {package_id: {"extendinfo": json_str}}
    package_custom_fields: dict = field(default_factory=dict)


# ═══════════════════════════════════════════════════════════
# 数据加载器
# ═══════════════════════════════════════════════════════════

async def load_package_sync_context(
    db: AsyncSession,
    package_ids: list[int],
) -> PackageSyncContext:
    """
    以 package_ids 为入口，加载同步所需的全部数据。

    :param db: 数据库会话
    :param package_ids: 要同步的 Package ID 列表
    :return: 聚合完成的 PackageSyncContext
    """
    ctx = PackageSyncContext()

    # ── 1. 加载 Package ───────────────────────────────
    stmt = select(Package).where(
        Package.id.in_(package_ids),
        Package.is_deleted.is_(False),
    )
    ctx.packages = list((await db.execute(stmt)).scalars().all())

    if not ctx.packages:
        return ctx

    # ── 2. 加载自定义字段（复用 loader 的通用逻辑，含多语言回退）──
    from app.soap.c2.loader import _load_languages, _load_custom_fields_for_entities
    languages, primary_language = await _load_languages(db)
    ctx.package_custom_fields = await _load_custom_fields_for_entities(
        db, "package", [p.id for p in ctx.packages], languages, primary_language,
    )

    return ctx


# ═══════════════════════════════════════════════════════════
# Package 同步构建器
# ═══════════════════════════════════════════════════════════

class PackageSyncBuilder:
    """
    Package 同步 ADI XML 构建器。

    使用示例::

        builder = PackageSyncBuilder(db)
        result = await builder.build_sync(package_ids=[1, 2, 3])
        # result = {"xml_str": "...", "file_path": "c2/xxx.xml",
        #           "stats": {"regist": 2, "update": 0, "skip": 0}}
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def build_sync(
        self,
        package_ids: list[int],
    ) -> dict:
        """
        构建 Package 同步 ADI XML。

        1. 加载数据 → 2. 构建 XML → 3. 上传 SFTP
        → 4. 创建注入历史 → 5. SOAP 通知 → 6. 更新状态

        :param package_ids: 要同步的 Package ID 列表
        :return: {
            "xml_str": str,         # 生成的 XML 字符串
            "file_path": str,       # SFTP 路径
            "stats": dict,          # {regist: N, update: 0, skip: 0}
            "synced_ids": list[int] # 实际输出的 Package ID
        }
        """
        ctx = await load_package_sync_context(self.db, package_ids)
        if not ctx.packages:
            logger.warning(f"未找到待同步的 Package: {package_ids}")
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

        # ── 处理 Package ──────────────────────────────
        for pkg in ctx.packages:
            logger.info(f"[PackageSync] Package#{pkg.id} → REGIST")
            objects_el.append(
                obj_builders.build_package_object(
                    pkg, Action.REGIST,
                    custom_fields=ctx.package_custom_fields.get(pkg.id),
                )
            )
            stats["regist"] += 1
            synced_entity_statuses.append(("Package", pkg.id, "REGIST"))

        xml_str = _serialize(root)

        # ── 上传到 SFTP ────────────────────────────────
        file_path = self._write_to_file(xml_str)

        # ── 生成 CorrelateID 与 CmdFileURL ─────────────
        correlate_id = uuid.uuid4().hex
        cmd_file_url = self._build_cmd_file_url(file_path)

        # ── 创建注入历史记录（初始 status="failure"，等待 LSP 回调更新）──
        await self._create_ingest_history_records(
            ctx, file_path, correlate_id=correlate_id,
        )

        # ── 创建 PublishTask ───────────────────────────
        first_pkg = ctx.packages[0]
        publish_task = PublishTask(
            entity_type="Package",
            entity_id=first_pkg.id,
            entity_name=first_pkg.name,
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
                    f"[PackageSync] SOAP ExecCmd 结果 - success={soap_success}, "
                    f"CorrelateID={correlate_id}"
                )
            except Exception as e:
                soap_error = str(e)
                logger.error(f"[PackageSync] SOAP 调用异常: {e}")
        else:
            # SOAP 未启用，模拟成功
            logger.warning(f"[PackageSync] SOAP 未启用，模拟通知成功 - CorrelateID={correlate_id}")
            soap_success = True

        # ── 根据 SOAP 结果更新状态 ─────────────────────
        if soap_success and soap_settings.enabled:
            from sqlalchemy import update as sa_update
            ingest_stmt = (
                sa_update(Package)
                .where(Package.id.in_(package_ids))
                .values(ingest_status="processing", updated_at=Package.updated_at)
            )
            await self.db.execute(ingest_stmt)
            logger.info(
                f"[PackageSync] SOAP 通知已发送，等待 LSP 回调 | "
                f"CorrelateID={correlate_id}"
            )
        elif soap_success and not soap_settings.enabled:
            # SOAP 未启用，直接标记为成功
            from sqlalchemy import update as sa_update
            ingest_stmt = (
                sa_update(Package)
                .where(Package.id.in_(package_ids))
                .values(ingest_status="success", updated_at=Package.updated_at)
            )
            await self.db.execute(ingest_stmt)
            # 同步更新 IngestHistory 状态
            history_stmt = (
                sa_update(IngestHistory)
                .where(IngestHistory.entity_type == "Package")
                .where(IngestHistory.entity_id.in_(package_ids))
                .where(IngestHistory.correlate_id == correlate_id)
                .values(status="success", end_date=datetime.now())
            )
            await self.db.execute(history_stmt)
            publish_task.status = "success"
            publish_task.publish_status = "success"
            await publish_repository.update_publish_task(self.db, publish_task)
            logger.info(
                f"[PackageSync] SOAP 未启用，直接标记同步成功 | "
                f"CorrelateID={correlate_id}"
            )
        else:
            from sqlalchemy import update as sa_update
            ingest_stmt = (
                sa_update(Package)
                .where(Package.id.in_(package_ids))
                .values(ingest_status="failure", updated_at=Package.updated_at)
            )
            await self.db.execute(ingest_stmt)
            publish_task.status = "failure"
            publish_task.publish_status = "failure"
            publish_task.error_message = f"SOAP 调用失败: {soap_error}"
            await publish_repository.update_publish_task(self.db, publish_task)
            logger.error(
                f"[PackageSync] SOAP 调用失败，任务标记为失败 | "
                f"CorrelateID={correlate_id}, Error: {soap_error}"
            )

        # ── 更新 object_publish_status（不含 commit）───
        await self._update_publish_statuses(synced_entity_statuses)

        # ── 统一提交 ──────────────────────────────────
        await self.db.commit()

        logger.info(
            f"[PackageSync] 同步完成 | packages={len(ctx.packages)} "
            f"| stats={stats} | "
            f"soap_success={soap_success} CorrelateID={correlate_id}"
        )

        return {
            "xml_str": xml_str,
            "file_path": file_path,
            "stats": stats,
            "synced_ids": [pkg.id for pkg in ctx.packages],
            "correlate_id": correlate_id,
            "soap_success": soap_success,
            "soap_error": soap_error,
            "soap_enabled": soap_settings.enabled,
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

        文件名格式: ``c2/YYYYMM/package_sync_{uuid8}_{timestamp}.xml``

        :return: SFTP 相对路径（c2/YYYYMM/xxx.xml）
        """
        from .file_utils import generate_c2_filename, upload_c2_xml

        filename = generate_c2_filename(
            prefix="package_sync",
            entity_type="package",
            entity_id=0,
        )

        return upload_c2_xml(xml_str, filename, storage_service)

    @staticmethod
    def _build_cmd_file_url(file_path: str) -> str:
        """
        根据 file_path 构建 LSP 可访问的 CmdFileURL。

        优先级：SOAP_CMD_FILE_URL_PREFIX > FTP/SFTP 自动拼接
        """
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
        ctx: PackageSyncContext,
        file_path: str,
        correlate_id: str = "",
    ) -> None:
        """
        为本次同步创建 IngestHistory + IngestHistoryDetail 记录。

        每个 Package 创建一条 IngestHistory + 一条 IngestHistoryDetail。

        初始 status="failure"，等待 LSP 回调后更新为 success/failure。
        """
        now = datetime.now()

        for pkg in ctx.packages:
            history = IngestHistory(
                entity_type="Package",
                entity_id=pkg.id,
                entity_name=pkg.name,
                action="REGIST",
                status="failure",  # 等待 LSP 回调更新
                create_date=now,
                send_date=now,
                ingest_xml_path=file_path,
                correlate_id=correlate_id,
            )
            self.db.add(history)
            await self.db.flush()

            # Package 自身明细
            self.db.add(IngestHistoryDetail(
                history_id=history.id,
                entity_type="Package",
                entity_id=pkg.id,
                entity_name=pkg.name,
                action="REGIST",
            ))

        logger.info(
            f"[PackageSync] 注入历史记录创建完成 | "
            f"packages={len(ctx.packages)}"
        )


# ═══════════════════════════════════════════════════════════
# 序列化工具
# ═══════════════════════════════════════════════════════════

def _serialize(root: Element) -> str:
    """将 Element 序列化为带 XML 声明、缩进美化的字符串"""
    raw = tostring(root, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    return pretty.decode("utf-8")
