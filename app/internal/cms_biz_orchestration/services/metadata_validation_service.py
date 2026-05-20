"""
元数据实时校验服务。

职责：
- 在元数据创建/更新时实时校验必填字段
- 支持条件必填逻辑（mandatory_condition）
- 支持长度、枚举、正则等校验规则
- 提供友好的错误提示信息

使用场景：
- Program/Series/Channel/Schedule 元数据创建
- Program/Series/Channel/Schedule 元数据更新
"""

import json
import re
from typing import Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.i18n import get_msg
from app.internal.cms_biz_system.models.metadata_validation_rule import MetadataValidationRule
from app.internal.cms_biz_system.services.metadata_validation_rule_service import (
    get_rules_by_entity_type,
)


# ═══════════════════════════════════════════════════════════
# 公共校验入口
# ═══════════════════════════════════════════════════════════

async def validate_metadata(
    db: AsyncSession,
    entity_type: str,
    data: Any,
    content_type: str | None = None,
    content_id: int | None = None,
) -> None:
    """
    校验元数据是否符合校验规则。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型 (PROGRAM/SERIES/CHANNEL/SCHEDULE)
        data: 元数据 Pydantic 模型或 dict
        content_type: 内容类型 (MOVIE/EPISODE/SERIES/SEASON/CHANNEL/SCHEDULE)
                     用于条件必填判断
        content_id: 内容ID（编辑模式时使用，用于获取完整元数据进行校验）
    
    Raises:
        BusinessException: 校验失败时抛出，包含详细的错误信息
    """
    # 获取该实体类型的所有启用规则
    rules = await get_rules_by_entity_type(db, entity_type)
    
    if not rules:
        logger.debug(f"实体类型 {entity_type} 无校验规则，跳过校验")
        return
    
    # 将 data 转换为 dict 便于访问
    if hasattr(data, 'model_dump'):
        data_dict = data.model_dump(exclude_unset=True)
    elif isinstance(data, dict):
        data_dict = data
    else:
        data_dict = vars(data)
    
    # 编辑模式下，需要从数据库获取完整的元数据，合并后再校验
    # 因为 exclude_unset=True 只会包含用户修改的字段
    if content_id and entity_type in ('PROGRAM', 'SERIES', 'CHANNEL', 'SCHEDULE'):
        from app.internal.cms_biz_orchestration.repositories import (
            get_content_metadata_by_content_id,
            get_series_metadata_by_content_id,
            get_channel_metadata_by_content_id,
            get_schedule_metadata_by_content_id,
            get_content_by_id,
        )
        
        # 根据实体类型获取对应的元数据
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
            # 将数据库中的值合并到 data_dict 中（用户提交的优先）
            existing_dict = {
                k: v for k, v in existing_metadata.__dict__.items()
                if not k.startswith('_') and k != 'id'
            }
            # 合并：existing_dict 作为默认值，data_dict 覆盖
            data_dict = {**existing_dict, **data_dict}
            logger.debug(f"编辑模式：合并现有元数据用于校验 (entity_type={entity_type}, content_id={content_id})")
        
        # 特殊处理：对于 PROGRAM/SERIES/CHANNEL/SCHEDULE，
        # genre_id 存储在 content 主表中，需要从 content 获取
        content = await get_content_by_id(db, content_id)
        if content and hasattr(content, 'genre_id') and content.genre_id is not None:
            # 将 genre_id 加入到 data_dict 中用于校验（用户提交的优先）
            data_dict['genre_id'] = data_dict.get('genre_id') or content.genre_id
            logger.debug(f"从 content 主表获取 genre_id={content.genre_id} 用于校验")
    
    # 收集所有校验错误
    errors = []
    
    for rule in rules:
        # 获取字段值（大小写不敏感）
        field_value = _get_field_value(data_dict, rule.field_name)
        
        # 执行校验
        # 注意：只有 rule_type == "mandatory" 时才执行必填校验
        # is_mandatory 字段只是标记，不应用于判断是否执行必填校验
        if rule.rule_type == "mandatory":
            error = _validate_mandatory(rule, field_value, data_dict, content_type)
            if error:
                errors.append(error)
        
        elif rule.rule_type == "length" and field_value:
            error = _validate_length(rule, field_value)
            if error:
                errors.append(error)
        
        elif rule.rule_type == "regex" and field_value:
            error = _validate_regex(rule, field_value)
            if error:
                errors.append(error)
        
        elif rule.rule_type == "enum" and field_value:
            error = _validate_enum(rule, field_value)
            if error:
                errors.append(error)
    
    # 如果有错误，统一抛出
    if errors:
        error_message = "; ".join(errors)
        logger.warning(f"元数据校验失败: entity_type={entity_type}, errors={error_message}")
        raise BusinessException(ErrorCode.VALIDATION_ERROR, error_message)
    
    logger.debug(f"元数据校验通过: entity_type={entity_type}")


