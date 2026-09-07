"""SOAP 结果通知接收接口

LSP 执行完指令后回调此接口，CMS 根据 CorrelateID 关联到对应的
PublishTask 和 IngestHistory，更新业务状态。

提供两个接收接口：
  - POST /soap/result-notify      接收 JSON 格式的结果通知
  - POST /soap/result-notify-xml  接收 SOAP XML 格式的结果通知
"""
import httpx
from datetime import datetime, timezone
from pathlib import Path

# defusedxml：解析不可信 XML（LSP 回调报文）时禁用实体/DTD，防 XML 炸弹（billion laughs）DoS
from defusedxml import ElementTree as ET

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_db
from app.common.core.i18n import get_msg
from app.common.core.exceptions import BusinessException, ErrorCode
from app.internal.cms_biz_publish.repositories import publish_repository

router = APIRouter(prefix="/soap", tags=["SOAP Content Distribution"])


class ResultNotifyReq(BaseModel):
    """结果通知请求模型"""
    CSPID: str = Field(..., description="Upper-layer ID", max_length=32)
    LSPID: str = Field(..., description="Lower-layer ID", max_length=32)
    CorrelateID: str = Field(..., description="Correlation ID", max_length=32)
    CmdResult: int = Field(..., description="Command execution result: 0=success, -1=failure")
    ResultFileURL: Optional[str] = Field(None, description="Result XML file URL")


class ResultNotifyRes(BaseModel):
    """Result notification response model"""
    Result: int = Field(..., description="Message receive result: 0=success, -1=failure")
    ErrorDescription: Optional[str] = Field(None, description="Error description")


@router.post("/result-notify", response_model=ResultNotifyRes)
async def receive_result_notify(
    request: ResultNotifyReq,
    db: AsyncSession = Depends(get_db),
):
    """
    接收 LSP 的命令执行结果通知 (JSON 格式)

    LSP 执行完指令后，以 HTTP POST JSON 方式调用此接口通知执行结果。
    CMS 根据 CorrelateID 关联到 PublishTask 和 IngestHistory，更新状态。
    """
    try:
        logger.info(
            f"收到结果通知(JSON) - CSPID: {request.CSPID}, LSPID: {request.LSPID}, "
            f"CorrelateID: {request.CorrelateID}, CmdResult: {request.CmdResult}"
        )

        # 捕获请求体
        req_detail = request.model_dump_json()

        # 处理后再捕获响应体
        result = await _process_result_notify(
            db=db,
            csp_id=request.CSPID,
            lsp_id=request.LSPID,
            correlate_id=request.CorrelateID,
            cmd_result=request.CmdResult,
            result_file_url=request.ResultFileURL,
            request_detail=req_detail,
        )
        resp_detail = result.model_dump_json()

        # 补充响应体到历史记录
        history = await publish_repository.get_ingest_history_by_correlate_id(
            db, request.CorrelateID
        )
        if history:
            history.soap_response_detail = resp_detail
            await publish_repository.update_ingest_history(db, history)

        # 提交事务，持久化所有状态变更
        await db.commit()

        return result
    except BusinessException as e:
        # 受控业务错误：message 为 i18n 文案，可放入 ErrorDescription 返回 LSP
        logger.error(f"处理结果通知失败 - CorrelateID: {request.CorrelateID}, 错误: {e}")
        await db.rollback()
        return ResultNotifyRes(Result=-1, ErrorDescription=e.message)
    except Exception as e:
        # 非预期异常不对外暴露细节（防异常信息泄露），完整信息仅记录服务端日志
        logger.error(f"处理结果通知失败 - CorrelateID: {request.CorrelateID}, 错误: {e}")
        await db.rollback()
        return ResultNotifyRes(Result=-1, ErrorDescription=get_msg("INTERNAL_ERROR"))


