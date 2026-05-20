"""
MetadataQualityCheck 业务逻辑 - 元数据质量检查引擎。

功能：
    1. 从 Content 表扫描在线内容（MOVIE/EPISODE/SERIES/SEASON/CHANNEL/SCHEDULE）；
    2. 从 metadata_validation_rule 表加载校验规则；
    3. 按规则检查必填字段、格式、长度、枚举值；
    4. 将不合格项写入 metadata_quality_issue 表；
    5. 在 metadata_quality_check 表更新统计计数。
"""

import json
import re
import time
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.i18n import get_msg
from app.internal.cms_biz_system.models.metadata_quality import MetadataQualityCheck, MetadataQualityIssue
from app.internal.cms_biz_system.models.metadata_validation_rule import MetadataValidationRule
from app.internal.cms_biz_orchestration.models.content_metadata import (
    ContentMetadata,
    SeriesMetadata,
    ChannelMetadata,
    ScheduleMetadata,
)
from app.internal.cms_biz_package.models.package import Content


# 实体类型映射
ENTITY_TYPE_MAP = {
    "MOVIE": "PROGRAM",
    "EPISODE": "PROGRAM",
    "SERIES": "SERIES",
    "SEASON": "SERIES",
    "CHANNEL": "CHANNEL",
    "SCHEDULE": "SCHEDULE",
}

# 元数据表映射
METADATA_TABLE_MAP = {
    "PROGRAM": ContentMetadata,
    "SERIES": SeriesMetadata,
    "CHANNEL": ChannelMetadata,
    "SCHEDULE": ScheduleMetadata,
}


