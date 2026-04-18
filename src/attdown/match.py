# Copyright 2026 Hall Boys Inc
# SPDX-License-Identifier: Apache-2.0
"""Build OData filters from a list of key values (e.g. a CSV of VendorIDs).

Flow:
    values -> dedupe + clean -> chunk (default 50) -> `(Field eq 'A' or Field eq 'B' ...)`
"""

from __future__ import annotations

import csv
from pathlib import Path


def read_keys_from_csv(
    path: str | Path, column: str | int, *, has_header: bool = True
) -> list[str]:
    """Read one column from a CSV (header name or 0-indexed position).

    Returns non-empty trimmed strings, preserving order of first occurrence.
    Accepts values with surrounding quotes (`"ACME"`, `'ACME'`) and strips them.

    When `column` is an integer, the first row is treated as a header by default
    (pass `has_header=False` to include it as data).
    """
    p = Path(path)
    with p.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return []

    if isinstance(column, int):
        idx = column
        start = 1 if has_header else 0
    else:
        header = rows[0]
        lower = [h.strip().lower() for h in header]
        needle = column.strip().lower()
        try:
            idx = lower.index(needle)
        except ValueError as e:
            raise ValueError(
                f"Column '{column}' not found in CSV header: {header}"
            ) from e
        start = 1

    out: list[str] = []
    for row in rows[start:]:
        if idx >= len(row):
            continue
        v = row[idx].strip()
        # strip surrounding single or double quotes if present
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1].strip()
        if v:
            out.append(v)
    return out


def _escape_odata(v: str) -> str:
    """Escape a value destined for a single-quoted OData string literal."""
    return v.replace("'", "''")


def dedupe_keep_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for v in values:
        v = v.strip()
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def build_match_filters(
    field: str,
    values: list[str],
    *,
    chunk_size: int = 50,
    extra_filter: str | None = None,
) -> list[str]:
    """Return a list of OData filter strings, each scoped to <= chunk_size values.

    If `extra_filter` is given, each chunk is AND'ed with it: `(chunk) and (extra)`.
    """
    clean = dedupe_keep_order(values)
    if not clean:
        return []
    out: list[str] = []
    for i in range(0, len(clean), chunk_size):
        batch = clean[i : i + chunk_size]
        clauses = " or ".join(f"{field} eq '{_escape_odata(v)}'" for v in batch)
        chunk = f"({clauses})"
        if extra_filter:
            chunk = f"{chunk} and ({extra_filter})"
        out.append(chunk)
    return out


def resolve_match_values(match: "MatchConfig") -> list[str]:  # noqa: F821
    """Return the effective list of key values for a MatchConfig."""
    from attdown.config import MatchConfig  # local import to avoid cycle

    assert isinstance(match, MatchConfig)
    if match.values:
        return dedupe_keep_order(match.values)
    if match.from_csv:
        assert match.column is not None
        return dedupe_keep_order(
            read_keys_from_csv(match.from_csv, match.column, has_header=match.has_header)
        )
    return []
