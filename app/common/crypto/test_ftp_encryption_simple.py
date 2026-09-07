"""
FTP URL 加密解密功能快速验证脚本

不依赖pytest，直接运行验证核心功能
"""

import sys
import os

# 添加backend目录到路径
backend_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
sys.path.insert(0, backend_dir)

# 切换到backend目录以加载.env
os.chdir(backend_dir)

# 现在可以导入应用模块
from app.common.crypto.aes_gcm import AESGCMCipher
from app.common.crypto.url_encoder import CustomURLEncoder


def test_aes_gcm():
    """测试AES-256-GCM加密解密"""
    print("=" * 60)
    print("测试1: AES-256-GCM 基础加密解密")
    print("=" * 60)
    
    key = "kX9mPq2wL5vN3zT8rY6tF1cB7hJ4dR2a"
    cipher = AESGCMCipher(key)
    
    plaintext = "ftp://192.168.1.100:21/path/to/file.txt"
    print(f"明文: {plaintext}")
    
    # 加密
    encrypted = cipher.encrypt(plaintext)
    print(f"加密后长度: {len(encrypted)} 字节")
    print(f"加密数据(Hex): {encrypted.hex()}")
    
    # 解密
    decrypted = cipher.decrypt(encrypted)
    print(f"解密后: {decrypted.decode('utf-8')}")
    
    assert decrypted.decode("utf-8") == plaintext
    print("✅ 测试通过\n")


def test_base64():
    """测试Base64编码"""
    print("=" * 60)
    print("测试2: Base64 编码加密解密")
    print("=" * 60)
    
    key = "kX9mPq2wL5vN3zT8rY6tF1cB7hJ4dR2a"
    cipher = AESGCMCipher(key)
    
    plaintext = "ftp://user:pass@server.com/data/movie.mp4"
    print(f"明文: {plaintext}")
    
    # 加密并Base64
    base64_str = cipher.encrypt_to_base64(plaintext)
    print(f"Base64: {base64_str}")
    
    # 解密
    decrypted = cipher.decrypt_from_base64_str(base64_str)
    print(f"解密后: {decrypted}")
    
    assert decrypted == plaintext
    print("✅ 测试通过\n")


def test_url_encoder():
    """测试自定义URLEncode"""
    print("=" * 60)
    print("测试3: 自定义 URLEncode 编码解码")
    print("=" * 60)
    
    # 测试安全字符
    safe = "abc123.-*_"
    encoded = CustomURLEncoder.encode(safe)
    print(f"安全字符: {safe} -> {encoded}")
    assert encoded == safe
    
    # 测试空格
    space = "hello world"
    encoded = CustomURLEncoder.encode(space)
    print(f"空格编码: {space} -> {encoded}")
    assert encoded == "hello+world"
    
    # 测试特殊字符
    special = "+/="
    encoded = CustomURLEncoder.encode(special)
    print(f"特殊字符: {special} -> {encoded}")
    assert "%2B" in encoded and "%2F" in encoded and "%3D" in encoded
    
    # 测试往返
    original = "YUIzeFB6N2tMbTlxKi.4Hzw+9b9KuVdDuqp4/np7ceZIp5YM6XnO8E="
    encoded = CustomURLEncoder.encode(original)
    decoded = CustomURLEncoder.decode(encoded)
    print(f"往返测试: {original}")
    print(f"编码后: {encoded}")
    print(f"解码后: {decoded}")
    assert decoded == original
    
    print("✅ 测试通过\n")


def test_ftp_crypto_service():
    """测试FTP URL加密服务"""
    print("=" * 60)
    print("测试4: FTP URL 完整加密解密流程")
    print("=" * 60)
    
    from app.common.crypto.ftp_url_crypto import FTPURLCryptoService
    from app.config import settings
    
    # 临时启用加密
    original_enabled = settings.ftp_url_encryption_enabled
    original_key = settings.ftp_url_encryption_key
    settings.ftp_url_encryption_enabled = True
    settings.ftp_url_encryption_key = "kX9mPq2wL5vN3zT8rY6tF1cB7hJ4dR2a"
    
    crypto_service = FTPURLCryptoService()
    
    test_urls = [
        "ftp://192.168.1.100:21/path/to/file.txt",
        "ftp://user:password@server.com/data/movie.mp4",
        "ftp://10.0.0.1:2121/video/episode01.ts",
    ]
    
    for url in test_urls:
        print(f"\n原始URL: {url}")
        
        # 加密
        encrypted = crypto_service.encrypt_ftp_url(url)
        print(f"加密后: {encrypted[:50]}...")
        
        # 解密
        decrypted = crypto_service.decrypt_ftp_url(encrypted)
        print(f"解密后: {decrypted}")
        
        assert decrypted == url, f"解密失败: {decrypted} != {url}"
    
    # 恢复原始配置
    settings.ftp_url_encryption_enabled = original_enabled
    settings.ftp_url_encryption_key = original_key
    
    print("\n✅ 所有测试通过\n")


def test_encryption_disabled():
    """测试加密禁用"""
    print("=" * 60)
    print("测试5: 加密禁用时返回原文")
    print("=" * 60)
    
    from app.common.crypto.ftp_url_crypto import FTPURLCryptoService
    from app.config import settings
    
    original_enabled = settings.ftp_url_encryption_enabled
    
    # 临时禁用
    settings.ftp_url_encryption_enabled = False
    crypto_service = FTPURLCryptoService()
    
    url = "ftp://192.168.1.100/file.txt"
    encrypted = crypto_service.encrypt_ftp_url(url)
    
    print(f"原始URL: {url}")
    print(f"加密后（禁用状态）: {encrypted}")
    assert encrypted == url, "加密禁用时应返回原文"
    
    # 恢复
    settings.ftp_url_encryption_enabled = original_enabled
    print("✅ 测试通过\n")


def main():
    """运行所有测试"""
    print("\n" + "=" * 60)
    print("FTP URL 加密解密功能验证")
    print("=" * 60 + "\n")
    
    try:
        test_aes_gcm()
        test_base64()
        test_url_encoder()
        test_ftp_crypto_service()
        test_encryption_disabled()
        
        print("=" * 60)
        print("✅ 所有测试通过！")
        print("=" * 60)
        return 0
        
    except Exception as e:
        print(f"\n❌ 测试失败: {e}")
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
