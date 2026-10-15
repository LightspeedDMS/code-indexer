"""Health reason for stored credentials (git credentials, CI tokens) that a
node cannot decrypt and that need re-entry.

The state is the ``needs_reentry_at`` mark on the shared credential rows
(SQLite solo, PostgreSQL cluster), so every node reports the same count;
saving a new value or a successful read clears it. Health is visible to
every authenticated user, so the reason is a count only: which credential
needs re-entry (and, in a cluster, which node could not decrypt it) is shown
only on the owner's git credentials page and the admin Config page.
"""

from __future__ import annotations

from typing import Any, Dict, List, Protocol


class _NeedsReentryRows(Protocol):
    def list_needs_reentry(self) -> List[Dict[str, Any]]: ...


class _CredentialRegistry(Protocol):
    git_credentials: _NeedsReentryRows
    ci_tokens: _NeedsReentryRows


def stored_credential_health_reasons(registry: _CredentialRegistry) -> List[str]:
    """One count-only reason when stored credentials need re-entry. A read
    failure is a reason of its own: an unreadable store must never look
    healthy."""
    try:
        count = len(registry.git_credentials.list_needs_reentry()) + len(
            registry.ci_tokens.list_needs_reentry()
        )
    except Exception as exc:
        return [
            f"Stored credential state unreadable ({type(exc).__name__}): "
            f"credentials needing re-entry cannot be counted"
        ]
    if count == 0:
        return []
    noun = "stored credential needs" if count == 1 else "stored credentials need"
    return [f"{count} {noun} re-entry (see the credentials pages)"]