async def run_metadata_quality_check(db: AsyncSession, operator: str | None = None, check_id: int | None = None) -> dict:
    """
    元数据质量检查：真实实现。
    
    流程：
        1. 加载所有启用的校验规则
        2. 扫描所有在线内容
        3. 逐条检查元数据质量
        4. 记录问题并统计
    
    Args:
        db: 数据库会话
        operator: 操作人（手动触发时记录）
        check_id: 可选，待更新的检查记录ID。如果提供，将更新该记录而不是创建新记录
    
    Returns:
        检查结果字典
    """
    logger.info(f"[MetadataQualityCheck] ========== 任务开始 ==========")
    logger.info(f"[MetadataQualityCheck] 任务被触发 operator={operator}, check_id={check_id}")
    
    start_time = time.time()
    now = datetime.now(timezone.utc)
    
    try:
        # 步骤1：创建或获取检查记录
        if check_id is not None:
            # 使用传入的 check_id，获取现有记录
            logger.info(f"[MetadataQualityCheck] 使用现有质检记录 check_id={check_id}...")
            result = await db.execute(
                select(MetadataQualityCheck).where(MetadataQualityCheck.id == check_id)
            )
            record = result.scalar_one_or_none()
            if record is None:
                logger.warning(f"[MetadataQualityCheck] 未找到 check_id={check_id} 的记录，将创建新记录")
                record = MetadataQualityCheck(
                    check_time=now,
                    status="running",
                    total_contents=0,
                    passed_count=0,
                    failed_count=0,
                    is_deleted=False,
                )
                db.add(record)
                await db.flush()
            else:
                # 更新现有记录状态为 running
                record.status = "running"
                record.check_time = now
                await db.flush()
            logger.info(f"[MetadataQualityCheck] 质检记录准备就绪，record_id={record.id}")
        else:
            # 创建新的检查记录
            logger.info(f"[MetadataQualityCheck] 创建新质检记录...")
            record = MetadataQualityCheck(
                check_time=now,
                status="running",
                total_contents=0,
                passed_count=0,
                failed_count=0,
                is_deleted=False,
            )
            db.add(record)
            await db.flush()  # 获取 record.id
            logger.info(f"[MetadataQualityCheck] 质检记录创建成功，record_id={record.id}")
        
        # 步骤2：加载所有启用的校验规则
        logger.info(f"[MetadataQualityCheck] 加载校验规则...")
        rules = (
            await db.execute(
                select(MetadataValidationRule).where(
                    MetadataValidationRule.is_deleted == False,
                    MetadataValidationRule.is_enabled == True,
                )
            )
        ).scalars().all()
        
        rules_by_entity = {}
        for rule in rules:
            if rule.entity_type not in rules_by_entity:
                rules_by_entity[rule.entity_type] = []
            rules_by_entity[rule.entity_type].append(rule)
        
        logger.info(f"[MetadataQualityCheck] 加载规则完成，共 {len(rules)} 条")
        
        # 步骤3：扫描所有在线内容
        logger.info(f"[MetadataQualityCheck] 扫描在线内容...")
        contents = (
            await db.execute(
                select(Content).where(
                    Content.is_deleted == False,
                    Content.is_discarded == False,
                    Content.status.in_([
                        "ReadyForPublish",    # 准备发布（元数据已填完）
                        "PublishFailed",      # 发布失败
                    ])
                )
            )
        ).scalars().all()
        
        total_contents = len(contents)
        logger.info(f"[MetadataQualityCheck] 找到 {total_contents} 条在线内容")
        
        # 步骤4：逐条检查
        total_passed = 0
        total_failed = 0
        total_issues = 0
        
        for idx, content in enumerate(contents, 1):
            if idx % 50 == 0:
                logger.info(f"[MetadataQualityCheck] 检查进度: {idx}/{total_contents}")
            
            entity_type = ENTITY_TYPE_MAP.get(content.content_type)
            if not entity_type:
                logger.debug(f"[MetadataQualityCheck] 跳过未知类型: {content.content_type}")
                total_passed += 1
                continue
            
            # 获取该实体类型的规则
            entity_rules = rules_by_entity.get(entity_type, [])
            if not entity_rules:
                logger.debug(f"[MetadataQualityCheck] 无规则，跳过: content_id={content.id}")
                total_passed += 1
                continue
            
            # 获取元数据
            metadata = await _get_metadata(db, entity_type, content.id)
            if not metadata:
                logger.debug(f"[MetadataQualityCheck] 无元数据，跳过: content_id={content.id}")
                total_passed += 1
                continue
            
            # 执行检查
            content_issues = await _check_content(db, record.id, content, entity_type, metadata, entity_rules)
            
            if content_issues == 0:
                total_passed += 1
            else:
                total_failed += 1
                total_issues += content_issues
        
        # 步骤5：更新检查记录
        duration = time.time() - start_time
        record.status = "completed"
        record.total_contents = total_contents
        record.passed_count = total_passed
        record.failed_count = total_failed
        record.duration = Decimal(f"{duration:.2f}")
        
        await db.commit()
        await db.refresh(record)
        
        logger.info(f"[MetadataQualityCheck] 检查完成: total={total_contents}, passed={total_passed}, failed={total_failed}, issues={total_issues}")
        
    except Exception as exc:
        logger.exception(f"[MetadataQualityCheck] 质检失败：{exc}")
        await db.rollback()
        
        # 记录失败状态
        duration = time.time() - start_time
        try:
            record.status = "failed"
            record.duration = Decimal(f"{duration:.2f}")
            await db.commit()
        except:
            pass
        
        raise
    
    result = {
        "check_id": record.id,
        "total": total_contents,
        "passed": total_passed,
        "failed": total_failed,
        "issues": total_issues,
        "duration": float(record.duration),
    }
    
    # 使用 i18n 生成多语言摘要
    summary = get_msg("QUALITY_CHECK_SUMMARY", 
                     total=total_contents, 
                     passed=total_passed, 
                     failed=total_failed, 
                     issues=total_issues)
    result["summary"] = summary
    
    logger.info(f"[MetadataQualityCheck] ========== 任务结束 ===========")
    return result


# ═══════════════════════════════════════════════════════════
# 辅助函数
# ═══════════════════════════════════════════════════════════


