from datetime import datetime

from fastapi import APIRouter, Depends, Query, Request, UploadFile
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import _get_ip, get_current_user
from app.common.dependencies import get_db
from app.common.services.sensitive_check_service import SensitiveCheckService
from app.internal.cms_biz_system.models.sensitive_word import SensitiveWord
from app.internal.cms_biz_system.models.user import User
from app.common.schemas import BatchDeleteRequest
from app.internal.cms_biz_system.schemas.sensitive_word import (
    BatchStatusRequest,
    ImportResult,
    SensitiveWordCreate,
    SensitiveWordListItem,
    SensitiveWordUpdate,
    StatusRequest,
)
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict
from app.internal.cms_biz_system.services.sensitive_word_service import (
    batch_delete,
    batch_toggle_status,
    create_sensitive_word,
    delete_sensitive_word,
    export_excel,
    generate_import_template,
    get_sensitive_word,
    import_excel,
    list_sensitive_words,
    toggle_status,
    update_sensitive_word,
)

router = APIRouter(prefix="/sensitive-words")


class SensitiveCheckRequest(BaseModel):
    """敏感词预校验请求体：任意键值对，递归检查所有字符串值。"""
    data: dict


class SensitiveCheckResponse(BaseModel):
    """敏感词预校验响应。"""
    has_sensitive: bool
    matched_fields: list[str] = []


@router.get("/", response_model=PaginatedResponse[SensitiveWordListItem])
async def get_sensitive_word_list(
    page: int = 1,
    page_size: int = 10,
    keyword: str | None = None,
    type_codes: list[str] | None = Query(None),
    status: str | None = None,
    created_start: datetime | None = None,
    created_end: datetime | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_sensitive_words(
        db, page, page_size, keyword, type_codes, status,
        created_start, created_end, sort_by, sort_order,
    )


@router.get("/import-template")
async def download_import_template_api(
    _: User = Depends(get_current_user),
):
    """下载导入模板，表头按请求语言（Accept-Language）本地化"""
    data = generate_import_template()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=sensitive_words_import_template.xlsx"},
    )


