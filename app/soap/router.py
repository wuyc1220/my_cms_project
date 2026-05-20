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
from xml.etree import ElementTree as ET

from fastapi import APIRouter, Depends, Request, Response
from pydantic import BaseModel, Field
from typing import Optional
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.dependencies import get_db
from app.internal.cms_biz_publish.repositories import publish_repository

router = APIRouter(prefix="/soap", tags=["SOAP内容分发"])


class ResultNotifyReq(BaseModel):
    """结果通知请求模型"""
    CSPID: str = Field(..., description="上层 ID", max_length=32)
    LSPID: str = Field(..., description="下层 ID", max_length=32)
    CorrelateID: str = Field(..., description="关联 ID", max_length=32)
    CmdResult: int = Field(..., description="命令执行结果: 0=成功, -1=失败")
    ResultFileURL: Optional[str] = Field(None, description="查询结果 XML 文件 URL")


class ResultNotifyRes(BaseModel):
    """结果通知响应模型"""
    Result: int = Field(..., description="消息接收结果: 0=成功, -1=失败")
    ErrorDescription: Optional[str] = Field(None, description="错误描述")


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

        return result
    except Exception as e:
        logger.error(f"处理结果通知失败 - CorrelateID: {request.CorrelateID}, 错误: {e}")
        return ResultNotifyRes(Result=-1, ErrorDescription=str(e))


