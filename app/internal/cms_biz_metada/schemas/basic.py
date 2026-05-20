from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator


def _validate_dimension_or_size(v: int) -> int:
    """验证宽度/高度/文件大小：必须是正整数或 -1（-1 表示不限制）"""
    if v != -1 and v <= 0:
        raise ValueError("Value must be a positive integer or -1 (unlimited)")
    return v


# ---------- Tag ----------

class TagListItem(BaseModel):
    id: int
    name: str
    language: str
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class TagCreate(BaseModel):
    name: str = Field(..., max_length=100)
    language: str = Field(..., max_length=50)


class TagUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=50)


# ---------- CustomTag ----------

class CustomTagListItem(BaseModel):
    id: int
    name: str
    language: str
    is_deleted: bool = False
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class CustomTagCreate(BaseModel):
    name: str = Field(..., max_length=100)


class CustomTagUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)


# ---------- Genre ----------

class GenreListItem(BaseModel):
    id: int
    name: str
    language: str
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class GenreCreate(BaseModel):
    name: str = Field(..., max_length=100)
    language: str = Field(..., max_length=50)


class GenreUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    language: str | None = Field(None, max_length=50)


# ---------- ContentType ----------

class ContentTypeListItem(BaseModel):
    id: int
    name: str
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class ContentTypeCreate(BaseModel):
    name: str = Field(..., max_length=100)


class ContentTypeUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)


# ---------- PosterSize ----------

class PosterSizeListItem(BaseModel):
    id: int
    name: str
    belongings: list[str] = []
    extensions: list[str] = []
    width: int
    height: int
    max_file_size_kb: int
    mapping_type: int | None = None
    mandatory: bool
    aspect_ratio: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm(cls, obj: object) -> "PosterSizeListItem":
        from ..models.basic import PosterSize
        ps: PosterSize = obj  # type: ignore[assignment]
        w, h = ps.width, ps.height
        aspect_ratio: str | None = None
        if w > 0 and h > 0:
            from math import gcd
            g = gcd(w, h)
            aspect_ratio = f"{w // g}:{h // g}"
        return cls(
            id=ps.id,
            name=ps.name,
            belongings=[b.belonging for b in ps.belongings],
            extensions=[e.extension for e in ps.extensions],
            width=w,
            height=h,
            max_file_size_kb=ps.max_file_size_kb,
            mapping_type=ps.mapping_type,
            mandatory=ps.mandatory,
            aspect_ratio=aspect_ratio,
            created_at=ps.created_at,
        )


class PosterSizeCreate(BaseModel):
    name: str = Field(..., max_length=100)
    belongings: list[str] = []
    extensions: list[str] = []
    width: int = -1
    height: int = -1
    max_file_size_kb: int = -1
    mapping_type: int = Field(..., description="C2规范生成mappings时需要传入的Mapping type值")
    mandatory: bool = False

    @field_validator("width", "height", "max_file_size_kb")
    @classmethod
    def validate_dimension(cls, v: int) -> int:
        return _validate_dimension_or_size(v)


class PosterSizeUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    belongings: list[str] | None = None
    extensions: list[str] | None = None
    width: int | None = None
    height: int | None = None
    max_file_size_kb: int | None = None
    mapping_type: int | None = None
    mandatory: bool | None = None

    @field_validator("width", "height", "max_file_size_kb")
    @classmethod
    def validate_dimension(cls, v: int | None) -> int | None:
        if v is None:
            return None
        return _validate_dimension_or_size(v)


# ---------- Category ----------

class CategoryListItem(BaseModel):
    id: int
    parent_id: int | None = None
    platform: str
    name: str
    sequence: int = 0
    category_type: str | None = None
    vod_count: int = 0
    description: str | None = None
    jump_category_code: str | None = None
    status: int = 0
    ingest_status: str = "failure"
    children: list["CategoryListItem"] = []
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class CategoryCreate(BaseModel):
    parent_id: int | None = None
    platform: str = Field(..., max_length=50)
    name: str = Field(..., max_length=100)
    sequence: int = 0
    category_type: str | None = Field(None, max_length=100)
    vod_count: int = 0
    description: str | None = Field(None, max_length=500)
    jump_category_code: str | None = Field(None, max_length=100)
    status: int = 0


