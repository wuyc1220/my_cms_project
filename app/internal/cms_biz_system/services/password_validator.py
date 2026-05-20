"""
密码验证服务

验证规则：
1. 密码长度 >= 8 位
2. 必须包含大写字母、小写字母、数字、特殊符号中的至少3种
3. 禁止连续重复字符（如 aaa, 111）
4. 禁止键盘序列（如 qwerty, 123456）
5. 禁止与账号相同
6. 禁止常见英文单词/拼音
"""

import re
from typing import List, Tuple
from loguru import logger

# 常见弱密码（英文单词 + 拼音）
COMMON_PASSWORDS = {
    # 英文单词
    'password', 'admin', 'root', 'user', 'login', 'welcome', 'hello', 'world',
    'monkey', 'dragon', 'master', 'letmein', 'qwerty', 'trustno1', 'iloveyou',
    'sunshine', 'princess', 'football', 'baseball', 'soccer', 'hockey',
    'batman', 'superman', 'shadow', 'ashley', 'michael', 'jennifer',
    # 常见拼音
    'mima', 'wode', 'nihao', 'woaini', 'zhangsan', 'lisi', 'wangwu',
    'zhaoliu', 'sunqi', 'zhouba', 'wujiu', 'zhengshi', 'ceshi', 'test',
    'admin123', 'password123', '123456', 'abcdef', 'abc123', 'aaa', 'bbb',
    'qweasd', 'asdzxc', 'qazwsx', '1q2w3e', 'q1w2e3',
}

# 键盘序列
KEYBOARD_SEQUENCES = [
    # 横向序列
    'qwerty', 'asdfgh', 'zxcvbn', 'qwertyuiop', 'asdfghjkl', 'zxcvbnm',
    'qazwsx', 'qweasd', 'asdzxc',
    # 数字序列
    '123456', '234567', '345678', '456789', '567890',
    '098765', '987654', '876543', '765432', '654321',
    # 特殊符号序列
    '!@#$%^', '@#$%^&', '#$%^&*', '$%^&*(', '%^&*()',
]


def validate_password(password: str, username: str | None = None, min_length: int = 8) -> Tuple[bool, List[str]]:
    """
    验证密码是否符合所有规则。

    Args:
        password: 待验证的密码
        username: 用户账号（可选，用于检查密码是否与账号相同）
        min_length: 密码最小长度（默认8位）

    Returns:
        Tuple[bool, List[str]]: (是否有效, 错误消息列表)
    """
    errors = []

    # 规则1：长度检查
    if len(password) < min_length:
        errors.append(f"密码长度不能少于{min_length}位")

    # 规则2：至少包含3种字符类型
    has_upper = bool(re.search(r'[A-Z]', password))
    has_lower = bool(re.search(r'[a-z]', password))
    has_digit = bool(re.search(r'[0-9]', password))
    has_special = bool(re.search(r'[!@#$%^&*()_+\-=\[\]{}|;:\'",.<>?/~`]', password))

    type_count = sum([has_upper, has_lower, has_digit, has_special])
    if type_count < 3:
        errors.append("密码必须包含大写字母、小写字母、数字、特殊符号中的至少3种")

    # 规则3：禁止连续重复字符（3个或以上相同字符）
    if re.search(r'(.)\1{2,}', password):
        errors.append("密码不能包含连续重复的字符（如aaa、111）")

    # 规则4：禁止连续序列字符（如abc、123、cba、321）
    lower_pwd = password.lower()
    for i in range(len(lower_pwd) - 2):
        # 检查升序序列（abc、123）
        if ord(lower_pwd[i + 1]) == ord(lower_pwd[i]) + 1 and \
           ord(lower_pwd[i + 2]) == ord(lower_pwd[i]) + 2:
            errors.append("密码不能包含连续的字符序列（如abc、123）")
            break
        # 检查降序序列（cba、321）
        if ord(lower_pwd[i + 1]) == ord(lower_pwd[i]) - 1 and \
           ord(lower_pwd[i + 2]) == ord(lower_pwd[i]) - 2:
            errors.append("密码不能包含连续的字符序列（如cba、321）")
            break

    # 规则5：密码不能与账号相同
    if username and lower_pwd == username.lower():
        errors.append("密码不能与账号相同")

    # 规则6：禁止常见英文单词/拼音
    if lower_pwd in COMMON_PASSWORDS:
        errors.append("密码过于简单，不能使用常见单词或拼音")

    # 规则7：禁止键盘序列
    for seq in KEYBOARD_SEQUENCES:
        if seq in lower_pwd:
            errors.append("密码不能包含键盘序列（如qwerty、123456）")
            break

    return len(errors) == 0, errors
