"""
C2 规范数据校验服务。

职责：
- 在提交审批时，使用 C2 BuildContext 进行完整数据校验
- 确保生成的 ADI XML 符合 C2 规范要求
- 只校验 REGIST（首次发布）动作，UPDATE 和 DELETE 不需要校验

使用场景：
- 用户点击"提交审批"按钮时触发
- 校验通过后才能进入审批流程
- 校验失败返回详细错误列表，阻止提交
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.i18n import get_msg
from app.internal.cms_biz_package.models.package import ContentGenre
from app.internal.cms_biz_system.services.metadata_validation_rule_service import (
    get_rules_by_entity_type,
)

from .constants import Action, ElementType
from .loader import BuildContext, load_build_context


# ═══════════════════════════════════════════════════════════
# 校验结果
# ═══════════════════════════════════════════════════════════

@dataclass
class ValidationResult:
    """校验结果"""
    success: bool = True
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    
    def add_error(self, error: str):
        """添加错误"""
        self.success = False
        self.errors.append(error)
    
    def add_warning(self, warning: str):
        """添加警告"""
        self.warnings.append(warning)
    
    def merge(self, other: ValidationResult):
        """合并另一个校验结果"""
        self.success = self.success and other.success
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)


# ═══════════════════════════════════════════════════════════
# 公共校验入口
# ═══════════════════════════════════════════════════════════

async def validate_content_for_c2(
    db: AsyncSession,
    content_id: int,
) -> ValidationResult:
    """
    校验 Content 是否符合 C2 规范（用于 REGIST 场景）。
    
    Args:
        db: 数据库会话
        content_id: 内容 ID
    
    Returns:
        ValidationResult: 校验结果，包含错误和警告列表
    
    校验逻辑：
        1. 加载 C2 BuildContext（聚合所有业务数据）
        2. 根据 Action 判断是否需要校验（只校验 REGIST）
        3. 校验主元数据（Program/Series/Channel/Schedule）
        4. 校验关联对象（Movies、CastRoleMaps 等）
        5. 返回完整错误列表
    """
    result = ValidationResult()
    
    # 1. 加载 C2 BuildContext
    ctx = await load_build_context(db, content_id)
    if ctx is None:
        result.add_error(f"Content 不存在或已删除: id={content_id}")
        return result
    
    logger.info(f"开始校验 Content {content_id} (type={ctx.content.content_type})")
    
    content_type = ctx.content.content_type
    
    # 4. 根据 content_type 分支校验
    if content_type in ("MOVIE", "EPISODE"):
        await _validate_program_scope(db, ctx, result)
    elif content_type in ("SERIES", "SEASON_SERIES", "SEASON"):
        await _validate_series_scope(db, ctx, result)
    elif content_type == "CHANNEL":
        await _validate_channel_scope(db, ctx, result)
    elif content_type == "SCHEDULE":
        await _validate_schedule_scope(db, ctx, result)
    else:
        result.add_error(f"不支持的 content_type: {content_type}")
    
    # 5. 记录校验结果
    if result.success:
        logger.info(f"Content {content_id} 校验通过")
    else:
        logger.warning(
            f"Content {content_id} 校验失败: {len(result.errors)} 个错误, "
            f"{len(result.warnings)} 个警告"
        )
    
    return result


# ═══════════════════════════════════════════════════════════
# Program 校验（MOVIE / EPISODE）
# ═══════════════════════════════════════════════════════════

async def _validate_program_scope(
    db: AsyncSession,
    ctx: BuildContext,
    result: ValidationResult,
):
    """校验 Program 范围（MOVIE/EPISODE）"""
    
    # 1. 校验主元数据
    if ctx.program_meta:
        meta_result = await _validate_metadata_by_rules(
            db, "PROGRAM", ctx.program_meta, ctx.content.content_type, ctx.content.id
        )
        result.merge(meta_result)
    else:
        result.add_error("Program 元数据不存在")
    
    # 2. 校验 Movies（材料）
    for movie in ctx.movies:
        movie_result = await _validate_movie(db, movie)
        result.merge(movie_result)
    
    # 3. 校验 CastRoleMaps（演职人员）
    # TODO: 如果需要校验演职人员，在这里添加


# ═══════════════════════════════════════════════════════════
# Series 校验（SERIES / SEASON）
# ═══════════════════════════════════════════════════════════

async def _validate_series_scope(
    db: AsyncSession,
    ctx: BuildContext,
    result: ValidationResult,
):
    """校验 Series 范围（SERIES/SEASON）"""
    
    # 1. 校验主元数据
    if ctx.series_meta:
        meta_result = await _validate_metadata_by_rules(
            db, "SERIES", ctx.series_meta, ctx.content.content_type, ctx.content.id
        )
        result.merge(meta_result)
    else:
        result.add_error("Series 元数据不存在")
    
    # 2. 校验子 EPISODE（如果有）
    for child_content in ctx.child_contents:
        if child_content.content_type == "EPISODE":
            # 递归校验每个 EPISODE
            child_result = await _validate_program_scope_for_child(db, child_content)
            result.merge(child_result)


# ═══════════════════════════════════════════════════════════
# Channel 校验
# ═══════════════════════════════════════════════════════════

async def _validate_channel_scope(
    db: AsyncSession,
    ctx: BuildContext,
    result: ValidationResult,
):
    """校验 Channel 范围"""
    
    # 1. 校验主元数据
    if ctx.channel_meta:
        meta_result = await _validate_metadata_by_rules(
            db, "CHANNEL", ctx.channel_meta, ctx.content.content_type, ctx.content.id
        )
        result.merge(meta_result)
    else:
        result.add_error("Channel 元数据不存在")
    
    # 2. 校验 PhysicalChannel（物理频道）
    for physical_channel in ctx.physical_channels:
        # PhysicalChannel 目前不需要校验
        pass


# ═══════════════════════════════════════════════════════════
# Schedule 校验
# ═══════════════════════════════════════════════════════════

async def _validate_schedule_scope(
    db: AsyncSession,
    ctx: BuildContext,
    result: ValidationResult,
):
    """校验 Schedule 范围"""
    
    # 1. 校验主元数据
    if ctx.schedule_meta:
        meta_result = await _validate_metadata_by_rules(
            db, "SCHEDULE", ctx.schedule_meta, ctx.content.content_type, ctx.content.id
        )
        result.merge(meta_result)
    else:
        result.add_error("Schedule 元数据不存在")


# ═══════════════════════════════════════════════════════════
# Movie 校验
# ═══════════════════════════════════════════════════════════

async def _validate_movie(
    db: AsyncSession,
    movie: Any,
) -> ValidationResult:
    """校验单个 Movie（材料）"""
    return await _validate_metadata_by_rules(
        db, "MOVIE", movie, content_type=None, content_id=None
    )


async def _validate_program_scope_for_child(
    db: AsyncSession,
    child_content: Any,
) -> ValidationResult:
    """校验子 EPISODE"""
    result = ValidationResult()
    
    # 加载子内容的 BuildContext
    ctx = await load_build_context(db, child_content.id)
    if ctx is None:
        result.add_error(f"子内容不存在: id={child_content.id}")
        return result
    
    await _validate_program_scope(db, ctx, result)
    return result


# ═══════════════════════════════════════════════════════════
# 基于规则的元数据校验
# ═══════════════════════════════════════════════════════════

async def _validate_metadata_by_rules(
    db: AsyncSession,
    entity_type: str,
    data: Any,
    content_type: str | None = None,
    content_id: int | None = None,
) -> ValidationResult:
    """
    基于 metadata_validation_rule 表校验元数据。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型 (PROGRAM/SERIES/CHANNEL/SCHEDULE/MOVIE)
        data: 元数据对象（SQLAlchemy 模型或 Pydantic 模型）
        content_type: 内容类型（用于条件必填判断）
        content_id: 内容 ID（用于合并现有数据）
    
    Returns:
        ValidationResult: 校验结果
    """
    result = ValidationResult()
    
    # 1. 获取校验规则
    rules = await get_rules_by_entity_type(db, entity_type)
    if not rules:
        logger.debug(f"实体类型 {entity_type} 无校验规则，跳过校验")
        return result
    
    # 2. 转换数据为 dict
    data_dict = _to_dict(data)
    
    # 3. 编辑模式下合并现有数据
    if content_id and entity_type in ('PROGRAM', 'SERIES', 'CHANNEL', 'SCHEDULE'):
        data_dict = await _merge_existing_data(db, entity_type, content_id, data_dict)
    
    # 4. 特殊处理：从 content 主表获取 genre_id
    if content_id and entity_type in ('PROGRAM', 'SERIES', 'CHANNEL', 'SCHEDULE'):
        await _inject_genre_id(db, content_id, data_dict)
    
    # 5. 执行校验
    from app.internal.cms_biz_orchestration.services.metadata_validation_service import (
        _validate_mandatory,
        _validate_length,
        _validate_regex,
        _validate_enum,
        _validate_range,
        _evaluate_condition,
        _get_field_value,
    )
    
    for rule in rules:
        field_value = _get_field_value(data_dict, rule.field_name)
        
        # 必填校验
        if rule.rule_type == "mandatory":
            error = _validate_mandatory(rule, field_value, data_dict, content_type)
            if error:
                result.add_error(error)
        
        # 长度校验
        elif rule.rule_type == "length" and field_value:
            error = _validate_length(rule, field_value)
            if error:
                result.add_error(error)
        
        # 正则校验
        elif rule.rule_type == "regex" and field_value:
            error = _validate_regex(rule, field_value)
            if error:
                result.add_error(error)
        
        # 枚举校验
        elif rule.rule_type == "enum" and field_value:
            error = _validate_enum(rule, field_value)
            if error:
                result.add_error(error)
        
        # 范围校验
        elif rule.rule_type == "range" and field_value:
            error = _validate_range(rule, field_value)
            if error:
                result.add_error(error)
    
    return result


# ═══════════════════════════════════════════════════════════
# 工具函数
# ═══════════════════════════════════════════════════════════

def _to_dict(data: Any) -> dict:
    """将对象转换为 dict"""
    if hasattr(data, 'model_dump'):
        # Pydantic 模型
        return data.model_dump(exclude_unset=True)
    elif hasattr(data, '__dict__'):
        # SQLAlchemy 模型
        return {
            k: v for k, v in data.__dict__.items()
            if not k.startswith('_') and k != 'id'
        }
    elif isinstance(data, dict):
        return data
    else:
        return vars(data)


async def _merge_existing_data(
    db: AsyncSession,
    entity_type: str,
    content_id: int,
    data_dict: dict,
) -> dict:
    """合并数据库中的现有数据"""
    from app.internal.cms_biz_orchestration.repositories import (
        get_content_metadata_by_content_id,
        get_series_metadata_by_content_id,
        get_channel_metadata_by_content_id,
        get_schedule_metadata_by_content_id,
    )
    
    existing_metadata = None
    if entity_type == 'PROGRAM':
        existing_metadata = await get_content_metadata_by_content_id(db, content_id)
    elif entity_type == 'SERIES':
        existing_metadata = await get_series_metadata_by_content_id(db, content_id)
    elif entity_type == 'CHANNEL':
        existing_metadata = await get_channel_metadata_by_content_id(db, content_id)
    elif entity_type == 'SCHEDULE':
        existing_metadata = await get_schedule_metadata_by_content_id(db, content_id)
    
    if existing_metadata:
        existing_dict = _to_dict(existing_metadata)
        # 合并：existing_dict 作为默认值，data_dict 覆盖
        return {**existing_dict, **data_dict}
    
    return data_dict


async def _inject_genre_id(
    db: AsyncSession,
    content_id: int,
    data_dict: dict,
):
    """从 content_genre 中间表注入 genre_id 和 genre_ids"""
    from sqlalchemy import select
    
    stmt = select(ContentGenre.genre_id).where(
        ContentGenre.content_id == content_id,
        ContentGenre.is_deleted.is_(False),
    )
    rows = (await db.execute(stmt)).all()
    genre_ids = [r[0] for r in rows]
    if genre_ids:
        # 用户提交的优先，未提交则从中间表获取
        # 同时设置 genre_id（单数，取第一个值）和 genre_ids（复数，完整列表）
        if not data_dict.get('genre_ids'):
            data_dict['genre_ids'] = genre_ids
            logger.debug(f"从 content_genre 中间表获取 genre_ids={genre_ids}")
        if not data_dict.get('genre_id') and genre_ids:
            data_dict['genre_id'] = genre_ids[0]
            logger.debug(f"从 content_genre 中间表获取 genre_id={genre_ids[0]}")

