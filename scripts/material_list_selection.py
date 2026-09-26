"""Pure local filtering and sorting for persisted normalized Yuntu list records."""

from __future__ import annotations

from datetime import datetime
import unicodedata

try:
    from .material_field_registry import MaterialFieldRegistry, MaterialFieldRegistryError
except ImportError:
    from material_field_registry import MaterialFieldRegistry, MaterialFieldRegistryError


class MaterialListSelectionError(ValueError):
    """Raised when a normalized local selection cannot be evaluated safely."""


def _text(value):
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _date_value(value):
    if not isinstance(value, str):
        return None
    for fmt in ("%Y%m%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt).date()
        except ValueError:
            continue
    return None


def _compare(actual, expected, operator):
    if actual is None or isinstance(actual, bool) or isinstance(expected, bool):
        return False
    if operator == "eq":
        return actual == expected
    if operator == "gt":
        return actual > expected
    if operator == "gte":
        return actual >= expected
    if operator == "lt":
        return actual < expected
    if operator == "lte":
        return actual <= expected
    raise MaterialListSelectionError(f"unsupported operator: {operator}")


def _matches(record, condition, registry):
    try:
        field = registry.resolve(condition["field"])
    except (KeyError, TypeError, MaterialFieldRegistryError) as exc:
        raise MaterialListSelectionError("unknown material field in selection") from exc
    operator = condition.get("operator")
    if operator not in field.filter_operators:
        raise MaterialListSelectionError(f"unsupported operator for {field.id}: {operator}")
    value = registry.value(record, field.id)
    expected = condition.get("value")
    if field.kind in {"text", "identifier"}:
        if not isinstance(value, str) or not isinstance(expected, str):
            return False
        actual_text = _text(value)
        expected_text = _text(expected)
        return expected_text in actual_text if operator == "contains" else actual_text == expected_text
    if field.kind == "string_list":
        if not isinstance(value, list) or not isinstance(expected, str):
            return False
        expected_text = _text(expected)
        values = [_text(item) for item in value if isinstance(item, str)]
        if operator == "contains":
            return any(expected_text in item for item in values)
        return any(item == expected_text for item in values)
    if field.kind == "date":
        actual_date = _date_value(value)
        expected_date = _date_value(expected)
        return actual_date is not None and expected_date is not None and _compare(
            actual_date, expected_date, operator
        )
    return _compare(value, expected, operator)


def _sort_value(record, field, registry):
    value = registry.value(record, field.id)
    if value is None or isinstance(value, bool):
        return None
    if field.kind == "date":
        return _date_value(value)
    if field.kind in {"text", "identifier"}:
        return _text(value) if isinstance(value, str) else None
    if field.kind == "string_list":
        return None
    return value


def _validate_condition_value(field, value):
    if field.kind in {"text", "identifier", "string_list"}:
        if not isinstance(value, str) or not value.strip():
            raise MaterialListSelectionError(f"filter value is invalid for {field.id}")
        return
    if field.kind == "date":
        if _date_value(value) is None:
            raise MaterialListSelectionError(f"filter date is invalid for {field.id}")
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise MaterialListSelectionError(f"filter value is invalid for {field.id}")
    if field.kind == "integer" and not isinstance(value, int):
        raise MaterialListSelectionError(f"filter value must be an integer for {field.id}")


def select_materials(records: list[dict], material_list: dict, registry: MaterialFieldRegistry) -> list[dict]:
    if not isinstance(records, list) or any(not isinstance(item, dict) for item in records):
        raise MaterialListSelectionError("records must be a list of objects")
    if not isinstance(material_list, dict):
        raise MaterialListSelectionError("material_list must be an object")
    filters = material_list.get("filters")
    sort = material_list.get("sort")
    if not isinstance(filters, list) or not isinstance(sort, dict):
        raise MaterialListSelectionError("material_list filters and sort are required")
    if set(sort) != {"field", "direction"}:
        raise MaterialListSelectionError("sort must contain field and direction")
    try:
        sort_field = registry.resolve(sort["field"])
    except (KeyError, TypeError, MaterialFieldRegistryError) as exc:
        raise MaterialListSelectionError("unknown sort field") from exc
    if not sort_field.sortable:
        raise MaterialListSelectionError(f"field is not sortable: {sort_field.id}")
    if sort["direction"] not in {"asc", "desc"}:
        raise MaterialListSelectionError("sort direction must be asc or desc")
    for condition in filters:
        if not isinstance(condition, dict) or set(condition) != {"field", "operator", "value"}:
            raise MaterialListSelectionError("filter must contain field, operator, and value")
        try:
            field = registry.resolve(condition["field"])
        except (KeyError, TypeError, MaterialFieldRegistryError) as exc:
            raise MaterialListSelectionError("unknown filter field") from exc
        if condition["operator"] not in field.filter_operators:
            raise MaterialListSelectionError(
                f"unsupported operator for {field.id}: {condition['operator']}"
            )
        _validate_condition_value(field, condition["value"])
    matched = [
        record for record in records
        if all(_matches(record, condition, registry) for condition in filters)
    ]
    present = []
    missing = []
    for index, record in enumerate(matched):
        value = _sort_value(record, sort_field, registry)
        (missing if value is None else present).append((record, value, index))
    present.sort(key=lambda item: (
        item[0].get("rank", 0), str(item[0].get("material_id", ""))
    ))
    present.sort(key=lambda item: item[1], reverse=sort["direction"] == "desc")
    missing.sort(key=lambda item: (
        item[0].get("rank", 0), str(item[0].get("material_id", ""))
    ))
    return [item[0] for item in present] + [item[0] for item in missing]
