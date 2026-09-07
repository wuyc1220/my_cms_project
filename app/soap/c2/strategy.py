"""
C2 规范注入策略。

统一 REGIST 策略：每次发布都全量输出，下游系统根据 ID 是否存在自行判断新增/更新。

    发布任务 → REGIST（全量属性）
    下架任务 → DELETE（仅 ID）
"""
from .constants import Action


def decide_action(is_unpublish: bool = False) -> Action:
    """
    决策本次应采用的 Action。

    :param is_unpublish: 是否为下架任务；True 时返回 DELETE
    :return: REGIST / DELETE
    """
    if is_unpublish:
        return Action.DELETE
    return Action.REGIST
