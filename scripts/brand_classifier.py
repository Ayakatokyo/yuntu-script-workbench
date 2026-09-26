"""Conservative, runtime-scoped brand classification for fact packages."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata


BRAND_RULE_VERSION = "runtime-title-rule-v1"
BRAND_CLASSIFICATION_VERSION = BRAND_RULE_VERSION
UNKNOWN_BRAND = "未知品牌"


@dataclass(frozen=True)
class BrandRule:
    """One canonical runtime brand and its explicitly supplied match tokens."""

    canonical: str
    tokens: tuple[str, ...]


def _normalize(value: object) -> str:
    return unicodedata.normalize("NFKC", str(value or "")).casefold()


def build_brand_rules(brands: list[str] | tuple[str, ...] | None) -> tuple[BrandRule, ...]:
    """Build rules only from the current run's candidate brands.

    A slash separates aliases belonging to one canonical candidate. Empty
    candidates are ignored, and duplicates retain their first occurrence.
    This function intentionally has no default brand table.
    """

    rules: list[BrandRule] = []
    seen: set[str] = set()
    for raw_brand in brands or ():
        canonical = str(raw_brand or "").strip()
        if not canonical or canonical in seen:
            continue
        tokens = tuple(token.strip() for token in canonical.split("/") if token.strip())
        if not tokens:
            continue
        seen.add(canonical)
        rules.append(BrandRule(canonical=canonical, tokens=tokens))
    return tuple(rules)


def _match_tokens(title: str, rule: BrandRule) -> list[str]:
    normalized_title = _normalize(title)
    matches: list[str] = []
    seen_normalized: set[str] = set()
    for token in rule.tokens:
        normalized_token = _normalize(token)
        if not normalized_token or normalized_token in seen_normalized:
            continue
        if normalized_token in normalized_title:
            matches.append(token)
            seen_normalized.add(normalized_token)

    # This is the only controlled variant. It deliberately requires both
    # "九阳" and "豆浆" in one bounded expression; "九阳" alone is not a hit.
    if not matches and any(_normalize(token) == "九阳豆浆" for token in rule.tokens):
        variant = re.search(r"九阳[^\s#，。！？、；：,.!?]{0,6}豆浆", normalized_title)
        if variant:
            matches.append(variant.group(0))
    return matches


def classify_brand(title: str, *, brands: list[str] | tuple[str, ...] | None) -> dict:
    """Classify a title against only the supplied runtime brand candidates."""

    matches: list[str] = []
    match_tokens: dict[str, list[str]] = {}
    for rule in build_brand_rules(brands):
        tokens = _match_tokens(title, rule)
        if tokens:
            matches.append(rule.canonical)
            match_tokens[rule.canonical] = tokens

    if not matches:
        return {
            "value": UNKNOWN_BRAND,
            "matchedBrands": [],
            "source": "unclassified",
            "match_tokens": {},
        }
    value: str | list[str] = matches[0] if len(matches) == 1 else matches
    return {
        "value": value,
        "matchedBrands": matches,
        "source": "runtime_title_rule",
        "match_tokens": match_tokens,
    }
