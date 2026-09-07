"""
元数据校验规则管理 API 路由层。

路由前缀：/metadata-validation-rules

接口列表：
    GET    /metadata-validation-rules/              分页查询规则列表
    GET    /metadata-validation-rules/{rule_id}     获取规则详情
    POST   /metadata-validation-rules/              创建校验规则
    PUT    /metadata-validation-rules/{rule_id}     更新校验规则
    DELETE /metadata-validation-rules/{rule_id}     删除校验规则
    DELETE /metadata-validation-rules/batch         批量删除规则
    POST   /metadata-validation-rules/import        从 Excel 导入规则
    GET    /metadata-validation-rules/by-entity     按实体类型查询规则
"""

from typing import Optional

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user, get_db
from app.common.schemas import BatchDeleteRequest, PaginatedResponse
from app.internal.cms_biz_system.models.metadata_validation_rule import MetadataValidationRule
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.metadata_validation_rule import (
    MetadataValidationRuleCreate,
    MetadataValidationRuleDetail,
    MetadataValidationRuleImportResult,
    MetadataValidationRuleOut,
    MetadataValidationRuleUpdate,
)
from app.internal.cms_biz_system.services.metadata_validation_rule_service import (
    batch_delete_validation_rules,
    create_validation_rule,
    delete_validation_rule,
    get_rules_by_entity_type,
    get_validation_rule,
    import_rules_from_excel,
    list_validation_rules,
    update_validation_rule,
)
from app.internal.cms_biz_system.services.operation_log_service import (
    OperationType,
    write_log,
)
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict

router = APIRouter(prefix="/metadata-validation-rules", tags=["元数据校验规则管理"])


