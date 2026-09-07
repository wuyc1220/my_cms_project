"""
FTP URL 加密服务

提供完整的FTP URL加密解密流程：
明文 → AES-256-GCM加密 → (IV + 密文 + Tag) → Base64编码 → URLEncode → 最终加密串

依赖配置：
- FTP_URL_ENCRYPTION_ENABLED: 是否启用加密（true/false）
- FTP_URL_ENCRYPTION_KEY: 32字节加密密钥

使用示例：
    from app.common.crypto import FTPURLCryptoService
    
    # 加密FTP URL
    crypto_service = FTPURLCryptoService()
    encrypted_url = crypto_service.encrypt_ftp_url("ftp://192.168.1.100/path/to/file")
    
    # 解密FTP URL
    decrypted_url = crypto_service.decrypt_ftp_url(encrypted_url)
"""

from loguru import logger

from app.common.core.exceptions import BusinessException, ErrorCode
from app.common.core.i18n import get_msg
from app.config import settings

from .aes_gcm import AESGCMCipher
from .url_encoder import CustomURLEncoder


class FTPURLCryptoService:
    """FTP URL 加密解密服务"""

    def __init__(self):
        """
        初始化FTP URL加密服务

        从环境变量读取配置：
        - FTP_URL_ENCRYPTION_ENABLED: 是否启用加密
        - FTP_URL_ENCRYPTION_KEY: 32字节密钥

        Raises:
            BusinessException: 配置无效时抛出
        """
        self.enabled = settings.ftp_url_encryption_enabled

        if self.enabled:
            # 验证密钥配置
            if not settings.ftp_url_encryption_key:
                raise BusinessException(
                    ErrorCode.VALIDATION_ERROR,
                    get_msg("FTP_ENCRYPTION_KEY_NOT_CONFIGURED"),
                )

            # 初始化AES加密器
            self.cipher = AESGCMCipher(settings.ftp_url_encryption_key)
            logger.info("FTP URL encryption service initialized")
        else:
            self.cipher = None
            logger.info("FTP URL encryption service disabled")

    def encrypt_ftp_url(self, ftp_url: str) -> str:
        """
        加密FTP URL

        完整流程：
        1. AES-256-GCM加密（生成 IV + 密文 + Tag）
        2. Base64编码
        3. 自定义URLEncode

        Args:
            ftp_url: 原始FTP URL明文

        Returns:
            str: 加密后的URL字符串

        Raises:
            BusinessException: 加密失败或未启用加密时抛出
        """
        if not self.enabled:
            logger.warning("FTP URL encryption is disabled, returning original URL")
            return ftp_url

        if not ftp_url:
            return ftp_url

        try:
            # Step 1: AES-256-GCM加密（返回 IV + 密文 + Tag）
            # Step 2: Base64编码
            base64_str = self.cipher.encrypt_to_base64(ftp_url)

            # Step 3: 自定义URLEncode
            encrypted_url = CustomURLEncoder.encode(base64_str)

            logger.debug("FTP URL encrypted successfully")
            return encrypted_url

        except BusinessException:
            raise
        except Exception as e:
            logger.error(f"Failed to encrypt FTP URL: {e}")
            raise BusinessException(
                ErrorCode.ENCRYPTION_FAILED,
                get_msg("FTP_URL_ENCRYPTION_FAILED"),
            ) from e

    def decrypt_ftp_url(self, encrypted_url: str) -> str:
        """
        解密FTP URL

        完整流程：
        1. URLEncode解码
        2. Base64解码
        3. AES-256-GCM解密

        Args:
            encrypted_url: 加密后的URL字符串

        Returns:
            str: 解密后的FTP URL明文

        Raises:
            BusinessException: 解密失败或未启用加密时抛出
        """
        if not self.enabled:
            logger.warning("FTP URL encryption is disabled, returning original URL")
            return encrypted_url

        if not encrypted_url:
            return encrypted_url

        try:
            # Step 1: URLEncode解码
            base64_str = CustomURLEncoder.decode(encrypted_url)

            # Step 2 & 3: Base64解码 + AES-256-GCM解密
            ftp_url = self.cipher.decrypt_from_base64_str(base64_str)

            logger.debug("FTP URL decrypted successfully")
            return ftp_url

        except BusinessException:
            raise
        except Exception as e:
            logger.error(f"Failed to decrypt FTP URL: {e}")
            raise BusinessException(
                ErrorCode.DECRYPTION_FAILED,
                get_msg("FTP_URL_DECRYPTION_FAILED"),
            ) from e

    def is_encryption_enabled(self) -> bool:
        """
        检查加密功能是否启用

        Returns:
            bool: 是否启用加密
        """
        return self.enabled


# 全局单例（延迟初始化）
_crypto_service_instance = None


def get_ftp_crypto_service() -> FTPURLCryptoService:
    """
    获取FTP URL加密服务单例

    Returns:
        FTPURLCryptoService: 加密服务实例
    """
    global _crypto_service_instance
    if _crypto_service_instance is None:
        _crypto_service_instance = FTPURLCryptoService()
    return _crypto_service_instance


def encrypt_ftp_url(ftp_url: str) -> str:
    """
    便捷函数：加密FTP URL

    Args:
        ftp_url: 原始FTP URL

    Returns:
        str: 加密后的URL
    """
    return get_ftp_crypto_service().encrypt_ftp_url(ftp_url)


def decrypt_ftp_url(encrypted_url: str) -> str:
    """
    便捷函数：解密FTP URL

    Args:
        encrypted_url: 加密后的URL

    Returns:
        str: 解密后的FTP URL
    """
    return get_ftp_crypto_service().decrypt_ftp_url(encrypted_url)
