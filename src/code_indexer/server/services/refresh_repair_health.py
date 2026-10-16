"""Health reasons for golden repositories whose refresh is failing in a way
no automatic repair can fix (Story #2126) -- e.g. an established
collection's chunk store is missing and no usable published snapshot exists
to restore it from, or the store keeps disappearing and the repository was
quarantined.

The state is the ``unrepairable_reason`` of the shared refresh failure
backoff row (SQLite solo, PostgreSQL cluster), so every node reports the
same repositories; a verified publish clears it. One query over the failing
aliases' rows only, never the whole fleet.
"""

from __future__ import annotations

from typing import Any, List


def unrepairable_refresh_health_reasons(registry: Any) -> List[str]:
    """One reason per repository needing an operator, read from the
    *registry*'s golden-repo metadata store. A read failure is reported as a
    reason of its own: an unreadable store must never look healthy."""
    try:
        rows = registry.golden_repo_metadata.list_unrepairable_refresh_failures()
        return [
            f"Refresh of {row['golden_alias']} needs an operator: {row['reason']}"
            for row in rows
        ]
    except Exception as exc:
        return [
            f"Golden-repo refresh repair state unreadable "
            f"({type(exc).__name__}): repositories needing an operator cannot "
            f"be listed"
        ]