# ═══════════════════════════════════════════════════════════
# 字段值获取（支持大小写不敏感）
# ═══════════════════════════════════════════════════════════

def _get_field_value(data_dict: dict, field_name: str) -> Any:
    """
    从数据字典中获取字段值（大小写不敏感）。
    
    Args:
        data_dict: 数据字典
        field_name: 字段名称
    
    Returns:
        字段值，如果不存在则返回 None
    """
    # 1. 精确匹配
    if field_name in data_dict:
        return data_dict[field_name]
    
    # 2. 大小写不敏感匹配
    field_name_lower = field_name.lower()
    for key, value in data_dict.items():
        if key.lower() == field_name_lower:
            return value
    
    return None


# ═══════════════════════════════════════════════════════════
# 必填字段校验
# ═══════════════════════════════════════════════════════════

def _validate_mandatory(
    rule: MetadataValidationRule,
    field_value: Any,
    data_dict: dict,
    content_type: str | None = None,
) -> str | None:
    """
    校验必填字段。
    
    Args:
        rule: 校验规则
        field_value: 字段值
        data_dict: 完整数据字典（用于条件必填判断）
        content_type: 内容类型（用于条件必填判断）
    
    Returns:
        错误信息，如果校验通过返回 None
    """
    # 条件必填判断
    if rule.is_mandatory == "C":
        if not _check_mandatory_condition(rule, data_dict, content_type):
            # 条件不满足，不需要必填
            return None
    
    # 检查字段是否为空
    is_empty = (
        field_value is None or
        (isinstance(field_value, str) and field_value.strip() == "") or
        (isinstance(field_value, list) and len(field_value) == 0)
    )
    
    if is_empty:
        # 使用规则说明作为错误提示，如果没有说明则使用字段名
        field_label = rule.description or rule.field_name
        return get_msg("METADATA_FIELD_REQUIRED", field_name=field_label)
    
    return None


def _check_mandatory_condition(
    rule: MetadataValidationRule,
    data_dict: dict,
    content_type: str | None = None,
) -> bool:
    """
    检查条件必填的触发条件是否满足。
    
    支持的 condition 格式：
    1. 简单字段匹配：{"field": "content_type", "operator": "equals", "value": "EPISODE"}
    2. 多条件 AND：{"logic": "AND", "conditions": [...]}
    3. 多条件 OR：{"logic": "OR", "conditions": [...]}
    4. 字段存在性：{"field": "program_id", "operator": "exists"}
    5. 字段值比较：{"field": "series_type", "operator": "in", "value": [1, 2]}
    
    Args:
        rule: 校验规则
        data_dict: 完整数据字典
        content_type: 内容类型
    
    Returns:
        条件是否满足（满足则需要必填）
    """
    if not rule.mandatory_condition:
        return False
    
    try:
        condition = json.loads(rule.mandatory_condition)
    except json.JSONDecodeError:
        logger.warning(f"条件必填 JSON 解析失败: rule_id={rule.id}, condition={rule.mandatory_condition}")
        return False
    
    return _evaluate_condition(condition, data_dict, content_type)


