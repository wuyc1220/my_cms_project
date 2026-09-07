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
from app.common.crypto import encrypt_storage_url, decrypt_storage_url
from app.common.core.i18n import get_msg
from loguru import logger


# ── 本地存储目录（仅在 storage_type=local 时使用） ──
LOCAL_UPLOAD_DIR = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "uploads"))


# ── 存储后端抽象基类 ──────────────────────────────

class StorageBackend(ABC):
    """存储后端抽象基类"""
    
    @abstractmethod
    def save_file(self, file_content: bytes, filename: str, category: str, skip_uuid_prefix: bool = False) -> dict:
        """保存文件，返回 {file_path, file_name, file_size}
        
        Args:
            skip_uuid_prefix: 为 True 时不在文件名前加 UUID 前缀，直接使用 filename 作为相对路径的一部分
        """
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

    def __init__(self):
        self._base_path_verified = False

    def _verify_base_path(self, client) -> None:
        """验证 SFTP 基础路径是否存在"""
        if self._base_path_verified:
            return
        base = settings.file_base_path
        try:
            client.stat(base)
            self._base_path_verified = True
        except FileNotFoundError:
            raise FileNotFoundError(
                f"SFTP 基础路径 '{base}' 在服务器上不存在。"
                f"请手动创建该目录并确保用户 {settings.file_username} 有写入权限"
            )
        except PermissionError as e:
            raise PermissionError(
                f"无法访问 SFTP 基础路径 '{base}': {e}. "
                f"请检查该目录的权限设置"
            )

    def _connect(self):
        """建立 SFTP 连接"""
        import paramiko
        
        transport = paramiko.Transport((settings.file_host, settings.file_port))
        # 在 connect 之前设置 socket 超时，影响 TCP 连接建立
        transport.sock.settimeout(120)
        transport.set_keepalive(30)  # 每30秒发送心跳包，保持连接
        # 注意：paramiko 4.0 Transport.connect() 不支持 timeout 参数
        transport.connect(
            username=settings.file_username,
            password=settings.file_password,
        )
        # connect 之后再次设置，确保后续 SFTP 读写操作也有超时保护
        transport.sock.settimeout(120)
        client = paramiko.SFTPClient.from_transport(transport)
        return transport, client
    
    def _ensure_dirs(self, client, path: str) -> None:
        """递归创建远程目录"""
        current = ""
        for part in path.strip("/").split("/"):
            if not part:
                continue
            current = f"{current}/{part}"
            try:
                client.stat(current)
            except FileNotFoundError:
                try:
                    client.mkdir(current)
                except PermissionError as e:
                    raise PermissionError(
                        f"无法在 SFTP 服务器上创建目录 '{current}': {e}. "
                        f"请确保该目录存在且用户 {settings.file_username} 有写入权限"
                    ) from e
            except PermissionError as e:
                raise PermissionError(
                    f"无法访问 SFTP 服务器目录 '{current}': {e}. "
                    f"请检查该目录的权限设置"
                ) from e
    
    def save_file(self, file_content: bytes, filename: str, category: str, skip_uuid_prefix: bool = False) -> dict:
        if skip_uuid_prefix:
            relative_path = posixpath.join(category, filename)
        else:
            relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        remote_path = posixpath.join(settings.file_base_path, relative_path)
        transport, client = self._connect()
        try:
            self._verify_base_path(client)
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
        remote_path = posixpath.join(settings.file_base_path, relative_path)
        transport, client = self._connect()
        try:
            self._verify_base_path(client)
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
        remote_path = posixpath.join(settings.file_base_path, file_path)
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
        remote_path = posixpath.join(settings.file_base_path, file_path)
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
        ftp.connect(settings.file_host, settings.file_port)
        ftp.login(settings.file_username, settings.file_password)
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
    
    def save_file(self, file_content: bytes, filename: str, category: str, skip_uuid_prefix: bool = False) -> dict:
        if skip_uuid_prefix:
            relative_path = posixpath.join(category, filename)
        else:
            relative_path = posixpath.join(category, f"{uuid4().hex}_{filename}")
        remote_path = posixpath.join(settings.file_base_path, relative_path)
        
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
        remote_path = posixpath.join(settings.file_base_path, relative_path)
        
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
        remote_path = posixpath.join(settings.file_base_path, file_path)
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
        remote_path = posixpath.join(settings.file_base_path, file_path)
        
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
    
    def save_file(self, file_content: bytes, filename: str, category: str, skip_uuid_prefix: bool = False) -> dict:
        if skip_uuid_prefix:
            relative_path = posixpath.join(category, filename)
        else:
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
        # 根据 storage_type 初始化对应的存储后端
        storage_type = settings.storage_type.lower()
        
        logger.info(f"初始化存储后端: {storage_type}")
        
        if storage_type == "sftp":
            if not settings.file_host:
                raise ValueError(get_msg("STORAGE_SFTP_REQUIRES_HOST"))
            return SFTPStorageBackend()
        elif storage_type == "ftp":
            if not settings.file_host:
                raise ValueError(get_msg("STORAGE_FTP_REQUIRES_HOST"))
            return FTPStorageBackend()
        elif storage_type == "local":
            return LocalStorageBackend()
        else:
            raise ValueError(get_msg("STORAGE_TYPE_UNSUPPORTED", type=storage_type))
    
    def _to_relative_path(self, file_path: str) -> str:
        """
        将完整存储 URL 还原为相对路径；若已是相对路径则原样返回。
        
        支持三种输入：
        1. 相对路径（旧数据）: pictures/2024/06/09/image.jpg → 直接返回
        2. 明文完整URL: sftp://user:pass@host/cms/pictures/xxx.jpg → 提取相对路径
        3. 加密URL（新数据）: XGZ6eagNc92z5xkQrGGqd2SOZ... → 先解密再提取
        """
        # 【关键】先尝试解密加密URL（智能兼容，decrypt_storage_url内部已判断）
        resolved_path = decrypt_storage_url(file_path)
        
        # 如果不是完整URL协议头，直接返回（已经是相对路径）
        if not resolved_path.startswith(("http://", "https://", "ftp://", "sftp://")):
            return resolved_path
        
        # 从完整URL中提取相对路径
        from urllib.parse import urlparse
        parsed = urlparse(resolved_path)
        path = parsed.path.lstrip("/")
        base = ""
        if settings.storage_type in ("sftp", "ftp") and settings.file_base_path:
            base = settings.file_base_path.strip("/")
        if base and path.startswith(base + "/"):
            path = path[len(base) + 1:]
        elif base and path.startswith(base):
            path = path[len(base):].lstrip("/")
        return path
    
    def get_storage_url(self, file_path: str) -> str:
        """
        将相对路径转换为完整存储 URL（sftp:// / ftp://），用于 C2 规范等场景
        
        加密控制：
        - 如果 FTP_URL_ENCRYPTION_ENABLED=true，自动加密FTP/SFTP URL
        - 如果 FTP_URL_ENCRYPTION_ENABLED=false，返回明文URL
        
        Args:
            file_path: 相对路径或已加密的URL
            
        Returns:
            完整存储URL（加密或明文，取决于配置）
        """
        # 如果已经是完整URL（加密或明文），直接返回
        if file_path.startswith(("http://", "https://", "ftp://", "sftp://")):
            return file_path
        
        # 生成完整URL
        full_url = file_path
        if settings.storage_type == "sftp" and settings.file_host:
            sftp_prefix = (
                f"sftp://{settings.file_username}:{settings.file_password}"
                f"@{settings.file_host}:{settings.file_port}"
                f"{settings.file_base_path.rstrip('/')}"
            )
            full_url = f"{sftp_prefix}/{file_path.lstrip('/')}"
        elif settings.storage_type == "ftp" and settings.file_host:
            ftp_prefix = (
                f"ftp://{settings.file_username}:{settings.file_password}"
                f"@{settings.file_host}:{settings.file_port}"
                f"{settings.file_base_path.rstrip('/')}"
            )
            full_url = f"{ftp_prefix}/{file_path.lstrip('/')}"
        
        # 【关键】自动加密FTP/SFTP URL（受开关控制）
        encrypted_url = encrypt_storage_url(full_url)
        return encrypted_url
    
    def save_file(self, file_content: bytes, filename: str, category: str, skip_uuid_prefix: bool = False) -> dict:
        """保存文件"""
        return self.backend.save_file(file_content, filename, category, skip_uuid_prefix)
    
    def save_file_stream(self, file_stream: BinaryIO, filename: str, category: str, file_size: int = 0) -> dict:
        """从文件流保存文件，支持大文件"""
        return self.backend.save_file_stream(file_stream, filename, category, file_size)
    
    def delete_file(self, file_path: str) -> bool:
        """删除文件"""
        return self.backend.delete_file(self._to_relative_path(file_path))
    
    def get_file(self, file_path: str) -> bytes:
        """
        读取文件内容。
        
        支持三种输入，每种使用正确的读取方式：
        1. 完整URL（sftp://xxx 或 ftp://xxx）→ 从URL指定的服务器直接下载（不依赖当前配置）
        2. 相对路径（pictures/xxx.jpg）→ 用当前storage_service配置读取
        3. 加密URL → 先解密再按规则1或2处理
        
        这样即使以后换了FTP服务器，存着旧服务器URL的数据也能正确读取。
        """
        # 先解密（兼容：未加密的直接原样返回）
        resolved_path = decrypt_storage_url(file_path)
        
        # 如果是完整URL，从URL指向的服务器下载（用URL自身的连接信息）
        if resolved_path.startswith(("sftp://", "ftp://")):
            return self._download_from_url(resolved_path)
        
        # 如果是相对路径，用当前配置的backend读取
        return self.backend.get_file(resolved_path)

    def _download_from_url(self, url: str) -> bytes:
        """
        从完整URL直接下载文件，使用URL中的连接信息，不依赖当前配置。
        
        这样即使以后换了FTP服务器，存着旧URL的数据也能从正确的服务器读取。
        """
        from urllib.parse import urlparse
        
        parsed = urlparse(url)
        host = parsed.hostname or ""
        port = parsed.port or (22 if parsed.scheme == "sftp" else 21)
        username = parsed.username or ""
        password = parsed.password or ""
        remote_path = parsed.path or ""
        
        logger.debug(f"从URL直接下载文件 - {parsed.scheme}://{host}:{port}{remote_path}")
        
        if parsed.scheme == "sftp":
            import paramiko
            transport = paramiko.Transport((host, port))
            transport.connect(username=username, password=password)
            try:
                client = paramiko.SFTPClient.from_transport(transport)
                try:
                    with client.file(remote_path, "rb") as f:
                        return f.read()
                finally:
                    client.close()
            finally:
                transport.close()
        
        elif parsed.scheme == "ftp":
            from ftplib import FTP
            ftp = FTP()
            ftp.connect(host, port)
            ftp.login(username, password)
            try:
                file_obj = BytesIO()
                ftp.retrbinary(f"RETR {remote_path}", file_obj.write)
                return file_obj.getvalue()
            finally:
                ftp.quit()
        
        else:
            raise ValueError(get_msg("STORAGE_DOWNLOAD_UNSUPPORTED", scheme=parsed.scheme))
    
    def get_file_url(self, file_path: str, relative_path: str | None = None) -> str:
        """生成文件可访问 URL（通过统一下载接口）

        Args:
            file_path: 文件路径（可能是加密URL或相对路径）
            relative_path: 相对路径（本地上传时有值，外部FTP时为空）
        """
        from urllib.parse import quote as url_quote
        # 外部FTP场景：relative_path 为空，使用 file_path（加密URL，下载时解密）
        # 本地上传场景：relative_path 有值，直接使用
        path = relative_path if relative_path else file_path
        return f"/api/v1/attachments/download?path={url_quote(path, safe='')}&inline=1"


# 全局单例
storage_service = StorageService()
