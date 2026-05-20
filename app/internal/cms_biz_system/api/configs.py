from fastapi import APIRouter, Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_current_user, _get_ip
from app.common.dependencies import get_db
from app.internal.cms_biz_system.models.user import User
from app.internal.cms_biz_system.schemas.user_crud import (
    ConfigCreate,
    ConfigListItem,
    ConfigUpdate,
    LanguageConfigResponse,
)
from app.common.schemas import PaginatedResponse
from app.internal.cms_biz_system.services.config_service import (
    create_config,
    delete_config,
    get_config,
    get_config_int,
    get_ui_language,
    list_configs,
    update_config,
)
from app.internal.cms_biz_system.services.operation_log_service import OperationType, write_log
from app.common.utils.log_enricher import prepare_log_values, orm_to_dict

router = APIRouter(prefix="/configs")


@router.get("/ui-language", response_model=LanguageConfigResponse)
async def get_ui_language_config(
    db: AsyncSession = Depends(get_db),
):
    return await get_ui_language(db)


@router.get("/public/default-page-size")
async def get_default_page_size(db: AsyncSession = Depends(get_db)):
    """获取默认分页大小（公开接口，无需认证）"""
    page_size = await get_config_int(db, "DEFAULT_PAGE_SIZE", 10)
    return {"value": page_size}


@router.get("/public/password-min-length")
async def get_password_min_length(db: AsyncSession = Depends(get_db)):
    """获取密码最小长度（公开接口，无需认证）"""
    min_length = await get_config_int(db, "PASSWORD_MIN_LENGTH", 8)
    return {"value": min_length}


@router.get("/public/{key}")
async def get_public_config(
    key: str,
    db: AsyncSession = Depends(get_db),
):
    """获取公开配置项的值（无需认证）。"""
    from app.internal.cms_biz_system.services.config_service import get_config_value
    value = await get_config_value(db, key)
    return {"key": key, "value": value}


@router.get("/", response_model=PaginatedResponse[ConfigListItem])
async def get_configs(
    page: int = 1,
    page_size: int = 10,
    config_key: str | None = None,
    config_name: str | None = None,
    description: str | None = None,
    sort_by: str | None = None,
    sort_order: str | None = None,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    return await list_configs(db, page, page_size, config_key, config_name, description, sort_by, sort_order)


@router.get("/{config_id}", response_model=ConfigListItem)
async def get_config_detail(
    config_id: int,
    db: AsyncSession = Depends(get_db),
    _: User = Depends(get_current_user),
):
    config = await get_config(db, config_id)
    return ConfigListItem.model_validate(config)


@router.post("/", response_model=ConfigListItem)
async def create_config_api(
    body: ConfigCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    config = await create_config(db, body)
    new_data = orm_to_dict(config)
    prev_val, new_val, raw_val = await prepare_log_values(db, "config", None, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONFIG_CREATE,
        operation_object=f"参数 {body.config_key}",
        operation_content=f"Created config: name={body.config_name}, key={body.config_key}, value={body.config_value}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="config",
        entity_id=config.id,
    )
    await db.commit()
    return ConfigListItem.model_validate(config)


@router.put("/{config_id}", response_model=ConfigListItem)
async def update_config_api(
    config_id: int,
    body: ConfigUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    old_config = await get_config(db, config_id)
    old_data = orm_to_dict(old_config)
    config = await update_config(db, config_id, body)
    new_data = orm_to_dict(config)
    prev_val, new_val, raw_val = await prepare_log_values(db, "config", old_data, new_data)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONFIG_EDIT,
        operation_object=f"参数 {config.config_key}",
        operation_content=(
            f"Updated config: key={config.config_key},"
            f" old value={old_config.config_value}, new value={body.config_value}"
        ),
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="config",
        entity_id=config_id,
    )
    await db.commit()
    return ConfigListItem.model_validate(config)


@router.delete("/{config_id}")
async def delete_config_api(
    config_id: int,
    request: Request,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_current_user),
):
    config = await get_config(db, config_id)
    config_key = config.config_key
    old_data = orm_to_dict(config)
    await delete_config(db, config_id)
    prev_val, new_val, raw_val = await prepare_log_values(db, "config", old_data, None)
    await write_log(
        db,
        user_id=current_user.id,
        user_name=current_user.username,
        operation_type=OperationType.CONFIG_DELETE,
        operation_object=f"参数 {config_key}",
        operation_content=f"Deleted config: ID={config_id}, key={config_key}",
        ip_address=_get_ip(request),
        result="success",
        previous_value=prev_val,
        updated_value=new_val,
        updated_value_json=raw_val,
        entity_type="config",
        entity_id=config_id,
    )
    await db.commit()
    return {"success": True}
