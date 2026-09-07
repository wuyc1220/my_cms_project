"""
密码验证服务

验证规则：
1. 密码长度 >= 8 位
2. 必须包含大写字母、小写字母、数字、特殊符号中的至少3种
3. 禁止连续重复字符（如 aaaaaa, 111111）
4. 禁止键盘序列（如 qwerty, 123456）
5. 禁止与账号相同

重复字符/连续字符/键盘序列三个阈值统一由 pattern_min_len 控制（默认 6，可配置）。
"""

import re
from functools import lru_cache
from typing import List, Tuple
from loguru import logger

from app.common.core.i18n import get_msg

# 键盘布局：字母行、数字行、符号行、列走位、数字前缀列走位（如 1qaz2wsx 型）
KEYBOARD_LINES = [
    'qwertyuiop', 'asdfghjkl', 'zxcvbnm', '1234567890', '!@#$%^&*()',
    'qazwsx', 'wsxedc', 'edcrfv',                      # 相邻列组合走位
    '1qaz', '2wsx', '3edc', '4rfv', '5tgb', '6yhn', '7ujm',  # 数字前缀列走位
]

# 模式（重复/连续/键盘序列）触发阈值默认值
PATTERN_MIN_LEN_DEFAULT = 6


@lru_cache(maxsize=None)
def _build_keyboard_sequences(min_len: int) -> frozenset:
    """基于键盘布局生成所有键盘序列子串（正向 + 反向），按阈值缓存。"""
    sequences = set()
    for line in KEYBOARD_LINES:
        for s in (line, line[::-1]):
            for n in range(min_len, len(s) + 1):
                for i in range(len(s) - n + 1):
                    sequences.add(s[i:i + n])
    return frozenset(sequences)


def _has_consecutive_chars(text: str, min_len: int) -> bool:
    """检查是否存在 min_len 个及以上连续递增/递减字符（如 abcdef、987654）。"""
    if min_len < 2:
        return True
    for i in range(len(text) - min_len + 1):
        ords = [ord(c) for c in text[i:i + min_len]]
        if all(ords[k + 1] - ords[k] == 1 for k in range(len(ords) - 1)):
            return True
        if all(ords[k + 1] - ords[k] == -1 for k in range(len(ords) - 1)):
            return True
    return False


def validate_password(
    password: str,
    username: str | None = None,
    min_length: int = 8,
    pattern_min_len: int = PATTERN_MIN_LEN_DEFAULT,
) -> Tuple[bool, List[str]]:
    """
    验证密码是否符合规则，命中第一条即返回。

    Args:
        password: 待验证的密码
        username: 用户账号（可选，用于检查密码是否与账号相同）
        min_length: 密码最小长度（默认8位）
        pattern_min_len: 重复字符/连续字符/键盘序列触发阈值（默认6，即6个及以上才拦截）

    Returns:
        Tuple[bool, List[str]]: (是否有效, 错误消息列表)
    """
    if len(password) < min_length:
        return False, [get_msg("PASSWORD_TOO_SHORT", min_length=min_length)]

    has_upper = bool(re.search(r'[A-Z]', password))
    has_lower = bool(re.search(r'[a-z]', password))
    has_digit = bool(re.search(r'[0-9]', password))
    has_special = bool(re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:\'",.<>?/~`]', password))

    type_count = sum([has_upper, has_lower, has_digit, has_special])
    if type_count < 3:
        return False, [get_msg("PASSWORD_COMPLEXITY")]

    if re.search(rf'(.)\1{{{pattern_min_len - 1},}}', password):
        return False, [get_msg("PASSWORD_NO_REPEAT")]

    lower_pwd = password.lower()
    if _has_consecutive_chars(lower_pwd, pattern_min_len):
        return False, [get_msg("PASSWORD_NO_SEQUENCE")]

    if username and lower_pwd == username.lower():
        return False, [get_msg("PASSWORD_NOT_USERNAME")]

    if any(seq in lower_pwd for seq in _build_keyboard_sequences(pattern_min_len)):
        return False, [get_msg("PASSWORD_NO_KEYBOARD")]

    return True, []