@router.get("/{word_id}", response_model=SensitiveWordListItem)
async def get_sensitive_word_detail(
    word_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return SensitiveWordListItem.model_validate(await get_sensitive_word(db, word_id))


@router.post("/", response_model=SensitiveWordListItem)
async def create_sensitive_word_api(
    body: SensitiveWordCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    word = await create_sensitive_word(db, body)
    new_data = orm_to_dict(word)
    prev_val, new_val, raw_val = await prepare_log_values(db, "sensitive_word", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_CREATE,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": body.keyword},
        operation_content_code="LOG_SENSITIVE_WORD_CREATE", operation_content_params={"keyword": body.keyword},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="sensitive_word",
        entity_id=word.id,
    )
    await db.commit()
    return SensitiveWordListItem.model_validate(word)


@router.put("/{word_id}", response_model=SensitiveWordListItem)
async def update_sensitive_word_api(
    word_id: int,
    body: SensitiveWordUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old = await get_sensitive_word(db, word_id)
    old_data = orm_to_dict(old)
    old_keyword = old.keyword
    word = await update_sensitive_word(db, word_id, body)
    new_data = orm_to_dict(word)
    prev_val, new_val, raw_val = await prepare_log_values(db, "sensitive_word", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_EDIT,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": old_keyword},
        operation_content_code="LOG_SENSITIVE_WORD_EDIT", operation_content_params={"keyword": word.keyword},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="sensitive_word",
        entity_id=word_id,
    )
    await db.commit()
    return SensitiveWordListItem.model_validate(word)


@router.put("/{word_id}/status", response_model=SensitiveWordListItem)
async def toggle_sensitive_word_status_api(
    word_id: int,
    body: StatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_word = await get_sensitive_word(db, word_id)
    old_data = orm_to_dict(old_word)
    word = await toggle_status(db, word_id, body.status)
    new_data = orm_to_dict(word)
    prev_val, new_val, raw_val = await prepare_log_values(db, "sensitive_word", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_STATUS,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": word.keyword},
        operation_content_code="LOG_SENSITIVE_WORD_STATUS_ENABLED" if body.status == "active" else "LOG_SENSITIVE_WORD_STATUS_DISABLED",
        operation_content_params={"keyword": word.keyword},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="sensitive_word",
        entity_id=word_id,
    )
    await db.commit()
    return SensitiveWordListItem.model_validate(word)


@router.post("/batch-status")
async def batch_toggle_sensitive_word_status_api(
    body: BatchStatusRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    count = await batch_toggle_status(db, body)
    rows = (await db.execute(select(SensitiveWord.keyword).where(SensitiveWord.id.in_(body.ids)))).scalars().all()
    word_names = ", ".join(rows) if rows else str(body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_BATCH_STATUS,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": word_names},
        operation_content_code="LOG_SENSITIVE_WORD_BATCH_STATUS_ENABLED" if body.status == "active" else "LOG_SENSITIVE_WORD_BATCH_STATUS_DISABLED",
        operation_content_params={"names": word_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="sensitive_word",
    )
    await db.commit()
    return {"success": True, "count": count}


@router.delete("/batch")
async def batch_delete_sensitive_words_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    deleted = await batch_delete(db, body.ids)
    rows = (await db.execute(select(SensitiveWord.keyword).where(SensitiveWord.id.in_(body.ids)))).scalars().all()
    word_names = ", ".join(rows) if rows else str(body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_BATCH_DELETE,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": word_names},
        operation_content_code="LOG_SENSITIVE_WORD_BATCH_DELETE", operation_content_params={"names": word_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="sensitive_word",
    )
    await db.commit()
    return {"success": True, "deleted": deleted}


@router.delete("/{word_id}")
async def delete_sensitive_word_api(
    word_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    word = await get_sensitive_word(db, word_id)
    keyword = word.keyword
    old_data = orm_to_dict(word)
    await delete_sensitive_word(db, word_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "sensitive_word", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_DELETE,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": keyword},
        operation_content_code="LOG_SENSITIVE_WORD_DELETE", operation_content_params={"keyword": keyword},
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="sensitive_word",
        entity_id=word_id,
    )
    await db.commit()
    return {"success": True}


@router.post("/export")
async def export_sensitive_words_api(
    body: BatchDeleteRequest,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    data = await export_excel(db, body.ids)
    rows = (await db.execute(select(SensitiveWord.keyword).where(SensitiveWord.id.in_(body.ids)))).scalars().all()
    word_names = ", ".join(rows) if rows else str(body.ids)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_BATCH_EXPORT,
        operation_object_code="OBJ_SENSITIVE_WORD", operation_object_params={"name": word_names},
        operation_content_code="LOG_SENSITIVE_WORD_BATCH_EXPORT", operation_content_params={"names": word_names},
        ip_address=_get_ip(request),
        result="success",
        entity_type="sensitive_word",
    )
    await db.commit()
    return StreamingResponse(
        iter([data]),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": "attachment; filename=sensitive_words.xlsx"},
    )


@router.post("/import", response_model=ImportResult)
async def import_sensitive_words_api(
    file: UploadFile,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    result = await import_excel(db, file)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.SENSITIVE_WORD_IMPORT,
        operation_object_code="OBJ_SENSITIVE_WORD_IMPORT",
        operation_content_code="LOG_SENSITIVE_WORD_IMPORT",
        operation_content_params={"total": result.total, "created": result.created, "updated": result.updated},
        ip_address=_get_ip(request),
        result="success",
        entity_type="sensitive_word",
    )
    await db.commit()
    return result


@router.post("/check", response_model=SensitiveCheckResponse)
async def check_sensitive_words_api(
    body: SensitiveCheckRequest,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    """
    敏感词预校验：前端在提交表单前调用此接口，检查数据中是否包含敏感词。
    返回命中的字段路径列表，前端据此提示用户修改后再提交。
    """
    check_service = SensitiveCheckService.get_instance()
    matched_fields: list[str] = []

    async def _check_recursive(value, path: str) -> None:
        if isinstance(value, str) and value.strip():
            if await check_service.check_text(db, value):
                matched_fields.append(path)
        elif isinstance(value, dict):
            for k, v in value.items():
                await _check_recursive(v, f"{path}.{k}" if path else k)
        elif isinstance(value, list):
            for i, item in enumerate(value):
                await _check_recursive(item, f"{path}[{i}]")

    await _check_recursive(body.data, "")

    return SensitiveCheckResponse(
        has_sensitive=len(matched_fields) > 0,
        matched_fields=matched_fields,
    )
