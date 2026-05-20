"""
统一存储服务（策略模式）

支持多种存储后端：
- SFTP：通过 paramiko 连接 SFTP 服务器
- FTP：通过 ftplib 连接 vsftpd 服务器
- Local：本地文件系统（开发/降级使用）

通过配置项 storage_type 切换存储后端。
"""
import os
import posixpath
import shutil
from abc import ABC, abstractmethod
from uuid import uuid4
from io import BytesIO
from typing import BinaryIO

from app.config import settings
from loguru import logger


# ── 本地存储目录（仅在 storage_type=local 时使用） ──
LOCAL_UPLOAD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "uploads"))


# ── 存储后端抽象基类 ──────────────────────────────

class StorageBackend(ABC):
    """存储后端抽象基类"""
    
    @abstractmethod
    def save_file(self, file_content: bytes, filename: str, category: str) -> dict:
        """保存文件，返回 {file_path, file_name, file_size}"""
        pass
    
    @abstractmethod
    def save_file_stream(self, file_stream: BinaryIO, filename: str, category: str, file_size: int = 0) -> dict:
        """从文件流保存文件，返回 {file_path, file_name, file_size}
        
        用于大文件上传，避免一次性读取到内存
        """
        pass
    
    @abstractmethod
    def delete_file(self, file_path: str) -> bool:
        """删除文件，返回是否成功"""
        pass
    
    @abstractmethod
    def get_file(self, file_path: str) -> bytes:
        """读取文件内容，返回字节数据"""
        pass


# ── SFTP 存储后端 ─────────────────────────────────

class SFTPStorageBackend(StorageBackend):
    """SFTP 存储后端"""
    
    def _connect(self):
        """建立 SFTP 连接"""
        import paramiko
        
        transport = paramiko.Transport((settings.sftp_host, settings.sftp_port))
        # 在 connect 之前设置 socket 超时，影响 TCP 连接建立
        transport.sock.settimeout(120)
        transport.set_keepalive(30)  # 每30秒发送心跳包，保持连接
        # 注意：paramiko 4.0 Transport.connect() 不支持 timeout 参数
        transport.connect(
            username=settings.sftp_username,
            password=settings.sftp_password,
        )
        # connect 之后再次设置，确保后续 SFTP 读写操作也有超时保护
        transport.sock.settimeout(120)
        client = paramiko.SFTPClient.from_transport(transport)
        return transport, client
    
    def _ensure_dirs(self, client, path: str) -> None:
        """递归创建远程目录"""
        current = ""
        for part in path.strip("/").split("/"):
            current = f"{current}/{part}"
            try:
                client.stat(current)
            except FileNotFoundError:
                client.mkdir(current)
    
    def save_file(self, file_content: bytes, filename: str, category: str) -> dict:
        relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        remote_path = posixpath.join(settings.sftp_base_path, relative_path)
        transport, client = self._connect()
        try:
            self._ensure_dirs(client, posixpath.dirname(remote_path))
            with client.file(remote_path, "wb") as f:
                f.write(file_content)
        finally:
            client.close()
            transport.close()
        return {"file_path": relative_path, "file_name": filename, "file_size": len(file_content)}
    
    def save_file_stream(self, file_stream: BinaryIO, filename: str, category: str, file_size: int = 0) -> dict:
        """从文件流保存到 SFTP，支持大文件"""
        relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        remote_path = posixpath.join(settings.sftp_base_path, relative_path)
        transport, client = self._connect()
        try:
            self._ensure_dirs(client, posixpath.dirname(remote_path))
            with client.file(remote_path, "wb") as f:
                # 分块读取并写入，避免内存问题
                chunk_size = 262144  # 256KB
                total_size = 0
                while True:
                    chunk = file_stream.read(chunk_size)
                    if not chunk:
                        break
                    f.write(chunk)
                    total_size += len(chunk)
        finally:
            client.close()
            transport.close()
        return {"file_path": relative_path, "file_name": filename, "file_size": total_size}
    
    def delete_file(self, file_path: str) -> bool:
        remote_path = posixpath.join(settings.sftp_base_path, file_path)
        transport, client = self._connect()
        try:
            try:
                client.remove(remote_path)
                return True
            except FileNotFoundError:
                return False
        finally:
            client.close()
            transport.close()
    
    def get_file(self, file_path: str) -> bytes:
        remote_path = posixpath.join(settings.sftp_base_path, file_path)
        transport, client = self._connect()
        try:
            with client.file(remote_path, "rb") as f:
                return f.read()
        finally:
            client.close()
            transport.close()


# ── FTP 存储后端（vsftpd） ────────────────────────

