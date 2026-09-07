# FTP URL 加密架构方案

## 🎯 两种加密方案对比

### 方案A：数据库存储时加密（推荐）✅

**架构流程：**
```
文件上传 → 生成URL → 【加密】→ 存入数据库 → 所有输出自动加密
                                ↓
                        C2 XML/SOAP/API 直接使用加密URL
```

**实现要点：**
1. 在文件上传服务层（`storage.py`）加密URL
2. 数据库中的 `file_path` 字段存储加密后的URL
3. C2 XML、SOAP、API等直接读取加密URL，无需额外处理

**优势：**
- ✅ 数据源头加密，数据库无明文
- ✅ 一次加密，全局生效
- ✅ C2 XML生成逻辑简化（无需判断加密）
- ✅ 安全性更高（即使数据库泄露也无法获取原始URL）

**劣势：**
- ⚠️ 需要修改存储层代码
- ⚠️ 需要数据迁移（已有明文数据需加密）
- ⚠️ 调试时需要手动解密查看

---

### 方案B：C2 XML生成时加密（已实现）

**架构流程：**
```
文件上传 → 生成URL → 存入数据库（明文）→ C2 XML/SOAP生成时【加密】
```

**实现要点：**
1. 数据库存储明文URL
2. 在每个XML生成器中判断和加密URL
3. 需要在多个位置重复处理（Picture、Movie、其他对象）

**优势：**
- ✅ 数据库保持明文，便于调试

**劣势：**
- ❌ 数据库存在明文URL泄露风险
- ❌ 需要在多处重复判断和加密
- ❌ 代码分散，维护成本高
- ❌ 容易遗漏（忘记加密某个输出位置）

---

## 📋 推荐方案：混合加密（数据库 + 输出层）

**最佳实践：**
```
1. 数据库存储时加密（保护静态数据）
2. 输出层解密后使用（业务逻辑需要明文）
3. API接口返回时再加密（保护传输安全）
```

**实现步骤：**

### 步骤1：文件上传时加密

```python
# backend/app/internal/cms_biz_orchestration/services/storage.py

from app.common.crypto import encrypt_storage_url

class StorageService:
    async def upload_file(self, file_content: bytes, file_name: str) -> str:
        """上传文件到SFTP/FTP"""
        # 1. 上传文件到远程服务器
        remote_path = await self._upload_to_ftp(file_content, file_name)
        
        # 2. 生成完整URL（包含凭证）
        file_url = self._build_full_url(remote_path)
        # file_url = "sftp://user:pass@192.168.10.91:22/cms/pictures/xxx.jpg"
        
        # 3. 【关键】加密URL后返回
        encrypted_url = encrypt_storage_url(file_url)
        return encrypted_url
```

### 步骤2：数据库读取时解密

```python
# backend/app/internal/cms_biz_metada/models/basic.py

from app.common.crypto import decrypt_storage_url

class Picture(Base):
    __tablename__ = "picture"
    
    file_path = Column(String)  # 数据库中存储加密URL
    
    @property
    def file_path_decrypted(self) -> str:
        """获取解密后的文件路径（用于业务逻辑）"""
        return decrypt_storage_url(self.file_path)
```

### 步骤3：C2 XML直接使用（无需额外处理）

```python
# backend/app/soap/c2/objects.py

def build_picture_object(pic: Picture, action: Action) -> Element:
    obj = _make_object(ElementType.PICTURE, action, pic.id)
    
    # 直接使用加密URL（数据库中已加密）
    # 无需判断协议，无需调用加密函数
    _add_property(obj, "FileURL", pic.file_path)
    
    return obj
```

### 步骤4：API接口返回时保持加密

```python
# backend/app/routers/picture.py

@router.get("/pictures/{picture_id}")
async def get_picture(picture_id: int, db: AsyncSession = Depends(get_db)):
    pic = await picture_repository.get_picture(db, picture_id)
    
    # 返回加密URL（安全）
    return {
        "id": pic.id,
        "file_path": pic.file_path,  # 已加密
        "file_name": pic.file_name,
    }
```

---

## 🔧 需要修改的文件清单

### 1. 存储层（加密）
- `backend/app/internal/cms_biz_orchestration/services/storage.py`
  - `upload_file()` 方法返回加密URL
  - `_build_full_url()` 方法生成完整URL后加密

### 2. 数据模型层（解密属性）
- `backend/app/internal/cms_biz_metada/models/basic.py`
  - `Picture.file_path_decrypted` 属性
- `backend/app/internal/cms_biz_orchestration/models/movie.py`
  - `Movie.file_path_decrypted` 属性

### 3. C2 XML生成（移除加密逻辑）
- `backend/app/soap/xml_generator.py`
  - 移除第51-56行的加密判断
  - 直接使用 `file_url`（已加密）
- `backend/app/soap/c2/objects.py`
  - `_build_file_url()` 直接返回（不处理）

### 4. 数据迁移脚本
- `backend/scripts/migrate_encrypt_file_paths.py`
  - 扫描所有 `file_path` 字段
  - 加密未加密的记录
  - 验证加密结果

---

## 📊 数据流对比

### 当前（方案B）
```
数据库 → 明文URL → C2 XML生成时加密 → 输出加密URL
                    ↑
                 每次都要判断和加密
```

### 推荐（方案A）
```
数据库 → 加密URL → C2 XML直接使用 → 输出加密URL
         ↑
     存储时已加密，无需额外处理
```

---

## 🎯 实施建议

### 第一阶段：存储层加密
1. ✅ 创建 `StorageURLCryptoService`
2. ⏳ 修改 `storage.py` 的 `upload_file()` 方法
3. ⏳ 测试文件上传后数据库存储加密URL

### 第二阶段：数据模型层解密
1. ⏳ 在 `Picture` 和 `Movie` 模型添加 `file_path_decrypted` 属性
2. ⏳ 修改需要明文URL的业务逻辑（如文件下载、预览）

### 第三阶段：移除输出层加密
1. ⏳ 移除 `xml_generator.py` 中的加密逻辑
2. ⏳ 移除 `objects.py` 中的加密判断
3. ⏳ 验证C2 XML输出仍然正确

### 第四阶段：数据迁移
1. ⏳ 编写数据迁移脚本
2. ⏳ 在测试环境验证
3. ⏳ 生产环境执行迁移

---

## ❓ 需要你确认的问题

1. **是否采用方案A（数据库存储时加密）？**
   - 推荐：是
   - 原因：更安全、代码更简洁

2. **是否需要我实现存储层加密？**
   - 如果是：我会修改 `storage.py` 并在上传时加密
   - 如果否：保持当前方案B（C2 XML生成时加密）

3. **是否需要数据迁移脚本？**
   - 如果采用方案A：需要
   - 如果保持方案B：不需要

4. **如何处理已有数据？**
   - 立即迁移：运行脚本加密所有现有URL
   - 渐进迁移：新数据加密，旧数据保持明文（需要兼容处理）

---

## 📝 总结

**你的建议非常好！** 数据库存储时加密是更优的架构方案。

**优势：**
- 数据更安全
- 代码更简洁
- 维护更简单

**实施难度：**
- 中等（需要修改存储层和数据模型）
- 需要数据迁移

**建议：**
采用方案A，分阶段实施，先完成新数据的加密存储，再逐步迁移旧数据。

请确认是否采用此方案，我会继续实现！
