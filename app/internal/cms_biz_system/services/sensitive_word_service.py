import io
from datetime import datetime

from fastapi import UploadFile, status
from openpyxl import load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.dict import DictNode
from ..models.sensitive_word import SensitiveWord
from app.common.core import SensitiveWordStatus
from app.common.core.exceptions import ErrorCode, BusinessException, NotFoundException
from app.internal.cms_biz_system.schemas.sensitive_word import (
    BatchStatusRequest,
    ImportResult,
    SensitiveWordCreate,
    SensitiveWordListItem,
    SensitiveWordUpdate,
)
from app.common.schemas import PaginatedResponse

from loguru import logger
from app.common.core.i18n import get_msg
from app.common.services.sensitive_check_service import SensitiveCheckService

async def list_sensitive_words(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    keyword: str | None = None,
    type_codes: list[str] | None = None,
    status: str | None = None,
    created_start: datetime | None = None,
    created_end: datetime | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
) -> PaginatedResponse[SensitiveWordListItem]:
    query = select(SensitiveWord).where(SensitiveWord.is_deleted == False)

    if keyword:
        query = query.where(SensitiveWord.keyword.ilike(f"%{keyword}%"))
    if type_codes:
        type_root = (await db.execute(
            select(DictNode.id).where(DictNode.parent_id.is_(None), DictNode.code == "Sensitive_Word_Type", DictNode.is_deleted == False)
        )).scalar_one_or_none()
        expanded_codes = set(type_codes)
        if type_root is not None:
            dict_names = (await db.execute(
                select(DictNode.code, DictNode.name).where(
                    DictNode.parent_id == type_root,
                    DictNode.code.in_(type_codes),
                    DictNode.status != "deleted",
                    DictNode.is_deleted == False,
                )
            )).all()
            for d_code, d_name in dict_names:
                expanded_codes.add(d_name)
        query = query.where(SensitiveWord.type_code.in_(expanded_codes))
    if status:
        query = query.where(SensitiveWord.status == status)
    if created_start:
        query = query.where(SensitiveWord.created_at >= created_start)
    if created_end:
        query = query.where(SensitiveWord.created_at <= created_end)

    if sort_by and sort_order:
        sort_column = getattr(SensitiveWord, sort_by, None)
        if sort_column is not None:
            order_func = sort_column.asc() if sort_order == "asc" else sort_column.desc()
            query = query.order_by(order_func, SensitiveWord.id.desc())
        else:
            query = query.order_by(SensitiveWord.id.desc())
    else:
        query = query.order_by(SensitiveWord.id.desc())

    count_result = await db.execute(select(func.count()).select_from(query.subquery()))
    total = count_result.scalar_one()

    items = (
        await db.execute(query.offset((page - 1) * page_size).limit(page_size))
    ).scalars().all()

    return PaginatedResponse(
        total=total,
        page=page,
        page_size=page_size,
        items=[SensitiveWordListItem.model_validate(item) for item in items],
    )

