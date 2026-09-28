"""Server-held seal for indexer resume state.

The interrupted-operation resume state lives in
``.code-indexer/metadata-<provider>.json`` inside the repository working
tree, which is repository content. Server-spawned indexing therefore uses
that state only when it carries a seal that only the server can produce:
an HMAC-SHA256 keyed by a random key stored in the server data directory
(``CIDX_SERVER_DATA_DIR``, default ``~/.cidx-server``), outside every
repository tree.

The seal covers the full metadata content and is bound to the resolved
metadata file path: state that was not written by a server-context run for
this exact file, or that changed after it was written, has no valid seal
and is not trusted. A missing or unreadable key never makes unsealed state
trusted.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

#: Metadata field holding the seal. Never part of the sealed content.
RESUME_SEAL_FIELD = "resume_seal"

#: Key file name inside the server data directory.
RESUME_SEAL_KEY_FILENAME = "indexer_resume_seal.key"

_SEAL_KEY_BYTES = 32
_SEAL_DOMAIN = b"cidx-indexer-resume-seal-v1"


def server_data_dir() -> Path:
    """The server data directory, honoring ``CIDX_SERVER_DATA_DIR``."""
    return Path(
        os.environ.get("CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server"))
    )


def _publish_new_key(key_path: Path) -> None:
    """Create the key file atomically: write a temp file, then hard-link it
    into place. ``os.link`` fails if the key already exists, so concurrent
    creators never overwrite each other and a reader never observes a
    partially written key."""
    fd, tmp_name = tempfile.mkstemp(dir=str(key_path.parent), prefix=".seal-key-")
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(secrets.token_bytes(_SEAL_KEY_BYTES))
            handle.flush()
            os.fsync(handle.fileno())
        try:
            os.link(tmp_name, str(key_path))
        except FileExistsError:
            pass  # Another process published the key first; use theirs.
    finally:
        os.unlink(tmp_name)


def load_or_create_resume_seal_key(
    directory: Optional[Path] = None,
) -> Optional[bytes]:
    """Return the server-held seal key, creating it on first use.

    Returns None (after a WARNING) when the key cannot be created or read,
    or has the wrong length; callers must then treat resume state as
    untrusted.
    """
    base = directory if directory is not None else server_data_dir()
    key_path = base / RESUME_SEAL_KEY_FILENAME
    try:
        base.mkdir(parents=True, exist_ok=True)
        if not key_path.exists():
            _publish_new_key(key_path)
        key = key_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "Indexer resume-state seal key is unavailable (%s); stored resume "
            "state will not be trusted for this run.",
            type(exc).__name__,
        )
        return None
    if len(key) != _SEAL_KEY_BYTES:
        logger.warning(
            "Indexer resume-state seal key has an unexpected length; stored "
            "resume state will not be trusted for this run."
        )
        return None
    return key


def content_digest(metadata: Dict[str, Any]) -> bytes:
    """SHA-256 over the canonical JSON form of ``metadata`` without its seal."""
    unsealed = {k: v for k, v in metadata.items() if k != RESUME_SEAL_FIELD}
    canonical = json.dumps(unsealed, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).digest()


def compute_resume_seal(key: bytes, binding: str, digest: bytes) -> str:
    """HMAC-SHA256 over the domain tag, the path binding and the digest."""
    message = (
        _SEAL_DOMAIN
        + b"\0"
        + binding.encode("utf-8", "surrogateescape")
        + b"\0"
        + digest
    )
    return hmac.new(key, message, hashlib.sha256).hexdigest()


def seal_matches(key: bytes, binding: str, digest: bytes, seal: object) -> bool:
    """Constant-time check that ``seal`` is the valid seal for ``digest``."""
    if not isinstance(seal, str):
        return False
    return hmac.compare_digest(seal, compute_resume_seal(key, binding, digest))
