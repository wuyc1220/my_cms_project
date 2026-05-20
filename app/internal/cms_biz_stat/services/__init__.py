"""
看板统计模块业务逻辑层。

提供看板数据统计查询和用户配置管理服务。
"""

from datetime import datetime, date, timedelta
from typing import Any

from fastapi import HTTPException, status
from loguru import logger
from sqlalchemy import func, select, case, or_
from sqlalchemy.ext.asyncio import AsyncSession
import json

from app.internal.cms_biz_stat.models import UserDashboardConfig
from app.internal.cms_biz_stat.schemas import (
    ContentStatusCountResponse,
    DashboardDataResponse,
    GenreConfigItem,
    GenreStatusMatrixResponse,
    ModuleConfigItem,
    PieDataItem,
    PublishedStatsResponse,
    StatusConfigItem,
    TaskAssignedMatrixItem,
    TaskAssignedMatrixResponse,
    TaskCompletionStatsResponse,
    TaskStatusCountResponse,
    UserDashboardConfigResponse,
    UserDashboardConfigUpdate,
)
from app.internal.cms_biz_metada.models.basic import Genre
from app.internal.cms_biz_orchestration.models.content_metadata import (
    ContentMetadata,
    SeriesMetadata,
)
from app.internal.cms_biz_package.models.package import Content, ContentPackage, Package, PackagePlatform
from app.internal.cms_biz_package.models.task import Task
from app.internal.cms_biz_scp.models.trade import License, LicenseContent, LicensePlatform
from app.internal.cms_biz_system.models.dict import DictNode
from app.internal.cms_biz_system.models.operation_log import OperationLog
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.services.dict_service import get_dict_children_by_code
from app.internal.cms_biz_system.services.config_service import get_config_int
from app.internal.cms_biz_system.services.data_auth_filter import (
    apply_content_data_auth,
    has_task_assign_role,
    TASK_RELATED_MODULE_CODES,
)


# ═══════════════════════════════════════════════════════════
# 1. 默认配置常量
# ═══════════════════════════════════════════════════════════

DEFAULT_MODULE_CONFIG = [
    {"code": "published_stats", "name": "Vod Published Statistics | Vod内容发布统计", "visible": True, "sort_order": 1},
    {"code": "content_status_count", "name": "Vod Status Count | 按状态统计Vod内容数量", "visible": True, "sort_order": 2},
    {"code": "genre_status_table", "name": "Vod Genre/Status Table | 按题材x状态的矩阵统计表", "visible": True, "sort_order": 3},
    {"code": "assigned_to_me", "name": "Assigned To Me Table | 分配给当前用户的任务列表", "visible": True, "sort_order": 4},
    {"code": "task_completion_stats", "name": "Task Completion Statistics | 任务完成统计", "visible": True, "sort_order": 5},
    {"code": "task_status_count", "name": "Task Status Count | 任务状态数量统计", "visible": True, "sort_order": 6},
    {"code": "task_assigned_table", "name": "Task Assigned/Status Table | 任务分配与状态矩阵", "visible": True, "sort_order": 7},
    {"code": "not_assigned_tasks", "name": "Not Assigned Tasks Table | 未分配任务列表", "visible": True, "sort_order": 8},
]

# 计算状态（不从字典表获取，由系统计算得出）
COMPUTED_STATUSES = [
    {"code": "Expired", "name": "Expired", "visible": True, "sort_order": 97},
    {"code": "NearExpiry", "name": "NearExpiry", "visible": True, "sort_order": 98},
    {"code": "Deleted", "name": "Deleted", "visible": True, "sort_order": 99},
]


# ═══════════════════════════════════════════════════════════
# 2. 用户配置服务
# ═══════════════════════════════════════════════════════════