def _evaluate_condition(
    condition: dict,
    data_dict: dict,
    content_type: str | None = None,
) -> bool:
    """
    评估条件表达式。
    
    Args:
        condition: 条件表达式
        data_dict: 数据字典
        content_type: 内容类型
    
    Returns:
        条件是否满足
    """
    # 处理逻辑运算符 (AND/OR)
    if "logic" in condition:
        logic = condition["logic"].upper()
        sub_conditions = condition.get("conditions", [])
        
        if logic == "AND":
            return all(
                _evaluate_condition(sub_cond, data_dict, content_type)
                for sub_cond in sub_conditions
            )
        elif logic == "OR":
            return any(
                _evaluate_condition(sub_cond, data_dict, content_type)
                for sub_cond in sub_conditions
            )
    
    # 处理简单条件
    field = condition.get("field")
    operator = condition.get("operator", "equals").lower()
    expected_value = condition.get("value")
    
    # 获取字段实际值
    if field == "content_type":
        actual_value = content_type
    else:
        actual_value = _get_field_value(data_dict, field)
    
    # 执行运算符判断
    if operator == "equals":
        return str(actual_value) == str(expected_value)
    
    elif operator == "not_equals":
        return str(actual_value) != str(expected_value)
    
    elif operator == "in":
        return actual_value in expected_value
    
    elif operator == "not_in":
        return actual_value not in expected_value
    
    elif operator == "exists":
        return actual_value is not None
    
    elif operator == "not_exists":
        return actual_value is None
    
    elif operator == "greater_than":
        try:
            return float(actual_value) > float(expected_value)
        except (TypeError, ValueError):
            return False
    
    elif operator == "less_than":
        try:
            return float(actual_value) < float(expected_value)
        except (TypeError, ValueError):
            return False
    
    else:
        logger.warning(f"不支持的条件运算符: {operator}")
        return False


# ═══════════════════════════════════════════════════════════
# 长度校验
# ═══════════════════════════════════════════════════════════

def _validate_length(
    rule: MetadataValidationRule,
    field_value: Any,
) -> str | None:
    """
    校验字段长度。
    
    Args:
        rule: 校验规则
        field_value: 字段值
    
    Returns:
        错误信息，如果校验通过返回 None
    """
    if not isinstance(field_value, str):
        return None
    
    length = len(field_value)
    violations = []
    
    if rule.min_length is not None and length < rule.min_length:
        violations.append(f"最小长度{rule.min_length}")
    
    if rule.max_length is not None and length > rule.max_length:
        violations.append(f"最大长度{rule.max_length}")
    
    if violations:
        field_label = rule.description or rule.field_name
        return f"{field_label}: {', '.join(violations)}"
    
    return None


# ═══════════════════════════════════════════════════════════
# 正则表达式校验
# ═══════════════════════════════════════════════════════════

def _validate_regex(
    rule: MetadataValidationRule,
    field_value: Any,
) -> str | None:
    """
    校验字段格式（正则表达式）。
    
    Args:
        rule: 校验规则
        field_value: 字段值
    
    Returns:
        错误信息，如果校验通过返回 None
    """
    if not isinstance(field_value, str):
        return None
    
    if not rule.regex_pattern:
        return None
    
    try:
        if not re.match(rule.regex_pattern, field_value):
            field_label = rule.description or rule.field_name
            return f"{field_label}: 格式不符合要求"
    except re.error as e:
        logger.error(f"正则表达式错误: rule_id={rule.id}, pattern={rule.regex_pattern}, error={e}")
        return None
    
    return None


# ═══════════════════════════════════════════════════════════
# 枚举值校验
# ═══════════════════════════════════════════════════════════

def _validate_enum(
    rule: MetadataValidationRule,
    field_value: Any,
) -> str | None:
    """
    校验字段枚举值。
    
    Args:
        rule: 校验规则
        field_value: 字段值
    
    Returns:
        错误信息，如果校验通过返回 None
    """
    if not rule.allowed_values:
        return None
    
    # 解析允许值列表
    try:
        # 尝试 JSON 数组格式
        allowed = json.loads(rule.allowed_values)
        if not isinstance(allowed, list):
            allowed = [allowed]
    except json.JSONDecodeError:
        # 逗号分隔格式
        allowed = [v.strip() for v in rule.allowed_values.split(",") if v.strip()]
    
    # 检查字段值是否在允许值列表中
    if isinstance(field_value, list):
        # 数组类型：检查每个元素
        for val in field_value:
            if str(val) not in [str(a) for a in allowed]:
                field_label = rule.description or rule.field_name
                return f"{field_label}: 值 '{val}' 不在允许范围内"
    else:
        # 单值类型
        if str(field_value) not in [str(a) for a in allowed]:
            field_label = rule.description or rule.field_name
            return f"{field_label}: 值 '{field_value}' 不在允许范围内"
    
    return None
