"""
MetadataQualityCheck 业务逻辑 - 元数据质量检查引擎。

功能：
    1. 从 Content 表扫描在线内容（MOVIE/EPISODE/SERIES/SEASON/CHANNEL/SCHEDULE）；
    2. 从 metadata_validation_rule 表加载校验规则；
    3. 按规则检查必填字段、格式、长度、枚举值；
    4. 内置检查：关联关系缺失（无效数据）、敏感词（含敏感词）、许可证授权（授权异常）；
    5. 将不合格项写入 metadata_quality_issue 表；
    6. 在 metadata_quality_check 表更新统计计数。
"""

import json
import re
import time
from datetime import date, datetime, timezone
from decimal import Decimal
from typing import Any, Optional

from loguru import logger
from sqlalchemy import select
from sqlalchemy import inspect as sa_inspect
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.i18n import get_msg
from app.common.services.sensitive_check_service import SensitiveCheckService
from app.internal.cms_biz_system.models.metadata_quality import MetadataQualityCheck, MetadataQualityIssue
from app.internal.cms_biz_system.models.metadata_validation_rule import MetadataValidationRule
from app.internal.cms_biz_system.models.dict import DictNode
from app.internal.cms_biz_orchestration.models.content_metadata import (
    ContentMetadata,
    SeriesMetadata,
    ChannelMetadata,
    ScheduleMetadata,
)
from app.internal.cms_biz_orchestration.models.cast_role_map import CastRoleMap
from app.internal.cms_biz_orchestration.services.metadata_validation_service import (
    _SYSTEM_MAINTAINED_FIELDS,
)
from app.internal.cms_biz_metada.models.basic import Cast, Genre
from app.internal.cms_biz_scp.models.trade import Contract, License, LicenseContent
from app.internal.cms_biz_package.models.package import Content, ContentGenre


