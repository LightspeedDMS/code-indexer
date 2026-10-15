"""Startup sweep of stored secrets (git credentials, CI tokens).

Runs once per process start, off the event loop. For every row:
- a v2 value under the stored-secret key: nothing to do;
- a legacy value this node can read: rewritten as v2 with a compare-and-set
  when the cluster gate is open, else left for a later start;
- a value no available key decrypts: kept and marked as needing re-entry.

Idempotent and stateless: a later start repeats it until nothing legacy
remains. One pass over each table's rows (credentials, never repositories).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import (
    TYPE_CHECKING,
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
)

from code_indexer.server.services.stored_secret_key import StoredSecretCipher

if TYPE_CHECKING:
    from code_indexer.server.storage.factory import BackendRegistry

logger = logging.getLogger(__name__)


class StoredSecretRows(Protocol):
    def list_secret_rows(self) -> List[Dict[str, Any]]: ...

    def replace_encrypted_token_if_unchanged(
        self, row_id: str, expected: str, new_value: str
    ) -> bool: ...

    def mark_needs_reentry(
        self, row_id: str, expected: str, node_id: Optional[str] = None
    ) -> bool: ...

    def clear_needs_reentry(self, row_id: str, expected: str) -> bool: ...


@dataclass
class SweepCounts:
    rewritten: int = 0
    unreadable: int = 0
    remaining: int = 0  # legacy rows left as they are (gate closed or changed)
    already_v2: int = 0


def _sweep_one(cipher: StoredSecretCipher, backend: StoredSecretRows) -> SweepCounts:
    counts = SweepCounts()
    for row in backend.list_secret_rows():
        row_id, value = row["id"], row["encrypted_token"]
        try:
            plaintext, is_legacy = cipher.decrypt(value)
        except ValueError:
            counts.unreadable += 1
            if row["needs_reentry_at"] is None:
                backend.mark_needs_reentry(row_id, value, node_id=cipher.node_id)
            continue
        if row["needs_reentry_at"] is not None:
            backend.clear_needs_reentry(row_id, value)
        if not is_legacy:
            counts.already_v2 += 1
            continue
        new_value = cipher.rewrite_value(plaintext)
        if new_value is not None and backend.replace_encrypted_token_if_unchanged(
            row_id, value, new_value
        ):
            counts.rewritten += 1
        else:
            counts.remaining += 1
    return counts


def sweep_stored_secrets(
    cipher: StoredSecretCipher, sources: Sequence[Tuple[str, StoredSecretRows]]
) -> Dict[str, SweepCounts]:
    """Sweep each (name, backend) source; returns counts per source."""
    results: Dict[str, SweepCounts] = {}
    for name, backend in sources:
        counts = _sweep_one(cipher, backend)
        results[name] = counts
        logger.info(
            "Stored-secret sweep %s: rewritten=%d unreadable=%d remaining=%d "
            "already_v2=%d",
            name,
            counts.rewritten,
            counts.unreadable,
            counts.remaining,
            counts.already_v2,
        )
        if counts.unreadable:
            logger.warning(
                "Stored-secret sweep %s: %d stored secret(s) cannot be decrypted "
                "and need re-entry (see health)",
                name,
                counts.unreadable,
            )
    return results


def run_stored_secret_sweep(
    server_dir: Path, registry: "BackendRegistry"
) -> Dict[str, SweepCounts]:
    """Sweep the registry's git credential and CI token tables."""
    cipher = StoredSecretCipher(Path(server_dir), "stored-secret sweep")
    return sweep_stored_secrets(
        cipher,
        [
            ("git_credentials", registry.git_credentials),
            ("ci_tokens", registry.ci_tokens),
        ],
    )
