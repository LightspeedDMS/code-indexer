"""
JournalEntry builder helper for Story #908 Phase 3.7 tests.

Separated from test_dep_map_908_builders.py to keep each module focused.
"""

from typing import Any, Dict

# The JournalEntry schema has 12 fields total. 11 are overrideable by callers;
# `timestamp` is auto-generated at construction time (datetime.now(timezone.utc))
# and is intentionally excluded from the override set so tests always get a real
# timestamp rather than a potentially-wrong synthetic value.
_OVERRIDEABLE_JOURNAL_FIELDS = frozenset(
    {
        "anomaly_type",
        "source_domain",
        "target_domain",
        "source_repos",
        "target_repos",
        "verdict",
        "action",
        "citations",
        "file_writes",
        "claude_response_raw",
        "effective_mode",
    }
)


def make_journal_entry(**overrides: Any):
    """Construct a JournalEntry with SELF_LOOP defaults, applying keyword overrides.

    The JournalEntry schema has 12 fields. 11 are overrideable here; `timestamp`
    is auto-generated at construction and cannot be overridden via this helper.

    Raises KeyError when an override key is not in the 11 overrideable fields.
    """
    unknown = set(overrides.keys()) - _OVERRIDEABLE_JOURNAL_FIELDS
    if unknown:
        raise KeyError(
            f"Unknown overrideable JournalEntry field(s): {sorted(unknown)}. "
            f"Allowed fields: {sorted(_OVERRIDEABLE_JOURNAL_FIELDS)}"
        )

    from code_indexer.server.services.dep_map_repair_executor import JournalEntry

    defaults: Dict[str, Any] = dict(
        anomaly_type="SELF_LOOP",
        source_domain="domain-a",
        target_domain="domain-a",
        source_repos=[],
        target_repos=[],
        verdict="N_A",
        action="self_loop_deleted",
        citations=[],
        file_writes=[{"path": "domain-a.md", "operation": "deleted_row"}],
        claude_response_raw="",
        effective_mode="enabled",
    )
    defaults.update(overrides)
    return JournalEntry(**defaults)
