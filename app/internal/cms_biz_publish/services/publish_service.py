"""
发布管理业务逻辑层。

处理发布/下架任务的创建、修改、取消，以及注入历史的查询。
"""
import uuid
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import NotFoundException, ErrorCode, BusinessException
from app.common.core.i18n import get_msg
from app.internal.cms_biz_publish.models.publish_task import PublishTask
from app.internal.cms_biz_orchestration.models.ingest_history import IngestHistory
from app.internal.cms_biz_publish.repositories import publish_repository
from app.internal.cms_biz_publish.schemas.publish import (
    BatchPublishRequest,
    IngestHistoryItem,
    PublishListItem,
    PublishPlanCreate,
    PublishPlanResponse,
    PublishPlanUpdate,
)
from app.common.schemas import PaginatedResponse
from app.soap.config import soap_settings
from app.internal.cms_biz_orchestration.services.workflow_service import (
    complete_process_and_update_status,
)


# ═══════════════════════════════════════════════════════════
# 发布任务列表
# ═══════════════════════════════════════════════════════════

async def list_publish_tasks(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    content_name: Optional[str] = None,
    content_types: Optional[list[str]] = None,
    ingest_statuses: Optional[list[str]] = None,
    publish_statuses: Optional[list[str]] = None,
    publish_time_from: Optional[str] = None,
    publish_time_to: Optional[str] = None,
    unpublish_time_from: Optional[str] = None,
    unpublish_time_to: Optional[str] = None,
) -> PaginatedResponse[PublishListItem]:
    """查询发布任务列表"""
    tasks, total = await publish_repository.list_publish_tasks(
        db,
        page=page,
        page_size=page_size,
        content_name=content_name,
        content_types=content_types,
        ingest_statuses=ingest_statuses,
        publish_statuses=publish_statuses,
        publish_time_from=publish_time_from,
        publish_time_to=publish_time_to,
        unpublish_time_from=unpublish_time_from,
        unpublish_time_to=unpublish_time_to,
    )

    items = []
    for task in tasks:
        # 获取实体的 Ingest 状态
        ingest_status = None
        if task.entity_type == "Content" and task.entity_id:
            content = await publish_repository.get_content_by_id(db, task.entity_id)
            if content:
                ingest_status = content.status

        items.append(PublishListItem(
            id=task.id,
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            entity_name=task.entity_name,
            content_type=task.content_type,
            ingest_status=ingest_status,
            publish_status=task.publish_status,
            task_type=task.task_type,
            publish_time=task.publish_time,
            unpublish_time=task.unpublish_time,
            scheduled_time=task.scheduled_time,
            execution_mode=task.execution_mode,
        ))

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ═══════════════════════════════════════════════════════════
# 发布/下架计划管理
# ═══════════════════════════════════════════════════════════

def _get_initial_publish_status(task_type: str, execution_mode: str) -> str:
    """根据任务类型和执行方式获取初始发布状态"""
    if execution_mode == "now":
        return "publishing" if task_type == "publish" else "closed"
    else:
        return "plan"