class DashboardConfigService:
    """看板配置服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    async def _get_default_language(self) -> str | None:
        """获取 Multi_Languages 字典表的第一种语言（默认语言）"""
        root = (
            await self.db.execute(
                select(DictNode).where(DictNode.parent_id.is_(None), DictNode.code == "Multi_Languages", DictNode.is_deleted == False)
            )
        ).scalar_one_or_none()
        if root is None:
            return None

        first_lang = (
            await self.db.execute(
                select(DictNode)
                .where(DictNode.parent_id == root.id, DictNode.status == "active", DictNode.is_deleted == False)
                .order_by(DictNode.sort_order, DictNode.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        return first_lang.code if first_lang else None

    async def _sync_genre_config(self, current_config: list[dict]) -> list[dict]:
        """同步题材配置，按 Multi_Languages 第一种语言过滤，保留用户可见性设置"""
        # 获取 Multi_Languages 第一种语言
        default_language = await self._get_default_language()

        # 获取该语言的所有题材
        genre_query = select(Genre).where(Genre.is_deleted.is_(False))
        if default_language:
            genre_query = genre_query.where(Genre.language == default_language)
        genre_query = genre_query.order_by(Genre.id)

        genre_result = await self.db.execute(genre_query)
        db_genres = genre_result.scalars().all()

        # 将当前配置转为字典，保留用户的 visible 设置
        current_visible_map = {item.get("name"): item.get("visible", True) for item in current_config if item.get("name")}

        # 构建新的题材配置
        genre_config = []
        for i, g in enumerate(db_genres):
            genre_config.append({
                "id": g.id,
                "name": g.name,
                "visible": current_visible_map.get(g.name, True),  # 保留用户设置，新增默认为True
                "sort_order": i + 1
            })

        return genre_config

    async def _sync_content_status_config(self, current_config: list[dict]) -> list[dict]:
        """同步内容状态配置，从 Ingest_Status 字典获取，保留用户可见性设置，同时保留计算状态"""
        # 从字典获取 Ingest_Status 所有子节点
        ingest_status_items = await get_dict_children_by_code(self.db, "Ingest_Status")

        # 将当前配置转为字典，保留用户的 visible 设置（以 code 为 key）
        current_visible_map = {item.get("code"): item.get("visible", True) for item in current_config if item.get("code")}

        # 构建状态配置（从字典获取的状态）
        status_config = []
        for i, item in enumerate(ingest_status_items):
            status_config.append({
                "code": item.code,
                "name": item.name,  # 使用字典中的 name（英文）
                "visible": current_visible_map.get(item.code, True),  # 保留用户设置，新增默认为True
                "sort_order": i + 1
            })

        # 添加计算状态（Expired, NearExpiry）
        for computed in COMPUTED_STATUSES:
            status_config.append({
                "code": computed["code"],
                "name": computed["name"],  # 使用中文名
                "visible": current_visible_map.get(computed["code"], True),
                "sort_order": computed["sort_order"]
            })

        return status_config

    async def get_or_create_config(self, user_id: int) -> UserDashboardConfig:
        """获取或创建用户配置"""
        result = await self.db.execute(
            select(UserDashboardConfig).where(
                UserDashboardConfig.user_id == user_id,
                UserDashboardConfig.is_deleted.is_(False),
            )
        )
        config = result.scalar_one_or_none()

        if not config:
            # 创建新配置
            genre_config = await self._sync_genre_config([])
            content_status_config = await self._sync_content_status_config([])

            # 使用 INSERT ... ON CONFLICT DO UPDATE 来避免并发冲突
            from sqlalchemy.dialects.postgresql import insert

            stmt = insert(UserDashboardConfig).values(
                user_id=user_id,
                module_config=DEFAULT_MODULE_CONFIG,
                content_status_config=content_status_config,
                content_genre_config=genre_config,
            )
            # 如果发生冲突，则更新现有记录
            stmt = stmt.on_conflict_do_update(
                index_elements=['user_id'],
                set_={
                    'module_config': stmt.excluded.module_config,
                    'content_status_config': stmt.excluded.content_status_config,
                    'content_genre_config': stmt.excluded.content_genre_config,
                    'updated_at': func.now(),
                }
            )

            await self.db.execute(stmt)
            await self.db.flush()

            # 重新获取配置
            result = await self.db.execute(
                select(UserDashboardConfig).where(
                    UserDashboardConfig.user_id == user_id,
                    UserDashboardConfig.is_deleted.is_(False),
                )
            )
            config = result.scalar_one()
        else:
            # 同步现有配置的题材列表（按语言过滤，保留用户可见性设置）
            synced_genre_config = await self._sync_genre_config(config.content_genre_config)
            # 如果题材配置有变化，更新数据库
            # 将数据库的 JSON 配置转为列表进行比较
            current_genre_list = list(config.content_genre_config) if config.content_genre_config else []
            if synced_genre_config != current_genre_list:
                config.content_genre_config = synced_genre_config
                await self.db.flush()

            # 同步内容状态配置（从字典获取，保留用户可见性设置）
            synced_status_config = await self._sync_content_status_config(config.content_status_config)
            current_status_list = list(config.content_status_config) if config.content_status_config else []
            if synced_status_config != current_status_list:
                config.content_status_config = synced_status_config
                await self.db.flush()

        return config

    async def get_config_response(self, user_id: int, current_user: User | None = None) -> UserDashboardConfigResponse:
        """获取配置响应"""
        config = await self.get_or_create_config(user_id)

        response = UserDashboardConfigResponse.model_validate(config)

        if current_user is not None:
            can_see_task = await has_task_assign_role(self.db, current_user)
            if not can_see_task:
                response.module_config = [
                    m for m in response.module_config if m.code not in TASK_RELATED_MODULE_CODES
                ]

        return response

    async def update_config(
        self, user_id: int, data: UserDashboardConfigUpdate, user_name: str | None = None
    ) -> UserDashboardConfigResponse:
        """更新配置"""
        config = await self.get_or_create_config(user_id)

        # 记录操作前的配置
        previous_value = {
            "module_config": config.module_config,
            "content_status_config": config.content_status_config,
            "content_genre_config": config.content_genre_config,
        }

        config.module_config = [m.model_dump() for m in data.module_config]

        # 同步用户提交的状态配置与字典（保留用户可见性设置，添加新增的字典状态）
        user_status_config = [s.model_dump() for s in data.content_status_config]
        synced_status_config = await self._sync_content_status_config(user_status_config)
        config.content_status_config = synced_status_config

        config.content_genre_config = [g.model_dump() for g in data.content_genre_config]
        config.updated_by = user_id

        await self.db.flush()

        # 记录操作日志
        operation_log = OperationLog(
            user_id=user_id,
            user_name=user_name,
            operation_type="UPDATE",
            operation_object="UserDashboardConfig",
            operation_content="更新用户看板配置",
            previous_value=json.dumps(previous_value, ensure_ascii=False),
            updated_value=json.dumps({
                "module_config": config.module_config,
                "content_status_config": config.content_status_config,
                "content_genre_config": config.content_genre_config,
            }, ensure_ascii=False),
            result="success",
        )
        self.db.add(operation_log)
        await self.db.flush()

        return UserDashboardConfigResponse.model_validate(config)

    async def reset_config(self, user_id: int, user_name: str | None = None) -> UserDashboardConfigResponse:
        """重置为默认配置"""
        config = await self.get_or_create_config(user_id)

        # 记录操作前的配置
        previous_value = {
            "module_config": config.module_config,
            "content_status_config": config.content_status_config,
            "content_genre_config": config.content_genre_config,
        }

        # 重新获取题材配置（按 Multi_Languages 第一种语言过滤，重置时所有题材默认可见）
        genre_config = await self._sync_genre_config([])
        # 重新获取内容状态配置（从字典获取，重置时所有状态默认可见）
        content_status_config = await self._sync_content_status_config([])

        config.module_config = DEFAULT_MODULE_CONFIG
        config.content_status_config = content_status_config
        config.content_genre_config = genre_config
        config.updated_by = user_id

        await self.db.flush()

        # 记录操作日志
        operation_log = OperationLog(
            user_id=user_id,
            user_name=user_name,
            operation_type="RESET",
            operation_object="UserDashboardConfig",
            operation_content="重置用户看板配置为默认值",
            previous_value=json.dumps(previous_value, ensure_ascii=False),
            updated_value=json.dumps({
                "module_config": config.module_config,
                "content_status_config": config.content_status_config,
                "content_genre_config": config.content_genre_config,
            }, ensure_ascii=False),
            result="success",
        )
        self.db.add(operation_log)
        await self.db.flush()

        return UserDashboardConfigResponse.model_validate(config)


# ═══════════════════════════════════════════════════════════
# 3. 看板统计服务
# ═══════════════════════════════════════════════════════════


class DashboardStatService:
    """看板统计服务"""

    def __init__(self, db: AsyncSession):
        self.db = db

    # ───────────────────────────────────────────────────────
    # 3.1 内容发布统计
    # ───────────────────────────────────────────────────────

    async def get_published_stats(self, visible_genres: list[str] | None = None, current_user: User | None = None) -> PublishedStatsResponse:
        """获取内容发布统计

        Args:
            visible_genres: 用户配置中可见的题材名称列表，为None时统计所有题材
            current_user: 当前用户，用于数据权限过滤
        """
        logger.info(f"get_published_stats 入参: visible_genres={visible_genres}")

        base_query = select(Content).where(
            Content.status == "Published",
            Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
            Content.is_deleted.is_(False),
            Content.is_discarded.is_(False),
            Content.is_archived.is_(False),
        )

        platform_query = (
            select(LicensePlatform.platform, func.count(Content.id.distinct()))
            .join(License, LicensePlatform.license_id == License.id)
            .join(LicenseContent, LicenseContent.license_id == License.id)
            .join(Content, LicenseContent.content_id == Content.id)
            .where(
                Content.status == "Published",
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
                License.is_deleted.is_(False),
                LicenseContent.is_deleted.is_(False),
                LicensePlatform.is_deleted.is_(False),
            )
        )
        if current_user is not None:
            platform_query = await apply_content_data_auth(self.db, current_user, platform_query)
        platform_query = platform_query.group_by(LicensePlatform.platform)
        result = await self.db.execute(platform_query)
        platform_counts = {row[0]: row[1] for row in result.all()}

        # 从字典配置动态获取 Platform 字典的所有子节点
        platform_dict_items = await get_dict_children_by_code(self.db, "Platform")
        by_platform = [
            PieDataItem(name=item.name, value=platform_counts.get(item.code, 0))
            for item in platform_dict_items
        ]

        content_type_query = (
            select(Content.content_type, func.count(Content.id))
            .where(
                Content.status == "Published",
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
            )
        )
        if current_user is not None:
            content_type_query = await apply_content_data_auth(self.db, current_user, content_type_query)
        content_type_query = content_type_query.group_by(Content.content_type)
        result = await self.db.execute(content_type_query)
        content_type_counts = {row[0]: row[1] for row in result.all()}
        by_content_type = [
            PieDataItem(name="Movie", value=content_type_counts.get("MOVIE", 0)),
            PieDataItem(name="Series", value=content_type_counts.get("SERIES", 0)),
            PieDataItem(name="Season", value=content_type_counts.get("SEASON", 0)),
        ]

        genre_query = (
            select(Genre.name, func.count(Content.id))
            .join(Content, Content.genre_id == Genre.id)
            .where(
                Content.status == "Published",
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
                Genre.is_deleted.is_(False),
            )
        )
        if current_user is not None:
            genre_query = await apply_content_data_auth(self.db, current_user, genre_query)
        if visible_genres:
            genre_query = genre_query.where(Genre.name.in_(visible_genres))
        genre_query = genre_query.group_by(Genre.name)

        result = await self.db.execute(genre_query)
        genre_counts = {row[0]: row[1] for row in result.all()}
        logger.info(f"get_published_stats 题材统计原始结果: {genre_counts}")

        # 确保所有可见题材都出现在结果中（没有数据的显示为0）
        by_genre = [
            PieDataItem(name=name, value=genre_counts.get(name, 0))
            for name in (visible_genres or [])
        ]
        logger.info(f"get_published_stats 题材统计最终结果: {by_genre}")

        ingest_status_query = (
            select(Content.status, func.count(Content.id))
            .where(
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
            )
        )
        if current_user is not None:
            ingest_status_query = await apply_content_data_auth(self.db, current_user, ingest_status_query)
        ingest_status_query = ingest_status_query.group_by(Content.status)
        result = await self.db.execute(ingest_status_query)
        ingest_status_counts = {row[0]: row[1] for row in result.all()}

        ingest_status_filter = ["InProgress", "ReadyForPublish", "Published", "NoActiveLicense", "Closed"]
        ingest_status_items = await get_dict_children_by_code(self.db, "Ingest_Status")
        by_ingest_status = [
            PieDataItem(name=item.name, value=ingest_status_counts.get(item.code, 0))
            for item in ingest_status_items
            if item.code in ingest_status_filter
        ]

        return PublishedStatsResponse(
            by_platform=by_platform,
            by_content_type=by_content_type,
            by_genre=by_genre,
            by_ingest_status=by_ingest_status,
        )

    # ───────────────────────────────────────────────────────
    # 3.2 内容状态统计
    # ───────────────────────────────────────────────────────

    async def get_content_status_count(self, current_user: User | None = None) -> ContentStatusCountResponse:
        """获取内容状态统计"""
        status_query = (
            select(Content.status, func.count(Content.id))
            .where(
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
            )
        )
        if current_user is not None:
            status_query = await apply_content_data_auth(self.db, current_user, status_query)
        status_query = status_query.group_by(Content.status)
        result = await self.db.execute(status_query)
        status_counts = {row[0]: row[1] for row in result.all()}

        near_expiry_days = await get_config_int(self.db, "NEAR_EXPIRY_DAYS", 7)
        near_expiry_count = await self._count_near_expiry_contents(near_expiry_days, current_user)

        # 统计已删除内容数量（is_deleted=True 且 content_type 为 MOVIE/SEASON/SERIES）
        deleted_result = await self.db.execute(
            select(func.count(Content.id))
            .where(
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(True),
            )
        )
        deleted_count = deleted_result.scalar() or 0

        return ContentStatusCountResponse(
            waiting_for_materials=status_counts.get("WaitingForMaterials", 0),
            in_progress=status_counts.get("InProgress", 0),
            ready_for_publish=status_counts.get("ReadyForPublish", 0),
            publishing=status_counts.get("Publishing", 0),
            published=status_counts.get("Published", 0),
            publish_failed=status_counts.get("PublishFailed", 0),
            no_active_license=status_counts.get("NoActiveLicense", 0),
            expired=status_counts.get("Expired", 0),
            near_expiry=near_expiry_count,
            near_expiry_days=near_expiry_days,
            deleted=deleted_count,
            closed=status_counts.get("Closed", 0),
            none_status=status_counts.get("None", 0),
        )

    async def _count_near_expiry_contents(self, near_expiry_days: int, current_user: User | None = None) -> int:
        """
        统计临近过期内容数量。

        判定条件：
        1. 内容状态为 Published
        2. 内容关联的许可证中，至少存在一条 end_date 在 [today, today + near_expiry_days) 范围内
        3. 内容关联的许可证中，不存在任何 end_date IS NULL OR end_date >= today + near_expiry_days 的有效许可证
        即：所有许可证都将在 near_expiry_days 天内到期，但尚未过期
        """
        if near_expiry_days <= 0:
            return 0

        today = date.today()
        threshold = today + timedelta(days=near_expiry_days)

        has_near_expiry_subq = (
            select(LicenseContent.content_id)
            .join(License, License.id == LicenseContent.license_id)
            .where(
                LicenseContent.is_deleted.is_(False),
                License.is_deleted.is_(False),
                License.end_date.is_not(None),
                License.end_date >= today,
                License.end_date < threshold,
            )
            .distinct()
        )

        has_long_valid_subq = (
            select(LicenseContent.content_id)
            .join(License, License.id == LicenseContent.license_id)
            .where(
                LicenseContent.is_deleted.is_(False),
                License.is_deleted.is_(False),
                or_(
                    License.end_date.is_(None),
                    License.end_date >= threshold,
                ),
            )
            .distinct()
        )

        near_expiry_query = (
            select(func.count(Content.id))
            .where(
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.status == "Published",
                Content.id.in_(has_near_expiry_subq),
                Content.id.notin_(has_long_valid_subq),
            )
        )
        if current_user is not None:
            near_expiry_query = await apply_content_data_auth(self.db, current_user, near_expiry_query)
        result = await self.db.execute(near_expiry_query)
        return result.scalar() or 0

    # ───────────────────────────────────────────────────────
    # 3.3 题材x状态矩阵
    # ───────────────────────────────────────────────────────

    async def get_genre_status_matrix(
        self, visible_genres: list[str] | None = None, visible_statuses: list[str] | None = None, current_user: User | None = None
    ) -> GenreStatusMatrixResponse:
        """获取题材x状态矩阵

        Args:
            visible_genres: 用户配置中可见的题材名称列表，为None时返回所有题材
            visible_statuses: 用户配置中可见的状态代码列表，为None时返回所有状态
            current_user: 当前用户，用于数据权限过滤
        """
        genre_query = select(Genre.name).where(Genre.is_deleted.is_(False))
        if visible_genres:
            genre_query = genre_query.where(Genre.name.in_(visible_genres))
        genre_query = genre_query.order_by(Genre.id)

        genre_result = await self.db.execute(genre_query)
        genres = [row[0] for row in genre_result.all()]

        if visible_statuses:
            statuses = visible_statuses
        else:
            statuses = [
                "WaitingForMaterials", "InProgress", "ReadyForPublish", "Publishing",
                "Published", "PublishFailed", "NoActiveLicense", "Expired", "Closed", "None"
            ]

        matrix_query = (
            select(Genre.name, Content.status, func.count(Content.id))
            .join(Content, Content.genre_id == Genre.id)
            .where(
                Content.content_type.in_(["MOVIE", "SEASON", "SERIES"]),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
                Content.is_archived.is_(False),
                Genre.is_deleted.is_(False),
            )
        )
        if current_user is not None:
            matrix_query = await apply_content_data_auth(self.db, current_user, matrix_query)
        if visible_genres:
            matrix_query = matrix_query.where(Genre.name.in_(visible_genres))
        if visible_statuses:
            matrix_query = matrix_query.where(Content.status.in_(visible_statuses))
        matrix_query = matrix_query.group_by(Genre.name, Content.status)

        result = await self.db.execute(matrix_query)

        data: dict[str, dict[str, int]] = {genre: {status: 0 for status in statuses} for genre in genres}
        for row in result.all():
            genre_name, status, count = row
            if genre_name in data and status in data[genre_name]:
                data[genre_name][status] = count

        return GenreStatusMatrixResponse(
            genres=genres,
            statuses=statuses,
            data=data,
        )

    # ───────────────────────────────────────────────────────
    # 3.4 任务完成统计
    # ───────────────────────────────────────────────────────

    async def get_task_completion_stats(self) -> TaskCompletionStatsResponse:
        """获取任务完成统计"""
        task_types = ["arrangement", "review L1", "review L2", "review L3"]
        stats = {}

        for task_type in task_types:
            result = await self.db.execute(
                select(Task.task_status, func.count(Task.id))
                .join(Content, Content.id == Task.content_id)
                .where(
                    Task.task_type == task_type,
                    Task.is_deleted.is_(False),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
                .group_by(Task.task_status)
            )
            status_counts = {row[0]: row[1] for row in result.all()}

            # Pending = 待处理/待审核, Completed = 已处理/已审核
            stats[task_type.replace(" ", "_").lower()] = [
                PieDataItem(name="Pending", value=status_counts.get("Pending", 0)),
                PieDataItem(name="Completed", value=status_counts.get("Completed", 0)),
            ]

        return TaskCompletionStatsResponse(
            arrangement=stats["arrangement"],
            review_l1=stats["review_l1"],
            review_l2=stats["review_l2"],
            review_l3=stats["review_l3"],
        )

    # ───────────────────────────────────────────────────────
    # 3.6 任务状态数量统计
    # ───────────────────────────────────────────────────────

    async def get_task_status_count(self) -> TaskStatusCountResponse:
        """获取任务状态数量统计"""
        # Pending 和 Completed 按 task_status 统计
        result = await self.db.execute(
            select(Task.task_status, func.count(Task.id))
            .join(Content, Content.id == Task.content_id)
            .where(Task.is_deleted.is_(False), Content.is_deleted.is_(False), Content.is_discarded.is_(False))
            .group_by(Task.task_status)
        )
        status_counts = {row[0]: row[1] for row in result.all()}

        # Not Assigned 按 assignee_id is None 且 task_status == "Not Assigned" 统计
        # 与任务列表 API 的 assignee_is_null 查询条件保持一致（包含 Content.is_deleted.is_(False) 条件）
        not_assigned_result = await self.db.execute(
            select(func.count(Task.id))
            .join(Content, Content.id == Task.content_id)
            .where(
                Task.assignee_id.is_(None),
                Task.task_status == "Not Assigned",
                Task.is_deleted.is_(False),
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
        not_assigned_count = not_assigned_result.scalar() or 0

        return TaskStatusCountResponse(
            pending=status_counts.get("Pending", 0),
            completed=status_counts.get("Completed", 0),
            not_assigned=not_assigned_count,
        )

    # ───────────────────────────────────────────────────────
    # 3.7 任务分配与状态矩阵
    # ───────────────────────────────────────────────────────

    async def get_task_assigned_matrix(self) -> TaskAssignedMatrixResponse:
        """获取任务分配与状态矩阵"""
        # 获取所有用户
        user_result = await self.db.execute(
            select(User.id, User.username, User.display_name)
            .where(User.is_deleted.is_(False), User.status == "active")
        )
        users = user_result.all()

        user_names = []
        data = []

        for user_id, username, display_name in users:
            user_name = f"{display_name or username}（{username}）"
            user_names.append(user_name)

            # 统计各类型任务数量
            result = await self.db.execute(
                select(Task.task_type, Task.task_status, func.count(Task.id))
                .join(Content, Content.id == Task.content_id)
                .where(
                    Task.assignee_id == user_id,
                    Task.is_deleted.is_(False),
                    Content.is_deleted.is_(False),
                    Content.is_discarded.is_(False),
                )
                .group_by(Task.task_type, Task.task_status)
            )

            counts = {}
            for row in result.all():
                task_type, task_status, count = row
                key = f"{task_type}_{task_status}"
                counts[key] = count

            arrangement_pending = counts.get("arrangement_Pending", 0)
            review_l1_pending = counts.get("review L1_Pending", 0)
            review_l2_pending = counts.get("review L2_Pending", 0)
            review_l3_pending = counts.get("review L3_Pending", 0)
            arrangement_completed = counts.get("arrangement_Completed", 0)
            review_completed = (
                counts.get("review L1_Completed", 0)
                + counts.get("review L2_Completed", 0)
                + counts.get("review L3_Completed", 0)
            )

            total = arrangement_pending + review_l1_pending + review_l2_pending + review_l3_pending + arrangement_completed + review_completed
            completion_rate = (review_completed / total * 100) if total > 0 else 0

            data.append(
                TaskAssignedMatrixItem(
                    user_id=user_id,
                    user_name=user_name,
                    arrangement_pending=arrangement_pending,
                    review_l1_pending=review_l1_pending,
                    review_l2_pending=review_l2_pending,
                    review_l3_pending=review_l3_pending,
                    arrangement_completed=arrangement_completed,
                    review_completed=review_completed,
                    completion_rate=round(completion_rate, 2),
                )
            )

        return TaskAssignedMatrixResponse(users=user_names, data=data)

    # ───────────────────────────────────────────────────────
    # 3.8 综合看板数据
    # ───────────────────────────────────────────────────────

    async def get_dashboard_data(self, user_id: int, current_user: User | None = None) -> DashboardDataResponse:
        """获取综合看板数据"""
        config_service = DashboardConfigService(self.db)
        config = await config_service.get_or_create_config(user_id)

        visible_genres = [
            item.get("name") for item in config.content_genre_config
            if item.get("visible") and item.get("name")
        ]
        visible_statuses = [
            item.get("code") for item in config.content_status_config
            if item.get("visible") and item.get("code")
        ]
        logger.info(f"get_dashboard_data 用户 {user_id} 的可见题材: {visible_genres}")
        logger.info(f"get_dashboard_data 用户 {user_id} 的可见状态: {visible_statuses}")
        logger.info(f"get_dashboard_data 用户配置: {config.content_genre_config}")

        can_see_task = current_user is not None and await has_task_assign_role(self.db, current_user)

        task_completion_stats = await self.get_task_completion_stats() if can_see_task else TaskCompletionStatsResponse(
            arrangement=[], review_l1=[], review_l2=[], review_l3=[],
        )
        task_status_count = await self.get_task_status_count() if can_see_task else TaskStatusCountResponse(
            pending=0, completed=0, not_assigned=0,
        )
        task_assigned_matrix = await self.get_task_assigned_matrix() if can_see_task else TaskAssignedMatrixResponse(
            users=[], data=[],
        )

        return DashboardDataResponse(
            published_stats=await self.get_published_stats(visible_genres, current_user),
            content_status_count=await self.get_content_status_count(current_user),
            genre_status_matrix=await self.get_genre_status_matrix(visible_genres, visible_statuses, current_user),
            task_completion_stats=task_completion_stats,
            task_status_count=task_status_count,
            task_assigned_matrix=task_assigned_matrix,
        )
