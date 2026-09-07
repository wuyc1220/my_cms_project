"""
自定义 URLEncode 实现

规则：
- 保留字符（不编码）：a-z A-Z 0-9 . - * _
- 空格：转换为 +
- 其他字符：转换为 %xy 格式（UTF-8编码）

注意：这不是标准的 application/x-www-form-urlencoded 编码，
而是项目自定义的编码规则。
"""


class CustomURLEncoder:
    """自定义 URL 编码器"""

    # 不编码的字符集合
    SAFE_CHARS = set(
        "abcdefghijklmnopqrstuvwxyz"
        "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
        "0123456789"
        "-*._"
    )

    @classmethod
    def encode(cls, text: str) -> str:
        """
        按自定义规则编码字符串

        Args:
            text: 待编码的字符串

        Returns:
            str: 编码后的字符串
        """
        encoded_chars = []

        for char in text:
            if char in cls.SAFE_CHARS:
                # 安全字符，保持原样
                encoded_chars.append(char)
            elif char == " ":
                # 空格转换为加号
                encoded_chars.append("+")
            else:
                # 其他字符转换为 %xy 格式（UTF-8编码）
                utf8_bytes = char.encode("utf-8")
                for byte in utf8_bytes:
                    encoded_chars.append(f"%{byte:02X}")

        return "".join(encoded_chars)

    @classmethod
    def decode(cls, text: str) -> str:
        """
        解码自定义编码的字符串

        Args:
            text: 已编码的字符串

        Returns:
            str: 解码后的原始字符串
        """
        import re

        # 先将 + 转换回空格
        text = text.replace("+", " ")

        # 解码 %xy 格式的字符
        def replace_percent(match):
            hex_str = match.group(1)
            byte_value = int(hex_str, 16)
            return chr(byte_value)  # 返回字符而不是字节

        # 使用正则匹配所有 %xy 模式
        pattern = r"%([0-9A-Fa-f]{2})"
        decoded_text = re.sub(pattern, replace_percent, text)

        return decoded_text
