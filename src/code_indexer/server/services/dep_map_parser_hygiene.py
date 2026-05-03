"""
dep_map_parser_hygiene -- identifier hygiene and anomaly types (Story #887).

Pure functions for backtick stripping, prose-fragment rejection, case
normalization, anomaly deduplication, aggregation, and channel splitting.
No I/O. No side effects. All callees may call freely.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from enum import Enum
from typing import Dict, List, Literal, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

MAX_DOMAIN_NAME_LENGTH = 120
_DEFAULT_AGGREGATE_THRESHOLD = 5
_DEFAULT_ANOMALY_EXAMPLE_LIMIT = 3

# Prose-fragment heuristics
_RE_THREE_PLUS_SPACES = re.compile(r" {3,}")
_RE_URL_SCHEME = re.compile(r"^https?://")


# ---------------------------------------------------------------------------
# AnomalyType -- enum with bound channel
# ---------------------------------------------------------------------------


class AnomalyType(Enum):
    """Typed anomaly variants with a bound channel attribute.

    Each variant carries a ``channel`` attribute whose value is either
    ``"parser"`` (structural/format-level parse errors) or ``"data"``
    (semantic/consistency-level data issues).
    """

    # Class-level annotation so mypy resolves entry.type.channel without
    # attr-defined errors. The attribute is bound dynamically in __new__.
    channel: Literal["parser", "data"]

    # Parser-channel variants
    MALFORMED_YAML = ("malformed_yaml", "parser")
    PATH_TRAVERSAL_REJECTED = ("path_traversal_rejected", "parser")

    # Data-channel variants
    BIDIRECTIONAL_MISMATCH = ("bidirectional_mismatch", "data")
    SELF_LOOP = ("self_loop", "data")
    GARBAGE_DOMAIN_REJECTED = ("garbage_domain_rejected", "data")
    CASE_NORMALIZATION_APPLIED = ("case_normalization_applied", "data")

    def __new__(
        cls,
        value: str,
        channel: Literal["parser", "data"],
    ) -> "AnomalyType":
        obj = object.__new__(cls)
        obj._value_ = value
        # Enum subclass pattern requires dynamic channel binding because
        # dataclass-style class attributes are not supported inside Enum bodies.
        obj.channel = channel  # type: ignore[attr-defined]
        return obj


# ---------------------------------------------------------------------------
# Anomaly data structures
# ---------------------------------------------------------------------------


@dataclass
class AnomalyEntry:
    """Single anomaly occurrence."""

    type: AnomalyType
    file: str
    message: str
    channel: str
    count: int = 1


@dataclass
class AnomalyAggregate:
    """Collapsed anomaly group when per-type count exceeds threshold."""

    type: AnomalyType
    count: int
    examples: List[AnomalyEntry] = field(default_factory=list)


# ---------------------------------------------------------------------------
# AC1: Backtick stripping
# ---------------------------------------------------------------------------


def strip_backticks(s: str) -> str:
    """Strip all wrapper backticks (leading and trailing) from *s*.

    Interior backticks are never affected — only contiguous leading and
    trailing backtick runs are removed. Satisfies AC1 invariant: no
    emitted identifier string starts or ends with a backtick.

    Examples::

        strip_backticks("`repo-name`")   -> "repo-name"
        strip_backticks("`foo")          -> "foo"
        strip_backticks("foo`")          -> "foo"
        strip_backticks("a`b`c")         -> "a`b`c"  (interior preserved)
        strip_backticks("``x``")         -> "x"
    """
    while s.startswith("`"):
        s = s[1:]
    while s.endswith("`"):
        s = s[:-1]
    assert not s.startswith("`") and not s.endswith("`"), (
        f"Invariant violated: {s!r} has wrapper backticks after strip"
    )
    return s


# ---------------------------------------------------------------------------
# AC2: Prose-fragment rejection
# ---------------------------------------------------------------------------


def _strip_url_scheme(s: str) -> str:
    """Remove a leading http:// or https:// scheme from *s*, if present."""
    match = _RE_URL_SCHEME.match(s)
    if match:
        return s[match.end() :]
    return s


