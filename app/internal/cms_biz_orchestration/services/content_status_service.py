"""
内容状态检查服务。

封装所有内容操作入口的状态判断逻辑，确保前后端判断一致。
"""

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_metada.models.basic import Picture, PosterSize
from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap
from app.internal.cms_biz_orchestration.models.content_metadata import ContentMetadata
from app.internal.cms_biz_orchestration.models.movie import Movie
from app.internal.cms_biz_package.models.package import Content, ContentPackage, ContentCategory


_STATUS_PRIORITY: dict[str, int] = {
    "None": 0,
    "WaitingForMaterials": 1,
    "InProgress": 2,
    "ReadyForPublish": 3,
    "Publishing": 4,
    "PublishFailed": 5,
    "Published": 6,
    "NoActiveLicense": 7,
    "Closed": 8,
}

_DERIVABLE_STATUSES = {"None", "WaitingForMaterials", "InProgress"}


class ContentStatusService:
    """内容状态检查服务，统一封装所有状态判断逻辑。"""

    @staticmethod
    async def check_materials(db: AsyncSession, content_id: int) -> bool:
        """
        检查材料注入是否完成。
        判断标准：有正片(movie_type=1)即算完成。
        """
        result = await db.execute(
            select(Movie).where(
                Movie.content_id == content_id,
                Movie.movie_type == 1,
                Movie.is_deleted.is_(False), Movie.is_discarded.is_(False)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_metadata(db: AsyncSession, content_id: int, content_type: str | None = None) -> bool:
        """
        检查元数据是否完成。
        判断标准：内容类型对应的元数据表有记录即算完成。
        - MOVIE/EPISODE → ContentMetadata
        - SERIES/SEASON/SEASON_SERIES → SeriesMetadata
        - CHANNEL → ChannelMetadata
        - SCHEDULE → ScheduleMetadata
        """
        from app.internal.cms_biz_orchestration.models.content_metadata import (
            ChannelMetadata, ScheduleMetadata, SeriesMetadata,
        )

        if content_type in ("SERIES", "SEASON", "SEASON_SERIES"):
            model = SeriesMetadata
        elif content_type == "CHANNEL":
            model = ChannelMetadata
        elif content_type == "SCHEDULE":
            model = ScheduleMetadata
        else:
            # MOVIE/EPISODE 及未传类型默认查 VOD 元数据表
            model = ContentMetadata
        result = await db.execute(
            select(model).where(
                model.content_id == content_id,
                model.is_deleted.is_(False), model.is_discarded.is_(False)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def get_posters_completion(
        db: AsyncSession, content_id: int, content_type: str
    ) -> dict:
        """
        获取海报节点的完成状态（含 completed / warning / detail）。

        判断标准：
        1. 有必传规格时，所有必传规格都有海报即算完成
        2. 无必传规格时，有任意海报即算完成
        """
        entity_type = _get_entity_type(content_type)
        belonging = entity_type.capitalize()

        # 查询当前内容类型适用的必传海报规格（只查未删除）
        poster_sizes = (
            await db.execute(
                select(PosterSize).where(
                    PosterSize.mandatory.is_(True),
                    PosterSize.is_deleted.is_(False),
                )
            )
        ).scalars().all()

        applicable_mandatory_sizes = [
            ps for ps in poster_sizes
            if any(b.belonging in (belonging, 'ALL') for b in ps.belongings)
        ]

        # 查询已上传的海报规格 ID（排除软删除/废弃）
        uploaded_size_ids = {
            row[0]
            for row in (
                await db.execute(
                    select(Picture.poster_size_id).where(
                        Picture.entity_type == entity_type,
                        Picture.entity_id == content_id,
                        Picture.is_deleted.is_(False),
                        Picture.is_discarded.is_(False),
                    )
                )
            ).all()
        }

        if not applicable_mandatory_sizes:
            pic_count = len(uploaded_size_ids)
            return {
                "completed": pic_count > 0,
                "warning": pic_count == 0,
                "detail": f"{pic_count} poster(s)" if pic_count else "No posters",
            }

        mandatory_size_ids = {ps.id for ps in applicable_mandatory_sizes}
        uploaded_mandatory_count = len(uploaded_size_ids & mandatory_size_ids)
        completed = uploaded_mandatory_count >= len(mandatory_size_ids)
        return {
            "completed": completed,
            "warning": 0 < uploaded_mandatory_count < len(mandatory_size_ids),
            "detail": f"{uploaded_mandatory_count}/{len(mandatory_size_ids)} mandatory posters",
        }

    @staticmethod
    async def check_posters(db: AsyncSession, content_id: int, content_type: str) -> bool:
        """检查海报是否完成。"""
        result = await ContentStatusService.get_posters_completion(
            db, content_id, content_type
        )
        return result["completed"]

    @staticmethod
    async def check_cast_role_map(db: AsyncSession, content_id: int) -> bool:
        """
        检查演员角色映射是否完成。
        判断标准：有演员角色映射记录即算完成。
        """
        result = await db.execute(
            select(CastRoleMap).where(
                CastRoleMap.content_id == content_id,
                CastRoleMap.is_deleted.is_(False), CastRoleMap.is_discarded.is_(False)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_trailer(db: AsyncSession, content_id: int) -> bool:
        """
        检查预告片是否上传（可选）。
        判断标准：有预告片(movie_type=2)即算完成。
        """
        result = await db.execute(
            select(Movie).where(
                Movie.content_id == content_id,
                Movie.movie_type == 2,
                Movie.is_deleted.is_(False), Movie.is_discarded.is_(False)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_music_effects(db: AsyncSession, content_id: int) -> bool:
        """
        检查音乐音效文件是否上传（可选）。
        判断标准：有音乐音效文件(movie_type=3)即算完成。
        """
        result = await db.execute(
            select(Movie).where(
                Movie.content_id == content_id,
                Movie.movie_type == 3,
                Movie.is_deleted.is_(False), Movie.is_discarded.is_(False)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_package(db: AsyncSession, content_id: int) -> bool:
        """
        检查服务包是否关联。
        判断标准：有服务包关联记录即算完成。
        """
        result = await db.execute(
            select(ContentPackage).where(
                ContentPackage.content_id == content_id,
                ContentPackage.is_deleted.is_(False),
                ContentPackage.is_discarded.is_(False),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_category(db: AsyncSession, content_id: int) -> bool:
        """
        检查栏目是否关联。
        判断标准：有栏目关联记录即算完成。
        """
        result = await db.execute(
            select(ContentCategory).where(
                ContentCategory.content_id == content_id,
                ContentCategory.is_deleted.is_(False), ContentCategory.is_discarded.is_(False)
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_sub_content(db: AsyncSession, content_id: int) -> bool:
        """
        检查是否绑定了子内容。
        判断标准：有未删除的子内容记录即算完成。
        用于 SERIES/SEASON 类型判断是否需要进入待上传素材状态。
        """
        result = await db.execute(
            select(Content).where(
                Content.parent_id == content_id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def check_physical_channel(db: AsyncSession, content_id: int) -> bool:
        """
        检查是否绑定了物理频道。
        判断标准：有未删除的物理频道记录即算完成。
        用于 CHANNEL 类型判断是否需要进入待上传素材状态。
        """
        from app.internal.cms_biz_package.models.package import PhysicalChannel

        result = await db.execute(
            select(PhysicalChannel).where(
                PhysicalChannel.channel_id == content_id,
                PhysicalChannel.is_deleted.is_(False), PhysicalChannel.is_discarded.is_(False),
            ).limit(1)
        )
        return result.scalar_one_or_none() is not None

    @staticmethod
    async def should_be_waiting_for_materials(
        db: AsyncSession,
        content_id: int,
        content_type: str,
    ) -> bool:
        """
        判断内容是否应该变为待上传素材状态。

        规则：
        - PROGRAM (MOVIE/EPISODE): 没有上传素材（正片）→ 返回 True
        - SERIES/SEASON: 没有绑定子内容 → 返回 True
        - CHANNEL: 没有绑定物理频道 → 返回 True
        - SCHEDULE/ARCHIVED: 不存在待上传素材状态 → 返回 False
        """
        if content_type in ("SCHEDULE",):
            return False

        is_archived_check = (
            await db.execute(
                select(Content.is_archived).where(Content.id == content_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False)).limit(1)
            )
        ).scalar_one_or_none()
        if is_archived_check is True:
            return False

        if content_type in ("MOVIE", "EPISODE"):
            return not await ContentStatusService.check_materials(db, content_id)

        if content_type in ("SERIES", "SEASON", "SEASON_SERIES"):
            return not await ContentStatusService.check_sub_content(db, content_id)

        if content_type == "CHANNEL":
            return not await ContentStatusService.check_physical_channel(db, content_id)

        return False

    @staticmethod
    async def get_all_status(db: AsyncSession, content_id: int, content_type: str) -> dict:
        """
        获取所有状态检查结果。
        返回一个字典，包含所有操作入口的完成状态。
        """
        return {
            'materials': await ContentStatusService.check_materials(db, content_id),
            'metadata': await ContentStatusService.check_metadata(db, content_id, content_type),
            'posters': await ContentStatusService.check_posters(db, content_id, content_type),
            'cast_role_map': await ContentStatusService.check_cast_role_map(db, content_id),
            'trailer': await ContentStatusService.check_trailer(db, content_id),
            'music_effects': await ContentStatusService.check_music_effects(db, content_id),
            'package': await ContentStatusService.check_package(db, content_id),
            'category': await ContentStatusService.check_category(db, content_id),
        }

    @staticmethod
    async def derive_status_from_children(db: AsyncSession, parent_id: int) -> str | None:
        """
        根据子内容状态派生父内容状态。

        仅同步 InProgress / WaitingForMaterials，父级进入 ReadyForPublish+ 后
        完全由自己的流程节点驱动。

        - 任一子内容为 InProgress → 父内容 InProgress（把父级从 None/WaitingForMaterials 拉起）
        - 无 InProgress 子内容但有子内容 → 返回 None（不触发父级更新，父级保持当前状态）
        - 无子内容 → 返回 WaitingForMaterials（父级缺少素材）

        仅适用于 SERIES / SEASON 类型。
        """
        result = await db.execute(
            select(Content.status).where(
                Content.parent_id == parent_id,
                Content.is_deleted.is_(False),
                Content.is_discarded.is_(False),
            )
        )
        child_statuses = [row[0] for row in result.all()]

        if not child_statuses:
            return "WaitingForMaterials"

        if any(s == "InProgress" for s in child_statuses):
            return "InProgress"
        return None

    @staticmethod
    async def sync_parent_status(
        db: AsyncSession,
        content_id: int,
        content_type: str,
    ) -> None:
        """
        当子内容状态变更后，同步更新父内容（SERIES/SEASON）的状态。

        仅在父内容当前状态处于可派生范围（None/WaitingForMaterials/InProgress）时
        才用子内容状态覆盖，避免将已进入后续阶段（ReadyForPublish+）的父内容拉回。

        递归向上：EPISODE → SERIES → SEASON。
        """
        if content_type not in ("MOVIE", "EPISODE", "SERIES", "SEASON_SERIES"):
            return

        # 查询触发同步的子内容：允许 is_discarded=True（删除子内容后仍需向上同步父级状态）
        content = (
            await db.execute(
                select(Content).where(Content.id == content_id, Content.is_deleted.is_(False))
            )
        ).scalar_one_or_none()
        if not content or content.parent_id is None:
            return

        parent = (
            await db.execute(
                select(Content).where(Content.id == content.parent_id, Content.is_deleted.is_(False), Content.is_discarded.is_(False))
            )
        ).scalar_one_or_none()
        if not parent:
            return

        if parent.content_type not in ("SERIES", "SEASON_SERIES", "SEASON"):
            return

        derived = await ContentStatusService.derive_status_from_children(db, parent.id)
        if derived is None:
            return

        if parent.status not in _DERIVABLE_STATUSES:
            logger.info(
                "父内容状态不可派生覆盖 | parent_id={} status={} derived={}",
                parent.id, parent.status, derived,
            )
            return

        if parent.status == derived:
            return

        old_status = parent.status
        parent.status = derived

        from app.internal.cms_biz_orchestration.services.workflow_service import record_status_change
        await record_status_change(
            db,
            content_id=parent.id,
            before_status=old_status,
            after_status=derived,
            processed_by="system",
        )

        logger.info(
            "同步父内容状态 | parent_id={} {} → {} (由子内容 content_id={} 触发)",
            parent.id, old_status, derived, content_id,
        )

        if parent.parent_id is not None:
            await ContentStatusService.sync_parent_status(db, parent.id, parent.content_type)


def _get_entity_type(content_type: str) -> str:
    """
    根据内容类型获取 entity_type（与前端保持一致，SERIES/SEASON_SERIES/SEASON 统一为 series）。
    """
    mapping = {
        'MOVIE': 'program',
        'EPISODE': 'program',
        'SERIES': 'series',
        'SEASON_SERIES': 'series',
        'SEASON': 'series',
        'CHANNEL': 'channel',
        'SCHEDULE': 'schedule',
    }
    return mapping.get(content_type, 'content')