@router.get("/", response_model=PaginatedResponse[MetadataValidationRuleOut])
async def api_list_validation_rules(
    page: int = Query(1, ge=1, description="页码"),
    page_size: int = Query(10, ge=1, le=100, description="每页数量"),
    entity_type: Optional[str] = Query(None, description="实体类型过滤"),
    field_name: Optional[str] = Query(None, description="字段名称过滤"),
    rule_type: Optional[str] = Query(None, description="规则类型过滤"),
    is_mandatory: Optional[str] = Query(None, description="必填标识过滤"),
    severity: Optional[str] = Query(None, description="严重级别过滤"),
    is_enabled: Optional[bool] = Query(None, description="启用状态过滤"),
    sort_by: Optional[str] = Query(None, description="排序字段"),
    sort_order: Optional[str] = Query(None, description="排序方向"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """分页查询校验规则列表。"""
    return await list_validation_rules(
        db=db,
        page=page,
        page_size=page_size,
        entity_type=entity_type,
        field_name=field_name,
        rule_type=rule_type,
        is_mandatory=is_mandatory,
        severity=severity,
        is_enabled=is_enabled,
        sort_by=sort_by,
        sort_order=sort_order,
    )


@router.get("/{rule_id}", response_model=MetadataValidationRuleDetail)
async def api_get_validation_rule(
    rule_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """获取规则详情。"""
    rule = await get_validation_rule(db, rule_id)
    return MetadataValidationRuleDetail.model_validate(rule)


@router.post("/", response_model=MetadataValidationRuleOut)
async def api_create_validation_rule(
    body: MetadataValidationRuleCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """创建校验规则。"""
    rule = await create_validation_rule(db, body)
    new_data = orm_to_dict(rule)
    prev_val, new_val, raw_val = await prepare_log_values(db, "validation_rule", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.VALIDATION_RULE_CREATE,
        operation_object_code="OBJ_VALIDATION_RULE", operation_object_params={"name": rule.entity_type},
        operation_content_code="LOG_VALIDATION_RULE_CREATE",
        operation_content_params={"name": f"{rule.entity_type}.{rule.field_name}"},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="validation_rule",
        entity_id=rule.id,
    )
    await db.commit()
    
    return MetadataValidationRuleOut.model_validate(rule)


@router.put("/{rule_id}", response_model=MetadataValidationRuleOut)
async def api_update_validation_rule(
    rule_id: int,
    body: MetadataValidationRuleUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """更新校验规则。"""
    old_rule = await get_validation_rule(db, rule_id)
    old_data = orm_to_dict(old_rule)
    rule = await update_validation_rule(db, rule_id, body)
    new_data = orm_to_dict(rule)
    prev_val, new_val, raw_val = await prepare_log_values(db, "validation_rule", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.VALIDATION_RULE_EDIT,
        operation_object_code="OBJ_VALIDATION_RULE", operation_object_params={"name": rule.entity_type},
        operation_content_code="LOG_VALIDATION_RULE_EDIT",
        operation_content_params={"name": f"{rule.entity_type}.{rule.field_name}"},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="validation_rule",
        entity_id=rule_id,
    )
    await db.commit()
    
    return MetadataValidationRuleOut.model_validate(rule)


@router.delete("/{rule_id}")
async def api_delete_validation_rule(
    rule_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """删除校验规则（软删除）。"""
    rule = await get_validation_rule(db, rule_id)
    old_data = orm_to_dict(rule)
    await delete_validation_rule(db, rule_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "validation_rule", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.VALIDATION_RULE_DELETE,
        operation_object_code="OBJ_VALIDATION_RULE", operation_object_params={"name": rule.entity_type},
        operation_content_code="LOG_VALIDATION_RULE_DELETE", operation_content_params={"id": rule_id},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="validation_rule",
        entity_id=rule_id,
    )
    await db.commit()
    
    return {"success": True}


@router.delete("/batch")
async def api_batch_delete_validation_rules(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """批量删除校验规则。"""
    rules = (await db.execute(select(MetadataValidationRule).where(MetadataValidationRule.id.in_(body.ids), MetadataValidationRule.is_deleted.is_(False)))).scalars().all()
    rule_names = ", ".join([f"{r.entity_type}.{r.field_name}" for r in rules]) if rules else str(body.ids)
    prev_data = [orm_to_dict(r) for r in rules]
    prev_val, _, raw_val = await prepare_log_values(db, "validation_rule", prev_data, None)
    deleted_count = await batch_delete_validation_rules(db, body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.VALIDATION_RULE_BATCH_DELETE,
        operation_object_code="OBJ_VALIDATION_RULE", operation_object_params={"name": rule_names},
        operation_content_code="LOG_VALIDATION_RULE_BATCH_DELETE", operation_content_params={"names": rule_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="validation_rule",
        entity_id=body.ids[0] if body.ids else None,
        previous_value=prev_val,
        updated_value=None,
        updated_value_json=raw_val,
    )
    await db.commit()
    
    return {"success": True, "deleted": deleted_count}


@router.post("/import", response_model=MetadataValidationRuleImportResult)
async def api_import_rules_from_excel(
    file: UploadFile,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    """从 Excel 文件导入校验规则。"""
    file_content = await file.read()
    result = await import_rules_from_excel(db, file_content)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.VALIDATION_RULE_IMPORT,
        operation_object_code="OBJ_VALIDATION_RULE_IMPORT", operation_object_params={"name": file.filename},
        operation_content_code="LOG_VALIDATION_RULE_IMPORT",
        operation_content_params={
            "result": (
                f"total={result.total}, created={result.created}, "
                f"updated={result.updated}, failed={result.failed}"
            )
        },
        ip_address=_get_ip(request),
        result="success",
        entity_type="validation_rule",
    )
    await db.commit()
    
    return result


@router.get("/by-entity", response_model=list[MetadataValidationRuleOut])
async def api_get_rules_by_entity_type(
    entity_type: str = Query(..., description="实体类型"),
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """按实体类型查询所有启用的规则（供质量检查使用）。"""
    rules = await get_rules_by_entity_type(db, entity_type)
    return [MetadataValidationRuleOut.model_validate(rule) for rule in rules]