@router.post("/result-notify-xml")
async def receive_result_notify_xml(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    接收 LSP 的命令执行结果通知 (SOAP XML 格式)

    LSP 执行完指令后，以 SOAP XML 方式调用此接口通知执行结果。
    解析 SOAP Envelope 中的 ResultNotifyReq 字段，业务逻辑与 JSON 接口一致。

    期望的 SOAP XML 入参格式：
    <SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/" ...>
      <SOAP-ENV:Body>
        <m:ResultNotifyReq xmlns:m="iptv" ...>
          <CSPID>001</CSPID>
          <LSPID>001</LSPID>
          <CorrelateID>xxx</CorrelateID>
          <CmdResult>0</CmdResult>
          <ResultFileURL>...</ResultFileURL>
        </m:ResultNotifyReq>
      </SOAP-ENV:Body>
    </SOAP-ENV:Envelope>

    出参格式（对标 LSP ExecCmdResponse 风格）：
    <SOAP-ENV:Envelope ...>
      <SOAP-ENV:Body>
        <m:ResultNotifyRes xmlns:m="iptv" ...>
          <Result>0</Result>
          <ErrorDescription></ErrorDescription>
        </m:ResultNotifyRes>
      </SOAP-ENV:Body>
    </SOAP-ENV:Envelope>
    """
    try:
        body = await request.body()
        xml_str = body.decode("utf-8")
        logger.info(f"========== 收到 SOAP XML 结果通知(入参) ==========\n{xml_str}\n====================================================")

        csp_id, lsp_id, correlate_id, cmd_result, result_file_url = _parse_soap_result_notify(xml_str)

        logger.info(
            f"解析 SOAP XML 结果 - CSPID: {csp_id}, LSPID: {lsp_id}, "
            f"CorrelateID: {correlate_id}, CmdResult: {cmd_result}"
        )

        result = await _process_result_notify(
            db=db,
            csp_id=csp_id,
            lsp_id=lsp_id,
            correlate_id=correlate_id,
            cmd_result=cmd_result,
            result_file_url=result_file_url,
            request_detail=xml_str,
        )

        soap_response = _build_soap_response(result.Result, result.ErrorDescription)
        logger.info(f"========== SOAP XML 结果通知响应(出参) ==========\n{soap_response}\n====================================================")
        
        # 补充响应体到历史记录
        history = await publish_repository.get_ingest_history_by_correlate_id(
            db, correlate_id
        )
        if history:
            history.soap_response_detail = soap_response
            await publish_repository.update_ingest_history(db, history)

        # 提交事务，持久化所有状态变更
        await db.commit()

        return Response(
            content=soap_response,
            media_type="text/xml",
        )

    except BusinessException as e:
        # 受控业务错误：message 为 i18n 文案，可放入 ErrorDescription 返回 LSP
        logger.error(f"处理 SOAP XML 结果通知失败: {e}")
        await db.rollback()
        soap_response = _build_soap_response(-1, e.message)
        return Response(
            content=soap_response,
            media_type="text/xml",
        )
    except Exception as e:
        # 非预期异常不对外暴露细节（防异常信息泄露），完整信息仅记录服务端日志
        logger.error(f"处理 SOAP XML 结果通知失败: {e}")
        await db.rollback()
        soap_response = _build_soap_response(-1, get_msg("INTERNAL_ERROR"))
        return Response(
            content=soap_response,
            media_type="text/xml",
        )


class ExecCmdRes(BaseModel):
    """Exec command response model"""
    Result: int = Field(..., description="Message receive result: 0=success, -1=failure")
    ErrorDescription: Optional[str] = Field(None, description="Error description")


@router.post("/exec-cmd-res", response_model=ExecCmdRes)
async def receive_exec_cmd_response(
    result: int,
    error_description: Optional[str] = None
):
    """
    接收 LSP 的 ExecCmdRes 响应（备用接口）

    某些 LSP 实现可能使用此接口返回 ExecCmdReq 的响应。
    仅记录日志，不关联业务状态（业务状态由 ResultNotify 更新）。
    """
    try:
        logger.info(
            f"收到 ExecCmdRes - Result: {result}, "
            f"ErrorDescription: {error_description}"
        )

        return ExecCmdRes(Result=0, ErrorDescription=None)

    except Exception as e:
        # 非预期异常不对外暴露细节（防异常信息泄露），完整信息仅记录服务端日志
        logger.error("ExecCmdRes processing failed: {}", e)
        return ExecCmdRes(Result=-1, ErrorDescription=get_msg("INTERNAL_ERROR"))


# ═══════════════════════════════════════════════════════════
# 公共业务逻辑
# ═══════════════════════════════════════════════════════════

async def _process_result_notify(
    db: AsyncSession,
    csp_id: str,
    lsp_id: str,
    correlate_id: str,
    cmd_result: int,
    result_file_url: Optional[str] = None,
    request_detail: Optional[str] = None,
    response_detail: Optional[str] = None,
) -> ResultNotifyRes:
    """
    处理结果通知的公共逻辑（JSON 和 SOAP XML 接口共用）

    流程：先根据 CmdResult 立即更新数据库状态，再尝试下载 Result XML（best effort）。
    下载失败不影响数据库状态，但错误信息会放入响应的 ErrorDescription 返回给 LSP。

    Args:
        db: 数据库会话
        csp_id: 上层 ID
        lsp_id: 下层 ID
        correlate_id: 关联 ID
        cmd_result: 命令执行结果 (0=成功, -1=失败)
        result_file_url: 结果 XML 文件 URL（可选）

    Returns:
        ResultNotifyRes（ErrorDescription 可能包含下载失败信息）
    """

    # ── 1. 根据 CorrelateID 查找关联的发布任务 ──
    task = await publish_repository.get_publish_task_by_correlate_id(
        db, correlate_id
    )
    if not task:
        logger.warning(
            f"未找到 CorrelateID 对应的发布任务 - CorrelateID: {correlate_id}"
        )
        return ResultNotifyRes(Result=0, ErrorDescription=None)

    # ── 2. 先更新数据库状态（不等下载，优先保证业务状态一致）──
    history = await publish_repository.get_ingest_history_by_correlate_id(
        db, correlate_id
    )
    if history:
        history.end_date = datetime.now(timezone.utc)
        history.status = "success" if cmd_result == 0 else "failure"
        # ── 记录 LSP 回调日志到 IngestHistory ──
        history.soap_csp_id = csp_id
        history.soap_lsp_id = lsp_id
        history.soap_notify_cmd_result = cmd_result
        if request_detail:
            history.soap_request_detail = request_detail
        if response_detail:
            history.soap_response_detail = response_detail
        logger.info(
            f"更新 IngestHistory - ID: {history.id}, "
            f"Status: {history.status}"
        )

    # ── 3. 更新 PublishTask 状态 ──
    if cmd_result == 0:
        task.status = "success"
        if task.task_type == "publish":
            task.publish_status = "success"
            task.publish_time = datetime.now(timezone.utc)
        else:
            task.publish_status = "closed"
            task.unpublish_time = datetime.now(timezone.utc)
        task.error_message = None
        logger.info(
            f"发布任务成功 - TaskID: {task.id}, "
            f"Type: {task.task_type}"
        )
    else:
        task.status = "failure"
        task.publish_status = "failure"
        task.error_message = f"LSP 执行失败, CmdResult: {cmd_result}"
        logger.warning(
            f"发布任务失败 - TaskID: {task.id}, "
            f"CmdResult: {cmd_result}"
        )

    await publish_repository.update_publish_task(db, task)

    # 发布执行成功后补写 PublishPlan 流程记录（真实模式回调）：
    # 创建计划（尤其是未来时间的计划）时流程记录为 Pending（计划只是未来安排，不算已处理），
    # 只有任务真正执行成功才补记 Passed（与 check_publish_plan 检查器口径一致）。
    # Processed By 显示操作账号：回溯任务创建者（立即发布=点击人，定时发布=计划创建人），兜底 system
    if cmd_result == 0 and task.entity_type == "Content" and task.task_type == "publish" and task.content_type:
        from app.internal.cms_biz_orchestration.services.workflow_service import (
            complete_process_and_update_status,
        )
        from app.internal.cms_biz_publish.services.publish_service import (
            _resolve_task_operator,
        )
        operator = await _resolve_task_operator(db, task, None)
        await complete_process_and_update_status(
            db,
            content_id=task.entity_id,
            content_type=task.content_type,
            process_name="PublishPlan",
            processed_by=operator,
            info=f"发布任务执行成功: 任务#{task.id}",
            skip_status_update=True,
        )

    # 定时计划（execution_mode='plan'）在真实模式下由 SOAP 回调驱动执行，
    # 不经过 API 层，需补写操作日志；立即发布（'now'）已在 API 层写日志，勿重复
    if cmd_result == 0 and task.entity_type == "Content" and task.execution_mode == "plan":
        from app.internal.cms_biz_publish.services.publish_service import (
            write_plan_execute_log,
        )
        await write_plan_execute_log(db, task)

    # ── 4. 同步 content.status 和 object_publish_status（仅 Content 类型）──
    if task.entity_type == "Content" and task.entity_id:
        from app.internal.cms_biz_publish.services.publish_service import (
            _sync_content_ingest_status,
        )
        from app.soap.c2.loader import load_build_context
        from app.internal.cms_biz_publish.services.object_publish_status_service import (
            batch_mark_objects_from_context,
        )

        ingest_status = None
        if cmd_result == 0:
            if task.task_type == "publish":
                ingest_status = "Published"
                
                try:
                    ctx = await load_build_context(db, task.entity_id)
                    if ctx:
                        await batch_mark_objects_from_context(
                            db, task.entity_id, ctx, "REGIST",
                            ingest_history_id=history.id if history else None,
                        )
                        logger.info(
                            f"发布成功，已更新所有对象发布状态 | content_id={task.entity_id}"
                        )
                    else:
                        logger.warning(
                            f"发布成功但BuildContext加载失败，无法更新对象发布状态 | content_id={task.entity_id}"
                        )
                except Exception as e:
                    logger.error(f"更新发布状态失败 | content_id={task.entity_id} error={e}")
                    
                # 发布成功，将 arrangement 任务标记为已完成
                try:
                    from app.internal.cms_biz_package.models.task import Task, TaskHistory
                    
                    arrangement_task = (
                        await db.execute(
                            select(Task).where(
                                Task.content_id == task.entity_id,
                                Task.task_type == "arrangement",
                                Task.is_deleted.is_(False),
                            )
                        )
                    ).scalar_one_or_none()
                    
                    if arrangement_task and arrangement_task.task_status != "Completed":
                        old_status = arrangement_task.task_status
                        arrangement_task.task_status = "Completed"
                        arrangement_task.end_time = datetime.now(timezone.utc)
                        db.add(
                            TaskHistory(
                                task_id=arrangement_task.id,
                                processed_type="Complete",
                                processed_by="LSP",
                                previous_value=old_status,
                                updated_value="任务完成: 内容已发布",
                            )
                        )
                        logger.info(f"发布成功，arrangement任务标记为已完成 | content_id={task.entity_id}")
                except Exception as e:
                    logger.error(f"更新 arrangement 任务状态失败 | content_id={task.entity_id} error={e}")
                    
            elif task.task_type == "unpublish":
                ingest_status = "Closed"
                
                try:
                    from app.internal.cms_biz_publish.repositories.publish_repository import (
                        get_or_create_object_publish_status,
                    )
                    from app.internal.cms_biz_publish.repositories import publish_repository as pr
                    status = await get_or_create_object_publish_status(
                        db, "Content", task.entity_id, task.entity_id
                    )
                    status.mark_as_unpublished()
                    await pr.update_object_publish_status(db, status)
                    logger.info(
                        f"下架成功，已更新对象下架状态 | content_id={task.entity_id}"
                    )
                except Exception as e:
                    logger.error(f"更新下架状态失败 | content_id={task.entity_id} error={e}")
                
                # 下架成功，arrangement 任务恢复为待处理（PRD 3.7.2.4）
                try:
                    from app.internal.cms_biz_package.services import task_service
                    await task_service.reopen_arrangement_task(
                        db,
                        content_id=task.entity_id,
                        processed_by="LSP",
                        reason="内容已下架",
                    )
                except Exception as e:
                    logger.error(f"恢复 arrangement 任务状态失败 | content_id={task.entity_id} error={e}")
        else:
            if task.task_type == "publish":
                ingest_status = "PublishFailed"

        if ingest_status:
            await _sync_content_ingest_status(
                db,
                content_id=task.entity_id,
                new_status=ingest_status,
                processed_by="LSP",
            )

    # ── 4b. 同步 Category.ingest_status ──
    # 记录有实际下发内容的 IngestHistory ID（非 SKIP），后续 result_xml_path 只写这些
    synced_history_ids: set[int] = set()
    if task.entity_type == "Category" and task.entity_id:
        from sqlalchemy import update as sa_update
        from app.internal.cms_biz_metada.models.basic import Category

        if cmd_result == 0:
            cat_ingest_status = "success"
            logger.info(
                f"Category LSP 回调成功 - CorrelateID={correlate_id}"
            )
        else:
            cat_ingest_status = "failure"
            logger.warning(
                f"Category LSP 回调失败 - CmdResult={cmd_result}, CorrelateID={correlate_id}"
            )

        # 更新本次同步涉及的所有 Category（通过 IngestHistoryDetail 收集）
        from sqlalchemy import select
        from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail

        # 获取所有同 correlate_id 的 IngestHistory
        all_histories = await publish_repository.get_ingest_histories_by_correlate_id(
            db, correlate_id
        )
        history_ids = [h.id for h in all_histories]

        # 更新所有 IngestHistory 的状态
        for h in all_histories:
            h.end_date = datetime.now(timezone.utc)
            h.status = "success" if cmd_result == 0 else "failure"
            h.soap_csp_id = csp_id
            h.soap_lsp_id = lsp_id
            h.soap_notify_cmd_result = cmd_result
            if request_detail:
                h.soap_request_detail = request_detail

        # 收集所有受影响的 Category ID（仅非 SKIP，通过 IngestHistoryDetail）
        cat_ids_to_update: set[int] = set()
        if history_ids:
            details = (await db.execute(
                select(IngestHistoryDetail).where(
                    IngestHistoryDetail.history_id.in_(history_ids),
                    IngestHistoryDetail.entity_type == "Category",
                )
            )).scalars().all()
            for d in details:
                cat_ids_to_update.add(d.entity_id)
                synced_history_ids.add(d.history_id)

        if not cat_ids_to_update:
            cat_ids_to_update.add(task.entity_id)

        ingest_stmt = (
            sa_update(Category)
            .where(Category.id.in_(list(cat_ids_to_update)))
            .values(ingest_status=cat_ingest_status, updated_at=Category.updated_at)
        )
        await db.execute(ingest_stmt)
        logger.info(
            f"已更新 Category.ingest_status={cat_ingest_status} | "
            f"category_ids={list(cat_ids_to_update)}"
        )

    # ── 4c. 同步 Cast.ingest_status（内容发布失败时回写 failure）──
    # 说明：
    #   - 发布成功时由 batch_mark_objects_from_context 统一回写 success（含 Cast）
    #   - 这里仅处理发布失败 (cmd_result != 0) 的情况，避免 Cast 一直停留在 processing
    #   - 通过 IngestHistoryDetail 按 correlate_id 收集本次涉及的 Cast ID
    if (
        task.entity_type == "Content"
        and task.entity_id
        and cmd_result != 0
        and task.task_type == "publish"
    ):
        from sqlalchemy import update as sa_update, select as sa_select
        from app.internal.cms_biz_metada.models.basic import Cast
        from app.internal.cms_biz_orchestration.models.ingest_history_detail import IngestHistoryDetail

        all_histories = await publish_repository.get_ingest_histories_by_correlate_id(
            db, correlate_id
        )
        history_ids = [h.id for h in all_histories]

        cast_ids_to_update: set[int] = set()
        if history_ids:
            cast_details = (await db.execute(
                sa_select(IngestHistoryDetail).where(
                    IngestHistoryDetail.history_id.in_(history_ids),
                    IngestHistoryDetail.entity_type == "Cast",
                )
            )).scalars().all()
            for d in cast_details:
                cast_ids_to_update.add(d.entity_id)

        if cast_ids_to_update:
            cast_stmt = (
                sa_update(Cast)
                .where(Cast.id.in_(list(cast_ids_to_update)))
                .values(ingest_status="failure", updated_at=Cast.updated_at)
            )
            await db.execute(cast_stmt)
            logger.info(
                f"已更新 Cast.ingest_status=failure | "
                f"cast_ids={list(cast_ids_to_update)}"
            )

    # ── 5. 下载 Result XML（best effort，不影响已完成的数据库更新）──
    result_xml_path = None
    download_error = None
    if result_file_url:
        try:
            result_xml_path = await _download_result_xml(
                result_file_url, correlate_id
            )
        except BusinessException as e:
            # 受控业务错误：message 为 i18n 文案，可安全放入 ErrorDescription 返回 LSP
            download_error = e.message
            logger.error(
                f"Result XML 下载失败 - URL: {result_file_url}, "
                f"Error: {e}"
            )
        except Exception as e:
            # 非预期异常不对外暴露细节（防异常信息泄露），完整信息仅记录服务端日志
            download_error = get_msg("INTERNAL_ERROR")
            logger.error(
                f"Result XML 下载失败 - URL: {result_file_url}, "
                f"Error: {e}"
            )

        # 下载失败原因记录到数据库
        if download_error:
            if history:
                history.soap_error_description = download_error
            task.error_message = (task.error_message or "") + f"; 下载失败: {download_error}"

        if result_xml_path:
            # 下载成功，补充路径到已有记录
            if history:
                history.result_xml_path = result_xml_path
            # Category 同步：只给有实际下发内容（非 SKIP）的 IngestHistory 写入 result_xml_path
            if synced_history_ids:
                cat_histories = await publish_repository.get_ingest_histories_by_correlate_id(
                    db, correlate_id
                )
                for h in cat_histories:
                    if h.id in synced_history_ids:
                        h.result_xml_path = result_xml_path
            task.result_xml_path = result_xml_path
            logger.info(f"Result XML 已保存: {result_xml_path}")

    # ── 6. 构建响应（下载失败返回 Result=-1 并将错误信息放入 ErrorDescription）──
    return ResultNotifyRes(
        Result=-1 if download_error else 0,
        ErrorDescription=download_error if download_error else None,
    )


# ═══════════════════════════════════════════════════════════
# SOAP XML 解析与构建
# ═══════════════════════════════════════════════════════════

def _find_by_local_name(element, local_name: str):
    """按本地名称查找节点，忽略 XML 命名空间前缀"""
    for el in element.iter():
        tag = el.tag.split("}")[-1] if "}" in el.tag else el.tag
        if tag == local_name:
            return el
    return None


def _parse_soap_result_notify(xml_str: str) -> tuple:
    """
    解析 SOAP XML 格式的 ResultNotify（兼容 LSP 命名，支持命名空间前缀）

    Returns:
        (csp_id, lsp_id, correlate_id, cmd_result, result_file_url)
    """
    root = ET.fromstring(xml_str)

    # 按本地名称查找 ResultNotify / ResultNotifyReq 节点（忽略命名空间前缀）
    req_element = _find_by_local_name(root, "ResultNotify") or _find_by_local_name(root, "ResultNotifyReq")
    if req_element is None:
        raise BusinessException(ErrorCode.SOAP_PARSE_ROOT_NOT_FOUND, get_msg("SOAP_PARSE_ROOT_NOT_FOUND", tag=root.tag))

    def _get_text(element, tag: str) -> str:
        child = _find_by_local_name(element, tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    csp_id = _get_text(req_element, "CSPID")
    lsp_id = _get_text(req_element, "LSPID")
    correlate_id = _get_text(req_element, "CorrelateID")
    cmd_result_str = _get_text(req_element, "CmdResult")
    result_file_url = _get_text(req_element, "ResultFileURL") or None

    if not correlate_id:
        raise BusinessException(ErrorCode.SOAP_CORRELATE_ID_REQUIRED, get_msg("SOAP_CORRELATE_ID_REQUIRED"))

    try:
        cmd_result = int(cmd_result_str)
    except (ValueError, TypeError):
        cmd_result = -1

    return csp_id, lsp_id, correlate_id, cmd_result, result_file_url


def _build_soap_response(result: int, error_description: Optional[str] = None) -> str:
    """
    构建 SOAP XML 格式的 ResultNotifyRes 响应（对标 LSP SOAP 格式）

    Returns:
        SOAP XML 字符串
    """
    error_desc = error_description or ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<SOAP-ENV:Envelope xmlns:SOAP-ENV="http://schemas.xmlsoap.org/soap/envelope/"
    xmlns:SOAP-ENC="http://schemas.xmlsoap.org/soap/encoding"
    xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"
    xmlns:xsd="http://www.w3.org/2001/XMLSchema"
    xmlns:ns1="iptv">
<SOAP-ENV:Body SOAP-ENV:encodingStyle="http://schemas.xmlsoap.org/soap/encoding/">
<ns1:ResultNotifyRes>
    <Result>{result}</Result>
    <ErrorDescription>{error_desc}</ErrorDescription>
</ns1:ResultNotifyRes>
</SOAP-ENV:Body>
</SOAP-ENV:Envelope>"""


# ═══════════════════════════════════════════════════════════
# 内部辅助方法
# ═══════════════════════════════════════════════════════════

def _validate_result_url_host(parsed) -> None:
    """
    SSRF 防护：校验结果文件 URL 的目标地址，禁止指向私网/保留网段。

    - 解析 hostname 的全部 IP（防 DNS Rebinding 到内网）
    - 拒绝环回/私网/链路本地（含云元数据 169.254.169.254）/保留/多播地址
    - 白名单（SOAP_RESULT_URL_PRIVATE_ALLOWLIST，逗号分隔 hostname 或 CIDR）优先于拦截

    Raises:
        Exception: 目标地址不允许访问时抛出（由调用方捕获后放入 ErrorDescription）
    """
    import ipaddress
    import socket

    from .config import soap_settings

    hostname = parsed.hostname
    if not hostname:
        raise BusinessException(ErrorCode.SOAP_RESULT_URL_FORBIDDEN, get_msg("SOAP_RESULT_URL_FORBIDDEN", url=parsed.geturl()))

    # 白名单：hostname 精确匹配（大小写不敏感）
    allowlist = [item.strip() for item in soap_settings.result_url_private_allowlist.split(",") if item.strip()]
    if hostname.lower() in (item.lower() for item in allowlist):
        return

    # 解析 hostname 的全部 IP
    try:
        addr_infos = socket.getaddrinfo(hostname, None)
        ips = [ipaddress.ip_address(info[4][0]) for info in addr_infos]
    except (socket.gaierror, ValueError) as e:
        raise BusinessException(ErrorCode.SOAP_RESULT_URL_FORBIDDEN, get_msg("SOAP_RESULT_URL_FORBIDDEN", url=parsed.geturl())) from e

    # 白名单：CIDR 网段匹配
    allow_networks = []
    for item in allowlist:
        try:
            allow_networks.append(ipaddress.ip_network(item, strict=False))
        except ValueError:
            continue  # 非法 CIDR 忽略（hostname 白名单已在上面处理）

    for ip in ips:
        if any(ip in network for network in allow_networks):
            continue
        # 多播地址（224.0.0.0/4）在 Python 语义中 is_global=True，需显式拦截
        if not ip.is_global or ip.is_multicast:
            raise BusinessException(ErrorCode.SOAP_RESULT_URL_FORBIDDEN, get_msg("SOAP_RESULT_URL_FORBIDDEN", url=parsed.geturl()))


async def _download_result_xml(result_file_url: str, correlate_id: str) -> Optional[str]:
    """
    从 LSP 下载 Result XML 文件，上传到 SFTP 的 soap 目录。

    注意：SFTP/FTP 下载使用 asyncio.to_thread 放到线程池执行，避免同步 I/O 阻塞事件循环。

    Returns:
        上传后的相对路径；不支持的协议返回 None

    Raises:
        BusinessException: 下载或上传失败时抛出（message 为 i18n 受控文案，
            由调用方捕获后放入 ErrorDescription 返回 LSP）
    """
    import asyncio

    from .config import soap_settings
    from urllib.parse import urlparse
    from app.internal.cms_biz_orchestration.services.storage import storage_service

    parsed = urlparse(result_file_url)
    content: bytes

    # SSRF 防护：sftp/ftp/http/https 四个分支统一校验目标地址（DNS 解析放线程池，避免阻塞事件循环）
    if parsed.scheme in ("http", "https", "sftp", "ftp"):
        await asyncio.to_thread(_validate_result_url_host, parsed)

    if parsed.scheme == "sftp":
        host = parsed.hostname or ""
        port = parsed.port or 22
        username = parsed.username or ""
        password = parsed.password or ""
        remote_path = parsed.path or ""

        logger.info(
            f"通过 SFTP 下载 Result XML - Host: {host}:{port}, "
            f"Path: {remote_path}, CorrelateID: {correlate_id}"
        )

        try:
            content = await asyncio.to_thread(
                _sftp_download,
                host, port, username, password, remote_path,
                soap_settings.timeout,
            )
        except Exception as e:
            # 完整异常仅记录服务端日志，不随对外消息返回（防异常信息泄露）
            logger.error(f"SFTP 下载 Result XML 失败 - CorrelateID: {correlate_id}, Error: {e}")
            raise BusinessException(ErrorCode.SOAP_SFTP_DOWNLOAD_FAILED, get_msg("SOAP_SFTP_DOWNLOAD_FAILED")) from e

    elif parsed.scheme == "ftp":
        host = parsed.hostname or ""
        port = parsed.port or 21
        username = parsed.username or "anonymous"
        password = parsed.password or ""
        remote_path = parsed.path or ""

        logger.info(
            f"通过 FTP 下载 Result XML - Host: {host}:{port}, "
            f"Path: {remote_path}, CorrelateID: {correlate_id}"
        )

        try:
            content = await asyncio.to_thread(
                _ftp_download,
                host, port, username, password, remote_path,
                soap_settings.timeout,
            )
        except Exception as e:
            # 完整异常仅记录服务端日志，不随对外消息返回（防异常信息泄露）
            logger.error(f"FTP 下载 Result XML 失败 - CorrelateID: {correlate_id}, Error: {e}")
            raise BusinessException(ErrorCode.SOAP_FTP_DOWNLOAD_FAILED, get_msg("SOAP_FTP_DOWNLOAD_FAILED")) from e

    elif parsed.scheme in ("http", "https"):
        import httpx
        try:
            async with httpx.AsyncClient(timeout=soap_settings.timeout) as client:
                response = await client.get(result_file_url)
                response.raise_for_status()
                content = response.content
        except Exception as e:
            # 完整异常仅记录服务端日志，不随对外消息返回（防异常信息泄露）
            logger.error(f"HTTP 下载 Result XML 失败 - CorrelateID: {correlate_id}, Error: {e}")
            raise BusinessException(ErrorCode.SOAP_HTTP_DOWNLOAD_FAILED, get_msg("SOAP_HTTP_DOWNLOAD_FAILED")) from e
    else:
        logger.warning(
            get_msg("SOAP_UNSUPPORTED_PROTOCOL", scheme=parsed.scheme),
        )
        return None

    # 上传到 SFTP（同样放到线程池，避免阻塞事件循环）
    filename = f"result_{correlate_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
    try:
        result = await asyncio.to_thread(
            storage_service.save_file,
            content, filename, "soap",
        )
    except Exception as e:
        # 完整异常仅记录服务端日志，不随对外消息返回（防异常信息泄露）
        logger.error(f"Result XML 上传到 SFTP 失败 - CorrelateID: {correlate_id}, Error: {e}")
        raise BusinessException(ErrorCode.SOAP_RESULT_XML_UPLOAD_FAILED, get_msg("SOAP_RESULT_XML_UPLOAD_FAILED")) from e

    logger.info(f"Result XML 已上传至 SFTP: {result['file_path']}, 大小: {result['file_size']} bytes")
    return result["file_path"]


def _sftp_download(
    host: str, port: int, username: str, password: str,
    remote_path: str, timeout: int,
) -> bytes:
    """同步 SFTP 下载（在线程池中执行）"""
    import paramiko
    transport = paramiko.Transport((host, port))
    transport.sock.settimeout(timeout)
    try:
        transport.connect(username=username, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        try:
            with sftp.file(remote_path, "rb") as f:
                return f.read()
        finally:
            sftp.close()
    finally:
        transport.close()


def _ftp_download(
    host: str, port: int, username: str, password: str,
    remote_path: str, timeout: int,
) -> bytes:
    """同步 FTP 下载（在线程池中执行）"""
    from ftplib import FTP
    from io import BytesIO
    ftp = FTP()
    ftp.connect(host, port, timeout=timeout)
    try:
        ftp.login(username, password)
        buf = BytesIO()
        ftp.retrbinary(f"RETR {remote_path}", buf.write)
        return buf.getvalue()
    finally:
        ftp.quit()