async def get_sensitive_word(db: AsyncSession, word_id: int) -> SensitiveWord:
    word = (
        await db.execute(
            select(SensitiveWord).where(
                SensitiveWord.id == word_id,
                SensitiveWord.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    if not word:
        raise NotFoundException(ErrorCode.SENSITIVE_WORD_NOT_FOUND, get_msg("SENSITIVE_WORD_NOT_FOUND"))
    return word

async def create_sensitive_word(
    db: AsyncSession, data: SensitiveWordCreate
) -> SensitiveWord:
    existing = (
        await db.execute(
            select(SensitiveWord).where(
                SensitiveWord.keyword == data.keyword,
                SensitiveWord.type_code == data.type_code,
                SensitiveWord.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    if existing:
        raise BusinessException(ErrorCode.SENSITIVE_WORD_EXISTS, get_msg("SENSITIVE_WORD_EXISTS"))
    word = SensitiveWord(
        keyword=data.keyword,
        type_code=data.type_code,
        status=data.status,
    )
    db.add(word)
    await db.commit()
    await db.refresh(word)
    SensitiveCheckService.get_instance().invalidate_cache()
    return word

async def update_sensitive_word(
    db: AsyncSession, word_id: int, data: SensitiveWordUpdate
) -> SensitiveWord:
    word = await get_sensitive_word(db, word_id)
    new_keyword = data.keyword if data.keyword is not None else word.keyword
    new_type_code = data.type_code if data.type_code is not None else word.type_code

    if new_keyword != word.keyword or new_type_code != word.type_code:
        existing = (
            await db.execute(
                select(SensitiveWord).where(
                    SensitiveWord.keyword == new_keyword,
                    SensitiveWord.type_code == new_type_code,
                    SensitiveWord.id != word_id,
                    SensitiveWord.is_deleted == False,
                )
            )
        ).scalar_one_or_none()
        if existing:
            raise BusinessException(ErrorCode.SENSITIVE_WORD_EXISTS, get_msg("SENSITIVE_WORD_EXISTS"))

    if data.keyword is not None:
        word.keyword = data.keyword
    if data.type_code is not None:
        word.type_code = data.type_code
    if data.status is not None:
        word.status = data.status
    await db.commit()
    await db.refresh(word)
    SensitiveCheckService.get_instance().invalidate_cache()
    return word

async def delete_sensitive_word(db: AsyncSession, word_id: int) -> None:
    word = await get_sensitive_word(db, word_id)
    word.is_deleted = True
    await db.commit()
    SensitiveCheckService.get_instance().invalidate_cache()

async def batch_delete(db: AsyncSession, ids: list[int]) -> int:
    words = (
        await db.execute(
            select(SensitiveWord).where(
                SensitiveWord.id.in_(ids),
                SensitiveWord.is_deleted == False,
            )
        )
    ).scalars().all()
    for word in words:
        word.is_deleted = True
    await db.commit()
    SensitiveCheckService.get_instance().invalidate_cache()
    return len(words)

async def toggle_status(db: AsyncSession, word_id: int, new_status: str) -> SensitiveWord:
    word = await get_sensitive_word(db, word_id)
    word.status = new_status
    await db.commit()
    await db.refresh(word)
    SensitiveCheckService.get_instance().invalidate_cache()
    return word

async def batch_toggle_status(db: AsyncSession, req: BatchStatusRequest) -> int:
    words = (
        await db.execute(
            select(SensitiveWord).where(
                SensitiveWord.id.in_(req.ids),
                SensitiveWord.is_deleted == False,
            )
        )
    ).scalars().all()
    for word in words:
        word.status = req.status
    await db.commit()
    SensitiveCheckService.get_instance().invalidate_cache()
    return len(words)

async def export_excel(db: AsyncSession, ids: list[int]) -> bytes:
    import openpyxl

    words = (
        await db.execute(
            select(SensitiveWord).where(
                SensitiveWord.id.in_(ids),
                SensitiveWord.is_deleted == False,
            )
        )
    ).scalars().all()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "敏感词"

    headers = ["关键词", "类型", "状态", "创建时间", "最后更新时间"]
    header_fill = PatternFill(start_color="1677FF", end_color="1677FF", fill_type="solid")
    header_font = Font(color="FFFFFF", bold=True)

    for col, header in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=header)
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")

    col_widths = [24, 20, 12, 22, 22]
    for col, width in enumerate(col_widths, 1):
        ws.column_dimensions[ws.cell(row=1, column=col).column_letter].width = width

    for row_idx, word in enumerate(words, 2):
        created = word.created_at.strftime("%Y-%m-%d %H:%M:%S") if word.created_at else ""
        updated = word.updated_at.strftime("%Y-%m-%d %H:%M:%S") if word.updated_at else ""
        ws.cell(row=row_idx, column=1, value=word.keyword)
        ws.cell(row=row_idx, column=2, value=word.type_code)
        ws.cell(row=row_idx, column=3, value="已启用" if word.status == "active" else "已禁用")
        ws.cell(row=row_idx, column=4, value=created)
        ws.cell(row=row_idx, column=5, value=updated)

    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    return buf.read()

async def _get_or_create_type_code(db: AsyncSession, type_name: str) -> str:
    root = (
        await db.execute(
            select(DictNode).where(
                DictNode.parent_id.is_(None),
                DictNode.code == "Sensitive_Word_Type",
                DictNode.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    if root is None:
        raise NotFoundException(ErrorCode.DICT_SENSITIVE_WORD_TYPE_NOT_FOUND, get_msg("DICT_SENSITIVE_WORD_TYPE_NOT_FOUND"))

    existing = (
        await db.execute(
            select(DictNode).where(
                DictNode.parent_id == root.id,
                DictNode.name == type_name,
                DictNode.status != "deleted",
                DictNode.is_deleted == False,
            )
        )
    ).scalar_one_or_none()
    if existing:
        return existing.code

    max_sort = (
        await db.execute(
            select(func.coalesce(func.max(DictNode.sort_order), -1)).where(
                DictNode.parent_id == root.id,
                DictNode.is_deleted == False,
            )
        )
    ).scalar_one()
    new_node = DictNode(
        parent_id=root.id,
        code=type_name,
        name=type_name,
        sort_order=max_sort + 1,
        status=SensitiveWordStatus.ACTIVE.value,
        is_system=False,
    )
    db.add(new_node)
    await db.flush()
    return new_node.code

async def import_excel(db: AsyncSession, file: UploadFile) -> ImportResult:
    content = await file.read()
    wb = load_workbook(io.BytesIO(content), read_only=True)
    ws = wb.active

    rows = list(ws.iter_rows(min_row=2, values_only=True))
    wb.close()

    result = ImportResult()
    for row in rows:
        if not row or not row[0]:
            continue
        keyword = str(row[0]).strip()
        type_name = str(row[1]).strip() if len(row) > 1 and row[1] else ""
        status_str = str(row[2]).strip() if len(row) > 2 and row[2] else "active"

        if not keyword or not type_name:
            continue

        result.total += 1
        type_code = await _get_or_create_type_code(db, type_name)

        if status_str in ("已禁用", "inactive", "Inactive"):
            status_val = "inactive"
        else:
            status_val = "active"

        existing = (
            await db.execute(
                select(SensitiveWord).where(
                    SensitiveWord.keyword == keyword,
                    SensitiveWord.type_code == type_code,
                    SensitiveWord.is_deleted == False,
                )
            )
        ).scalar_one_or_none()

        if existing:
            existing.status = status_val
            result.updated += 1
        else:
            db.add(
                SensitiveWord(
                    keyword=keyword,
                    type_code=type_code,
                    status=status_val,
                )
            )
            result.created += 1

    await db.commit()
    SensitiveCheckService.get_instance().invalidate_cache()
    return result