async def _get_metadata(
    db: AsyncSession,
    entity_type: str,
    content_id: int,
) -> Optional[Any]:
    """获取内容对应的元数据。"""
    metadata_model = METADATA_TABLE_MAP.get(entity_type)
    if not metadata_model:
        return None
    
    result = (
        await db.execute(
            select(metadata_model).where(
                metadata_model.content_id == content_id,
                metadata_model.is_deleted == False,
                metadata_model.is_discarded == False,
            )
        )
    ).first()
    
    return result[0] if result else None


async def _check_content(
    db: AsyncSession,
    check_id: int,
    content: Content,
    entity_type: str,
    metadata: Any,
    rules: list[MetadataValidationRule],
) -> int:
    """
    检查单个内容的元数据质量。

    Returns:
        发现的问题数量
    """
    issue_count = 0

    # 获取metadata对象的所有属性名（用于大小写不敏感匹配）
    metadata_attrs = {}
    metadata_dict = {}
    for attr in dir(metadata):
        if not attr.startswith('_'):  # 忽略私有属性
            metadata_attrs[attr.lower()] = attr
            try:
                metadata_dict[attr] = getattr(metadata, attr, None)
            except Exception:
                pass

    for rule in rules:
        # 获取字段值（大小写不敏感匹配）
        field_value = None

        # 1. 先尝试精确匹配
        if hasattr(metadata, rule.field_name):
            field_value = getattr(metadata, rule.field_name, None)
        else:
            # 2. 再尝试小写匹配
            field_attr = metadata_attrs.get(rule.field_name.lower())
            if field_attr:
                field_value = getattr(metadata, field_attr, None)

        # 检查必填
        if rule.rule_type == "mandatory" or rule.is_mandatory in ("Y", "C"):
            issue = await _check_mandatory(
                db, check_id, content, entity_type, rule, field_value, metadata_dict
            )
            if issue:
                issue_count += 1

        # 检查长度
        if rule.rule_type == "length" and field_value:
            issue = await _check_length(
                db, check_id, content, entity_type, rule, field_value
            )
            if issue:
                issue_count += 1

        # 检查格式（正则）
        if rule.rule_type == "regex" and field_value:
            issue = await _check_regex(
                db, check_id, content, entity_type, rule, field_value
            )
            if issue:
                issue_count += 1

        # 检查枚举值
        if rule.rule_type == "enum" and field_value:
            issue = await _check_enum(
                db, check_id, content, entity_type, rule, field_value
            )
            if issue:
                issue_count += 1

    return issue_count


async def _check_mandatory(
    db: AsyncSession,
    check_id: int,
    content: Content,
    entity_type: str,
    rule: MetadataValidationRule,
    field_value: Any,
    metadata_dict: dict,
) -> Optional[MetadataQualityIssue]:
    """检查必填字段。"""
    # 条件必填检查
    if rule.is_mandatory == "C" and rule.mandatory_condition:
        try:
            condition = json.loads(rule.mandatory_condition)
            if not _evaluate_condition(condition, metadata_dict):
                # 条件不满足，不需要必填
                return None
        except json.JSONDecodeError:
            logger.warning(
                f"[MetadataQualityCheck] 条件必填 JSON 解析失败: "
                f"rule_id={rule.id}, condition={rule.mandatory_condition}"
            )
            return None

    # 检查是否为空
    is_empty = (
        field_value is None or
        (isinstance(field_value, str) and field_value.strip() == "") or
        (isinstance(field_value, list) and len(field_value) == 0)
    )

    if is_empty:
        issue = MetadataQualityIssue(
            check_id=check_id,
            content_id=content.id,
            content_name=content.title,
            content_type=content.content_type,
            issue_type="missing",
            field_name=rule.field_name,
            severity=rule.severity,
            expected_value="必填字段",
            actual_value="空值",
        )
        db.add(issue)
        await db.flush()
        return issue

    return None


