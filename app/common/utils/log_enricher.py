import json
from typing import Any

from loguru import logger
from sqlalchemy import inspect as sa_inspect, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.constants.log_field_mapping import (
    DICT_CODE_FIELDS,
    ENTITY_TABLE_MAP,
    FOREIGN_KEY_FIELDS,
    SENSITIVE_FIELDS,
    STATUS_ENUM_FIELDS,
    STATUS_ENUM_VALUES,
    EnrichFieldType,
)


async def enrich_entity(
    db: AsyncSession,
    entity_type: str,
    data: dict[str, Any],
) -> dict[str, Any]:
    enriched = dict(data)

    fk_mappings = FOREIGN_KEY_FIELDS.get(entity_type, [])
    for mapping in fk_mappings:
        fk_value = enriched.get(mapping.field_name)
        if fk_value is None:
            continue
        try:
            if mapping.enrich_type == EnrichFieldType.FOREIGN_KEY_ARRAY:
                if isinstance(fk_value, list):
                    names = []
                    for fk_id in fk_value:
                        name = await _resolve_foreign_key(db, mapping.source, fk_id)
                        names.append(name)
                    enriched[mapping.target_name] = names
                else:
                    name = await _resolve_foreign_key(db, mapping.source, fk_value)
                    enriched[mapping.target_name] = [name]
            else:
                name = await _resolve_foreign_key(db, mapping.source, fk_value)
                enriched[mapping.target_name] = name
        except Exception as exc:
            logger.warning("enrich_entity FK resolve failed: %s.%s=%s: %s", entity_type, mapping.field_name, fk_value, exc)

    dict_mappings = DICT_CODE_FIELDS.get(entity_type, [])
    if dict_mappings:
        dict_cache = await _load_dict_cache(db)
        for mapping in dict_mappings:
            code_value = enriched.get(mapping.field_name)
            if code_value is None:
                continue
            dict_map = dict_cache.get(mapping.source, {})
            if mapping.enrich_type == EnrichFieldType.DICT_CODE_ARRAY:
                if isinstance(code_value, list):
                    enriched[mapping.target_name] = [dict_map.get(str(v), str(v)) for v in code_value]
                else:
                    enriched[mapping.target_name] = [dict_map.get(str(code_value), str(code_value))]
            else:
                enriched[mapping.target_name] = dict_map.get(str(code_value), str(code_value))

    status_mappings = STATUS_ENUM_FIELDS.get(entity_type, [])
    for mapping in status_mappings:
        status_value = enriched.get(mapping.field_name)
        if status_value is None:
            continue
        enum_map = STATUS_ENUM_VALUES.get(mapping.source, {})
        label_key = enum_map.get(status_value)
        if label_key is not None:
            enriched[mapping.target_name] = label_key

    return enriched


async def prepare_log_values(
    db: AsyncSession,
    entity_type: str,
    old_data: dict[str, Any] | None,
    new_data: dict[str, Any] | None,
) -> tuple[str | None, str | None, str | None]:
    from app.common.utils import compute_diff_json

    if old_data is None and new_data is not None:
        enriched_new = await enrich_entity(db, entity_type, new_data)
        return (
            None,
            json.dumps(enriched_new, ensure_ascii=False, default=str),
            json.dumps(new_data, ensure_ascii=False, default=str),
        )
    elif old_data is not None and new_data is None:
        enriched_old = await enrich_entity(db, entity_type, old_data)
        return (
            json.dumps(enriched_old, ensure_ascii=False, default=str),
            None,
            json.dumps(old_data, ensure_ascii=False, default=str),
        )
    elif old_data is not None and new_data is not None:
        raw_prev_diff, raw_new_diff = compute_diff_json(
            json.dumps(old_data, ensure_ascii=False, default=str),
            json.dumps(new_data, ensure_ascii=False, default=str),
        )
        enriched_old = await enrich_entity(db, entity_type, old_data)
        enriched_new = await enrich_entity(db, entity_type, new_data)
        prev_diff, new_diff = compute_diff_json(
            json.dumps(enriched_old, ensure_ascii=False, default=str),
            json.dumps(enriched_new, ensure_ascii=False, default=str),
        )
        return (prev_diff, new_diff, raw_new_diff)
    else:
        return (None, None, None)


def orm_to_dict(obj: Any, entity_type: str | None = None) -> dict[str, Any]:
    try:
        mapper = sa_inspect(type(obj))
        data = {col.key: getattr(obj, col.key) for col in mapper.column_attrs}
    except Exception:
        pass
    else:
        if entity_type and entity_type in SENSITIVE_FIELDS:
            for field in SENSITIVE_FIELDS[entity_type]:
                data.pop(field, None)
        return data
    if hasattr(obj, "model_dump"):
        data = obj.model_dump()
    elif hasattr(obj, "__dict__"):
        data = {k: v for k, v in obj.__dict__.items() if not k.startswith("_")}
    else:
        return {}
    if entity_type and entity_type in SENSITIVE_FIELDS:
        for field in SENSITIVE_FIELDS[entity_type]:
            data.pop(field, None)
    return data


async def _resolve_foreign_key(
    db: AsyncSession,
    source_entity: str,
    fk_id: int,
) -> str:
    from app.database import Base
    table_name = ENTITY_TABLE_MAP.get(source_entity, source_entity)
    model_cls = None
    for mapper in Base.registry.mappers:
        cls = mapper.class_
        if getattr(cls, "__tablename__", None) == table_name:
            model_cls = cls
            break
    if model_cls is None:
        return str(fk_id)
    result = await db.execute(select(model_cls).where(model_cls.id == fk_id))
    obj = result.scalar_one_or_none()
    if obj is None:
        return str(fk_id)
    return getattr(obj, "name", str(fk_id))


async def _load_dict_cache(db: AsyncSession) -> dict[str, dict[str, str]]:
    from app.internal.cms_biz_system.models.dict import DictNode
    result = await db.execute(
        select(DictNode).where(DictNode.is_deleted.is_(False))
    )
    nodes = result.scalars().all()
    cache: dict[str, dict[str, str]] = {}
    for node in nodes:
        parent_code = None
        if node.parent_id:
            parent = next((n for n in nodes if n.id == node.parent_id), None)
            parent_code = parent.code if parent else None
        if parent_code:
            if parent_code not in cache:
                cache[parent_code] = {}
            cache[parent_code][node.code] = node.name
    return cache