@router.post("/result-notify-xml")
async def receive_result_notify_xml(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """
    接收 LSP 的命令执行结果通知 (SOAP XML 格式)

    LSP 执行完指令后，以 SOAP XML 方式调用此接口通知执行结果。
    解析 SOAP Envelope 中的 ResultNotifyReq 字段，业务逻辑与 JSON 接口一致。

    期望的 SOAP XML 格式：
    <soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
      <soap:Body>
        <ResultNotifyReq>
          <CSPID>SAAT-CMS-001</CSPID>
          <LSPID>ZTE-MW-001</LSPID>
          <CorrelateID>xxx</CorrelateID>
          <CmdResult>0</CmdResult>
          <ResultFileURL>http://...</ResultFileURL>
        </ResultNotifyReq>
      </soap:Body>
    </soap:Envelope>
    """
    try:
        body = await request.body()
        xml_str = body.decode("utf-8")
        logger.info(f"收到结果通知(SOAP XML) - 原始内容长度: {len(xml_str)}")

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
        
        # 补充响应体到历史记录
        history = await publish_repository.get_ingest_history_by_correlate_id(
            db, correlate_id
        )
        if history:
            history.soap_response_detail = soap_response
            await publish_repository.update_ingest_history(db, history)

        return Response(
            content=soap_response,
            media_type="text/xml",
        )

    except Exception as e:
        logger.error(f"处理 SOAP XML 结果通知失败: {e}")
        soap_response = _build_soap_response(-1, str(e))
        return Response(
            content=soap_response,
            media_type="text/xml",
        )


class ExecCmdRes(BaseModel):
    """执行指令响应模型"""
    Result: int = Field(..., description="消息接收结果: 0=成功, -1=失败")
    ErrorDescription: Optional[str] = Field(None, description="错误描述")


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
        logger.error(f"处理 ExecCmdRes 失败: {e}")
        return ExecCmdRes(Result=-1, ErrorDescription=str(e))


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

    Args:
        db: 数据库会话
        csp_id: 上层 ID
        lsp_id: 下层 ID
        correlate_id: 关联 ID
        cmd_result: 命令执行结果 (0=成功, -1=失败)
        result_file_url: 结果 XML 文件 URL（可选）

    Returns:
        ResultNotifyRes
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

    # ── 2. 下载并保存 Result XML（如果有） ──
    result_xml_path = None
    if result_file_url:
        result_xml_path = await _download_result_xml(
            result_file_url, correlate_id
        )
        logger.info(f"Result XML 已下载: {result_xml_path}")

    # ── 3. 更新 IngestHistory ──
    history = await publish_repository.get_ingest_history_by_correlate_id(
        db, correlate_id
    )
    if history:
        history.end_date = datetime.now(timezone.utc)
        history.status = "success" if cmd_result == 0 else "failure"
        if result_xml_path:
            history.result_xml_path = result_xml_path
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

    # ── 4. 更新 PublishTask 状态 ──
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

    if result_xml_path:
        task.result_xml_path = result_xml_path

    await publish_repository.update_publish_task(db, task)

    # ── 5. 同步 content.status 和 object_publish_status（仅 Content 类型）──
    if task.entity_type == "Content" and task.entity_id:
        from app.internal.cms_biz_publish.services.publish_service import (
            _sync_content_ingest_status,
        )
        from app.internal.cms_biz_publish.services.object_publish_status_service import (
            mark_object_as_unpublished,
        )
        from app.soap.c2.loader import load_build_context
        from app.internal.cms_biz_publish.services.object_publish_status_service import (
            batch_mark_objects_from_context,
        )

        ingest_status = None
        if cmd_result == 0:
            if task.task_type == "publish":
                ingest_status = "Published"
                
                # 发布成功，更新所有涉及对象的发布状态
                try:
                    ctx = await load_build_context(db, task.entity_id)
                    if ctx:
                        # 判断是REGIST还是UPDATE
                        from app.internal.cms_biz_publish.repositories.publish_repository import (
                            get_object_publish_status,
                        )
                        status = await get_object_publish_status(db, "Content", task.entity_id)
                        action = "UPDATE" if (status and status.is_published) else "REGIST"
                        
                        await batch_mark_objects_from_context(
                            db, task.entity_id, ctx, action,
                            ingest_history_id=history.id if history else None,
                        )
                        logger.info(
                            f"发布成功，已更新所有对象发布状态 | content_id={task.entity_id} action={action}"
                        )
                    else:
                        logger.warning(
                            f"发布成功但BuildContext加载失败，无法更新对象发布状态 | content_id={task.entity_id}"
                        )
                except Exception as e:
                    logger.error(f"更新发布状态失败 | content_id={task.entity_id} error={e}")
                    # 不重新抛出，因为这是回调处理，不应影响LSP的响应
                    
            elif task.task_type == "unpublish":
                ingest_status = "Closed"
                
                # 下架成功，更新主对象的下架状态
                try:
                    await mark_object_as_unpublished(db, "Content", task.entity_id)
                    logger.info(
                        f"下架成功，已更新对象下架状态 | content_id={task.entity_id}"
                    )
                except Exception as e:
                    logger.error(f"更新下架状态失败 | content_id={task.entity_id} error={e}")
                
                # 下架成功，恢复 arrangement 任务为待处理状态
                try:
                    from app.internal.cms_biz_package.models.task import Task
                    
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
                        logger.info(f"下架成功，arrangement任务恢复为待处理 | content_id={task.entity_id}")
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

    return ResultNotifyRes(Result=0, ErrorDescription=None)


# ═══════════════════════════════════════════════════════════
# SOAP XML 解析与构建
# ═══════════════════════════════════════════════════════════

def _parse_soap_result_notify(xml_str: str) -> tuple:
    """
    解析 SOAP XML 格式的 ResultNotifyReq

    支持两种格式：
    1. 带 SOAP Envelope 的标准 SOAP 消息
    2. 不带 Envelope 的纯 XML（兼容某些 LSP 实现）

    Returns:
        (csp_id, lsp_id, correlate_id, cmd_result, result_file_url)
    """
    root = ET.fromstring(xml_str)

    req_element = root.find(".//ResultNotifyReq")
    if req_element is None:
        req_element = root
        if root.tag != "ResultNotifyReq":
            raise ValueError(f"无法找到 ResultNotifyReq 节点，根节点为: {root.tag}")

    def _get_text(element, tag: str) -> str:
        child = element.find(tag)
        if child is not None and child.text:
            return child.text.strip()
        return ""

    csp_id = _get_text(req_element, "CSPID")
    lsp_id = _get_text(req_element, "LSPID")
    correlate_id = _get_text(req_element, "CorrelateID")
    cmd_result_str = _get_text(req_element, "CmdResult")
    result_file_url = _get_text(req_element, "ResultFileURL") or None

    if not correlate_id:
        raise ValueError("SOAP XML 中缺少必填字段 CorrelateID")

    try:
        cmd_result = int(cmd_result_str)
    except (ValueError, TypeError):
        cmd_result = -1

    return csp_id, lsp_id, correlate_id, cmd_result, result_file_url


def _build_soap_response(result: int, error_description: Optional[str] = None) -> str:
    """
    构建 SOAP XML 格式的 ResultNotifyRes 响应

    Returns:
        SOAP XML 字符串
    """
    error_desc = error_description or ""
    return f"""<?xml version="1.0" encoding="UTF-8"?>
<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
  <soap:Body>
    <ResultNotifyRes>
      <Result>{result}</Result>
      <ErrorDescription>{error_desc}</ErrorDescription>
    </ResultNotifyRes>
  </soap:Body>
</soap:Envelope>"""


# ═══════════════════════════════════════════════════════════
# 内部辅助方法
# ═══════════════════════════════════════════════════════════

async def _download_result_xml(result_file_url: str, correlate_id: str) -> Optional[str]:
    """
    从 LSP 下载 Result XML 文件并保存到本地

    Args:
        result_file_url: LSP 提供的结果 XML 下载地址
        correlate_id: 关联 ID，用于命名文件

    Returns:
        保存到本地的文件路径，失败返回 None
    """
    try:
        from .config import soap_settings

        storage_path = Path(soap_settings.xml_storage_path)
        storage_path.mkdir(parents=True, exist_ok=True)

        filename = f"result_{correlate_id[:8]}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xml"
        filepath = storage_path / filename

        async with httpx.AsyncClient(timeout=soap_settings.timeout) as client:
            response = await client.get(result_file_url)
            response.raise_for_status()

        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(response.text)

        logger.info(f"Result XML 下载成功: {filepath}")
        return str(filepath)

    except Exception as e:
        logger.error(f"Result XML 下载失败 - URL: {result_file_url}, Error: {e}")
        return None
