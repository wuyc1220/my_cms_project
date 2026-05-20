# C2 规范待修复问题清单

## 1. Rating 缺少 type/ID XML 属性

**规范要求：**

```xml
<Property Name="Rating" type="IMDB" ID="1049413">8.8</Property>
```

**当前实现：**

```python
_add_property(obj, "Rating", meta.rating if meta else None)
```

仅输出 `<Property Name="Rating">8.8</Property>`，缺少 `type` 和 `ID` 属性。

**原因：**

`ContentMetadata.rating` 是 `String(50)` 单值字段，无法拆分出评分来源（type）和外部 ID。

**修复方案（二选一）：**

- 方案 A：在 `ContentMetadata` 模型中新增 `rating_type`（如 `"IMDB"`）和 `rating_id`（如 `"1049413"`）字段，需要数据库迁移
- 方案 B：在自定义字段中配置这两个值，从 `custom_fields` 中读取

**涉及文件：**

- `backend/app/internal/cms_biz_orchestration/models/content_metadata.py` — 模型新增字段（方案 A）
- `backend/app/soap/c2/objects.py` — `build_program_object` / `build_series_object` 中 Rating 输出逻辑
- 数据库迁移文件（方案 A）

**优先级：** 中