class CategoryUpdate(BaseModel):
    parent_id: int | None = None
    platform: str | None = Field(None, max_length=50)
    name: str | None = Field(None, max_length=100)
    sequence: int | None = None
    category_type: str | None = Field(None, max_length=100)
    vod_count: int | None = None
    description: str | None = Field(None, max_length=500)
    jump_category_code: str | None = Field(None, max_length=100)
    status: int | None = None
    ingest_status: str | None = None


CategoryListItem.model_rebuild()


# ---------- Cast ----------

class CastListItem(BaseModel):
    id: int
    name: str
    description: str | None = None
    ingest_status: str = "failure"
    status: int = 1
    poster_url: str | None = None
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)


class CastCreate(BaseModel):
    name: str = Field(..., max_length=100)
    description: str | None = Field(None, max_length=500)
    status: int = 1


class CastUpdate(BaseModel):
    name: str | None = Field(None, max_length=100)
    description: str | None = Field(None, max_length=500)
    ingest_status: str | None = None
    status: int | None = None


# ---------- CustomField ----------

class CustomFieldOptionItem(BaseModel):
    id: int | None = None
    code: str = Field(..., max_length=100)
    names: dict[str, str] = {}
    sort_order: int = 0

    model_config = ConfigDict(from_attributes=True)


class CustomFieldListItem(BaseModel):
    id: int
    field_name: str
    field_code: str
    field_type: str
    mandatory: bool
    multi_language: bool
    tip: str | None = None
    belongings: list[str] = []
    options: list[CustomFieldOptionItem] = []
    created_at: datetime | None = None

    model_config = ConfigDict(from_attributes=True)

    @classmethod
    def from_orm(cls, obj: object) -> "CustomFieldListItem":
        from ..models.basic import CustomField
        cf: CustomField = obj  # type: ignore[assignment]
        return cls(
            id=cf.id,
            field_name=cf.field_name,
            field_code=cf.field_code,
            field_type=cf.field_type,
            mandatory=cf.mandatory,
            multi_language=cf.multi_language,
            tip=cf.tip,
            belongings=[b.belonging for b in cf.belongings],
            options=[
                CustomFieldOptionItem(id=o.id, code=o.code, names=o.names or {}, sort_order=o.sort_order)
                for o in cf.options
            ],
            created_at=cf.created_at,
        )


class CustomFieldCreate(BaseModel):
    field_name: str = Field(..., max_length=100)
    field_type: str = Field(..., max_length=50)
    belongings: list[str] = []
    mandatory: bool = True
    multi_language: bool = False
    tip: str | None = Field(None, max_length=500)
    options: list[CustomFieldOptionItem] = []


class CustomFieldUpdate(BaseModel):
    field_name: str | None = Field(None, max_length=100)
    field_type: str | None = Field(None, max_length=50)
    belongings: list[str] | None = None
    mandatory: bool | None = None
    multi_language: bool | None = None
    tip: str | None = Field(None, max_length=500)
    options: list[CustomFieldOptionItem] | None = None


# ---------- EntityFieldValue / EntityI18n ----------

class EntityFieldValueItem(BaseModel):
    custom_field_id: int
    value: str | None = None

    model_config = ConfigDict(from_attributes=True)


class EntityI18nItem(BaseModel):
    language: str
    field_name: str
    value: str | None = None

    model_config = ConfigDict(from_attributes=True)


class EntityFieldValuesPayload(BaseModel):
    """批量保存某实体的自定义字段值"""
    values: list[EntityFieldValueItem] = []


class EntityI18nPayload(BaseModel):
    """批量保存某实体的多语言值（一次传一种语言的所有字段）"""
    language: str
    fields: dict[str, str | None] = {}  # field_name -> value
