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

from . import objects as obj_builders
from .constants import (
    CONTENT_TYPE_TO_ELEMENT,
    PICTURE_ENTITY_TYPE_TO_ELEMENT,
    Action,
    ElementType,
)
from .loader import BuildContext, load_build_context
from .mappings import add_mapping
from .strategy import decide_action, decide_action_with_history, is_published


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
        构建发布 XML（REGIST / UPDATE / SKIP）。

        根据各对象的发布历史和变更状态自动选择 Action：
            - 未发布过 → REGIST
            - 已发布且有变更 → UPDATE
            - 已发布且无变更 → SKIP（仅输出Mapping，不输出Object）
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

        文件名格式: ``adi_{entity_id}_{correlate_id_8}_{timestamp}.xml``

        :return: SFTP 相对路径（格式: c2/xxx.xml）
        """
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        short_cid = correlate_id.replace("-", "")[:8]
        eid = entity_id if entity_id is not None else "na"
        filename = f"adi_{eid}_{short_cid}_{ts}.xml"

        result = storage_service.save_file(
            file_content=xml_str.encode("utf-8"),
            filename=filename,
            category="c2"
        )
        logger.info(f"ADI XML 已上传至 SFTP: {result['file_path']}")
        return result["file_path"]

    # ═══════════════════════════════════════════════════════
    # 内部构建流程
    # ═══════════════════════════════════════════════════════
    async def _build(self, content_id: int, is_unpublish: bool) -> str:
        """加载上下文 → 构建对象/映射 → 序列化"""
        ctx = await load_build_context(self.db, content_id)
        if ctx is None:
            raise ValueError(f"Content 不存在或已删除: id={content_id}")

        root = self._build_root()
        objects_el = SubElement(root, "Objects")
        mappings_el = SubElement(root, "Mappings")

        ct = ctx.content.content_type
        if ct in ("MOVIE", "EPISODE"):
            await self._build_program_scope(ctx, objects_el, mappings_el, is_unpublish)
        elif ct in ("SERIES", "SEASON"):
            await self._build_series_scope(ctx, objects_el, mappings_el, is_unpublish)
        elif ct == "CHANNEL":
            await self._build_channel_scope(ctx, objects_el, mappings_el, is_unpublish)
        elif ct == "SCHEDULE":
            await self._build_schedule_scope(ctx, objects_el, mappings_el, is_unpublish)
        else:
            raise ValueError(f"不支持的 content_type: {ct}")

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
        SubElement(header, "Timestamp").text = datetime.now().isoformat()
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
            - Movie 列表（全部 REGIST / UPDATE / SKIP）
            - Cast（先于 CastRoleMap）
            - CastRoleMap
        Mappings：
            - Program → Movie
            - Program → CastRoleMap
            - （如果是 EPISODE）父 SERIES → Program
        """
        content = ctx.content

        program_action = await decide_action_with_history(
            self.db, "Content", content.id, content.id, is_unpublish,
            obj_updated_at=content.updated_at,
        )

        if program_action is None:
            program_action = Action.DELETE if is_unpublish else Action.REGIST

        # 1. Program 主体（主对象不能SKIP，必须输出Object）
        objs.append(
            obj_builders.build_program_object(
                content, ctx.program_meta, program_action,
                license_data=ctx.license_data,
                genre_name=ctx.genre_name,
                genre_i18n=ctx.genre_i18n,
                content_type_name=ctx.content_type_name,
                custom_fields=ctx.custom_fields,
                custom_field_i18n=ctx.custom_field_i18n,
                tag_names=ctx.tag_names,
                tag_i18n=ctx.tag_i18n,
                i18n_data=ctx.i18n_data,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
            )
        )

        # 2. Movies（关联对象不下架，下架时跳过）
        map_action = Action.DELETE if is_unpublish else Action.REGIST
        for movie in ctx.movies:
            if is_unpublish:
                continue

            movie_action = await decide_action_with_history(
                self.db, "Movie", movie.id, content.id, is_unpublish=False,
                obj_updated_at=movie.updated_at,
            )
            if movie_action is None:
                movie_action = Action.REGIST

            if movie_action != Action.SKIP:
                movie_i18n = ctx.movie_i18n_data.get(movie.id)
                objs.append(obj_builders.build_movie_object(
                    movie, movie_action,
                    i18n_data=movie_i18n,
                    languages=ctx.languages,
                    primary_language=ctx.primary_language,
                    custom_fields=ctx.custom_fields,
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

            cast_action = await decide_action_with_history(
                self.db, "Cast", cast.id, content.id, is_unpublish=False,
                obj_updated_at=cast.updated_at,
            )
            if cast_action is None:
                cast_action = Action.REGIST

            if cast_action != Action.SKIP:
                cast_i18n = ctx.cast_i18n_data.get(cast.id)
                objs.append(obj_builders.build_cast_object(
                    cast, cast_action,
                    i18n_data=cast_i18n,
                    languages=ctx.languages,
                    primary_language=ctx.primary_language,
                    custom_fields=ctx.custom_fields,
                ))

        for rm in ctx.cast_role_maps:
            if is_unpublish:
                continue

            rm_action = await decide_action_with_history(
                self.db, "CastRoleMap", rm.map_id, content.id, is_unpublish=False,
                obj_updated_at=rm.updated_at,
            )
            if rm_action is None:
                rm_action = Action.REGIST

            if rm_action != Action.SKIP:
                objs.append(obj_builders.build_cast_role_map_object(rm, rm_action, custom_fields=ctx.custom_fields))
            add_mapping(
                self._maps_list(maps),
                ElementType.PROGRAM, content.id,
                ElementType.CAST_ROLE_MAP, rm.map_id,
                Action.DELETE if is_unpublish else Action.REGIST,
            )

        # 4. EPISODE 的父 SERIES → Program Mapping
        if content.content_type == "EPISODE" and content.parent_id:
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.parent_id,
                ElementType.PROGRAM, content.id,
                Action.DELETE if is_unpublish else Action.REGIST,
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
        Series 作用域：Series 主体 + 其下所有 Program/子 Series。

        注意：子 Program 的 Movie/CastRoleMap 不在此处展开；
             它们在子 Program 自己的发布任务中处理。
        """
        content = ctx.content

        series_action = await decide_action_with_history(
            self.db, "Content", content.id, content.id, is_unpublish,
            obj_updated_at=content.updated_at,
        )
        if series_action is None:
            series_action = Action.DELETE if is_unpublish else Action.REGIST

        # 1. Series 主体（主对象不能SKIP）
        objs.append(
            obj_builders.build_series_object(
                content, ctx.series_meta, series_action,
                license_data=ctx.license_data,
                genre_name=ctx.genre_name,
                genre_i18n=ctx.genre_i18n,
                content_type_name=ctx.content_type_name,
                custom_fields=ctx.custom_fields,
                custom_field_i18n=ctx.custom_field_i18n,
                tag_names=ctx.tag_names,
                tag_i18n=ctx.tag_i18n,
                i18n_data=ctx.i18n_data,
                languages=ctx.languages,
                primary_language=ctx.primary_language,
            )
        )

        # 2. CastRoleMap（挂在 Series 上，关联对象下架时跳过）
        for rm in ctx.cast_role_maps:
            if is_unpublish:
                continue

            rm_action = await decide_action_with_history(
                self.db, "CastRoleMap", rm.map_id, content.id, is_unpublish=False,
                obj_updated_at=rm.updated_at,
            )
            if rm_action is None:
                rm_action = Action.REGIST

            if rm_action != Action.SKIP:
                objs.append(obj_builders.build_cast_role_map_object(rm, rm_action, custom_fields=ctx.custom_fields))
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.id,
                ElementType.CAST_ROLE_MAP, rm.map_id,
                Action.DELETE if is_unpublish else Action.REGIST,
            )

        # 3. 关联的 Cast（关联对象下架时跳过）
        for cast in ctx.casts:
            if is_unpublish:
                continue

            cast_action = await decide_action_with_history(
                self.db, "Cast", cast.id, content.id, is_unpublish=False,
                obj_updated_at=cast.updated_at,
            )
            if cast_action is None:
                cast_action = Action.REGIST

            if cast_action != Action.SKIP:
                cast_i18n = ctx.cast_i18n_data.get(cast.id)
                objs.append(obj_builders.build_cast_object(
                    cast, cast_action,
                    i18n_data=cast_i18n,
                    languages=ctx.languages,
                    primary_language=ctx.primary_language,
                    custom_fields=ctx.custom_fields,
                ))

        # 4. 子 Content Mapping（Series → 子 Program 或 Series → 子 Series）
        for idx, child in enumerate(ctx.children, start=1):
            child_et = CONTENT_TYPE_TO_ELEMENT.get(child.content_type)
            if child_et is None:
                continue
            add_mapping(
                self._maps_list(maps),
                ElementType.SERIES, content.id,
                child_et, child.id,
                Action.DELETE if is_unpublish else Action.REGIST,
                sequence=idx,
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
        Channel 作用域：Channel 主体 + PhysicalChannel + Schedule 挂载。
        """
        content = ctx.content

        channel_action = await decide_action_with_history(
            self.db, "Content", content.id, content.id, is_unpublish,
            obj_updated_at=content.updated_at,
        )
        if channel_action is None:
            channel_action = Action.DELETE if is_unpublish else Action.REGIST

        # 1. Channel 主体（主对象不能SKIP）
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
            )
        )

        # 2. PhysicalChannel（关联对象下架时跳过）
        for pc in ctx.physical_channels:
            if is_unpublish:
                continue

            pc_action = await decide_action_with_history(
                self.db, "PhysicalChannel", pc.id, content.id, is_unpublish=False,
                obj_updated_at=pc.updated_at,
            )
            if pc_action is None:
                pc_action = Action.REGIST

            if pc_action != Action.SKIP:
                objs.append(
                    obj_builders.build_physical_channel_object(pc, channel_action, channel_type=ctx.channel_type, custom_fields=ctx.custom_fields)
                )
            add_mapping(
                self._maps_list(maps),
                ElementType.CHANNEL, content.id,
                ElementType.PHYSICAL_CHANNEL, pc.id,
                Action.DELETE if is_unpublish else Action.REGIST,
            )

        # 3. 子 Schedule Mapping（不展开 Schedule 本体）
        for child in ctx.children:
            if child.content_type != "SCHEDULE":
                continue
            add_mapping(
                self._maps_list(maps),
                ElementType.CHANNEL, content.id,
                ElementType.SCHEDULE, child.id,
                Action.DELETE if is_unpublish else Action.REGIST,
            )

    # ─────────────────────────────────────────────────────
    # 构建分支：SCHEDULE
    # ─────────────────────────────────────────────────────
    async def _build_schedule_scope(
        self,
        ctx: BuildContext,
        objs: Element,
        maps: Element,
        is_unpublish: bool,
    ) -> None:
        """Schedule 作用域：Schedule 主体 + 挂到父 Channel"""
        content = ctx.content

        schedule_action = await decide_action_with_history(
            self.db, "Content", content.id, content.id, is_unpublish,
            obj_updated_at=content.updated_at,
        )
        if schedule_action is None:
            schedule_action = Action.DELETE if is_unpublish else Action.REGIST

        objs.append(
            obj_builders.build_schedule_object(
                content, ctx.schedule_meta, schedule_action,
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
                pictures=ctx.pictures,
            )
        )

        # 父 Channel → Schedule Mapping
        if content.parent_id:
            add_mapping(
                self._maps_list(maps),
                ElementType.CHANNEL, content.parent_id,
                ElementType.SCHEDULE, content.id,
                Action.DELETE if is_unpublish else Action.REGIST,
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
            - Picture  → 主体（Program/Series/Channel/Category/Cast）

        其中 Category/Package/Picture 如果未注入，会同时输出 Object。
        已注入且无变更的对象（SKIP）仅输出 Mapping，不输出 Object。
        注意：这些关联对象在下架时跳过，不生成DELETE
        """
        content = ctx.content
        main_et = CONTENT_TYPE_TO_ELEMENT.get(content.content_type)
        if main_et is None:
            return

        map_action = Action.DELETE if is_unpublish else Action.REGIST

        # ── Category（可挂 Program/Series/Channel，关联对象下架时跳过） ─────
        for idx, cat in enumerate(ctx.categories, start=1):
            if is_unpublish:
                continue

            logger.info(
                f"[Category #{cat.id}] 判断 Action | "
                f"name={cat.name} | updated_at={cat.updated_at}"
            )
            
            cat_action = await decide_action_with_history(
                self.db, "Category", cat.id, content.id, is_unpublish=False,
                obj_updated_at=cat.updated_at,
            )
            
            logger.info(f"[Category #{cat.id}] Action 判断结果: {cat_action}")
            
            if cat_action is None:
                cat_action = Action.REGIST
                logger.warning(f"[Category #{cat.id}] Action 为 None，降级为 REGIST")

            if cat_action != Action.SKIP:
                logger.info(f"[Category #{cat.id}] 输出 Object，Action={cat_action}")
                cat_i18n = ctx.category_i18n_data.get(cat.id)
                objs.append(
                    obj_builders.build_category_object(
                        cat, cat_action,
                        i18n_data=cat_i18n,
                        languages=ctx.languages,
                        primary_language=ctx.primary_language,
                        custom_fields=ctx.custom_fields,
                    )
                )
            else:
                logger.info(f"[Category #{cat.id}] SKIP，仅输出 Mapping")
            add_mapping(
                self._maps_list(maps),
                ElementType.CATEGORY, cat.id,
                main_et, content.id,
                map_action,
                sequence=idx,
                licensing_window_start=ctx.license_data.get("licensing_window_start"),
                licensing_window_end=ctx.license_data.get("licensing_window_end"),
            )

        # ── Package（仅 Program/Series，关联对象下架时跳过） ────────────────
        for pkg in ctx.packages:
            if is_unpublish:
                continue

            pkg_action = await decide_action_with_history(
                self.db, "Package", pkg.id, content.id, is_unpublish=False,
                obj_updated_at=pkg.updated_at,
            )
            if pkg_action is None:
                pkg_action = Action.REGIST

            if pkg_action != Action.SKIP:
                objs.append(
                    obj_builders.build_package_object(
                        pkg, pkg_action,
                        custom_fields=ctx.custom_fields,
                    )
                )
            add_mapping(
                self._maps_list(maps),
                ElementType.PACKAGE, pkg.id,
                main_et, content.id,
                map_action,
            )

        # ── Picture（多态，按 entity_type 反查目标 ElementType，关联对象下架时跳过） ─
        for pic in ctx.pictures:
            if is_unpublish:
                continue

            pic_action = await decide_action_with_history(
                self.db, "Picture", pic.id, content.id, is_unpublish=False,
                obj_updated_at=pic.updated_at,
            )
            if pic_action is None:
                pic_action = Action.REGIST

            pic_target_et = PICTURE_ENTITY_TYPE_TO_ELEMENT.get(pic.entity_type.lower(), main_et)
            if pic_action != Action.SKIP:
                poster_size_name = ctx.poster_size_names.get(pic.id)
                objs.append(obj_builders.build_picture_object(pic, pic_action, poster_size_name=poster_size_name, custom_fields=ctx.custom_fields))
            add_mapping(
                self._maps_list(maps),
                ElementType.PICTURE, pic.id,
                pic_target_et,
                pic.entity_id,
                map_action,
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
    return pretty.decode("utf-8")