async def create_publish_plan(
    db: AsyncSession,
    data: PublishPlanCreate,
    user_id: Optional[int] = None,
    processed_by: Optional[str] = None,
) -> PublishPlanResponse:
    """创建或更新发布/下架计划"""
    # 检查实体是否存在
    if data.entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, data.entity_id)
        if not content:
            raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))

    # 获取现有任务
    existing_task = await publish_repository.get_entity_current_publish_status(
        db, data.entity_type, data.entity_id
    )

    # 检查状态冲突（已取消的任务允许重新设置；pending 状态允许修改计划时间）
    if existing_task:
        if existing_task.publish_status == "plan" and existing_task.status not in ("pending", "cancelled"):
            raise BusinessException(ErrorCode.PUBLISH_PLAN_ALREADY_EXISTS, get_msg("PUBLISH_PLAN_ALREADY_EXISTS"))
        if existing_task.publish_status == "publishing":
            raise BusinessException(ErrorCode.PUBLISH_TASK_IN_PROGRESS, get_msg("PUBLISH_TASK_IN_PROGRESS"))
        # 只有发布操作才检查是否已发布
        if data.task_type == "publish" and existing_task.publish_status == "success":
            raise BusinessException(ErrorCode.CONTENT_ALREADY_PUBLISHED, get_msg("CONTENT_ALREADY_PUBLISHED"))

    if existing_task:
        # 更新现有任务
        existing_task.task_type = data.task_type
        existing_task.execution_mode = data.execution_mode
        existing_task.scheduled_time = data.scheduled_time
        existing_task.status = "pending" if data.execution_mode == "plan" else "processing"
        existing_task.publish_status = _get_initial_publish_status(data.task_type, data.execution_mode)
        existing_task.updated_by = user_id
        task = await publish_repository.update_publish_task(db, existing_task)
    else:
        # 创建新任务
        task = PublishTask(
            entity_type=data.entity_type,
            entity_id=data.entity_id,
            entity_name=data.entity_name,
            content_type=data.content_type,
            task_type=data.task_type,
            execution_mode=data.execution_mode,
            scheduled_time=data.scheduled_time,
            status="pending" if data.execution_mode == "plan" else "processing",
            publish_status=_get_initial_publish_status(data.task_type, data.execution_mode),
            created_by=user_id,
        )
        task = await publish_repository.create_publish_task(db, task)

    # 如果是立即执行，触发发布流程
    if data.execution_mode == "now":
        await _execute_publish_task(db, task)

    # 级联创建子内容的发布/下架计划
    if data.entity_type == "Content" and data.content_type:
        await _cascade_create_plan_for_children(
            db, 
            content_id=data.entity_id,
            content_type=data.content_type,
            task_type=data.task_type,
            execution_mode=data.execution_mode,
            scheduled_time=data.scheduled_time,
            user_id=user_id,
        )

    # 完成 PublishPlan 流程节点并更新内容状态
    # 仅在发布任务时调用，下架任务的状态已由 _execute_publish_task 正确设置为 Closed
    # 若下架时也调用，complete_process_and_update_status 会根据 workflow 配置
    # 将 Closed 状态覆盖为 Publishing/Published，导致下架后 Ingest 状态异常
    if data.entity_type == "Content" and data.content_type and data.task_type == "publish":
        await complete_process_and_update_status(
            db,
            content_id=data.entity_id,
            content_type=data.content_type,
            process_name="PublishPlan",
            processed_by=processed_by,
            info=f"创建发布计划: {data.task_type}",
        )

    # 注意：_execute_publish_task 内部已经处理了 content.status 的同步
    # 无需在此处重复同步，避免状态被覆盖
    # - 模拟模式：_execute_publish_task 已同步为 Published/Closed
    # - 真实模式：SOAP 回调后会自动同步为 Published/Closed

    return PublishPlanResponse.model_validate(task)


async def get_current_plan(
    db: AsyncSession,
    entity_type: str,
    entity_id: int
) -> Optional[PublishPlanResponse]:
    """获取实体当前的发布计划"""
    task = await publish_repository.get_entity_current_publish_status(
        db, entity_type, entity_id
    )
    if not task or task.publish_status != "plan" or task.status == "cancelled":
        return None
    return PublishPlanResponse.model_validate(task)


async def update_publish_plan(
    db: AsyncSession,
    task_id: int,
    data: PublishPlanUpdate
) -> PublishPlanResponse:
    """修改发布/下架计划"""
    task = await publish_repository.get_publish_task_by_id(db, task_id)
    if not task:
        raise NotFoundException(ErrorCode.PUBLISH_TASK_NOT_FOUND, get_msg("PUBLISH_TASK_NOT_FOUND"))

    # 只能修改待执行或已取消的计划任务
    if task.status not in ("pending", "cancelled"):
        raise BusinessException(ErrorCode.PUBLISH_TASK_CANNOT_MODIFY, get_msg("PUBLISH_TASK_CANNOT_MODIFY"))

    task.execution_mode = data.execution_mode
    task.scheduled_time = data.scheduled_time

    await publish_repository.update_publish_task(db, task)
    return PublishPlanResponse.model_validate(task)


