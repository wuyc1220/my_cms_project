"""
元数据校验规则模型。

存储从 Excel 导入的字段级校验规则，支持：
- 必填字段检查（Mandatory）
- 格式检查（Format/Regex）
- 长度检查（Length）
- 枚举值检查（Enum）
- 自定义规则（Custom）

覆盖实体类型：
- PROGRAM (MOVIE/EPISODE)
- SERIES (SERIES/SEASON)
- CHANNEL (CHANNEL)
- SCHEDULE (SCHEDULE)
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class MetadataValidationRule(Base):
    """
    元数据校验规则表。
    
    每条记录定义一个字段的校验规则，支持：
    - 必填规则（is_mandatory: Y/N/C）
    - 长度限制（min_length, max_length）
    - 格式验证（regex_pattern）
    - 枚举值检查（allowed_values）
    - 条件必填（mandatory_condition JSON配置）
    """

    __tablename__ = "metadata_validation_rule"

    # ── 主键 ──────────────────────────────────────────────
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)

    # ── 规则分类 ──────────────────────────────────────────
    entity_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="实体类型: PROGRAM/SERIES/CHANNEL/SCHEDULE",
    )
    field_name: Mapped[str] = mapped_column(
        String(100), nullable=False,
        comment="字段名称",
    )
    rule_type: Mapped[str] = mapped_column(
        String(50), nullable=False,
        comment="规则类型: mandatory/format/length/enum/regex/custom",
    )

    # ── 核心规则参数 ─────────────────────────────────────
    is_mandatory: Mapped[str] = mapped_column(
        String(1), nullable=False, server_default="N",
        comment="必填标识: Y=必填, N=选填, C=条件必填",
    )
    mandatory_condition: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="条件必填触发条件，JSON格式",
    )
    max_length: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="最大长度限制",
    )
    min_length: Mapped[Optional[int]] = mapped_column(
        Integer, nullable=True,
        comment="最小长度限制",
    )
    allowed_values: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="允许值列表（逗号分隔或JSON数组）",
    )
    regex_pattern: Mapped[Optional[str]] = mapped_column(
        String(500), nullable=True,
        comment="正则表达式",
    )
    data_type: Mapped[Optional[str]] = mapped_column(
        String(50), nullable=True,
        comment="数据类型: string/integer/date/boolean/array",
    )
    multi_language: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
        comment="是否支持多语言",
    )

    # ── 扩展配置（JSONB存储复杂规则参数）──────────────────
    rule_config: Mapped[Optional[dict]] = mapped_column(
        JSONB, nullable=True,
        comment="灵活扩展配置",
    )

    # ── 规则属性 ─────────────────────────────────────────
    severity: Mapped[str] = mapped_column(
        String(20), nullable=False, server_default="medium",
        comment="严重级别: critical=阻断, medium=警告, minor=提示",
    )
    is_enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true",
        comment="是否启用",
    )
    is_system: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="true",
        comment="是否系统内置（防止误删）",
    )
    description: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="规则说明",
    )
    example_value: Mapped[Optional[str]] = mapped_column(
        Text, nullable=True,
        comment="示例值",
    )

    # ── 审计字段 ─────────────────────────────────────────
    is_deleted: Mapped[bool] = mapped_column(
        Boolean, nullable=False, server_default="false",
        comment="软删除标记",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False,
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False,
        comment="更新时间",
    )
    created_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True,
        comment="创建人ID",
    )
    updated_by: Mapped[Optional[int]] = mapped_column(
        ForeignKey("cms_user.id", ondelete="SET NULL"), nullable=True,
        comment="更新人ID",
    )

    def __repr__(self) -> str:
        return (
            f"<MetadataValidationRule(id={self.id}, "
            f"entity_type='{self.entity_type}', "
            f"field_name='{self.field_name}', "
            f"rule_type='{self.rule_type}')>"
        )
