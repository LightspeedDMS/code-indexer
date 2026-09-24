"""Whole-entry page packing for xray_truncation.py (Bug #1928).

Split out of xray_truncation.py to keep that module under the project's
file-size limit (Rule #6, Anti-File-Bloat) -- imported and re-exported by
xray_truncation.py, so all existing callers/tests keep using
`xray_truncation.pack_entries_into_pages` unchanged.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Tuple


def _pair_size(field_names: List[str], page: Dict[str, list]) -> int:
    return len(json.dumps({f: page.get(f, []) for f in field_names}))


def _entry_alone_size(field_names: List[str], field_name: str, entry: Any) -> int:
    page = {f: ([entry] if f == field_name else []) for f in field_names}
    return _pair_size(field_names, page)


# Bug #1928 final round (Opus P3.1): the O(1) running-size delta in
# pack_entries_into_pages() must count the SAME separator length that
# json.dumps() actually emits between list items -- json's default
# separators insert ", " (2 chars), but the round-3 implementation
# hardcoded 1, silently undercounting every entry after the first in a
# field's list. For long entries the 1-char shortfall is negligible;
# for MANY TINY entries (e.g. plain ints in `refine`) it compounds,
# producing pages up to ~1.5x over budget.
#
# Derived, never hardcoded: probe json.dumps([0, 0]) vs json.dumps([0])
# vs json.dumps(0) so this stays correct even if json's separators
# were ever reconfigured elsewhere in this module.
_LIST_ITEM_SEPARATOR_LEN = (
    len(json.dumps([0, 0])) - len(json.dumps([0])) - len(json.dumps(0))
)


def pack_entries_into_pages(
    field_names: List[str],
    fields: Dict[str, List[Any]],
    budget_chars: int,
) -> List[Dict[str, list]]:
    """Greedily pack WHOLE entries (in field_names order, each field's own
    entries in original order) into successive pages of <= budget_chars
    serialized JSON. NEVER splits, trims, or shrinks an entry -- an entry
    that alone exceeds budget_chars gets its own, oversized, page.

    Bug #1928 P3 (Opus): tracks a running serialized-size total instead
    of re-serializing the whole page (`json.dumps`) on every single
    entry -- the round-2 implementation was O(n^2) (~24s at a 200k-char
    budget with many entries). Each entry's own JSON length is computed
    ONCE; the page total is then updated by an O(1) delta (the entry's
    length, plus one separator comma when appending to an already
    non-empty field array) -- see the module-level derivation: for a
    dict `{f1: [...], f2: [...]}` with every field key always present
    (even as `[]`), serialized length = skeleton_size (the empty-array
    form's own length, constant for a given field_names) + per-field
    (sum of each entry's own JSON length + one comma per entry beyond
    the first in that field).

    Concatenating every page's field lists, in page order, reproduces the
    input exactly. Always returns at least one page.
    """
    combined: List[Tuple[str, Any]] = [
        (f, e) for f in field_names for e in fields.get(f, [])
    ]
    skeleton_size = _pair_size(field_names, {f: [] for f in field_names})

    pages: List[Dict[str, list]] = []
    current: Dict[str, list] = {f: [] for f in field_names}
    current_size = skeleton_size

    def _flush() -> None:
        nonlocal current, current_size
        pages.append(current)
        current = {f: [] for f in field_names}
        current_size = skeleton_size

    for fname, entry in combined:
        entry_len = len(json.dumps(entry))
        delta = entry_len + (_LIST_ITEM_SEPARATOR_LEN if current[fname] else 0)
        trial_size = current_size + delta
        if trial_size <= budget_chars:
            current[fname].append(entry)
            current_size = trial_size
            continue
        if any(current[f] for f in field_names):
            _flush()
        # Place the entry alone on the (now-empty) current page, WHOLE
        # and UNMODIFIED, regardless of whether it alone exceeds
        # budget_chars -- see xray_truncation.py's module docstring
        # (Bug #1928 P1).
        current[fname] = [entry]
        current_size = skeleton_size + entry_len

    if any(current[f] for f in field_names) or not pages:
        _flush()
    return pages