async def cancel_publish_plan(db: AsyncSession, task_id: int) -> bool:
    """取消发布/下架计划"""
    task = await publish_repository.get_publish_task_by_id(db, task_id)
    if not task:
        raise NotFoundException(ErrorCode.PUBLISH_TASK_NOT_FOUND, get_msg("PUBLISH_TASK_NOT_FOUND"))

    if task.status != "pending":
        raise BusinessException(ErrorCode.PUBLISH_TASK_CANNOT_CANCEL, get_msg("PUBLISH_TASK_CANNOT_CANCEL"))

    success = await publish_repository.cancel_publish_task(db, task_id)
    return success


# ═══════════════════════════════════════════════════════════
# 立即发布/下架
# ═══════════════════════════════════════════════════════════

async def _upgrade_plan_to_now(
    db: AsyncSession,
    existing: PublishTask,
    task_type: str,
) -> PublishPlanResponse:
    """将已有 plan 任务"升级"为立即执行：复用同一条记录，避免列表出现重复行。"""
    existing.task_type = task_type
    existing.execution_mode = "now"
    existing.scheduled_time = None
    existing.status = "processing"
    existing.publish_status = "publishing" if task_type == "publish" else "closed"
    await publish_repository.update_publish_task(db, existing)
    await _execute_publish_task(db, existing)

    # 注意：_execute_publish_task 内部已经处理了 content.status 的同步
    # 无需在此处重复同步，避免状态被覆盖

    return PublishPlanResponse.model_validate(existing)


async def publish_now(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    user_id: Optional[int] = None
) -> PublishPlanResponse:
    """立即发布"""
    # 获取实体信息
    entity_name = None
    content_type = None
    if entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, entity_id)
        if not content:
            raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))
        entity_name = content.title
        content_type = content.content_type

        # 级联发布子内容
        await _cascade_publish_children(db, content.id, user_id)

    # 立即发布场景：若已存在 plan 任务且未取消，复用同一条记录升级为立即执行
    existing = await publish_repository.get_entity_current_publish_status(
        db, entity_type, entity_id
    )
    if existing and existing.publish_status == "plan" and existing.status != "cancelled":
        return await _upgrade_plan_to_now(db, existing, task_type="publish")

    data = PublishPlanCreate(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        content_type=content_type,
        task_type="publish",
        execution_mode="now",
    )

    return await create_publish_plan(db, data, user_id)


async def unpublish_now(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    user_id: Optional[int] = None
) -> PublishPlanResponse:
    """立即下架"""
    # 获取实体信息
    entity_name = None
    content_type = None
    if entity_type == "Content":
        content = await publish_repository.get_content_by_id(db, entity_id)
        if not content:
            raise NotFoundException(ErrorCode.CONTENT_NOT_FOUND, get_msg("CONTENT_NOT_FOUND"))
        entity_name = content.title
        content_type = content.content_type

        # 级联下架子内容
        await _cascade_unpublish_children(db, content.id, user_id)

    # 立即下架场景：若已存在 plan 任务且未取消，复用同一条记录升级为立即执行
    existing = await publish_repository.get_entity_current_publish_status(
        db, entity_type, entity_id
    )
    if existing and existing.publish_status == "plan" and existing.status != "cancelled":
        return await _upgrade_plan_to_now(db, existing, task_type="unpublish")

    data = PublishPlanCreate(
        entity_type=entity_type,
        entity_id=entity_id,
        entity_name=entity_name,
        content_type=content_type,
        task_type="unpublish",
        execution_mode="now",
    )

    return await create_publish_plan(db, data, user_id)


# ═══════════════════════════════════════════════════════════
# 批量操作
# ═══════════════════════════════════════════════════════════

async def batch_publish(
    db: AsyncSession,
    data: BatchPublishRequest,
    user_id: Optional[int] = None
) -> list[PublishPlanResponse]:
    """批量发布/下架"""
    results = []

    for entity_id in data.entity_ids:
        entity_name = None
        content_type = None

        if data.entity_type == "Content":
            content = await publish_repository.get_content_by_id(db, entity_id)
            if content:
                entity_name = content.title
                content_type = content.content_type

        plan_data = PublishPlanCreate(
            entity_type=data.entity_type,
            entity_id=entity_id,
            entity_name=entity_name,
            content_type=content_type,
            task_type=data.task_type,
            execution_mode=data.execution_mode,
            scheduled_time=data.scheduled_time,
        )

        try:
            result = await create_publish_plan(db, plan_data, user_id)
            results.append(result)
            logger.info(f"批量{data.task_type} 成功: entity_type={data.entity_type}, entity_id={entity_id}")
        except BusinessException as e:
            # 跳过已存在任务的实体，继续处理其他
            logger.warning(f"批量{data.task_type} 跳过: entity_type={data.entity_type}, entity_id={entity_id}, reason={e.message}")
            continue

    return results


