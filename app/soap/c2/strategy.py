"""
C2 规范注入策略。

根据对象在数据库中的 ingest 状态，决策本次 XML 应采用何种 Action：

    未注入（None/failure）            → REGIST（完整属性）
    已注入（Published/success）       → UPDATE（增量/全量属性）
    下架任务                          → DELETE（仅 ID）
    已注入且此次无变更                → 跳过 Object，仅生成 Mapping

本模块仅提供判定函数，不直接接触 ORM 对象属性。
调用方传入 ingest_status 字符串即可。

注意：
    当前版本使用基于发布历史表的判断逻辑（推荐）
    旧版本基于ingest_status的判断逻辑保留为 backwards compatibility
"""
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from .constants import (
    INGEST_STATUS_FAILURE,
    INGEST_STATUS_NONE,
    INGEST_STATUS_PUBLISHED,
    INGEST_STATUS_SUCCESS,
    Action,
)


# ═══════════════════════════════════════════════════════════
# 新版本：基于发布历史表判断 Action
# ═══════════════════════════════════════════════════════════

async def decide_action_with_history(
    db: AsyncSession,
    entity_type: str,
    entity_id: int,
    content_id: Optional[int] = None,
    is_unpublish: bool = False,
    obj_updated_at: Optional[datetime] = None,
) -> Optional[Action]:
    """
    基于发布历史判断对象的Action（推荐方式）。
    
    核心逻辑：
    - 下架任务且是主对象 → DELETE
    - 下架任务但是关联对象 → None（跳过，不处理）
    - 曾经发布成功过且无变更 → SKIP（仅输出Mapping）
    - 曾经发布成功过且有变更 → UPDATE
    - 从未发布过 → REGIST
    
    Args:
        db: 数据库会话
        entity_type: 实体类型（Content/Cast/Package/Picture/Movie/Category）
        entity_id: 实体ID
        content_id: 关联的主内容ID
        is_unpublish: 是否为下架任务
        obj_updated_at: ORM对象的updated_at时间，用于变更检测
    
    Returns:
        Action.REGIST / Action.UPDATE / Action.DELETE / Action.SKIP / None（跳过）
    """
    from app.internal.cms_biz_publish.services.object_publish_status_service import (
        decide_action_for_object,
    )
    
    action_str = await decide_action_for_object(
        db, entity_type, entity_id, content_id, is_unpublish, obj_updated_at
    )
    
    if action_str is None:
        return None
    
    return Action(action_str)


# ═══════════════════════════════════════════════════════════
# 旧版本：基于 ingest_status 判断 Action（向后兼容）
# ═══════════════════════════════════════════════════════════


def decide_action(
    ingest_status: str | None,
    is_unpublish: bool = False,
) -> Action:
    """
    根据对象注入状态决策本次应采用的 Action（旧版本，向后兼容）。
    
    注意：推荐使用 decide_action_with_history() 基于发布历史表判断

    :param ingest_status: 对象当前 ingest 状态
        Content:        None / WaitingForMaterials / Published
        Category/Cast/Package: None / success / failure
    :param is_unpublish: 是否为下架任务；True 时一律返回 DELETE
    :return: REGIST / UPDATE / DELETE
    """
    if is_unpublish:
        return Action.DELETE

    # 已注入成功 → UPDATE
    if ingest_status in (INGEST_STATUS_PUBLISHED, INGEST_STATUS_SUCCESS):
        return Action.UPDATE

    # 未注入 / 注入失败 / 其它中间态 → REGIST
    return Action.REGIST


def is_published(ingest_status: str | None) -> bool:
    """
    判断对象是否已成功注入到 LSP。

    已注入的对象在后续流程中可跳过 Object 重建，仅需生成 Mapping。
    """
    return ingest_status in (INGEST_STATUS_PUBLISHED, INGEST_STATUS_SUCCESS)


def is_not_injected(ingest_status: str | None) -> bool:
    """
    判断对象是否从未成功注入过。
    """
    return ingest_status in (None, "", INGEST_STATUS_NONE, INGEST_STATUS_FAILURE)
