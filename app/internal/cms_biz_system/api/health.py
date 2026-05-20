from fastapi import APIRouter
from sqlalchemy import text
from app.database import AsyncSessionLocal
from app.config import settings
from app.common.services.master_platform_service import MasterPlatformService

router = APIRouter()


@router.get("/health")
async def health_check():
    try:
        async with AsyncSessionLocal() as session:
            await session.execute(text("SELECT 1"))
        return {"status": "ok", "database": "connected", "version": settings.app_version}
    except Exception:
        from fastapi.responses import JSONResponse
        return JSONResponse(
            status_code=503,
            content={"status": "ok", "database": "error", "version": settings.app_version},
        )


@router.get("/health/master-platform")
async def master_platform_status():
    master_service = MasterPlatformService.get_instance()
    return master_service.get_status_info()
