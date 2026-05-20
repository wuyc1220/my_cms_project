"""
注入历史（Ingest History）领域模型。

记录内容/栏目/服务包/人物等实体的发布/下架注入历史。
"""

from datetime import datetime

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class IngestHistory(Base):
    """
    注入历史记录表。

    字段：
        id              主键
        entity_type     实体类型: Content / Category / Package / Cast
        entity_id       实体ID
        entity_name     实体名称
        action          操作类型: REGIST / UPDATE / DELETE
        status          状态: success / failure
        create_date     任务创建时间
        send_date       发送XML时间
        end_date        收到反馈时间
        ingest_xml_path Ingest XML 文件路径
        result_xml_path Result XML 文件路径
        correlate_id    SOAP 关联 ID
    """

    __tablename__ = "ingest_history"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="实体类型: Content/Category/Package/Cast"
    )
    entity_id: Mapped[int] = mapped_column(
        Integer, nullable=False, index=True, comment="实体ID"
    )
    entity_name: Mapped[str | None] = mapped_column(
        String(200), nullable=True, comment="实体名称"
    )
    action: Mapped[str] = mapped_column(
        String(50), nullable=False, comment="操作类型: REGIST/UPDATE/DELETE"
    )
    status: Mapped[str] = mapped_column(
        String(50), nullable=False, default="failure", server_default="failure", comment="状态: success/failure"
    )
    create_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="任务创建时间"
    )
    send_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="发送XML时间"
    )
    end_date: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, comment="收到反馈时间"
    )
    ingest_xml_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Ingest XML 文件路径"
    )
    result_xml_path: Mapped[str | None] = mapped_column(
        String(500), nullable=True, comment="Result XML 文件路径"
    )
    correlate_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, index=True, comment="SOAP 关联 ID，用于匹配 LSP 结果通知"
    )

    # ── SOAP 交互日志字段 ──────────────────────────────────
    soap_csp_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="上层 CSP ID"
    )
    soap_lsp_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, comment="下层 LSP ID"
    )
    soap_cmd_result: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="ExecCmdReq 调用 LSP 返回的 Result"
    )
    soap_notify_cmd_result: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="LSP 回调 ResultNotify 的 CmdResult"
    )
    soap_request_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="LSP 调用通知接口的请求内容"
    )
    soap_response_detail: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="通知接口返回给 LSP 的响应内容"
    )
    soap_error_description: Mapped[str | None] = mapped_column(
        Text, nullable=True, comment="SOAP 交互错误描述"
    )

    # ── 审计字段 ───────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", comment="软删除标记"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False, comment="创建时间"
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False, comment="更新时间"
    )
    created_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="创建人ID"
    )
    updated_by: Mapped[int | None] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True, comment="更新人ID"
    )