class FTPStorageBackend(StorageBackend):
    """FTP (vsftpd) 存储后端"""
    
    def _connect(self):
        """建立 FTP 连接"""
        from ftplib import FTP
        ftp = FTP()
        ftp.connect(settings.ftp_host, settings.ftp_port)
        ftp.login(settings.ftp_username, settings.ftp_password)
        if settings.ftp_passive_mode:
            ftp.set_pasv(True)
        return ftp
    
    def _ensure_dirs(self, ftp, path: str) -> None:
        """FTP 目录递归创建"""
        current = ""
        for part in path.strip("/").split("/"):
            if not part:
                continue
            current = f"{current}/{part}"
            try:
                ftp.cwd(current)
            except Exception:
                ftp.mkd(current)
                ftp.cwd(current)
        # 回到根目录
        ftp.cwd("/")
    
    def save_file(self, file_content: bytes, filename: str, category: str) -> dict:
        relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        remote_path = posixpath.join(settings.ftp_base_path, relative_path)
        
        ftp = self._connect()
        try:
            # 确保目录存在
            dir_path = posixpath.dirname(remote_path)
            self._ensure_dirs(ftp, dir_path)
            
            # 上传文件
            ftp.cwd(dir_path)
            file_obj = BytesIO(file_content)
            ftp.storbinary(f"STOR {posixpath.basename(remote_path)}", file_obj)
        finally:
            ftp.quit()
        
        return {"file_path": relative_path, "file_name": filename, "file_size": len(file_content)}
    
    def save_file_stream(self, file_stream: BinaryIO, filename: str, category: str, file_size: int = 0) -> dict:
        """从文件流保存到 FTP，支持大文件"""
        relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        remote_path = posixpath.join(settings.ftp_base_path, relative_path)
        
        ftp = self._connect()
        try:
            # 确保目录存在
            dir_path = posixpath.dirname(remote_path)
            self._ensure_dirs(ftp, dir_path)
            
            # 上传文件
            ftp.cwd(dir_path)
            ftp.storbinary(f"STOR {posixpath.basename(remote_path)}", file_stream)
        finally:
            ftp.quit()
        
        return {"file_path": relative_path, "file_name": filename, "file_size": file_size}
    
    def delete_file(self, file_path: str) -> bool:
        remote_path = posixpath.join(settings.ftp_base_path, file_path)
        ftp = self._connect()
        try:
            try:
                ftp.delete(remote_path)
                return True
            except Exception:
                return False
        finally:
            ftp.quit()
    
    def get_file(self, file_path: str) -> bytes:
        remote_path = posixpath.join(settings.ftp_base_path, file_path)
        
        ftp = self._connect()
        try:
            file_obj = BytesIO()
            ftp.retrbinary(f"RETR {remote_path}", file_obj.write)
            return file_obj.getvalue()
        finally:
            ftp.quit()


# ── 本地存储后端 ──────────────────────────────────

class LocalStorageBackend(StorageBackend):
    """本地存储后端（开发/降级使用）"""
    
    def __init__(self):
        self.base_dir = LOCAL_UPLOAD_DIR
    
    def _get_local_path(self, file_path: str) -> str:
        """将相对路径转换为本地绝对路径"""
        return os.path.join(self.base_dir, *file_path.replace("/", os.sep).split(os.sep))
    
    def save_file(self, file_content: bytes, filename: str, category: str) -> dict:
        relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        local_path = self._get_local_path(relative_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        with open(local_path, "wb") as f:
            f.write(file_content)
        return {"file_path": relative_path, "file_name": filename, "file_size": len(file_content)}
    
    def save_file_stream(self, file_stream: BinaryIO, filename: str, category: str, file_size: int = 0) -> dict:
        """从文件流保存到本地，支持大文件"""
        relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        local_path = self._get_local_path(relative_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)
        
        total_size = 0
        with open(local_path, "wb") as f:
            # 分块复制，避免内存问题
            chunk_size = 262144  # 256KB
            while True:
                chunk = file_stream.read(chunk_size)
                if not chunk:
                    break
                f.write(chunk)
                total_size += len(chunk)
        
        return {"file_path": relative_path, "file_name": filename, "file_size": total_size}
    
    def delete_file(self, file_path: str) -> bool:
        local_path = self._get_local_path(file_path)
        try:
            os.remove(local_path)
            return True
        except FileNotFoundError:
            return False
    
    def get_file(self, file_path: str) -> bytes:
        local_path = self._get_local_path(file_path)
        with open(local_path, "rb") as f:
            return f.read()


# ── 统一存储服务（策略模式） ──────────────────────

class StorageService:
    """统一存储服务（策略模式）
    
    根据配置项 storage_type 自动选择存储后端：
    - sftp: SFTPStorageBackend
    - ftp:  FTPStorageBackend (vsftpd)
    - local: LocalStorageBackend
    """
    
    def __init__(self):
        self.backend = self._init_backend()
    
    def _init_backend(self) -> StorageBackend:
        """根据配置初始化存储后端"""
        # 向后兼容：如果 storage_type 未设置但 sftp_enabled=True，默认使用 sftp
        storage_type = settings.storage_type.lower()
        if storage_type == "sftp" and not settings.sftp_enabled:
            storage_type = "local"
            logger.warning("sftp_enabled=False，降级到 local 存储")
        
        logger.info(f"初始化存储后端: {storage_type}")
        
        if storage_type == "sftp":
            if not settings.sftp_host:
                raise ValueError("SFTP 模式需要配置 SFTP_HOST")
            return SFTPStorageBackend()
        elif storage_type == "ftp":
            if not settings.ftp_host:
                raise ValueError("FTP 模式需要配置 FTP_HOST")
            return FTPStorageBackend()
        elif storage_type == "local":
            return LocalStorageBackend()
        else:
            raise ValueError(f"不支持的存储类型: {storage_type}，可选值: sftp, ftp, local")
    
    def save_file(self, file_content: bytes, filename: str, category: str) -> dict:
        """保存文件"""
        return self.backend.save_file(file_content, filename, category)
    
    def save_file_stream(self, file_stream: BinaryIO, filename: str, category: str, file_size: int = 0) -> dict:
        """从文件流保存文件，支持大文件"""
        return self.backend.save_file_stream(file_stream, filename, category, file_size)
    
    def delete_file(self, file_path: str) -> bool:
        """删除文件"""
        return self.backend.delete_file(file_path)
    
    def get_file(self, file_path: str) -> bytes:
        """读取文件内容"""
        return self.backend.get_file(file_path)
    
    def get_file_url(self, file_path: str) -> str:
        """生成文件可访问 URL（通过统一下载接口）"""
        from urllib.parse import quote as url_quote
        return f"/api/v1/attachments/download?path={url_quote(file_path, safe='')}&inline=1"


# 全局单例
storage_service = StorageService()