async def _check_length(
    db: AsyncSession,
    check_id: int,
    content: Content,
    entity_type: str,
    rule: MetadataValidationRule,
    field_value: Any,
) -> Optional[MetadataQualityIssue]:
    """检查字段长度。"""
    if not isinstance(field_value, str):
        return None
    
    length = len(field_value)
    violations = []
    
    if rule.min_length and length < rule.min_length:
        violations.append(f"最小长度{rule.min_length}")
    
    if rule.max_length and length > rule.max_length:
        violations.append(f"最大长度{rule.max_length}")
    
    if violations:
        issue = MetadataQualityIssue(
            check_id=check_id,
            content_id=content.id,
            content_name=content.title,
            content_type=content.content_type,
            issue_type="format",
            field_name=rule.field_name,
            severity=rule.severity,
            expected_value=", ".join(violations),
            actual_value=f"当前长度{length}",
        )
        db.add(issue)
        await db.flush()
        return issue
    
    return None


async def _check_regex(
    db: AsyncSession,
    check_id: int,
    content: Content,
    entity_type: str,
    rule: MetadataValidationRule,
    field_value: Any,
) -> Optional[MetadataQualityIssue]:
    """检查正则表达式。"""
    if not isinstance(field_value, str) or not rule.regex_pattern:
        return None
    
    try:
        if not re.match(rule.regex_pattern, field_value):
            issue = MetadataQualityIssue(
                check_id=check_id,
                content_id=content.id,
                content_name=content.title,
                content_type=content.content_type,
                issue_type="format",
                field_name=rule.field_name,
                severity=rule.severity,
                expected_value=f"匹配正则: {rule.regex_pattern}",
                actual_value=field_value,
            )
            db.add(issue)
            await db.flush()
            return issue
    except re.error:
        logger.warning(f"[MetadataQualityCheck] 无效的正则表达式: {rule.regex_pattern}")
    
    return None


async def _check_enum(
    db: AsyncSession,
    check_id: int,
    content: Content,
    entity_type: str,
    rule: MetadataValidationRule,
    field_value: Any,
) -> Optional[MetadataQualityIssue]:
    """检查枚举值。"""
    if not rule.allowed_values:
        return None
    
    try:
        # 解析允许值（支持逗号分隔或JSON数组）
        allowed = rule.allowed_values.strip()
        if allowed.startswith("["):
            allowed_list = json.loads(allowed)
        else:
            allowed_list = [v.strip() for v in allowed.split(",")]
        
        # 检查值是否在允许列表中
        field_str = str(field_value).strip()
        if field_str not in allowed_list:
            issue = MetadataQualityIssue(
                check_id=check_id,
                content_id=content.id,
                content_name=content.title,
                content_type=content.content_type,
                issue_type="invalid",
                field_name=rule.field_name,
                severity=rule.severity,
                expected_value=f"允许值: {', '.join(allowed_list)}",
                actual_value=field_str,
            )
            db.add(issue)
            await db.flush()
            return issue
    except Exception as e:
        logger.warning(f"[MetadataQualityCheck] 解析允许值失败: {e}")

    return None


# ═══════════════════════════════════════════════════════════
# 条件必填评估（与 metadata_validation_service.py 保持一致）
# ═══════════════════════════════════════════════════════════

def _get_field_value_from_dict(data_dict: dict, field_name: str) -> Any:
    """从字典中获取字段值（大小写不敏感）。"""
    if field_name in data_dict:
        return data_dict[field_name]
    field_name_lower = field_name.lower()
    for key, value in data_dict.items():
        if key.lower() == field_name_lower:
            return value
    return None


def _evaluate_condition(condition: dict, data_dict: dict) -> bool:
    """
    评估条件表达式。

    支持:
    - logic: AND/OR + conditions 列表
    - field + operator + value
    """
    if "logic" in condition:
        logic = condition["logic"].upper()
        sub_conditions = condition.get("conditions", [])
        if logic == "AND":
            return all(_evaluate_condition(sub, data_dict) for sub in sub_conditions)
        elif logic == "OR":
            return any(_evaluate_condition(sub, data_dict) for sub in sub_conditions)
        return False

    field = condition.get("field")
    operator = condition.get("operator", "equals").lower()
    expected_value = condition.get("value")
    actual_value = _get_field_value_from_dict(data_dict, field) if field else None

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
        logger.warning(f"[MetadataQualityCheck] 不支持的条件运算符: {operator}")
        return False
