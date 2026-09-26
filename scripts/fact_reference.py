"""Canonical parsing and validation for persisted fact references."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Iterable


_CANONICAL = re.compile(r"^facts/([^/]+\.json)#/records/([0-9]+)$")
_LEGACY_PACKAGE_ALIASES = {
    "material_ranking": "material-ranking.json",
    "material_ranking.json": "material-ranking.json",
    "ranking": "material-ranking.json",
    "ranking.json": "material-ranking.json",
    "highlight_frames": "highlight-frames.json",
    "highlight_frames.json": "highlight-frames.json",
    "highlights": "highlight-frames.json",
    "highlights.json": "highlight-frames.json",
}


class FactReferenceError(ValueError):
    """Structured error raised for malformed, legacy, or out-of-scope refs."""

    def __init__(self, code: str, message: str, suggestion: str = ""):
        self.code = code
        self.path = "$"
        self.message = message
        self.suggestion = suggestion
        super().__init__(message)

    def to_dict(self) -> dict[str, str]:
        return {
            "code": self.code,
            "path": self.path,
            "message": self.message,
            "suggestion": self.suggestion,
        }


@dataclass(frozen=True)
class FactReference:
    package_path: str
    source_index: int

    @property
    def value(self) -> str:
        return f"{self.package_path}#/records/{self.source_index}"


def _suggest_legacy(value: str) -> str | None:
    match = re.match(r"^(?:facts/)?([^#/]+?)(?:\.json)?#/records/([0-9]+)$", value)
    if not match:
        return None
    filename = _LEGACY_PACKAGE_ALIASES.get(match.group(1))
    if filename is None:
        filename = _LEGACY_PACKAGE_ALIASES.get(f"{match.group(1)}.json")
    if filename is None:
        return None
    return f"facts/{filename}#/records/{match.group(2)}"


def parse_fact_ref(value: str, *, allowed: Iterable[str] | None = None) -> FactReference:
    if not isinstance(value, str) or not value.strip():
        raise FactReferenceError("fact_ref_type", "fact_ref must be a nonblank string", "使用 facts/<canonical-file>.json#/records/<index>。")
    value = value.strip()
    match = _CANONICAL.fullmatch(value)
    if not match:
        suggestion = _suggest_legacy(value)
        if suggestion:
            raise FactReferenceError(
                "fact_ref_legacy_alias",
                f"fact_ref 使用了旧别名：{value}",
                suggestion,
            )
        raise FactReferenceError(
            "fact_ref_invalid",
            f"fact_ref does not resolve because its record path is invalid: {value}",
            "使用 facts/<canonical-file>.json#/records/<nonnegative index>。",
        )
    if match.group(1) in _LEGACY_PACKAGE_ALIASES:
        canonical = _LEGACY_PACKAGE_ALIASES[match.group(1)]
        raise FactReferenceError(
            "fact_ref_legacy_alias",
            f"fact_ref 使用了旧别名：{value}",
            f"facts/{canonical}#/records/{match.group(2)}",
        )
    ref = FactReference(package_path=f"facts/{match.group(1)}", source_index=int(match.group(2)))
    if allowed is not None and ref.value not in set(allowed):
        raise FactReferenceError(
            "fact_ref_out_of_bounds",
            f"fact_ref does not resolve: {ref.value}",
            "只引用本次选择且存在于 allowed_fact_refs 的记录。",
        )
    return ref


def build_fact_ref(package_path: str, source_index: int) -> str:
    if not isinstance(package_path, str) or not package_path.startswith("facts/"):
        raise FactReferenceError("fact_ref_invalid", "package path must start with facts/", "使用 facts/<canonical-file>.json。")
    if not isinstance(source_index, int) or isinstance(source_index, bool) or source_index < 0:
        raise FactReferenceError("fact_ref_invalid", "source_index must be a nonnegative integer", "使用 records 下的非负整数下标。")
    return parse_fact_ref(f"{package_path}#/records/{source_index}").value


__all__ = ["FactReference", "FactReferenceError", "build_fact_ref", "parse_fact_ref"]
