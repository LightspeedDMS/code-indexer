"""ControlPlaneMetadataBackend Protocol (content seam step 5a).

Golden-repo alias pointers and write-mode markers are control-plane metadata:
they live in the server's registry (SQLite solo, PostgreSQL cluster), one row
per key, instead of in per-pod JSON files on the repository volume. Both kinds
share one shape -- a key, its JSON document, and when it was last written --
so one Protocol serves both, selected by ``kind``.

The dual-write outbox lives here too: a change written with ``enqueue=True``
commits its outbox row in the SAME transaction, and ``claim_outbox`` hands a
sweeper the rows it alone may replay.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import ContextManager, Sequence, Tuple

from ._shared import Any, Dict, List, Optional, Protocol, runtime_checkable

#: An alias pointer (the document golden-repos/aliases/<name>.json held).
KIND_ALIAS = "alias"
#: A write-mode marker (the document golden-repos/.write_mode/<name>.json held).
KIND_WRITE_MODE = "write_mode"

#: Registry table holding each kind. The single mapping both backends use.
METADATA_TABLES: Dict[str, str] = {
    KIND_ALIAS: "golden_repo_aliases",
    KIND_WRITE_MODE: "write_mode_markers",
}

#: Registry table remembering when each key was last deleted, so the startup
#: import never resurrects a key from a file the delete failed to remove (or
#: an old-image pod left behind). One row per (kind, name).
TOMBSTONE_TABLE = "control_plane_metadata_tombstones"


#: Fixed-width UTC format for ``updated_at``. Every stored value uses it, so
#: SQL can compare two of them lexically (the import's newer-wins upsert).
_UPDATED_AT_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def format_updated_at(moment: Optional[datetime] = None) -> str:
    """Render *moment* (default: now) in the registry's updated_at format.

    A naive datetime is taken as UTC; an aware one is converted to UTC.
    """
    value = moment if moment is not None else datetime.now(timezone.utc)
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime(_UPDATED_AT_FORMAT)


def decode_payload(raw: Any, name: str) -> Dict[str, Any]:
    """The stored JSON document for *name* (TEXT in both backends).

    Raises ValueError when the row holds no document -- the column is NOT
    NULL, so that is corruption, never an absent key.
    """
    from ..json_column import parse_json_column

    decoded: Optional[Dict[str, Any]] = parse_json_column(raw, dict, name)
    if decoded is None:
        raise ValueError(f"control-plane metadata row {name!r} has no payload")
    return decoded


def group_claimed_rows(rows: Sequence[Any]) -> List[Tuple[str, str, int]]:
    """``(kind, name, highest id)`` per key from claimed ``(id, kind, name)``
    rows, keys in the order their oldest row was claimed."""
    highest: Dict[Tuple[str, str], int] = {}
    for row_id, kind, name in rows:
        key = (str(kind), str(name))
        highest[key] = max(int(row_id), highest.get(key, int(row_id)))
    return [(kind, name, up_to) for (kind, name), up_to in highest.items()]


def metadata_table(kind: str) -> str:
    """Return the table for *kind*; ValueError for an unknown kind.

    Table names are interpolated into SQL, so they come only from this fixed
    mapping, never from caller input.
    """
    try:
        return METADATA_TABLES[kind]
    except KeyError:
        raise ValueError(
            f"Unknown control-plane metadata kind {kind!r}; "
            f"expected one of {sorted(METADATA_TABLES)}"
        ) from None


#: Seconds a SQLite outbox claim lasts before another sweeper may take it over
#: (a crashed sweeper's rows are not stranded). PostgreSQL row locks need none.
OUTBOX_CLAIM_LEASE_SECONDS = 300.0


@dataclass(frozen=True)
class OutboxRow:
    """One outbox row: which key changed. Never a payload."""

    id: int
    kind: str
    name: str


@dataclass
class OutboxState:
    """The singleton drain/stop state every node shares."""

    drain_started_epoch: Optional[float] = None
    drain_sink: Optional[str] = None
    stopped_at: Optional[str] = None
    stopped_reason: Optional[str] = None
    stopped_sink: Optional[str] = None


class OutboxClaim(Protocol):
    """Rows one sweeper claimed; no other sweeper replays them meanwhile."""

    def keys(self) -> List[Tuple[str, str, int]]:
        """``(kind, name, highest claimed id)`` per key, oldest key first."""
        ...

    def current(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        """The registry's CURRENT document for the key (None: key is gone)."""
        ...

    def complete(self, kind: str, name: str, up_to_id: int) -> None:
        """Drop every claimed row for the key up to *up_to_id* -- called only
        after the key's file write succeeded."""
        ...


@runtime_checkable
class ControlPlaneMetadataBackend(Protocol):
    """Registry storage for alias pointers and write-mode markers."""

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        """The stored document for *name*, or None when absent."""
        ...

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        """Every stored document whose name starts with *prefix*."""
        ...

    def put(
        self,
        kind: str,
        name: str,
        payload: Dict[str, Any],
        *,
        enqueue: bool = False,
    ) -> None:
        """Insert or replace the document for *name*; stamps updated_at now.
        With *enqueue*, an outbox row for the key commits in the same
        transaction -- both or neither."""
        ...

    def delete(self, kind: str, name: str, *, enqueue: bool = False) -> None:
        """Remove *name*; a missing key is not an error. Records a tombstone
        stamped now in the same transaction. *enqueue* as put."""
        ...

    def import_if_newer(
        self, kind: str, name: str, payload: Dict[str, Any], updated_at: str
    ) -> bool:
        """Store *payload* only if *updated_at* is strictly newer than both
        the stored row's updated_at (when the key exists) and the key's
        tombstone (when it was deleted). Returns True when it stored.

        The startup import uses this so an older file never clobbers a newer
        registry value, and a file that outlived its key's delete never
        brings the key back.
        """
        ...

    def outbox_depth(self) -> int:
        """Rows waiting in the outbox (the rollback gate wants 0)."""
        ...

    def outbox_rows(self) -> List[OutboxRow]:
        """Every outbox row, oldest first."""
        ...

    def delete_outbox_rows(self, ids: Sequence[int]) -> int:
        """Delete exactly these rows; returns how many were deleted."""
        ...

    def claim_outbox(self, limit: int) -> ContextManager[OutboxClaim]:
        """Claim up to *limit* of the oldest unclaimed rows for one sweep."""
        ...

    def get_outbox_state(self) -> OutboxState: ...

    def save_outbox_state(self, state: OutboxState) -> None: ...

    def close(self) -> None: ...
