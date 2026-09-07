"""
文件存储URL加密服务

在文件上传到SFTP/FTP后，对返回的URL进行加密存储。
所有从数据库读取的file_path字段都需要解密后使用。
"""

from loguru import logger

from app.common.crypto import encrypt_ftp_url, decrypt_ftp_url
from app.config import settings


class StorageURLCryptoService:
    """存储URL加密服务"""
    
    def __init__(self):
        self.enabled = settings.ftp_url_encryption_enabled
    
    def encrypt_storage_url(self, file_path: str) -> str:
        """
        加密存储URL（在写入数据库前调用）
        
        Args:
            file_path: 原始文件路径（如 sftp://user:pass@host/path/file.jpg）
            
        Returns:
            加密后的URL（如果是FTP/SFTP协议），否则保持原样
        """
        if not self.enabled:
            return file_path
        
        if not file_path:
            return file_path
        
        # 判断协议类型
        lower_path = file_path.lower()
        if lower_path.startswith('ftp://') or lower_path.startswith('sftp://'):
            encrypted = encrypt_ftp_url(file_path)
            logger.debug(f"存储URL已加密: {file_path[:50]}... -> {encrypted[:50]}...")
            return encrypted
        
        # HTTP/HTTPS等其他协议不加密
        return file_path
    
    def decrypt_storage_url(self, encrypted_path: str) -> str:
        """
        解密存储URL（从数据库读取后调用）
        
        智能识别逻辑：
        1. 如果URL是明文FTP/SFTP格式（包含 sftp:// 或 ftp://），直接返回
        2. 如果URL看起来是加密的（Base64+URLEncode格式），尝试解密
        3. 解密失败时返回原值（兼容旧数据）
        
        Args:
            encrypted_path: 可能是加密或未加密的URL
            
        Returns:
            解密后的原始URL，或原值（如果未加密或解密失败）
        """
        if not self.enabled:
            return encrypted_path
        
        if not encrypted_path:
            return encrypted_path
        
        # 智能识别：明文FTP/SFTP URL直接返回（兼容旧数据）
        lower_path = encrypted_path.lower()
        if lower_path.startswith('ftp://') or lower_path.startswith('sftp://'):
            logger.debug(f"URL已是明文格式，无需解密")
            return encrypted_path
        
        # 智能识别：HTTP/HTTPS URL不处理
        if lower_path.startswith('http://') or lower_path.startswith('https://'):
            return encrypted_path
        
        # 尝试解密（可能是加密后的URL）
        try:
            decrypted = decrypt_ftp_url(encrypted_path)
            logger.debug(f"存储URL已解密")
            return decrypted
        except Exception:
            # 解密失败，说明是相对路径（如 pictures/xxx.jpg）或其他格式，返回原值
            return encrypted_path


# 全局单例
_storage_crypto_service = None


def get_storage_crypto_service() -> StorageURLCryptoService:
    """获取存储URL加密服务单例"""
    global _storage_crypto_service
    if _storage_crypto_service is None:
        _storage_crypto_service = StorageURLCryptoService()
    return _storage_crypto_service


# 便捷函数
def encrypt_storage_url(file_path: str) -> str:
    """加密存储URL"""
    return get_storage_crypto_service().encrypt_storage_url(file_path)


def decrypt_storage_url(encrypted_path: str) -> str:
    """解密存储URL"""
    return get_storage_crypto_service().decrypt_storage_url(encrypted_path)
