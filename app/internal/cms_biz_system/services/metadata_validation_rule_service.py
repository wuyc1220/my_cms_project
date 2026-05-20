"""
元数据校验规则服务层。

职责：
- 规则列表分页查询（支持多维度过滤）
- 规则详情查询
- 规则创建/更新/删除
- 从 Excel 批量导入规则
- 按实体类型加载启用的规则（供质量检查使用）
"""

import io
import json
from datetime import datetime
from typing import Optional

from loguru import logger
from openpyxl import load_workbook
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException, ErrorCode, NotFoundException
from app.common.core.i18n import get_msg
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.models.metadata_validation_rule import MetadataValidationRule
from app.internal.cms_biz_system.schemas.metadata_validation_rule import (
    MetadataValidationRuleCreate,
    MetadataValidationRuleDetail,
    MetadataValidationRuleImportResult,
    MetadataValidationRuleOut,
    MetadataValidationRuleUpdate,
)


# 允许的实体类型
VALID_ENTITY_TYPES = {"PROGRAM", "SERIES", "CHANNEL", "SCHEDULE"}

# 允许的规则类型
VALID_RULE_TYPES = {"mandatory", "format", "length", "enum", "regex", "custom"}

# 允许的严重级别
VALID_SEVERITY_LEVELS = {"critical", "medium", "minor"}


async def list_validation_rules(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    entity_type: Optional[str] = None,
    field_name: Optional[str] = None,
    rule_type: Optional[str] = None,
    is_mandatory: Optional[str] = None,
    severity: Optional[str] = None,
    is_enabled: Optional[bool] = None,
    sort_by: Optional[str] = None,
    sort_order: Optional[str] = None,
) -> PaginatedResponse[MetadataValidationRuleOut]:
    """
    分页查询校验规则列表。
    
    Args:
        db: 数据库会话
        page: 页码
        page_size: 每页数量
        entity_type: 实体类型过滤
        field_name: 字段名称过滤
        rule_type: 规则类型过滤
        is_mandatory: 必填标识过滤
        severity: 严重级别过滤
        is_enabled: 启用状态过滤
        sort_by: 排序字段
        sort_order: 排序方向（asc/desc）
    
    Returns:
        分页响应
    """
    query = select(MetadataValidationRule).where(MetadataValidationRule.is_deleted == False)
    
    # 过滤条件
    if entity_type:
        query = query.where(MetadataValidationRule.entity_type == entity_type)
    if field_name:
        query = query.where(MetadataValidationRule.field_name.ilike(f"%{field_name}%"))
    if rule_type:
        query = query.where(MetadataValidationRule.rule_type == rule_type)
    if is_mandatory:
        query = query.where(MetadataValidationRule.is_mandatory == is_mandatory)
    if severity:
        query = query.where(MetadataValidationRule.severity == severity)
    if is_enabled is not None:
        query = query.where(MetadataValidationRule.is_enabled == is_enabled)
    
    # 排序
    if sort_by and sort_order:
        sort_column = getattr(MetadataValidationRule, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == "asc" else sort_column.desc()
            query = query.order_by(order_func, MetadataValidationRule.id.desc())
        else:
            query = query.order_by(MetadataValidationRule.id.desc())
    else:
        query = query.order_by(MetadataValidationRule.id.desc())
    
    # 总数
    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()
    
    # 分页查询
    items = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()
    
    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[MetadataValidationRuleOut.model_validate(item) for item in items],
    )


