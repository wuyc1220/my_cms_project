"""
事务管理装饰器
"""
from functools import wraps
from typing import Callable, Any

from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.core.exceptions import BusinessException


def transactional(func: Callable) -> Callable:
    """
    事务装饰器 - 自动管理数据库事务
    用法: @transactional
    注意: 被装饰的函数必须接收 db: AsyncSession 作为第一个参数
    """
    @wraps(func)
    async def wrapper(*args, **kwargs) -> Any:
        # 获取数据库session
        db = kwargs.get('db')
        if db is None and args:
            db = args[0]
            
        if not isinstance(db, AsyncSession):
            raise ValueError("事务装饰器要求第一个参数是 AsyncSession")
            
        try:
            # 执行函数
            result = await func(*args, **kwargs)
            # 提交事务
            await db.commit()
            return result
        except BusinessException as e:
            # 业务异常回滚
            await db.rollback()
            logger.warning(f"业务异常，事务回滚: {e.message}")
            raise
        except Exception as e:
            # 其他异常回滚
            await db.rollback()
            logger.error(f"系统异常，事务回滚: {str(e)}")
            raise
            
    return wrapper
