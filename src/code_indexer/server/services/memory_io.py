"""Deterministic serialization and atomic on-disk I/O for memory files.

Memory files live at cidx-meta/memories/{uuid}.md and consist of a YAML
frontmatter block followed by an optional markdown body.

Under a delegated content backend cidx-meta is SHARED content
(``cidx_meta_content``): the read, write and delete below go through the
content backend and the content writer, never this pod's disk. There the
caller's content hash becomes a compare-and-swap at the writer, so two pods
editing one memory conflict instead of losing an update.

Story #877 Phase 1b.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import yaml

from code_indexer.server.services import cidx_meta_content

logger = logging.getLogger(__name__)


class MemoryFileNotFoundError(FileNotFoundError):
    """Raised when a memory file is requested but does not exist."""


class MemoryFileCorruptError(ValueError):
    """Raised when a memory file cannot be parsed (bad YAML, missing ---, etc.)."""


def compute_content_hash(content: bytes) -> str:
    """SHA-256 hex digest of raw bytes."""
    if not isinstance(content, bytes):
        raise TypeError(f"content must be bytes, got {type(content).__name__}")
    return hashlib.sha256(content).hexdigest()


def serialize_memory(frontmatter_dict: Dict[str, Any], body: str = "") -> str:
    """Render a memory to its on-disk string form.

    Produces:
      - A leading '---\\n' line
      - YAML dump of frontmatter_dict (deterministic key order as provided,
        sort_keys=False, allow_unicode=True)
      - '---\\n' separator
      - Markdown body (may be empty)
      - Always ends with a trailing newline
    """
    if not isinstance(frontmatter_dict, dict):
        raise TypeError(
            f"frontmatter_dict must be a dict, got {type(frontmatter_dict).__name__}"
        )
    if not isinstance(body, str):
        raise TypeError(f"body must be str, got {type(body).__name__}")

    yaml_block = yaml.safe_dump(
        dict(frontmatter_dict),
        sort_keys=False,
        allow_unicode=True,
        default_flow_style=False,
    )
    result = f"---\n{yaml_block}---\n{body}"
    if not result.endswith("\n"):
        result += "\n"
    return result


def deserialize_memory(raw: str) -> Tuple[Dict[str, Any], str]:
    """Parse a memory file's string content into (frontmatter_dict, body).

    Requires the opening and closing '---' delimiters to each be their own
    exact line (no trimming). Raises MemoryFileCorruptError on malformed input.
    """
    if raw is None:
        raise TypeError("raw must be a str, got NoneType")
    if not isinstance(raw, str):
        raise TypeError(f"raw must be a str, got {type(raw).__name__}")

    lines = raw.splitlines()

    if not lines or lines[0] != "---":
        raise MemoryFileCorruptError(
            "Memory file does not start with a '---' frontmatter delimiter line."
        )

    # Scan for the closing delimiter starting from line 1
    closing_index = None
    for i in range(1, len(lines)):
        if lines[i] == "---":
            closing_index = i
            break

    if closing_index is None:
        raise MemoryFileCorruptError(
            "Memory file missing closing '---' frontmatter delimiter line."
        )

    yaml_text = "\n".join(lines[1:closing_index])
    body_lines = lines[closing_index + 1 :]
    body = "\n".join(body_lines)

    try:
        fm = yaml.safe_load(yaml_text)
    except yaml.YAMLError as exc:
        raise MemoryFileCorruptError(
            f"Memory file has invalid YAML frontmatter: {exc}"
        ) from exc

    if fm is None:
        fm = {}
    if not isinstance(fm, dict):
        raise MemoryFileCorruptError(
            f"Memory file frontmatter must be a YAML mapping, got {type(fm).__name__}."
        )

    return fm, body


def read_memory_file(path: Path) -> Tuple[Dict[str, Any], str, str]:
    """Read a memory file from disk.

    Returns (frontmatter_dict, body, content_hash).
    content_hash is the SHA-256 of the raw UTF-8 file bytes as a hex string.

    Raises:
        TypeError: when path is None or not a Path.
        MemoryFileNotFoundError: when path does not exist.
        MemoryFileCorruptError: when the file cannot be parsed.
    """
    if path is None:
        raise TypeError("path must be a Path, got NoneType")
    if not isinstance(path, Path):
        raise TypeError(f"path must be a Path, got {type(path).__name__}")

    shared = cidx_meta_content.shared_cidx_meta_for(path)
    if shared is not None:
        try:
            raw_bytes = shared.read_bytes(path)
        except FileNotFoundError:
            raise MemoryFileNotFoundError(f"Memory file not found: {path}") from None
    elif not path.exists():
        raise MemoryFileNotFoundError(f"Memory file not found: {path}")
    else:
        raw_bytes = path.read_bytes()
    content_hash = compute_content_hash(raw_bytes)

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryFileCorruptError(f"Memory file is not valid UTF-8: {path}") from exc

    frontmatter_dict, body = deserialize_memory(raw_text)
    return frontmatter_dict, body, content_hash


<<<<<<< HEAD
def _shared_precondition(
    shared: "cidx_meta_content.SharedCidxMeta",
    path: Path,
    expected_content_hash: Optional[str],
) -> Any:
    """The writer precondition for a shared write or delete.

    ``None`` content hash: the file must not exist (a create). Otherwise the
    current bytes must hash to it, and the write is conditional on exactly
    those bytes (their blob sha), so a change by another pod in between is a
    conflict rather than a lost update.
    """
    if expected_content_hash is None:
        return None
    try:
        data, blob_sha = shared.read_with_sha(path)
    except FileNotFoundError:
        raise MemoryFileNotFoundError(f"Memory file not found: {path}") from None
    if compute_content_hash(data) != expected_content_hash:
        raise cidx_meta_content.CidxMetaConflictError(
            f"Memory file {path} changed since it was read"
        )
    return blob_sha
=======
# Upper bound on the bytes read_memory_frontmatter consumes from one file.
MAX_FRONTMATTER_BYTES = 64 * 1024


def read_memory_frontmatter(
    path: Path, max_bytes: int = MAX_FRONTMATTER_BYTES
) -> Dict[str, Any]:
    """Read only a memory file's frontmatter, never its body.

    Consumes lines up to and including the closing '---' line, at most
    max_bytes bytes, and parses them with deserialize_memory.

    Raises:
        TypeError: when path is not a Path.
        ValueError: when max_bytes is not a positive integer.
        MemoryFileNotFoundError: when path does not exist.
        MemoryFileCorruptError: when the frontmatter is missing, unterminated
            within max_bytes, not UTF-8, or not a YAML mapping.
    """
    if not isinstance(path, Path):
        raise TypeError(f"path must be a Path, got {type(path).__name__}")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes < 1:
        raise ValueError(f"max_bytes must be a positive integer, got {max_bytes!r}")

    header = bytearray()
    try:
        with path.open("rb") as fh:
            # Each pass consumes >= 1 byte or stops, so max_bytes + 1 passes suffice.
            for line_no in range(max_bytes + 1):
                line = fh.readline(max_bytes - len(header) + 1)
                if not line:
                    raise MemoryFileCorruptError(
                        f"Memory file missing closing frontmatter delimiter: {path}"
                    )
                header += line
                if len(header) > max_bytes:
                    raise MemoryFileCorruptError(
                        f"Memory file frontmatter exceeds {max_bytes} bytes: {path}"
                    )
                is_delimiter = line.rstrip(b"\r\n") == b"---"
                if line_no == 0 and not is_delimiter:
                    raise MemoryFileCorruptError(
                        f"Memory file does not start with a '---' line: {path}"
                    )
                if line_no > 0 and is_delimiter:
                    break
    except FileNotFoundError as exc:
        raise MemoryFileNotFoundError(f"Memory file not found: {path}") from exc

    try:
        header_text = header.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryFileCorruptError(f"Memory file is not valid UTF-8: {path}") from exc

    frontmatter_dict, _body = deserialize_memory(header_text)
    return frontmatter_dict
>>>>>>> upstream/master


def atomic_write_memory_file(
    path: Path,
    frontmatter_dict: Dict[str, Any],
    body: str = "",
    *,
    expected_content_hash: Optional[str] = None,
) -> str:
    """Write a memory file atomically (tempfile + os.replace).

    - Creates parent directories if missing.
    - Uses tempfile.mkstemp(dir=path.parent, suffix='.tmp') in the same
      directory so os.replace is atomic on the same filesystem.
    - Best-effort cleanup of temp file on write failure (logs, does not mask
      the original exception).
    - Returns the SHA-256 content_hash of the bytes that were written
      (matches what read_memory_file would compute).

    Does NOT take any locks (caller owns locking).

    Shared cidx-meta (delegated content backend): written through the content
    writer; ``expected_content_hash`` None means the file must not exist yet,
    otherwise the file must still hold the bytes with that hash
    (CidxMetaConflictError if not). Ignored on the local disk, where the
    caller's locks and hash check govern exactly as before.

    Raises:
        TypeError: when path is None or not a Path.
    """
    if path is None:
        raise TypeError("path must be a Path, got NoneType")
    if not isinstance(path, Path):
        raise TypeError(f"path must be a Path, got {type(path).__name__}")

    shared = cidx_meta_content.shared_cidx_meta_for(path)
    if shared is not None:
        content_bytes = serialize_memory(frontmatter_dict, body).encode("utf-8")
        expected = _shared_precondition(shared, path, expected_content_hash)
        shared.put(path, content_bytes, expected=expected)
        return compute_content_hash(content_bytes)

    path.parent.mkdir(parents=True, exist_ok=True)

    content = serialize_memory(frontmatter_dict, body)
    content_bytes = content.encode("utf-8")

    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        os.replace(tmp_path, str(path))
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError as cleanup_exc:
            logger.debug(
                "atomic_write_memory_file: temp file cleanup failed "
                "(non-fatal, original exception will propagate): %s",
                cleanup_exc,
            )
        raise

    return compute_content_hash(content_bytes)


def atomic_delete_memory_file(
    path: Path, *, expected_content_hash: Optional[str] = None
) -> None:
    """Delete a memory file atomically.

    Shared cidx-meta: deleted through the content writer, conditional on the
    bytes with ``expected_content_hash`` when given (as for a write).

    Raises:
        TypeError: when path is None or not a Path.
        MemoryFileNotFoundError: when path does not exist.

    Caller owns locking.
    """
    if path is None:
        raise TypeError("path must be a Path, got NoneType")
    if not isinstance(path, Path):
        raise TypeError(f"path must be a Path, got {type(path).__name__}")

    shared = cidx_meta_content.shared_cidx_meta_for(path)
    if shared is not None:
        if expected_content_hash is None:
            if not shared.exists(path):
                raise MemoryFileNotFoundError(f"Memory file not found: {path}")
            expected: Any = cidx_meta_content.UNCONDITIONAL
        else:
            expected = _shared_precondition(shared, path, expected_content_hash)
        if not shared.delete(path, expected=expected):
            raise MemoryFileNotFoundError(f"Memory file not found: {path}")
        return

    if not path.exists():
        raise MemoryFileNotFoundError(f"Memory file not found: {path}")
    path.unlink()