# 实体类型映射
ENTITY_TYPE_MAP = {
    "MOVIE": "PROGRAM",
    "EPISODE": "PROGRAM",
    "SERIES": "SERIES",
    "SEASON_SERIES": "SERIES",
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

# 实体真实列缓存（含 genre_id 虚拟字段）
_VALID_FIELDS_CACHE: dict[str, set[str]] = {}


# ═══════════════════════════════════════════════════════════
# 问题描述 i18n 存储格式（语言无关，展示层按请求语言翻译）
#   {"key": "...", "params": {...}}  单条消息，params 的字符串值递归翻译
#   {"items": [...], "sep": "..."}   列表拼接，逐项递归翻译后以 sep 连接
# ═══════════════════════════════════════════════════════════


def _i18n_value(key: str, **params) -> str:
    """生成语言无关的问题描述（i18n key + 参数），展示层按当前语言翻译。"""
    return json.dumps(
        {"key": key, "params": {k: str(v) for k, v in params.items()}},
        ensure_ascii=False,
    )


def _i18n_list(items: list[str], sep: str = "、") -> str:
    """生成语言无关的列表描述（逐项翻译后以 sep 连接）。"""
    return json.dumps({"items": items, "sep": sep}, ensure_ascii=False)


def _get_entity_valid_fields(entity_type: str) -> set[str]:
    """
    获取实体元数据表的真实列集合（小写）。

    genre_id 存储在 content_genre 中间表（非元数据表列），视为有效字段；
    用于跳过引用了不存在字段的过期规则，与实时校验口径一致。
    统一小写比较，保留引擎对规则字段名的大小写不敏感匹配能力。
    """
    if entity_type not in _VALID_FIELDS_CACHE:
        model = METADATA_TABLE_MAP.get(entity_type)
        if model is None:
            _VALID_FIELDS_CACHE[entity_type] = {"genre_id"}
        else:
            cols = {attr.key.lower() for attr in sa_inspect(model).mapper.column_attrs}
            _VALID_FIELDS_CACHE[entity_type] = cols | {"genre_id"}
    return _VALID_FIELDS_CACHE[entity_type]


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
                        "InProgress",         # 审核中（需求3.8.1.4：扫描审核中/准备发布/发布失败）
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
            
            # 获取该实体类型的规则（无规则时仍执行内置检查：关联/敏感词/许可证）
            entity_rules = rules_by_entity.get(entity_type, [])

            # 获取元数据（缺失时仅跳过规则类检查，内置检查照常执行）
            metadata = await _get_metadata(db, entity_type, content.id)
            if not metadata:
                logger.debug(f"[MetadataQualityCheck] 无元数据，跳过规则类检查: content_id={content.id}")

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

    # 从 content_genre 中间表获取 genre_ids（用于 genre_id 字段校验及题材关联失效检查）
    genre_ids_from_db = []
    try:
        genre_stmt = select(ContentGenre.genre_id).where(
            ContentGenre.content_id == content.id,
            ContentGenre.is_deleted.is_(False),
        )
        genre_rows = (await db.execute(genre_stmt)).all()
        genre_ids_from_db = [r[0] for r in genre_rows]
    except Exception:
        pass

    # 元数据缺失：跳过规则类与敏感词检查，仅执行不依赖元数据的内置检查
    if metadata is None:
        issue_count += await _check_associations(db, check_id, content, genre_ids_from_db, {})
        issue_count += await _check_license(db, check_id, content)
        return issue_count

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

    # 该实体元数据表的真实列（genre_id 走 content_genre 中间表，视为有效）
    valid_fields = _get_entity_valid_fields(entity_type)

    for rule in rules:
        # 系统维护字段：与实时校验口径一致——用户无法在 UI 中补填，不参与缺失判定
        # （统一小写比较，与下方大小写不敏感匹配保持一致）
        if rule.field_name.lower() in _SYSTEM_MAINTAINED_FIELDS:
            continue

        # 规则字段不属于该实体元数据表（规则配置疑似过期），跳过并告警
        if rule.field_name.lower() not in valid_fields:
            logger.warning(
                "[MetadataQualityCheck] 规则字段 [{}] 不属于 {} 元数据表字段，跳过（规则配置疑似过期）",
                rule.field_name, entity_type,
            )
            continue

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
        
        # 3. 特殊处理 genre_id：从 content_genre 表获取
        if field_value is None and rule.field_name.lower() == 'genre_id':
            if genre_ids_from_db:
                field_value = genre_ids_from_db[0]  # 取第一个 genre_id

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

    # ── 内置检查（需求3.8.1.2（5），与规则无关，逐内容执行）──

    # 关联关系缺失检查（字典子项删除/Cast 删除/层级关联缺失）→ 无效数据
    issue_count += await _check_associations(db, check_id, content, genre_ids_from_db, metadata_dict)

    # 敏感词检查（按敏感词管理模块生效的定义）→ 含敏感词
    issue_count += await _check_sensitive_words(db, check_id, content, metadata_dict)

    # 许可证授权检查（是否关联有效许可证、是否在有效期内、是否关联合同）→ 授权异常
    issue_count += await _check_license(db, check_id, content)

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
            expected_value=_i18n_value("MQ_EXPECTED_MANDATORY"),
            actual_value=_i18n_value("MQ_ACTUAL_EMPTY"),
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
        violations.append(_i18n_value("MQ_EXPECTED_LENGTH_MIN", n=rule.min_length))

    if rule.max_length and length > rule.max_length:
        violations.append(_i18n_value("MQ_EXPECTED_LENGTH_MAX", n=rule.max_length))

    if violations:
        issue = MetadataQualityIssue(
            check_id=check_id,
            content_id=content.id,
            content_name=content.title,
            content_type=content.content_type,
            issue_type="format",
            field_name=rule.field_name,
            severity=rule.severity,
            expected_value=violations[0] if len(violations) == 1 else _i18n_list(violations, sep=", "),
            actual_value=_i18n_value("MQ_ACTUAL_LENGTH", n=length),
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
                expected_value=_i18n_value("MQ_EXPECTED_REGEX", pattern=rule.regex_pattern),
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
                expected_value=_i18n_value("MQ_EXPECTED_ENUM", values=", ".join(allowed_list)),
                actual_value=field_str,
            )
            db.add(issue)
            await db.flush()
            return issue
    except Exception as e:
        logger.warning(f"[MetadataQualityCheck] 解析允许值失败: {e}")

    return None


# ═══════════════════════════════════════════════════════════
# 内置检查：关联关系缺失 / 敏感词 / 许可证授权（需求3.8.1.2（5））
# ═══════════════════════════════════════════════════════════

# VOD 内容类型（许可证授权检查要求必须关联有效许可证的类型）
_VOD_CONTENT_TYPES = {"MOVIE", "EPISODE", "SERIES", "SEASON"}

# 以数据字典（dict 表）为来源的元数据字段
_DICT_BACKED_FIELDS = ("vod_type", "rating_level", "language")


async def _add_builtin_issue(
    db: AsyncSession,
    check_id: int,
    content: Content,
    issue_type: str,
    field_name: str,
    severity: str,
    expected_value: str,
    actual_value: str,
) -> None:
    """写入内置检查发现的问题。"""
    issue = MetadataQualityIssue(
        check_id=check_id,
        content_id=content.id,
        content_name=content.title,
        content_type=content.content_type,
        issue_type=issue_type,
        field_name=field_name,
        severity=severity,
        expected_value=expected_value,
        actual_value=actual_value,
    )
    db.add(issue)
    await db.flush()


async def _check_associations(
    db: AsyncSession,
    check_id: int,
    content: Content,
    genre_ids: list[int],
    metadata_dict: dict,
) -> int:
    """
    关联关系缺失检查（问题类型：无效数据）。

    覆盖场景（需求3.8.1.2（5））：
        1. 单集必须关联到一个连续剧名下；
        2. 单季连续剧必须关联到一个总季连续剧名下；
        3. 节目单必须关联到一个频道名下；
        4. 数据字典中子项被逻辑删除导致关联关系失效（题材、vod_type/rating_level/language）；
        5. Cast 人物被逻辑删除导致的人物角色关系失效。
    """
    issue_count = 0

    # 1-3) 层级关联（Content.parent_id：EPISODE→SERIES；单季SERIES→SEASON；SCHEDULE→CHANNEL）
    if content.content_type == "EPISODE" and content.parent_id is None:
        await _add_builtin_issue(db, check_id, content, "invalid", "parent_id", "medium",
                                 _i18n_value("MQ_EXPECTED_EPISODE_SERIES"),
                                 _i18n_value("MQ_ACTUAL_NO_SERIES"))
        issue_count += 1
    elif content.content_type == "SERIES" and content.series_type == 2 and content.parent_id is None:
        await _add_builtin_issue(db, check_id, content, "invalid", "parent_id", "medium",
                                 _i18n_value("MQ_EXPECTED_SEASON_SERIES"),
                                 _i18n_value("MQ_ACTUAL_NO_PARENT_SERIES"))
        issue_count += 1
    elif content.content_type == "SCHEDULE" and content.parent_id is None:
        await _add_builtin_issue(db, check_id, content, "invalid", "parent_id", "medium",
                                 _i18n_value("MQ_EXPECTED_SCHEDULE_CHANNEL"),
                                 _i18n_value("MQ_ACTUAL_NO_CHANNEL"))
        issue_count += 1

    # 4a) 题材（genre）被逻辑删除 → 关联关系失效
    if genre_ids:
        deleted_genre_ids = (
            await db.execute(
                select(Genre.id).where(
                    Genre.id.in_(genre_ids),
                    Genre.is_deleted == True,
                )
            )
        ).scalars().all()
        if deleted_genre_ids:
            await _add_builtin_issue(db, check_id, content, "invalid", "genre_id", "medium",
                                     _i18n_value("MQ_EXPECTED_GENRE_NOT_DELETED"),
                                     _i18n_value("MQ_ACTUAL_GENRE_DELETED", ids=deleted_genre_ids))
            issue_count += 1

    # 4b) 数据字典子项被逻辑删除 → 引用失效（仅标记"引用项已删除"，避免误报）
    dict_values = {}
    for field in _DICT_BACKED_FIELDS:
        value = _get_field_value_from_dict(metadata_dict, field)
        if isinstance(value, str) and value.strip():
            dict_values[field] = value.strip()
    if dict_values:
        deleted_rows = (
            await db.execute(
                select(DictNode.code, DictNode.name).where(DictNode.is_deleted == True)
            )
        ).all()
        deleted_codes = {r[0] for r in deleted_rows if r[0]}
        deleted_names = {r[1] for r in deleted_rows if r[1]}
        for field, value in dict_values.items():
            if value in deleted_codes or value in deleted_names:
                await _add_builtin_issue(db, check_id, content, "invalid", field, "medium",
                                         _i18n_value("MQ_EXPECTED_DICT_NOT_DELETED"),
                                         _i18n_value("MQ_ACTUAL_DICT_DELETED", value=value))
                issue_count += 1

    # 5) Cast 人物被逻辑删除 → 人物角色关系失效
    cast_ids = (
        await db.execute(
            select(CastRoleMap.cast_id).where(
                CastRoleMap.content_id == content.id,
                CastRoleMap.is_deleted == False,
                CastRoleMap.is_discarded == False,
            )
        )
    ).scalars().all()
    if cast_ids:
        deleted_casts = (
            await db.execute(
                select(Cast.id, Cast.name).where(
                    Cast.id.in_(cast_ids),
                    Cast.is_deleted == True,
                )
            )
        ).all()
        if deleted_casts:
            desc = ", ".join(f"{cid}({cname})" for cid, cname in deleted_casts)
            await _add_builtin_issue(db, check_id, content, "invalid", "cast_id", "medium",
                                     _i18n_value("MQ_EXPECTED_CAST_NOT_DELETED"),
                                     _i18n_value("MQ_ACTUAL_CAST_DELETED", desc=desc))
            issue_count += 1

    return issue_count


async def _check_sensitive_words(
    db: AsyncSession,
    check_id: int,
    content: Content,
    metadata_dict: dict,
) -> int:
    """
    敏感词检查（问题类型：含敏感词）。

    按敏感词管理模块中生效的定义（status=active 且未删除），
    逐字段检查实体元数据文本是否包含敏感词。
    """
    service = SensitiveCheckService.get_instance()
    issue_count = 0

    for field, value in metadata_dict.items():
        if not isinstance(value, str) or not value.strip():
            continue
        try:
            matched = await service.find_matches(db, value)
        except Exception as e:
            logger.warning(f"[MetadataQualityCheck] 敏感词检查异常，跳过字段 {field}: {e}")
            continue
        if matched:
            await _add_builtin_issue(db, check_id, content, "sensitive", field, "critical",
                                     _i18n_value("MQ_EXPECTED_NO_SENSITIVE"),
                                     _i18n_value("MQ_ACTUAL_SENSITIVE_HIT", words=", ".join(matched)))
            issue_count += 1

    return issue_count


async def _check_license(
    db: AsyncSession,
    check_id: int,
    content: Content,
) -> int:
    """
    许可证授权检查（问题类型：授权异常）。

    覆盖场景（需求3.8.1.2（5））：
        1. 检查各实体是否关联有效的许可证（VOD 内容必须关联）；
        2. 许可证是否在有效期范围内（start_date <= 今天 <= end_date，end_date 为空视为长期）；
        3. 许可证必须关联到一个合同名下（合同未逻辑删除）。
    """
    today = date.today()
    is_vod = content.content_type in _VOD_CONTENT_TYPES

    rows = (
        await db.execute(
            select(License, Contract)
            .outerjoin(Contract, License.contract_id == Contract.id)
            .join(LicenseContent, LicenseContent.license_id == License.id)
            .where(
                LicenseContent.content_id == content.id,
                LicenseContent.is_deleted == False,
                License.is_deleted == False,
            )
        )
    ).all()

    # 未关联任何许可证：仅 VOD 内容视为问题（频道/节目单可能无许可证要求）
    if not rows:
        if is_vod:
            await _add_builtin_issue(db, check_id, content, "authorization", "license", "medium",
                                     _i18n_value("MQ_EXPECTED_VALID_LICENSE"),
                                     _i18n_value("MQ_ACTUAL_NO_LICENSE"))
            return 1
        return 0

    # 有效性判定：在有效期内 + 已关联合同且合同未被逻辑删除
    has_valid = False
    invalid_desc = []
    for lic, contract in rows:
        reasons = []
        if lic.contract_id is None or contract is None:
            reasons.append(_i18n_value("MQ_REASON_LICENSE_NO_CONTRACT"))
        elif contract.is_deleted:
            reasons.append(_i18n_value("MQ_REASON_CONTRACT_DELETED"))
        if lic.start_date is not None and lic.start_date > today:
            reasons.append(_i18n_value("MQ_REASON_LICENSE_NOT_STARTED", date=lic.start_date))
        if lic.end_date is not None and lic.end_date < today:
            reasons.append(_i18n_value("MQ_REASON_LICENSE_EXPIRED", date=lic.end_date))
        if reasons:
            invalid_desc.append(_i18n_value("MQ_LICENSE_DESC", id=lic.id, name=lic.name,
                                            reasons=_i18n_list(reasons, sep=_i18n_value("MQ_SEP_ENUM"))))
        else:
            has_valid = True

    if not has_valid:
        await _add_builtin_issue(db, check_id, content, "authorization", "license", "medium",
                                 _i18n_value("MQ_EXPECTED_LICENSE_VALID"),
                                 _i18n_list(invalid_desc, sep=_i18n_value("MQ_SEP_SEMICOLON")))
        return 1

    return 0


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