# ═══════════════════════════════════════════════════════════
# 注入历史
# ═══════════════════════════════════════════════════════════

from urllib.parse import quote


def _build_ingest_history_item(history) -> IngestHistoryItem:
    """将 ORM 模型转换为 Pydantic 模型，并添加下载 URL。"""
    # 先获取字典数据
    data = {
        "id": history.id,
        "entity_type": history.entity_type,
        "entity_id": history.entity_id,
        "entity_name": history.entity_name,
        "action": history.action,
        "status": history.status,
        "create_date": history.create_date,
        "send_date": history.send_date,
        "end_date": history.end_date,
        "ingest_xml_path": history.ingest_xml_path,
        "result_xml_path": history.result_xml_path,
    }
    # 添加下载 URL（不含 /api/v1 前缀，由前端 axios baseURL 自动添加）
    if history.ingest_xml_path:
        data["ingest_xml_url"] = f"/attachments/download?path={quote(history.ingest_xml_path, safe='')}"
    if history.result_xml_path:
        data["result_xml_url"] = f"/attachments/download?path={quote(history.result_xml_path, safe='')}"
    return IngestHistoryItem(**data)


async def list_ingest_histories(
    db: AsyncSession,
    page: int = 1,
    page_size: int = 10,
    entity_type: Optional[str] = None,
    entity_id: Optional[int] = None,
    action: Optional[str] = None,
    status: Optional[str] = None,
) -> PaginatedResponse[IngestHistoryItem]:
    """查询注入历史"""
    histories, total = await publish_repository.list_ingest_histories(
        db,
        page=page,
        page_size=page_size,
        entity_type=entity_type,
        entity_id=entity_id,
        action=action,
        status=status,
    )

    items = [_build_ingest_history_item(h) for h in histories]

    return PaginatedResponse(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


# ═══════════════════════════════════════════════════════════
# 内部辅助方法
# ═══════════════════════════════════════════════════════════

async def _execute_publish_task(db: AsyncSession, task: PublishTask) -> None:
    """
    执行发布/下架任务：生成 XML → 发送 SOAP → 创建注入历史 → 等待 LSP 回调

    流程：
        1. 判断 SOAP 是否启用，未启用则模拟成功
        2. 生成 C2 规范 Ingest XML 文件（TODO: 待对接 C2 规范）
        3. 创建 IngestHistory 记录
        4. 调用 SOAP ExecCmdReq 通知接口机
        5. 更新任务状态为 publishing（等待 LSP 回调更新最终状态）
    """
    # ── SOAP 未启用时走模拟逻辑 ──
    if not soap_settings.enabled:
        logger.warning(f"SOAP 未启用，模拟发布成功 - TaskID: {task.id}")
        # 即使 SOAP 未启用，也生成 XML 文件供调试查看
        xml_filepath = None
        try:
            xml_filepath, xml_url = await _generate_ingest_xml(task)
            logger.info(f"[模拟模式] 生成 Ingest XML - TaskID: {task.id}, Path: {xml_filepath}, URL: {xml_url}")
        except Exception as e:
            logger.error(f"[模拟模式] 生成 XML 失败 - TaskID: {task.id}, Error: {e}")

        # 创建 IngestHistory 记录（模拟模式下默认成功）
        correlate_id = str(uuid.uuid4())
        action = "DELETE" if task.task_type == "unpublish" else "REGIST"
        now = datetime.now(timezone.utc)
        logger.info(f"[模拟模式] 创建 IngestHistory - TaskID: {task.id}, xml_filepath={xml_filepath}")
        history = IngestHistory(
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            entity_name=task.entity_name,
            action=action,
            status="success",  # 模拟模式下直接标记为成功
            create_date=now,
            send_date=now,
            end_date=now,  # 模拟模式下立即完成，设置结束时间
            ingest_xml_path=xml_filepath,
            correlate_id=correlate_id,
        )
        await publish_repository.create_ingest_history(db, history)
        logger.info(f"[模拟模式] 创建 IngestHistory - TaskID: {task.id}, HistoryID: {history.id}, ingest_xml_path={history.ingest_xml_path}")

        task.status = "success"
        task.correlate_id = correlate_id
        task.ingest_xml_path = xml_filepath
        if task.task_type == "publish":
            task.publish_status = "success"
            task.publish_time = datetime.now(timezone.utc)
        else:
            task.publish_status = "closed"
            task.unpublish_time = datetime.now(timezone.utc)
        await publish_repository.update_publish_task(db, task)

        # 模拟模式下同步 content.status
        if task.entity_type == "Content":
            if task.task_type == "publish":
                await _sync_content_ingest_status(
                    db, task.entity_id, "Published", processed_by="system"
                )
                
                # 模拟模式下同步更新 object_publish_status
                try:
                    from app.soap.c2.loader import load_build_context
                    from app.internal.cms_biz_publish.services.object_publish_status_service import (
                        batch_mark_objects_from_context,
                        mark_object_as_published,
                    )
                    from app.internal.cms_biz_publish.repositories.publish_repository import (
                        get_object_publish_status,
                    )
                    
                    # 判断是REGIST还是UPDATE
                    status = await get_object_publish_status(db, "Content", task.entity_id)
                    action = "UPDATE" if (status and status.is_published) else "REGIST"
                    
                    # 加载BuildContext，更新所有涉及对象的发布状态
                    ctx = await load_build_context(db, task.entity_id)
                    if ctx:
                        await batch_mark_objects_from_context(
                            db, task.entity_id, ctx, action,
                            ingest_history_id=history.id,
                        )
                        logger.info(
                            f"[模拟模式] 已更新 object_publish_status | content_id={task.entity_id} action={action}"
                        )
                    else:
                        # 降级：至少更新Content本身
                        await mark_object_as_published(
                            db, "Content", task.entity_id, task.entity_id, action
                        )
                        logger.warning(
                            f"[模拟模式] BuildContext加载失败，仅更新Content本身 | content_id={task.entity_id}"
                        )
                except Exception as e:
                    logger.error(f"[模拟模式] 更新 object_publish_status 失败 | content_id={task.entity_id} error={e}")
                    # 重新抛出异常，确保事务回滚
                    raise
                    
            elif task.task_type == "unpublish":
                await _sync_content_ingest_status(
                    db, task.entity_id, "Closed", processed_by="system"
                )
                
                # 模拟模式下同步更新 object_publish_status（下架）
                try:
                    from app.internal.cms_biz_publish.services.object_publish_status_service import (
                        mark_object_as_unpublished,
                    )
                    await mark_object_as_unpublished(db, "Content", task.entity_id)
                    logger.info(
                        f"[模拟模式] 已更新 object_publish_status（下架） | content_id={task.entity_id}"
                    )
                except Exception as e:
                    logger.error(f"[模拟模式] 更新 object_publish_status（下架）失败 | content_id={task.entity_id} error={e}")
                
                # 下架成功，恢复 arrangement 任务为待处理状态
                try:
                    from app.internal.cms_biz_package.models.task import Task
                    from sqlalchemy import select
                    
                    arrangement_task = (
                        await db.execute(
                            select(Task).where(
                                Task.content_id == task.entity_id,
                                Task.task_type == "arrangement",
                                Task.is_deleted.is_(False),
                            )
                        )
                    ).scalar_one_or_none()
                    
                    if arrangement_task:
                        arrangement_task.task_status = "Pending"
                        arrangement_task.end_time = None
                        logger.info(f"[模拟模式] 下架成功，arrangement任务恢复为待处理 | content_id={task.entity_id}")
                except Exception as e:
                    logger.error(f"[模拟模式] 恢复 arrangement 任务状态失败 | content_id={task.entity_id} error={e}")
        return

    try:
        # ── 1. 生成 C2 规范 XML 文件 ──
        # TODO: 待对接 C2 规范后实现，目前使用占位 XML
        xml_filepath, xml_url = await _generate_ingest_xml(task)
        logger.info(f"生成 Ingest XML - TaskID: {task.id}, Path: {xml_filepath}, URL: {xml_url}")

        # ── 2. 生成 CorrelateID ──
        correlate_id = str(uuid.uuid4())

        # ── 3. 创建 IngestHistory 记录 ──
        action = "DELETE" if task.task_type == "unpublish" else "REGIST"
        history = IngestHistory(
            entity_type=task.entity_type,
            entity_id=task.entity_id,
            entity_name=task.entity_name,
            action=action,
            status="failure",  # 初始状态，LSP 回调后更新
            create_date=datetime.now(timezone.utc),
            send_date=datetime.now(timezone.utc),
            ingest_xml_path=xml_filepath,
            correlate_id=correlate_id,
        )
        await publish_repository.create_ingest_history(db, history)
        logger.info(
            f"创建 IngestHistory - TaskID: {task.id}, "
            f"HistoryID: {history.id}, CorrelateID: {correlate_id}"
        )

        # ── 4. 更新 PublishTask：写入 correlate_id 和 XML 路径 ──
        task.correlate_id = correlate_id
        task.ingest_xml_path = xml_filepath
        task.status = "processing"
        task.publish_status = "publishing" if task.task_type == "publish" else "closed"
        await publish_repository.update_publish_task(db, task)

        # 同步 content.status 为 Publishing（计划发布执行时）
        if task.entity_type == "Content" and task.task_type == "publish":
            await _sync_content_ingest_status(
                db, task.entity_id, "Publishing", processed_by="system"
            )

        # ── 5. 调用 SOAP 发送 ExecCmdReq ──
        soap_result = await _send_soap_exec_cmd(
            correlate_id=correlate_id,
            cmd_file_url=xml_url
        )

        # ── 记录 SOAP ExecCmdReq 返回结果到 IngestHistory ──
        history.soap_csp_id = soap_settings.csp_id
        history.soap_lsp_id = soap_settings.lsp_id
        history.soap_cmd_result = soap_result.get("result")
        history.soap_error_description = soap_result.get("error_description")

        if not soap_result["success"]:
            # SOAP 调用本身失败（网络异常等），任务标记为失败
            task.status = "failure"
            task.publish_status = "failure"
            task.error_message = f"SOAP 调用失败: {soap_result['error_description']}"
            task.retry_attempts = (task.retry_attempts or 0) + 1
            history.status = "failure"
            await publish_repository.update_publish_task(db, task)
            logger.error(
                f"SOAP 调用失败 - TaskID: {task.id}, "
                f"Error: {soap_result['error_description']}"
            )
            # 同步 content.status 为 PublishFailed
            if task.entity_type == "Content" and task.task_type == "publish":
                await _sync_content_ingest_status(
                    db, task.entity_id, "PublishFailed", processed_by="system"
                )
            await publish_repository.update_ingest_history(db, history)
            return

        # ── 6. SOAP 调用成功，等待 LSP 异步回调更新状态 ──
        logger.info(
            f"SOAP 调用成功，等待 LSP 回调 - TaskID: {task.id}, "
            f"CorrelateID: {correlate_id}"
        )
        # ExecCmdReq 成功，写入到 ingest_history
        await publish_repository.update_ingest_history(db, history)

    except Exception as e:
        logger.error(f"执行发布任务异常 - TaskID: {task.id}, Error: {e}")
        task.status = "failure"
        task.publish_status = "failure"
        task.error_message = str(e)
        task.retry_attempts = (task.retry_attempts or 0) + 1
        await publish_repository.update_publish_task(db, task)
        # 同步 content.status 为 PublishFailed
        if task.entity_type == "Content" and task.task_type == "publish":
            await _sync_content_ingest_status(
                db, task.entity_id, "PublishFailed", processed_by="system"
            )


async def _generate_ingest_xml(task: PublishTask) -> tuple[str, str]:
    """
    生成 C2 规范的 Ingest XML 文件。

    仅支持 entity_type='Content' 的任务；其他类型暂降级为占位 XML。

    :returns: (xml_filepath, xml_url) 本地文件绝对路径和 LSP 可访问的 URL
    """
    # 仅 Content 类型走 C2 规范完整流程
    if task.entity_type != "Content":
        logger.warning(f"entity_type={task.entity_type} 暂不支持 C2 规范，回退到占位 XML")
        return await _generate_placeholder_xml(task)

    # 用独立会话生成 XML，避免占用外部事务 session
    from app.database import AsyncSessionLocal
    from app.soap.c2 import ADIBuilder

    async with AsyncSessionLocal() as xml_db:
        builder = ADIBuilder(xml_db)
        if task.task_type == "publish":
            xml_str = await builder.build_publish_xml(content_id=task.entity_id)
        else:
            xml_str = await builder.build_unpublish_xml(content_id=task.entity_id)

        # 落盘时 correlate_id 已在上层生成，但此处仅需文件命名唯一即可
        filepath = builder.write_to_file(
            xml_str,
            correlate_id=task.correlate_id or str(uuid.uuid4()),
            entity_id=task.entity_id,
        )

    # 计算 URL（与 file_server 路由 /commands/{filename} 对齐）
    from pathlib import Path
    from app.soap.config import soap_settings

    # 文件存储在 SFTP 的 c2 目录下，提取文件名用于 URL
    filename = Path(filepath).name
    xml_url = f"{soap_settings.xml_base_url.rstrip('/')}/{filename}"
    return filepath, xml_url


async def _generate_placeholder_xml(task: PublishTask) -> tuple[str, str]:
    """非 Content 类型的降级占位 XML。"""
    from app.soap.xml_generator import XMLCommandGenerator

    xml_gen = XMLCommandGenerator()
    if task.task_type == "publish":
        xml_filepath = xml_gen.generate_content_publish_xml(
            content_id=str(task.entity_id),
            content_type=task.content_type or "unknown",
            title=task.entity_name or "",
            file_url="",
        )
    else:
        xml_filepath = xml_gen.generate_content_unpublish_xml(
            content_id=str(task.entity_id),
            content_type=task.content_type or "unknown",
            reason="unpublish",
        )
    xml_url = xml_gen.get_xml_url(xml_filepath)
    return xml_filepath, xml_url


async def _send_soap_exec_cmd(
    correlate_id: str,
    cmd_file_url: str
) -> dict:
    """
    调用 SOAP ExecCmdReq 发送指令

    Returns:
        {"success": bool, "correlate_id": str, "result": int, "error_description": str}
    """
    try:
        from app.soap.client import SOAPClient

        soap_client = SOAPClient()
        return soap_client.send_exec_cmd_req(
            cmd_file_url=cmd_file_url,
            correlate_id=correlate_id
        )
    except Exception as e:
        logger.error(f"SOAP 客户端初始化/调用失败: {e}")
        return {
            "success": False,
            "correlate_id": correlate_id,
            "result": -1,
            "error_description": str(e)
        }


async def _cascade_publish_children(
    db: AsyncSession,
    parent_id: int,
    user_id: Optional[int] = None
) -> None:
    """
    级联发布子内容（立即发布场景）。
    
    层级关系：
        SEASON (总季) → SERIES (单季) → EPISODE (单集)
    """
    children = await publish_repository.get_child_contents(db, parent_id)

    for child in children:
        # 递归发布子内容
        await _cascade_publish_children(db, child.id, user_id)

        # 创建子内容的发布任务
        data = PublishPlanCreate(
            entity_type="Content",
            entity_id=child.id,
            entity_name=child.title,
            content_type=child.content_type,
            task_type="publish",
            execution_mode="now",
        )
        try:
            await create_publish_plan(db, data, user_id)
        except BusinessException:
            # 忽略已存在的任务
            pass


async def _cascade_unpublish_children(
    db: AsyncSession,
    parent_id: int,
    user_id: Optional[int] = None
) -> None:
    """
    级联下架子内容（立即下架场景）。
    
    层级关系：
        SEASON (总季) → SERIES (单季) → EPISODE (单集)
    """
    children = await publish_repository.get_child_contents(db, parent_id)

    for child in children:
        # 递归下架子内容
        await _cascade_unpublish_children(db, child.id, user_id)

        # 创建子内容的下架任务
        data = PublishPlanCreate(
            entity_type="Content",
            entity_id=child.id,
            entity_name=child.title,
            content_type=child.content_type,
            task_type="unpublish",
            execution_mode="now",
        )
        try:
            await create_publish_plan(db, data, user_id)
        except BusinessException:
            # 忽略已存在的任务
            pass


async def _cascade_create_plan_for_children(
    db: AsyncSession,
    content_id: int,
    content_type: str,
    task_type: str,
    execution_mode: str,
    scheduled_time: Optional[datetime] = None,
    user_id: Optional[int] = None
) -> None:
    """
    级联创建子内容的发布/下架计划。
    
    层级关系：
        SEASON (总季, series_type=3) → SERIES (单季, series_type=2) → EPISODE (单集)
    
    逻辑：
        1. 查询所有子内容（递归）
        2. 检查子内容是否已在发布管理表中
        3. 如果已存在且未取消 → 跳过（避免重复）
        4. 如果不存在或已取消 → 创建新的发布/下架任务
    """
    # 只有 SEASON 和 SERIES 才有子内容
    if content_type not in ("SEASON", "SERIES"):
        return

    children = await publish_repository.get_child_contents(db, content_id)
    
    if not children:
        return

    logger.info(
        f"[级联计划] 父内容 #{content_id} ({content_type}) | "
        f"task_type={task_type} | execution_mode={execution_mode} | "
        f"子内容数量={len(children)}"
    )

    for child in children:
        # 递归处理子内容
        await _cascade_create_plan_for_children(
            db,
            content_id=child.id,
            content_type=child.content_type,
            task_type=task_type,
            execution_mode=execution_mode,
            scheduled_time=scheduled_time,
            user_id=user_id,
        )

        # 检查子内容是否已有发布任务
        existing_task = await publish_repository.get_entity_current_publish_status(
            db, "Content", child.id
        )

        # 如果已有任务且未取消，跳过
        if existing_task and existing_task.status != "cancelled":
            logger.info(
                f"[级联计划] 子内容 #{child.id} ({child.content_type}) 已有任务，跳过 | "
                f"status={existing_task.status}"
            )
            continue

        # 创建子内容的发布/下架任务
        data = PublishPlanCreate(
            entity_type="Content",
            entity_id=child.id,
            entity_name=child.title,
            content_type=child.content_type,
            task_type=task_type,
            execution_mode=execution_mode,
            scheduled_time=scheduled_time,
        )
        try:
            await create_publish_plan(db, data, user_id)
            logger.info(
                f"[级联计划] 为子内容 #{child.id} ({child.content_type}) 创建{task_type}计划 | "
                f"execution_mode={execution_mode}"
            )
        except BusinessException as e:
            # 忽略已存在的任务
            logger.warning(
                f"[级联计划] 为子内容 #{child.id} 创建计划失败: {e.message}"
            )


async def _sync_content_ingest_status(
    db: AsyncSession,
    content_id: int,
    new_status: str,
    processed_by: Optional[str] = None,
) -> None:
    """
    同步内容的 Ingest 状态（content.status）。

    用于发布/下架流程中保持 publish_task.publish_status 与 content.status 一致。
    """
    from app.internal.cms_biz_orchestration.repositories.content_repository import get_content_by_id
    from app.internal.cms_biz_orchestration.services.workflow_service import record_status_change

    content = await get_content_by_id(db, content_id)
    if not content:
        logger.warning(f"同步 Ingest 状态失败，内容不存在: content_id={content_id}")
        return

    logger.info(f"[同步Ingest状态] content_id={content_id}, 当前状态={content.status}, 目标状态={new_status}")

    if content.status == new_status:
        logger.info(f"[同步Ingest状态] content_id={content_id} 状态已是 {new_status}，跳过更新")
        return

    old_status = content.status
    content.status = new_status
    logger.info(f"[同步Ingest状态] 已设置 content.status = {new_status} (content_id={content_id})")

    if new_status != "Closed" and hasattr(content, 'previous_status') and content.previous_status is not None:
        content.previous_status = None

    await record_status_change(
        db,
        content_id=content_id,
        before_status=old_status,
        after_status=new_status,
        processed_by=processed_by or "system",
    )
    logger.info(
        "同步 Ingest 状态 | content_id={} {} → {}",
        content_id, old_status, new_status,
    )
