"""Config-driven content filters for feed items.

Filters are deliberately simple and predictable:
  - include_keywords: if present, at least one keyword must appear
  - exclude_keywords: if present, no keyword may appear

Matching is case-insensitive substring matching against human-readable fields
such as title, summary, headline, event, counties, meeting location, and source.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass


TEXT_FIELDS = (
    "title",
    "summary",
    "description",
    "headline",
    "event",
    "severity",
    "counties",
    "when_text",
    "where",
    "source",
    "jurisdiction",
)


@dataclass(frozen=True)
class FilterSpec:
    """Compiled keyword filter.

    Include keywords use the most specific non-empty list supplied. Exclude
    keywords are additive across all supplied filter configs.
    """

    include_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...]


def _keywords(value, key: str) -> tuple[str, ...]:
    """Normalize a keyword setting to a tuple of non-empty strings."""
    if value is None:
        return ()
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, dict):
        raise ValueError(f"{key} must be a string or list of strings")
    elif isinstance(value, Iterable):
        values = value
    else:
        raise ValueError(f"{key} must be a string or list of strings")

    normalized = []
    for item in values:
        if not isinstance(item, str):
            raise ValueError(f"{key} entries must be strings")
        keyword = item.strip().lower()
        if keyword:
            normalized.append(keyword)
    return tuple(normalized)


def compile_filters(configs: Iterable[dict | None]) -> FilterSpec:
    """Compile global, channel, and source filters into one filter spec."""
    include_keywords: tuple[str, ...] = ()
    exclude_keywords: list[str] = []

    for cfg in configs:
        if not cfg:
            continue
        if not isinstance(cfg, dict):
            raise ValueError("filters must be mapping objects")

        includes = _keywords(cfg.get("include_keywords"), "include_keywords")
        excludes = _keywords(cfg.get("exclude_keywords"), "exclude_keywords")

        if includes:
            include_keywords = includes
        exclude_keywords.extend(excludes)

    return FilterSpec(include_keywords, tuple(exclude_keywords))


def _append_text(parts: list[str], value) -> None:
    if value is None:
        return
    if isinstance(value, str):
        if value.strip():
            parts.append(value)
        return
    if isinstance(value, (list, tuple, set)):
        for item in value:
            _append_text(parts, item)


def item_text(item: dict) -> str:
    """Return lower-cased text used for keyword matching."""
    parts: list[str] = []
    for field in TEXT_FIELDS:
        _append_text(parts, item.get(field))
    return " ".join(parts).lower()


def passes_filters(item: dict, spec: FilterSpec) -> bool:
    """Return True when an item should be allowed through."""
    haystack = item_text(item)

    if spec.include_keywords and not any(
        keyword in haystack for keyword in spec.include_keywords
    ):
        return False

    return not any(keyword in haystack for keyword in spec.exclude_keywords)