def is_prose_fragment(s: str) -> bool:
    """Return True when *s* should be rejected as a prose-fragment domain name.

    Rejects on:
    - Newline character present
    - Open or close parenthesis present
    - Colon present outside an http/https URL prefix
    - Three or more consecutive space characters
    - Length exceeding MAX_DOMAIN_NAME_LENGTH (120 chars)
    """
    if len(s) > MAX_DOMAIN_NAME_LENGTH:
        return True
    if "\n" in s:
        return True
    if "(" in s or ")" in s:
        return True
    # Strip the URL scheme before checking for colons so that
    # "https://example.com" passes but "foo: bar" is rejected.
    if ":" in _strip_url_scheme(s):
        return True
    if _RE_THREE_PLUS_SPACES.search(s):
        return True
    return False


# ---------------------------------------------------------------------------
# AC3: Case normalization
# ---------------------------------------------------------------------------


def normalize_identifier(s: str) -> Tuple[str, bool]:
    """Strip wrapper backticks then lowercase *s*.

    Returns:
        (normalized, modified) where *modified* is True when the output
        differs from the input string.
    """
    stripped = strip_backticks(s)
    lowered = stripped.lower()
    modified = lowered != s
    return lowered, modified


# ---------------------------------------------------------------------------
# AC6: Deduplication
# ---------------------------------------------------------------------------


def deduplicate_anomalies(entries: Sequence[AnomalyEntry]) -> List[AnomalyEntry]:
    """Collapse entries with identical (type, file, message) key.

    Collapsed entries accumulate the sum of all matching entry counts.
    Order of first occurrence is preserved in the output.
    """
    seen: Dict[Tuple, AnomalyEntry] = {}
    order: List[Tuple] = []

    for entry in entries:
        key = (entry.type, entry.file, entry.message)
        if key in seen:
            seen[key].count += entry.count
        else:
            seen[key] = AnomalyEntry(
                type=entry.type,
                file=entry.file,
                message=entry.message,
                channel=entry.channel,
                count=entry.count,
            )
            order.append(key)

    return [seen[k] for k in order]


# ---------------------------------------------------------------------------
# AC5: Aggregation
# ---------------------------------------------------------------------------


def aggregate_anomalies(
    entries: Sequence[AnomalyEntry],
    threshold: int = _DEFAULT_AGGREGATE_THRESHOLD,
) -> List[Union[AnomalyEntry, AnomalyAggregate]]:
    """Collapse per-type groups that exceed *threshold* into an AnomalyAggregate.

    Groups with total count <= threshold are returned as individual AnomalyEntry
    objects in their original order.  Groups exceeding the threshold are
    replaced by a single AnomalyAggregate whose ``examples`` field contains
    the first _DEFAULT_ANOMALY_EXAMPLE_LIMIT entries of that type.

    The aggregate condition is ``total_count > threshold`` (strictly greater).

    Raises:
        ValueError: when threshold is negative.
    """
    if threshold < 0:
        raise ValueError("threshold must be non-negative")

    by_type: Dict[AnomalyType, List[AnomalyEntry]] = defaultdict(list)
    type_order: List[AnomalyType] = []

    for entry in entries:
        if entry.type not in by_type:
            type_order.append(entry.type)
        by_type[entry.type].append(entry)

    result: List[Union[AnomalyEntry, AnomalyAggregate]] = []
    for atype in type_order:
        group = by_type[atype]
        total_count = sum(e.count for e in group)
        if total_count > threshold:
            result.append(
                AnomalyAggregate(
                    type=atype,
                    count=total_count,
                    examples=list(group[:_DEFAULT_ANOMALY_EXAMPLE_LIMIT]),
                )
            )
        else:
            result.extend(group)

    return result


# ---------------------------------------------------------------------------
# AC7: Channel splitting
# ---------------------------------------------------------------------------


def split_anomaly_channels(
    entries: Sequence[AnomalyEntry],
) -> Tuple[List[AnomalyEntry], List[AnomalyEntry]]:
    """Partition *entries* into (parser_anomalies, data_anomalies) by channel.

    Each entry is routed according to its ``type.channel`` attribute.
    """
    parser_out: List[AnomalyEntry] = []
    data_out: List[AnomalyEntry] = []

    for entry in entries:
        if entry.type.channel == "parser":
            parser_out.append(entry)
        else:
            data_out.append(entry)

    return parser_out, data_out
