"""
AES-256-GCM 加密解密核心实现

功能：
- 使用 AES-256-GCM 算法进行对称加密
- IV（12字节）自动拼接在密文前面
- 认证标签（16字节）自动拼接在密文后面
- 支持字符串和字节类型的加解密

数据结构：
完整加密串 = IV(12字节) + 密文(变长) + 认证标签(16字节)
"""

import os
from typing import Union

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.i18n import get_msg


# AES-GCM 固定参数
IV_LENGTH = 12  # GCM模式推荐IV长度：12字节
TAG_LENGTH = 16  # GCM认证标签长度：16字节
KEY_LENGTH = 32  # AES-256密钥长度：32字节（256位）


class AESGCMCipher:
    """AES-256-GCM 加密解密器"""

    def __init__(self, key: Union[str, bytes]):
        """
        初始化加密器

        Args:
            key: 加密密钥（32字节）
                 - 字符串类型：直接作为密钥
                 - 字节类型：直接作为密钥

        Raises:
            BusinessException: 密钥长度不是32字节时抛出
        """
        if isinstance(key, str):
            self.key_bytes = key.encode("utf-8")
        else:
            self.key_bytes = key

        if len(self.key_bytes) != KEY_LENGTH:
            raise BusinessException(
                ErrorCode.VALIDATION_ERROR,
                get_msg("ENCRYPTION_KEY_INVALID_LENGTH"),
            )

        self.aesgcm = AESGCM(self.key_bytes)

    def encrypt(self, plaintext: Union[str, bytes]) -> bytes:
        """
        加密数据

        流程：
        1. 生成随机IV（12字节）
        2. 使用AES-256-GCM加密（自动生成16字节认证标签）
        3. 返回：IV + 密文 + 认证标签

        Args:
            plaintext: 待加密的明文（字符串或字节）

        Returns:
            bytes: IV(12) + 密文 + Tag(16) 的完整加密数据
        """
        # 转换明文为字节
        if isinstance(plaintext, str):
            plaintext_bytes = plaintext.encode("utf-8")
        else:
            plaintext_bytes = plaintext

        # 生成随机IV（每次加密必须使用不同的IV）
        iv = os.urandom(IV_LENGTH)

        # GCM加密（自动附加16字节认证标签到密文后面）
        # 返回值：密文 + 认证标签
        ciphertext_with_tag = self.aesgcm.encrypt(iv, plaintext_bytes, None)

        # 拼接：IV + 密文 + 认证标签
        encrypted_data = iv + ciphertext_with_tag

        return encrypted_data

    def decrypt(self, encrypted_data: bytes) -> bytes:
        """
        解密数据

        流程：
        1. 从加密数据中分离IV（前12字节）
        2. 剩余部分为：密文 + 认证标签
        3. 使用AES-256-GCM解密（自动验证认证标签）

        Args:
            encrypted_data: IV + 密文 + Tag 的完整加密数据

        Returns:
            bytes: 解密后的明文（字节）

        Raises:
            BusinessException: 数据长度不足或认证失败时抛出
        """
        if len(encrypted_data) < IV_LENGTH + TAG_LENGTH:
            raise BusinessException(
                ErrorCode.DECRYPTION_FAILED,
                get_msg("ENCRYPTION_DATA_TOO_SHORT"),
            )

        # 分离IV（前12字节）
        iv = encrypted_data[:IV_LENGTH]

        # 分离密文+认证标签（剩余部分）
        ciphertext_with_tag = encrypted_data[IV_LENGTH:]

        try:
            # GCM解密（自动验证认证标签，失败会抛出异常）
            plaintext_bytes = self.aesgcm.decrypt(iv, ciphertext_with_tag, None)
            return plaintext_bytes
        except Exception as e:
            raise BusinessException(
                ErrorCode.DECRYPTION_FAILED,
                get_msg("ENCRYPTION_DECRYPTION_FAILED"),
            ) from e

    def encrypt_to_base64(self, plaintext: Union[str, bytes]) -> str:
        """
        加密并转换为Base64字符串

        Args:
            plaintext: 待加密的明文

        Returns:
            str: Base64编码的加密字符串
        """
        import base64

        encrypted_bytes = self.encrypt(plaintext)
        base64_str = base64.b64encode(encrypted_bytes).decode("utf-8")
        return base64_str

    def decrypt_from_base64(self, base64_str: str) -> bytes:
        """
        从Base64字符串解密

        Args:
            base64_str: Base64编码的加密字符串

        Returns:
            bytes: 解密后的明文（字节）
        """
        import base64

        encrypted_bytes = base64.b64decode(base64_str)
        return self.decrypt(encrypted_bytes)

    def encrypt_to_base64_str(self, plaintext: Union[str, bytes]) -> str:
        """
        加密并转换为Base64字符串（返回字符串类型）

        Args:
            plaintext: 待加密的明文

        Returns:
            str: Base64编码的加密字符串
        """
        return self.encrypt_to_base64(plaintext)

    def decrypt_from_base64_str(self, base64_str: str) -> str:
        """
        从Base64字符串解密（返回字符串类型）

        Args:
            base64_str: Base64编码的加密字符串

        Returns:
            str: 解密后的明文字符串
        """
        plaintext_bytes = self.decrypt_from_base64(base64_str)
        return plaintext_bytes.decode("utf-8")
