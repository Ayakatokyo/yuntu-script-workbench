"""Verified Yuntu material display-field registry and safe path access."""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Mapping
import unicodedata


ALLOWED_KINDS = {"text", "identifier", "string_list", "date", "integer", "number", "rate"}


class MaterialFieldRegistryError(ValueError):
    """Raised when the verified material-field registry is invalid or unresolved."""


def normalize_field_token(value: str) -> str:
    if not isinstance(value, str):
        raise MaterialFieldRegistryError("field token must be a string")
    return unicodedata.normalize("NFKC", value).strip().casefold()


@dataclass(frozen=True)
class FieldDefinition:
    id: str
    label: str
    kind: str
    path: tuple[str, ...]
    filter_operators: tuple[str, ...]
    sortable: bool
    aliases: tuple[str, ...] = ()


class MaterialFieldRegistry:
    def __init__(self, fields: tuple[FieldDefinition, ...]):
        self._fields = {field.id: field for field in fields}
        self._tokens = {}
        for field in fields:
            for token in (field.id, field.label, *field.aliases):
                normalized = normalize_field_token(token)
                if normalized in self._tokens:
                    raise MaterialFieldRegistryError(
                        f"duplicate field token: {token}"
                    )
                self._tokens[normalized] = field

    @classmethod
    def from_path(cls, path: Path) -> "MaterialFieldRegistry":
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise MaterialFieldRegistryError(f"unable to load registry: {path}") from exc
        if not isinstance(payload, dict) or payload.get("schemaVersion") != 1:
            raise MaterialFieldRegistryError("registry schemaVersion must be 1")
        entries = payload.get("fields")
        if not isinstance(entries, list) or not entries:
            raise MaterialFieldRegistryError("registry fields must be a non-empty array")
        fields = []
        ids = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise MaterialFieldRegistryError("registry field must be an object")
            allowed = {
                "id", "label", "aliases", "kind", "path", "filterOperators",
                "sortable", "labelEvidence",
            }
            unknown = set(entry) - allowed
            if unknown:
                raise MaterialFieldRegistryError(
                    "unknown registry field keys: " + ", ".join(sorted(unknown))
                )
            field_id = entry.get("id")
            label = entry.get("label")
            kind = entry.get("kind")
            path_value = entry.get("path")
            operators = entry.get("filterOperators")
            aliases = entry.get("aliases", [])
            if (
                not isinstance(field_id, str) or not field_id.strip()
                or not isinstance(label, str) or not label.strip()
                or kind not in ALLOWED_KINDS
                or not isinstance(entry.get("sortable"), bool)
                or not isinstance(entry.get("labelEvidence"), str)
                or not entry["labelEvidence"].strip()
            ):
                raise MaterialFieldRegistryError("registry field metadata is invalid")
            if normalize_field_token(field_id) in ids:
                raise MaterialFieldRegistryError(f"duplicate field id: {field_id}")
            ids.add(normalize_field_token(field_id))
            if (
                not isinstance(path_value, list)
                or not path_value
                or any(not isinstance(part, str) or not part for part in path_value)
            ):
                raise MaterialFieldRegistryError(f"field path is invalid: {field_id}")
            if (
                not isinstance(operators, list)
                or not operators
                or any(not isinstance(item, str) or not item for item in operators)
                or len(set(operators)) != len(operators)
            ):
                raise MaterialFieldRegistryError(f"filterOperators are invalid: {field_id}")
            if (
                not isinstance(aliases, list)
                or any(not isinstance(item, str) or not item.strip() for item in aliases)
            ):
                raise MaterialFieldRegistryError(f"aliases are invalid: {field_id}")
            fields.append(FieldDefinition(
                id=field_id.strip(),
                label=label.strip(),
                kind=kind,
                path=tuple(path_value),
                filter_operators=tuple(operators),
                sortable=entry["sortable"],
                aliases=tuple(item.strip() for item in aliases),
            ))
        try:
            return cls(tuple(fields))
        except MaterialFieldRegistryError:
            raise

    def resolve(self, field: str) -> FieldDefinition:
        try:
            return self._tokens[normalize_field_token(field)]
        except (KeyError, MaterialFieldRegistryError) as exc:
            raise MaterialFieldRegistryError(f"unknown material field: {field}") from exc

    def value(self, record: Mapping[str, Any], field_id: str) -> Any:
        field = self.resolve(field_id)
        current: Any = record
        for part in field.path:
            if not isinstance(current, Mapping) or part not in current:
                return None
            current = current[part]
        return current

    def definitions(self):
        return tuple(self._fields.values())


def default_material_field_registry() -> MaterialFieldRegistry:
    return MaterialFieldRegistry.from_path(
        Path(__file__).resolve().parent.parent / "config" / "yuntu-material-field-registry-v1.json"
    )
