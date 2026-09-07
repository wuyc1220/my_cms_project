"""
CMS 加密模块

提供 AES-256-GCM 加密解密功能，用于保护敏感字段（如FTP URL）。
"""

from .aes_gcm import AESGCMCipher
from .url_encoder import CustomURLEncoder
from .ftp_url_crypto import (
    FTPURLCryptoService,
    get_ftp_crypto_service,
    encrypt_ftp_url,
    decrypt_ftp_url,
)
from .storage_url_crypto import (
    StorageURLCryptoService,
    get_storage_crypto_service,
    encrypt_storage_url,
    decrypt_storage_url,
)

__all__ = [
    "AESGCMCipher",
    "CustomURLEncoder",
    "FTPURLCryptoService",
    "get_ftp_crypto_service",
    "encrypt_ftp_url",
    "decrypt_ftp_url",
    "StorageURLCryptoService",
    "get_storage_crypto_service",
    "encrypt_storage_url",
    "decrypt_storage_url",
]