async def get_validation_rule(db: AsyncSession, rule_id: int) -> MetadataValidationRule:
    """
    获取规则详情。
    
    Args:
        db: 数据库会话
        rule_id: 规则ID
    
    Returns:
        规则对象
    
    Raises:
        NotFoundException: 规则不存在
    """
    rule = (
        await db.execute(
            select(MetadataValidationRule).where(
                MetadataValidationRule.id == rule_id,
                MetadataValidationRule.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    
    if not rule:
        raise NotFoundException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("VALIDATION_RULE_NOT_FOUND")
        )
    
    return rule


async def create_validation_rule(
    db: AsyncSession,
    data: MetadataValidationRuleCreate,
) -> MetadataValidationRule:
    """
    创建校验规则。
    
    Args:
        db: 数据库会话
        data: 创建请求数据
    
    Returns:
        创建的规则对象
    
    Raises:
        BusinessException: 参数校验失败或规则已存在
    """
    # 参数校验
    _validate_rule_params(data.entity_type, data.rule_type, data.severity)
    
    # 检查唯一约束
    existing = (
        await db.execute(
            select(MetadataValidationRule.id).where(
                MetadataValidationRule.entity_type == data.entity_type,
                MetadataValidationRule.field_name == data.field_name,
                MetadataValidationRule.rule_type == data.rule_type,
                MetadataValidationRule.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    
    if existing:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("VALIDATION_RULE_ALREADY_EXISTS")
        )
    
    # 创建记录
    rule = MetadataValidationRule(**data.model_dump())
    db.add(rule)
    await db.commit()
    await db.refresh(rule)
    
    logger.info(f"创建校验规则成功: id={rule.id}, entity_type={rule.entity_type}, field_name={rule.field_name}")
    
    return rule


async def update_validation_rule(
    db: AsyncSession,
    rule_id: int,
    data: MetadataValidationRuleUpdate,
) -> MetadataValidationRule:
    """
    更新校验规则。
    
    Args:
        db: 数据库会话
        rule_id: 规则ID
        data: 更新请求数据
    
    Returns:
        更新后的规则对象
    
    Raises:
        NotFoundException: 规则不存在
        BusinessException: 参数校验失败
    """
    rule = await get_validation_rule(db, rule_id)
    
    # 参数校验
    if data.severity:
        _validate_severity(data.severity)
    
    # 更新字段
    update_data = data.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(rule, field, value)
    
    await db.commit()
    await db.refresh(rule)
    
    logger.info(f"更新校验规则成功: id={rule.id}")
    
    return rule


async def delete_validation_rule(db: AsyncSession, rule_id: int) -> None:
    """
    软删除校验规则。
    
    Args:
        db: 数据库会话
        rule_id: 规则ID
    
    Raises:
        NotFoundException: 规则不存在
        BusinessException: 系统内置规则不允许删除
    """
    rule = await get_validation_rule(db, rule_id)
    
    # 系统内置规则不允许删除
    if rule.is_system:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("SYSTEM_RULE_CANNOT_DELETE")
        )
    
    # 软删除
    rule.is_deleted = True
    await db.commit()
    
    logger.info(f"删除校验规则成功: id={rule_id}")


async def batch_delete_validation_rules(
    db: AsyncSession,
    rule_ids: list[int],
) -> int:
    """
    批量软删除校验规则。
    
    Args:
        db: 数据库会话
        rule_ids: 规则ID列表
    
    Returns:
        成功删除的数量
    """
    rules = (
        await db.execute(
            select(MetadataValidationRule).where(
                MetadataValidationRule.id.in_(rule_ids),
                MetadataValidationRule.is_deleted == False,
                MetadataValidationRule.is_system == False,  # 排除系统内置规则
            )
        )
    ).scalars().all()
    
    for rule in rules:
        rule.is_deleted = True
    
    await db.commit()
    
    deleted_count = len(rules)
    logger.info(f"批量删除校验规则成功: 删除{deleted_count}条")
    
    return deleted_count


async def import_rules_from_excel(
    db: AsyncSession,
    file_content: bytes,
) -> MetadataValidationRuleImportResult:
    """
    从 Excel 文件批量导入校验规则。
    
    Args:
        db: 数据库会话
        file_content: Excel 文件内容（字节）
    
    Returns:
        导入结果
    """
    result = MetadataValidationRuleImportResult()
    errors = []
    
    try:
        wb = load_workbook(io.BytesIO(file_content))
        
        for sheet_name in wb.sheetnames:
            ws = wb[sheet_name]
            
            # 映射Sheet名到实体类型
            entity_type = _map_sheet_to_entity_type(sheet_name)
            if not entity_type:
                errors.append(f"跳过未知Sheet: {sheet_name}")
                continue
            
            logger.info(f"处理Sheet: {sheet_name} -> entity_type={entity_type}")
            
            # 解析行数据
            for row_idx in range(2, ws.max_row + 1):  # 从第2行开始（第1行是表头）
                try:
                    rule_data = _parse_excel_row(ws, row_idx, entity_type)
                    if not rule_data:
                        continue
                    
                    # 检查是否已存在
                    existing = (
                        await db.execute(
                            select(MetadataValidationRule).where(
                                MetadataValidationRule.entity_type == rule_data["entity_type"],
                                MetadataValidationRule.field_name == rule_data["field_name"],
                                MetadataValidationRule.rule_type == rule_data["rule_type"],
                                MetadataValidationRule.is_deleted == False,
                            )
                        )
                    ).scalar_one_or_none()
                    
                    if existing:
                        # 更新现有规则
                        for field, value in rule_data.items():
                            setattr(existing, field, value)
                        result.updated += 1
                    else:
                        # 创建新规则
                        rule = MetadataValidationRule(**rule_data)
                        db.add(rule)
                        result.created += 1
                    
                    result.total += 1
                    
                except Exception as e:
                    errors.append(f"Sheet={sheet_name}, Row={row_idx}: {str(e)}")
                    result.failed += 1
        
        await db.commit()
        result.errors = errors
        
        logger.info(
            f"导入校验规则完成: total={result.total}, "
            f"created={result.created}, updated={result.updated}, failed={result.failed}"
        )
        
    except Exception as e:
        logger.error(f"导入Excel失败: {str(e)}")
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("EXCEL_IMPORT_FAILED")
        )
    
    return result


async def get_rules_by_entity_type(
    db: AsyncSession,
    entity_type: str,
) -> list[MetadataValidationRule]:
    """
    获取指定实体类型的所有启用规则（供质量检查使用）。
    
    Args:
        db: 数据库会话
        entity_type: 实体类型
    
    Returns:
        规则列表
    """
    rules = (
        await db.execute(
            select(MetadataValidationRule).where(
                MetadataValidationRule.entity_type == entity_type,
                MetadataValidationRule.is_deleted == False,
                MetadataValidationRule.is_enabled == True,
            )
        )
    ).scalars().all()
    
    return list(rules)


# ═══════════════════════════════════════════════════════════
# 内部辅助函数
# ═══════════════════════════════════════════════════════════


def _validate_rule_params(entity_type: str, rule_type: str, severity: str) -> None:
    """校验规则参数合法性。"""
    if entity_type not in VALID_ENTITY_TYPES:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("INVALID_ENTITY_TYPE")
        )
    if rule_type not in VALID_RULE_TYPES:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("INVALID_RULE_TYPE")
        )
    _validate_severity(severity)


