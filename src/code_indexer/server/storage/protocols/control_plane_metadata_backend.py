"""ControlPlaneMetadataBackend Protocol (content seam step 5a).

Golden-repo alias pointers and write-mode markers are control-plane metadata:
they live in the server's registry (SQLite solo, PostgreSQL cluster), one row
per key, instead of in per-pod JSON files on the repository volume. Both kinds
share one shape -- a key, its JSON document, and when it was last written --
so one Protocol serves both, selected by ``kind``.
"""

from __future__ import annotations

from datetime import datetime, timezone

from ._shared import Any, Dict, Optional, Protocol, runtime_checkable

#: An alias pointer (the document golden-repos/aliases/<name>.json held).
KIND_ALIAS = "alias"
#: A write-mode marker (the document golden-repos/.write_mode/<name>.json held).
KIND_WRITE_MODE = "write_mode"

#: Registry table holding each kind. The single mapping both backends use.
METADATA_TABLES: Dict[str, str] = {
    KIND_ALIAS: "golden_repo_aliases",
    KIND_WRITE_MODE: "write_mode_markers",
}


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


@runtime_checkable
class ControlPlaneMetadataBackend(Protocol):
    """Registry storage for alias pointers and write-mode markers."""

    def get(self, kind: str, name: str) -> Optional[Dict[str, Any]]:
        """The stored document for *name*, or None when absent."""
        ...

    def list_all(self, kind: str, prefix: str = "") -> Dict[str, Dict[str, Any]]:
        """Every stored document whose name starts with *prefix*."""
        ...

    def put(self, kind: str, name: str, payload: Dict[str, Any]) -> None:
        """Insert or replace the document for *name*; stamps updated_at now."""
        ...

    def delete(self, kind: str, name: str) -> None:
        """Remove *name*; a missing key is not an error."""
        ...

    def import_if_newer(
        self, kind: str, name: str, payload: Dict[str, Any], updated_at: str
    ) -> bool:
        """Store *payload* only if *name* is absent or its stored updated_at
        is strictly older than *updated_at*. Returns True when it stored.

        The startup import uses this so an older file never clobbers a newer
        registry value.
        """
        ...

    def close(self) -> None: ...
