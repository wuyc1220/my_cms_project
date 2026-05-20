"""数据库基础设施配置"""
from contextvars import ContextVar
from typing import Any

from sqlalchemy import event
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession, async_sessionmaker
from sqlalchemy.orm import DeclarativeBase
from app.config import settings

# 当前用户ID上下文变量，用于自动填充 created_by / updated_by
current_user_id: ContextVar[int | None] = ContextVar("current_user_id", default=None)


def get_current_user_id() -> int | None:
    """获取当前上下文中的用户ID。"""
    return current_user_id.get()


def set_current_user_id(user_id: int | None) -> None:
    """设置当前上下文中的用户ID。"""
    current_user_id.set(user_id)


# 异步数据库引擎（PostgreSQL）
# pool_pre_ping: 每次从池中取出连接时先做健康检查，避免使用已断开的连接
# pool_size: 连接池大小
# connect_args: 设置连接超时 10s、命令超时 30s，避免启动时长时间卡住
engine = create_async_engine(
    settings.database_url,
    echo=settings.database_echo,
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=5,
    connect_args={
        "timeout": 10,
        "command_timeout": 30,
    },
)

# 异步会话工厂，用于创建数据库会话
AsyncSessionLocal = async_sessionmaker(
    engine, class_=AsyncSession, expire_on_commit=False
)


class Base(DeclarativeBase):
    """所有 SQLAlchemy 模型的基类"""
    pass


# 自动填充 created_by 和 updated_by 的事件监听
@event.listens_for(Base, "before_insert", propagate=True)
def set_created_by(mapper: Any, connection: Any, target: Any) -> None:
    """在插入前自动设置 created_by。"""
    if hasattr(target, "created_by") and target.created_by is None:
        user_id = get_current_user_id()
        if user_id is not None:
            target.created_by = user_id


@event.listens_for(Base, "before_update", propagate=True)
def set_updated_by(mapper: Any, connection: Any, target: Any) -> None:
    """在更新前自动设置 updated_by。"""
    if hasattr(target, "updated_by"):
        user_id = get_current_user_id()
        if user_id is not None:
            target.updated_by = user_id