def _validate_severity(severity: str) -> None:
    """校验严重级别。"""
    if severity not in VALID_SEVERITY_LEVELS:
        raise BusinessException(
            ErrorCode.VALIDATION_ERROR,
            get_msg("INVALID_SEVERITY_LEVEL")
        )


def _map_sheet_to_entity_type(sheet_name: str) -> Optional[str]:
    """将Excel Sheet名映射到实体类型。"""
    mapping = {
        "Program": "PROGRAM",
        "Series": "SERIES",
        "Channel": "CHANNEL",
        "Schedule": "SCHEDULE",
    }
    return mapping.get(sheet_name)


def _parse_excel_row(ws, row_idx: int, entity_type: str) -> Optional[dict]:
    """
    解析Excel行数据。
    
    假设Excel列顺序：
    Column 2: Property (主列)
    Column 3: Property (子列)
    Column 4: Type
    Column 5: Description
    Column 6: Mandatory (Y/N/C)
    Column 7: Length
    Column 8: MultiLanguage
    Column 9: Allowed Values
    """
    property_main = ws.cell(row=row_idx, column=2).value
    property_sub = ws.cell(row=row_idx, column=3).value
    prop_type = ws.cell(row=row_idx, column=4).value
    description = ws.cell(row=row_idx, column=5).value
    mandatory = ws.cell(row=row_idx, column=6).value
    length = ws.cell(row=row_idx, column=7).value
    multi_lang = ws.cell(row=row_idx, column=8).value
    allowed_values = ws.cell(row=row_idx, column=9).value
    
    # 跳过空行
    if not property_main and not property_sub:
        return None
    
    # 确定字段名（优先使用子列）
    field_name = property_sub if property_sub else property_main
    
    # 解析长度
    min_length = None
    max_length = None
    if length:
        length_str = str(length).strip()
        if "-" in length_str:
            parts = length_str.split("-")
            min_length = int(parts[0].strip()) if parts[0].strip().isdigit() else None
            max_length = int(parts[1].strip()) if parts[1].strip().isdigit() else None
        elif length_str.isdigit():
            max_length = int(length_str)
    
    # 构建规则数据
    rule_data = {
        "entity_type": entity_type,
        "field_name": field_name,
        "rule_type": "mandatory" if mandatory in ("Y", "C") else "length",
        "is_mandatory": mandatory if mandatory in ("Y", "N", "C") else "N",
        "min_length": min_length,
        "max_length": max_length,
        "data_type": prop_type,
        "multi_language": multi_lang == "Y" if multi_lang else False,
        "description": description,
        "severity": "critical" if mandatory == "Y" else "medium",
        "is_enabled": True,
        "is_system": True,
    }
    
    # 如果有允许值，添加enum规则
    if allowed_values:
        rule_data["allowed_values"] = str(allowed_values).strip()
    
    return rule_data
