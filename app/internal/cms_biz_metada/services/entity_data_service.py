from sqlalchemy import and_, select, update, func
from sqlalchemy.ext.asyncio import AsyncSession

from ..models.basic import EntityFieldValue, EntityI18n
from app.internal.cms_biz_metada.schemas.basic import EntityFieldValueItem, EntityI18nItem, EntityFieldValuesPayload, EntityI18nPayload

_ENTITY_TYPE_TABLE_MAP: dict[str, tuple[str, str]] = {
    "Content": ("content", "id"),
    "cast": ("cast", "id"),
    "category": ("category", "id"),
    "package": ("package", "id"),
    "movie": ("movie", "id"),
    "cast_role_map": ("content_cast_role_map", "map_id"),
    "PhysicalChannel": ("physical_channel", "id"),
    "poster_size": ("poster_size", "id"),
}


async def _touch_parent_updated_at(db: AsyncSession, entity_type: str, entity_id: int) -> None:
    """
    更新实体的 updated_at 时间戳。
    
    注意：对于 category 类型，不更新父级栏目，只更新当前栏目本身。
    """
    entry = _ENTITY_TYPE_TABLE_MAP.get(entity_type)
    if entry is None:
        return
    table_name, pk_column = entry
    from app.database import Base
    table = Base.metadata.tables.get(table_name)
    if table is None:
        return
    # 更新当前实体本身的 updated_at
    await db.execute(
        update(table)
        .where(table.c[pk_column] == entity_id)
        .values(updated_at=func.now())
    )


async def get_field_values(db: AsyncSession, entity_type: str, entity_id: int) -> list[EntityFieldValueItem]:
    rows = (
        await db.execute(
            select(EntityFieldValue).where(
                and_(EntityFieldValue.entity_type == entity_type, EntityFieldValue.entity_id == entity_id)
            )
        )
    ).scalars().all()
    return [EntityFieldValueItem(custom_field_id=r.custom_field_id, value=r.value) for r in rows]


async def save_field_values(
    db: AsyncSession, entity_type: str, entity_id: int, payload: EntityFieldValuesPayload
) -> list[EntityFieldValueItem]:
    await db.execute(
        EntityFieldValue.__table__.delete().where(
            and_(EntityFieldValue.entity_type == entity_type, EntityFieldValue.entity_id == entity_id)
        )
    )
    for item in payload.values:
        if item.value is not None and item.value != "":
            db.add(EntityFieldValue(
                entity_type=entity_type,
                entity_id=entity_id,
                custom_field_id=item.custom_field_id,
                value=item.value,
            ))
    await _touch_parent_updated_at(db, entity_type, entity_id)
    await db.commit()
    return await get_field_values(db, entity_type, entity_id)


async def get_i18n_values(db: AsyncSession, entity_type: str, entity_id: int) -> list[EntityI18nItem]:
    rows = (
        await db.execute(
            select(EntityI18n).where(
                and_(EntityI18n.entity_type == entity_type, EntityI18n.entity_id == entity_id)
            )
        )
    ).scalars().all()
    return [EntityI18nItem(language=r.language, field_name=r.field_name, value=r.value) for r in rows]


async def save_i18n_values(
    db: AsyncSession, entity_type: str, entity_id: int, payload: EntityI18nPayload
) -> list[EntityI18nItem]:
    await db.execute(
        EntityI18n.__table__.delete().where(
            and_(
                EntityI18n.entity_type == entity_type,
                EntityI18n.entity_id == entity_id,
                EntityI18n.language == payload.language,
            )
        )
    )
    for field_name, value in payload.fields.items():
        if value is not None and value != "":
            db.add(EntityI18n(
                entity_type=entity_type,
                entity_id=entity_id,
                language=payload.language,
                field_name=field_name,
                value=value,
            ))
    await _touch_parent_updated_at(db, entity_type, entity_id)
    await db.commit()
    return await get_i18n_values(db, entity_type, entity_id)
