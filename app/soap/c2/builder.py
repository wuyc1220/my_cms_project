"""
ADI XML 主构建器。

作为 C2 模块对外唯一入口，串联 loader → objects → mappings，
输出符合 C2 规范的完整 ADI XML 字符串。

核心方法：
    build_publish_xml(content_id)    → 生成发布 XML（REGIST/UPDATE）
    build_unpublish_xml(content_id)  → 生成下架 XML（DELETE）
"""
from __future__ import annotations

import uuid
from datetime import datetime
from xml.dom import minidom
from xml.etree.ElementTree import Element, SubElement, tostring

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.internal.cms_biz_orchestration.services.storage import storage_service
from app.soap.config import soap_settings
from app.common.core.i18n import get_msg
from app.config import app_tz

from . import objects as obj_builders
from .constants import (
    CONTENT_TYPE_TO_ELEMENT,
    PICTURE_ENTITY_TYPE_TO_ELEMENT,
    Action,
    ElementType,
)
from .loader import BuildContext, load_build_context
from .mappings import add_mapping
from .strategy import decide_action


class ADIBuilder:
    """
    ADI XML 主构建器。

    使用示例::

        builder = ADIBuilder(db)
        xml_str = await builder.build_publish_xml(content_id=123)
        filepath = builder.write_to_file(xml_str, correlate_id="uuid-xxx")
    """

    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    # ═══════════════════════════════════════════════════════
    # 对外接口
    # ═══════════════════════════════════════════════════════
    async def build_publish_xml(self, content_id: int) -> str:
        """
        构建发布 XML（统一 REGIST）。

        所有关联对象（含 Category）每次发布均全量输出 Object + Mapping，
        无 SKIP 去重（与 Package 行为一致）。
        """
        return await self._build(content_id, is_unpublish=False)

    async def build_unpublish_xml(self, content_id: int) -> str:
        """
        构建下架 XML（DELETE）。

        DELETE 的 Object 只携带 ElementType + ID，无 Property；
        LSP 会自动级联删除与该对象相关的所有 Mapping。
        """
        return await self._build(content_id, is_unpublish=True)

    def write_to_file(
        self, xml_str: str, correlate_id: str, entity_id: int | None = None
    ) -> str:
        """
        将 XML 字符串上传到 SFTP 的 c2 目录。

        文件名格式: ``c2/YYYYMM/adi_{entity_id}_{correlate_id_8}_{timestamp}.xml``

        :return: SFTP 相对路径（格式: c2/YYYYMM/xxx.xml）
        """
        from .file_utils import generate_c2_filename, upload_c2_xml

        eid = entity_id if entity_id is not None else "na"

        # 自定义文件名: YYYYMM/content_{entity_id}_{uuid8}_{timestamp_ms}.xml
        now = datetime.now()
        date_folder = now.strftime("%Y%m")
        ts = now.strftime("%Y%m%d_%H%M%S") + f"_{now.microsecond // 1000:03d}"
        short_uuid = uuid.uuid4().hex[:8]
        filename = f"{date_folder}/content_{eid}_{short_uuid}_{ts}.xml"

        return upload_c2_xml(xml_str, filename, storage_service)

    # ═══════════════════════════════════════════════════════
    # 内部构建流程
    # ═══════════════════════════════════════════════════════
    async def _build(self, content_id: int, is_unpublish: bool) -> str:
        """加载上下文 → 构建对象/映射 → 序列化"""
        ctx = await load_build_context(self.db, content_id)
        if ctx is None:
            raise ValueError(get_msg("SOAP_CONTENT_NOT_FOUND", id=content_id))

        root = self._build_root()
        objects_el = SubElement(root, "Objects")
        mappings_el = SubElement(root, "Mappings")

        ct = ctx.content.content_type
        if ct in ("MOVIE", "EPISODE"):
            await self._build_program_scope(ctx, objects_el, mappings_el, is_unpublish)
        elif ct in ("SERIES", "SEASON_SERIES", "SEASON"):
            await self._build_series_scope(ctx, objects_el, mappings_el, is_unpublish)
        elif ct == "CHANNEL":
            await self._build_channel_scope(ctx, objects_el, mappings_el, is_unpublish)
        elif ct == "SCHEDULE":
            await self._build_schedule_scope(ctx, objects_el, mappings_el, is_unpublish)
        else:
            raise ValueError(get_msg("SOAP_UNSUPPORTED_CONTENT_TYPE", ct=ct))

        await self._attach_common_mappings(ctx, objects_el, mappings_el, is_unpublish)

        return _serialize(root)

    # ─────────────────────────────────────────────────────
    # 根元素骨架
    # ─────────────────────────────────────────────────────
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

    # ─────────────────────────────────────────────────────
    # 构建分支：MOVIE / EPISODE (Program)
    # ─────────────────────────────────────────────────────
    async def _build_program_scope(
        self,
        ctx: BuildContext,
        objs: Element,
        maps: Element,
        is_unpublish: bool,
    ) -> None:
        """
        Program 作用域：Program + Movie + CastRoleMap + 关联 Cast。

        XML 包含对象：
            - Program 主体（必定）
            - Movie 列表（全部 REGIST）
            - Cast（先于 CastRoleMap）
            - CastRoleMap
        Mappings：
            - Program → Movie
            - Program → CastRoleMap
            - （如果是 EPISODE）父 SERIES → Program
        """
        content = ctx.content
        program_action = decide_action(is_unpublish)

        # 1. Program 主体
        objs.append(
            obj_builders.build_program_object(
                content, ctx.program_meta, program_action,
                license_data=ctx.license_data,
                genre_name=ctx.genre_name,
                genre_i18n=ctx.genre_i18n,
                content_type_name=ctx.content_type_name,
                custom_fields=ctx.custom_fields,
                tag_names=ctx.tag_names,
                tag_i18n=ctx.tag_i18n,
                i18n_data=ctx.i18n_data,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
            )
        )

        # 2. Movies（关联对象不下架，下架时跳过）
        map_action = decide_action(is_unpublish)
        for movie in ctx.movies:
            if is_unpublish:
                continue

            movie_i18n = ctx.movie_i18n_data.get(movie.id)
            objs.append(obj_builders.build_movie_object(
                movie, Action.REGIST,
                i18n_data=movie_i18n,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                custom_fields=ctx.movie_custom_fields.get(movie.id, {}),
            ))
            add_mapping(
                self._maps_list(maps),
                ElementType.PROGRAM, content.id,
                ElementType.MOVIE, movie.id,
                map_action,
            )

        # 3. Cast + CastRoleMap（关联对象不下架，下架时跳过）
        for cast in ctx.casts:
            if is_unpublish:
                continue

            cast_i18n = ctx.cast_i18n_data.get(cast.id)
            objs.append(obj_builders.build_cast_object(
                cast, Action.REGIST,
                i18n_data=cast_i18n,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                custom_fields=ctx.cast_custom_fields.get(cast.id, {}),
            ))

        for rm in ctx.cast_role_maps:
            if is_unpublish:
                continue

            objs.append(obj_builders.build_cast_role_map_object(rm, Action.REGIST, custom_fields=ctx.cast_role_map_custom_fields.get(rm.map_id, {})))
            add_mapping(
                self._maps_list(maps),
                ElementType.PROGRAM, content.id,
                ElementType.CAST_ROLE_MAP, rm.map_id,
                decide_action(is_unpublish),
            )

        # 4. EPISODE 的父 SERIES → Program Mapping
        if content.content_type == "EPISODE" and content.parent_id:
            ep_sequence = ctx.content.sequence
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.parent_id,
                ElementType.PROGRAM, content.id,
                decide_action(is_unpublish),
                sequence=ep_sequence,
            )

    # ─────────────────────────────────────────────────────
    # 构建分支：SERIES / SEASON
    # ─────────────────────────────────────────────────────
    async def _build_series_scope(
        self,
        ctx: BuildContext,
        objs: Element,
        maps: Element,
        is_unpublish: bool,
    ) -> None:
        """
        Series 作用域：Series 主体 + Movie + CastRoleMap + Cast + 子 Content Mapping。

        注意：子 Program 的 Movie/CastRoleMap 不在此处展开；
             它们在子 Program 自己的发布任务中处理。
             但 Series 自身的 Movie（如片花/预告片）在此处输出。
        """
        content = ctx.content
        series_action = decide_action(is_unpublish)

        # 1. Series 主体
        objs.append(
            obj_builders.build_series_object(
                content, ctx.series_meta, series_action,
                license_data=ctx.license_data,
                genre_name=ctx.genre_name,
                genre_i18n=ctx.genre_i18n,
                content_type_name=ctx.content_type_name,
                custom_fields=ctx.custom_fields,
                tag_names=ctx.tag_names,
                tag_i18n=ctx.tag_i18n,
                i18n_data=ctx.i18n_data,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                children_count=len(ctx.children),
            )
        )

        # 2. CastRoleMap（关联对象下架时跳过）
        for rm in ctx.cast_role_maps:
            if is_unpublish:
                continue

            objs.append(obj_builders.build_cast_role_map_object(rm, Action.REGIST, custom_fields=ctx.cast_role_map_custom_fields.get(rm.map_id, {})))
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.id,
                ElementType.CAST_ROLE_MAP, rm.map_id,
                decide_action(is_unpublish),
            )

        # 3. 关联的 Cast（关联对象下架时跳过）
        for cast in ctx.casts:
            if is_unpublish:
                continue

            cast_i18n = ctx.cast_i18n_data.get(cast.id)
            objs.append(obj_builders.build_cast_object(
                cast, Action.REGIST,
                i18n_data=cast_i18n,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                custom_fields=ctx.cast_custom_fields.get(cast.id, {}),
            ))

        # 3.5 Series 自身的 Movie（如片花/预告片）
        for movie in ctx.movies:
            if is_unpublish:
                continue

            movie_i18n = ctx.movie_i18n_data.get(movie.id)
            objs.append(obj_builders.build_movie_object(
                movie, Action.REGIST,
                i18n_data=movie_i18n,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                custom_fields=ctx.movie_custom_fields.get(movie.id, {}),
            ))
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.id,
                ElementType.MOVIE, movie.id,
                decide_action(is_unpublish),
            )

        # 4. 子 Content Mapping（Series → 子 Program 或 Series → 子 Series）
        for child in ctx.children:
            child_et = CONTENT_TYPE_TO_ELEMENT.get(child.content_type)
            if child_et is None:
                continue
            child_seq = child.sequence if child.content_type == "EPISODE" else child.series_ordinal
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.id,
                child_et, child.id,
                decide_action(is_unpublish),
                sequence=child_seq,
            )

    # ─────────────────────────────────────────────────────
    # 构建分支：CHANNEL
    # ─────────────────────────────────────────────────────
    async def _build_channel_scope(
        self,
        ctx: BuildContext,
        objs: Element,
        maps: Element,
        is_unpublish: bool,
    ) -> None:
        """
        Channel 作用域：Channel 主体 + PhysicalChannel 追加。

        关键设计决策：
        - PhysicalChannel / Schedule（节目单）与 Channel 的从属关系由各自 Object 内的
          ChannelID 属性表达，**不再生成额外 Mapping**。
        - 下架频道时，不会级联下架其下的节目单（Schedule），Schedule 需要单独下架。
          因此无论发布还是下架，Channel 的 Mappings 中都不包含 Schedule。
        """
        content = ctx.content
        channel_action = decide_action(is_unpublish)

        # 1. Channel 主体
        objs.append(
            obj_builders.build_channel_object(
                content, ctx.channel_meta, channel_action,
                license_data=ctx.license_data,
                provider_code=ctx.provider_code,
                custom_fields=ctx.custom_fields,
                i18n_data=ctx.i18n_data,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                pictures=ctx.pictures,
                poster_size_mapping_types=ctx.poster_size_mapping_types,
            )
        )

        # 2. PhysicalChannel（关联对象下架时跳过）
        for pc in ctx.physical_channels:
            if is_unpublish:
                continue

            objs.append(
                obj_builders.build_physical_channel_object(
                    pc, Action.REGIST,
                    channel_type=ctx.channel_type,
                    channel_name=content.title,
                    channel_description=ctx.channel_meta.description if ctx.channel_meta else None,
                    custom_fields=ctx.physical_channel_custom_fields.get(pc.id, {}),
                )
            )

    async def _build_schedule_scope(
        self,
        ctx: BuildContext,
        objs: Element,
        maps: Element,
        is_unpublish: bool,
    ) -> None:
        """Schedule 作用域：Schedule 主体（与父 Channel 的关系由 ChannelID 属性表达）"""
        content = ctx.content
        schedule_action = decide_action(is_unpublish)

        objs.append(
            obj_builders.build_schedule_object(
                content, ctx.schedule_meta, schedule_action,
                license_data=ctx.license_data,
                channel_id=ctx.schedule_channel_id,
                channel_code=ctx.schedule_channel_code,
                genre_name=ctx.genre_name,
                genre_i18n=ctx.genre_i18n,
                custom_fields=ctx.custom_fields,
                i18n_data=ctx.i18n_data,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
                schedule_actor_i18n=ctx.schedule_actor_i18n,
                schedule_director_i18n=ctx.schedule_director_i18n,
                schedule_package_ids=ctx.schedule_package_ids,
                series_package_ids=ctx.series_package_ids,
                series_children_count=ctx.series_children_count,
                pictures=ctx.pictures,
                poster_size_mapping_types=ctx.poster_size_mapping_types,
            )
        )

    # ─────────────────────────────────────────────────────
    # 公共挂载：Category / Package / Picture
    # ─────────────────────────────────────────────────────
    async def _attach_common_mappings(
        self,
        ctx: BuildContext,
        objs: Element,
        maps: Element,
        is_unpublish: bool,
    ) -> None:
        """
        为所有类型通用的 Mapping 挂载：
            - Category → 主体（Program/Series/Channel）
            - Package  → 主体（Program/Series）
            - Picture  → 主体（Program/Series/Category/Cast，不含 Channel）
        
        关联对象在下架时跳过，不生成 DELETE。
        发布时所有关联对象统一 REGIST，全量输出。
        """
        content = ctx.content
        main_et = CONTENT_TYPE_TO_ELEMENT.get(content.content_type)
        if main_et is None:
            return

        map_action = decide_action(is_unpublish)

        # ── Category（可挂 Program/Series/Channel，关联对象下架时跳过） ─────
        # 与 Package 行为对齐：每次发布无条件全量输出 Object（无 SKIP 去重）
        for idx, (cat, cat_sequence) in enumerate(ctx.categories, start=1):
            if is_unpublish:
                continue

            cat_i18n = ctx.category_i18n_data.get(cat.id)
            # bug 32017：PosterType 字段先保留传空，无需再从首张海报取 mapping_type；
            # 待 C2 规范明确后，可在此重新收集 cat_pics 并传入 poster_type
            objs.append(
                obj_builders.build_category_object(
                    cat, Action.REGIST,
                    i18n_data=cat_i18n,
                    languages=ctx.languages,
                    primary_language=ctx.primary_language,
                    custom_fields=ctx.category_custom_fields.get(cat.id, {}),
                )
            )
            add_mapping(
                self._maps_list(maps),
                ElementType.CATEGORY, cat.id,
                main_et, content.id,
                map_action,
                sequence=cat_sequence if cat_sequence is not None else idx,  # 优先使用数据库 sequence，为 None 时使用索引
                licensing_window_start=ctx.license_data.get("licensing_window_start"),
                licensing_window_end=ctx.license_data.get("licensing_window_end"),
            )

        # ── Package（仅 Program/Series，关联对象下架时跳过） ────────────────
        for pkg in ctx.packages:
            if is_unpublish:
                continue

            objs.append(
                obj_builders.build_package_object(
                    pkg, Action.REGIST,
                    custom_fields=ctx.package_custom_fields.get(pkg.id, {}),
                )
            )
            add_mapping(
                self._maps_list(maps),
                ElementType.PACKAGE, pkg.id,
                main_et, content.id,
                map_action,
            )

        # ── Picture（多态，按 entity_type 反查目标 ElementType，关联对象下架时跳过） ─
        # 记录需要更新状态的海报
        pictures_to_update = []
        # 节目单/频道的海报已提前单独发布，不重复生成 Picture Object
        is_schedule_or_channel = content.content_type in ("SCHEDULE", "CHANNEL")
        
        for pic in ctx.pictures:
            if is_unpublish:
                continue

            pic_target_et = PICTURE_ENTITY_TYPE_TO_ELEMENT.get(pic.entity_type.lower(), main_et)
            poster_size_name = ctx.poster_size_names.get(pic.id)
            if not is_schedule_or_channel:
                objs.append(obj_builders.build_picture_object(
                    pic, Action.REGIST, poster_size_name=poster_size_name,
                    custom_fields=ctx.picture_custom_fields.get(pic.poster_size_id, {}),
                ))
            
            # 记录需要更新状态的海报
            pictures_to_update.append(pic)
            
            if pic_target_et != ElementType.CHANNEL:
                pic_mapping_type = ctx.poster_size_mapping_types.get(pic.id)
                add_mapping(
                    self._maps_list(maps),
                    ElementType.PICTURE, pic.id,
                    pic_target_et,
                    pic.entity_id,
                    map_action,
                    mapping_type=pic_mapping_type,
                )
        
        # 更新海报的发布状态（设置为 processing，等待 SOAP 回调后更新为 success/failed）
        # 节目单/频道发布时：不创建 ingest_history 记录，且已 success 的海报不更新状态
        if pictures_to_update:
            await self._update_pictures_ingest_status(
                pictures_to_update, "processing",
                create_history=not is_schedule_or_channel,
                skip_if_success=is_schedule_or_channel,
            )

        # 更新关联 Cast 的发布状态为 processing（发布时；下架时跳过）
        # 与 Picture 保持一致，等待 SOAP 回调后由 router 更新为 success/failure
        if not is_unpublish and ctx.casts:
            await self._update_casts_ingest_status(
                ctx.casts, "processing",
                create_history=not is_schedule_or_channel,
                skip_if_success=is_schedule_or_channel,
            )

    # ─────────────────────────────────────────────────────
    # 工具函数
    # ─────────────────────────────────────────────────────
    @staticmethod
    def _maps_list(maps_el: Element) -> list[Element]:
        """
        直接操作 ElementTree 的子节点列表。

        ElementTree 的 Element 本身就是可迭代容器，支持 append。
        :returns: 可用于 add_mapping 的列表引用
        """
        return maps_el  # type: ignore[return-value]

    async def _update_pictures_ingest_status(
        self,
        pictures: list,
        status: str,
        xml_path: str | None = None,
        correlate_id: str | None = None,
        create_history: bool = True,
        skip_if_success: bool = False,
    ) -> None:
        """
        更新海报的注入状态。
        同时更新 Picture 表的 ingest_status 字段，并可选创建 IngestHistory 记录保存详细信息。

        Args:
            pictures: 海报列表
            status: 状态 (none/processing/success/failed)
            xml_path: C2 XML 文件路径（可选）
            correlate_id: SOAP 关联 ID（可选）
            create_history: 是否创建 IngestHistory 记录（默认 True）
            skip_if_success: 如果海报已经是 success 状态则跳过更新（用于节目单发布时不覆盖已发布海报状态）
        """
        from datetime import datetime, timezone
        from sqlalchemy import update
        from app.internal.cms_biz_metada.models.basic import Picture
        from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory

        current_time = datetime.now(timezone.utc)
        
        # 1. 更新 Picture 表的 ingest_status 字段
        for pic in pictures:
            if skip_if_success and getattr(pic, 'ingest_status', None) == "success":
                continue
            await self.db.execute(
                update(Picture)
                .where(Picture.id == pic.id)
                .values(
                    ingest_status=status,
                    updated_at=current_time,
                )
            )
        
        # 2. 创建 IngestHistory 记录保存详细信息（仅在 processing 或最终状态时创建）
        if create_history and status in ("processing", "success", "failed"):
            for pic in pictures:
                if skip_if_success and getattr(pic, 'ingest_status', None) == "success":
                    continue
                history = IngestHistory(
                    entity_type="Picture",
                    entity_id=pic.id,
                    action="REGIST" if status in ("processing", "success") else "DELETE",
                    status="processing" if status == "processing" else ("success" if status == "success" else "failure"),
                    ingest_xml_path=xml_path,
                    correlate_id=correlate_id,
                    send_date=current_time,
                )
                self.db.add(history)
        
        await self.db.commit()
        logger.info(f"已更新 {len(pictures)} 张海报的注入状态为: {status}，并创建了 IngestHistory 记录")

    async def _update_casts_ingest_status(
        self,
        casts: list,
        status: str,
        xml_path: str | None = None,
        correlate_id: str | None = None,
        create_history: bool = True,
        skip_if_success: bool = False,
    ) -> None:
        """
        更新 Cast 的注入状态。

        与 _update_pictures_ingest_status 行为一致，用于：
            - 发布开始时写入 processing
            - 由 router 在 SOAP 回调成功时更新为 success，失败时更新为 failure

        Args:
            casts: Cast 列表
            status: 状态 (none/processing/success/failure)
            xml_path: C2 XML 文件路径（可选）
            correlate_id: SOAP 关联 ID（可选）
            create_history: 是否创建 IngestHistory 记录（默认 True）
            skip_if_success: 如果已经是 success 状态则跳过更新（用于节目单/频道发布时不覆盖已发布状态）
        """
        from datetime import datetime, timezone
        from sqlalchemy import update
        from app.internal.cms_biz_metada.models.basic import Cast
        from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory

        current_time = datetime.now(timezone.utc)

        # 1. 更新 Cast 表的 ingest_status 字段
        for cast in casts:
            if skip_if_success and getattr(cast, 'ingest_status', None) == "success":
                continue
            await self.db.execute(
                update(Cast)
                .where(Cast.id == cast.id)
                .values(
                    ingest_status=status,
                    updated_at=current_time,
                )
            )

        # 2. 创建 IngestHistory 记录（仅在 processing 或最终状态时创建）
        if create_history and status in ("processing", "success", "failure"):
            for cast in casts:
                if skip_if_success and getattr(cast, 'ingest_status', None) == "success":
                    continue
                history = IngestHistory(
                    entity_type="Cast",
                    entity_id=cast.id,
                    action="REGIST" if status in ("processing", "success") else "DELETE",
                    status=status,
                    ingest_xml_path=xml_path,
                    correlate_id=correlate_id,
                    send_date=current_time,
                )
                self.db.add(history)

        await self.db.commit()
        logger.info(f"已更新 {len(casts)} 个 Cast 的注入状态为: {status}，并创建了 IngestHistory 记录")


# ═══════════════════════════════════════════════════════════
# 序列化工具
# ═══════════════════════════════════════════════════════════
def _serialize(root: Element) -> str:
    """
    将 Element 序列化为带 XML 声明、缩进美化的字符串。

    minidom 美化会引入一些多余空白节点，对 LSP 无影响。
    """
    raw = tostring(root, encoding="utf-8")
    pretty = minidom.parseString(raw).toprettyxml(indent="  ", encoding="utf-8")
    # bug 32017：Category 的 JumpCategoryCode / PosterType 保留传空，
    # 需输出显式开闭标签
    return obj_builders.expand_empty_properties(pretty.decode("utf-8"))
