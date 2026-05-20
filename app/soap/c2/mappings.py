"""
C2 规范 Mapping 构建器。

Mapping 表达"父对象 → 子对象"的从属关系，形如::

    <Mapping ID="1" ParentType="Category" ParentID="1" ParentCode="1"
             ElementType="Program" ElementID="123" ElementCode="123"
             Action="REGIST">
        <Property Name="Sequence">1</Property>
    </Mapping>

规范属性名：
    - ID (att)           唯一标识
    - Action (att)       REGIST/UPDATE/DELETE
    - ParentType (att)   父对象 ElementType
    - ParentID (att)     父对象 ID
    - ParentCode (att)   同 ParentID
    - ElementType (att)  子对象 ElementType（非 ChildType）
    - ElementID (att)    子对象 ID（非 ChildID）
    - ElementCode (att)  同 ElementID

只允许 :data:`constants.VALID_MAPPINGS` 中定义的 15 种组合；
非法组合会被静默丢弃并记录 warning。
"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from xml.etree.ElementTree import Element, SubElement

from loguru import logger

from .constants import Action, ElementType, VALID_MAPPINGS


def build_mapping(
    parent_type: ElementType,
    parent_id: Any,
    element_type: ElementType,
    element_id: Any,
    action: Action = Action.REGIST,
    mapping_id: str | None = None,
    sequence: int | None = None,
    licensing_window_start: str | None = None,
    licensing_window_end: str | None = None,
    mapping_type: str | None = None,
    recommend_level: int | None = None,
) -> Element | None:
    """
    构建单个 <Mapping> 元素。

    :param parent_type:           父对象 ElementType
    :param parent_id:             父对象 ID
    :param element_type:          子对象 ElementType（规范叫 ElementType，非 ChildType）
    :param element_id:            子对象 ID（规范叫 ElementID，非 ChildID）
    :param action:                REGIST/UPDATE/DELETE
    :param mapping_id:            Mapping 唯一 ID，不传则自动生成 UUID
    :param sequence:              排序序号（Category→Program/Series, Series→Program）
    :param licensing_window_start:授权窗口开始（Category→Program/Series 必填）
    :param licensing_window_end:  授权窗口结束（Category→Program/Series 必填）
    :param mapping_type:          Mapping 类型（Picture Mapping 的海报位置）
    :param recommend_level:       推荐等级 1~10，默认 2
    """
    if (parent_type, element_type) not in VALID_MAPPINGS:
        logger.warning(
            f"非法的 Mapping 组合被跳过: {parent_type.value} → {element_type.value} "
            f"(ParentID={parent_id}, ElementID={element_id})"
        )
        return None

    mid = mapping_id or uuid4().hex[:16]
    pid_str = str(parent_id)
    eid_str = str(element_id)

    mapping_el = Element(
        "Mapping",
        {
            "ID": mid,
            "Action": action.value,
            "ParentType": parent_type.value,
            "ParentID": pid_str,
            "ParentCode": pid_str,
            "ElementType": element_type.value,
            "ElementID": eid_str,
            "ElementCode": eid_str,
        },
    )

    _add_mapping_property(mapping_el, "Type", mapping_type)
    _add_mapping_property(mapping_el, "Sequence", sequence)
    _add_mapping_property(mapping_el, "LicensingWindowStart", licensing_window_start)
    _add_mapping_property(mapping_el, "LicensingWindowEnd", licensing_window_end)
    _add_mapping_property(mapping_el, "RecommendLevel", recommend_level)

    return mapping_el


def add_mapping(
    container: list[Element],
    parent_type: ElementType,
    parent_id: Any,
    element_type: ElementType,
    element_id: Any,
    action: Action = Action.REGIST,
    **kwargs,
) -> None:
    """
    构建并追加到容器列表；非法组合自动忽略。

    便捷方法，避免调用方重复判 None。
    kwargs 透传给 build_mapping（sequence, licensing_window_start 等）。
    """
    el = build_mapping(parent_type, parent_id, element_type, element_id, action, **kwargs)
    if el is not None:
        container.append(el)


def _add_mapping_property(parent: Element, name: str, value: Any) -> None:
    """向 <Mapping> 追加 <Property>，None/空值跳过"""
    if value is None:
        return
    if isinstance(value, str) and value == "":
        return
    prop = SubElement(parent, "Property", {"Name": name})
    prop.text = str(value)
