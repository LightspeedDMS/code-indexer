"""Deterministic serialization and atomic on-disk I/O for memory files.

Memory files live at cidx-meta/memories/{uuid}.md and consist of a YAML
frontmatter block followed by an optional markdown body.

Story #877 Phase 1b.
"""

import hashlib
import logging
import os
import tempfile
from pathlib import Path
from typing import Any, Dict, Tuple

import yaml

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
    body_lines = lines[closing_index + 1:]
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

    if not path.exists():
        raise MemoryFileNotFoundError(f"Memory file not found: {path}")

    raw_bytes = path.read_bytes()
    content_hash = compute_content_hash(raw_bytes)

    try:
        raw_text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise MemoryFileCorruptError(
            f"Memory file is not valid UTF-8: {path}"
        ) from exc

    frontmatter_dict, body = deserialize_memory(raw_text)
    return frontmatter_dict, body, content_hash


def atomic_write_memory_file(
    path: Path,
    frontmatter_dict: Dict[str, Any],
    body: str = "",
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

    Raises:
        TypeError: when path is None or not a Path.
    """
    if path is None:
        raise TypeError("path must be a Path, got NoneType")
    if not isinstance(path, Path):
        raise TypeError(f"path must be a Path, got {type(path).__name__}")

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


def atomic_delete_memory_file(path: Path) -> None:
    """Delete a memory file atomically.

    Raises:
        TypeError: when path is None or not a Path.
        MemoryFileNotFoundError: when path does not exist.

    Caller owns locking.
    """
    if path is None:
        raise TypeError("path must be a Path, got NoneType")
    if not isinstance(path, Path):
        raise TypeError(f"path must be a Path, got {type(path).__name__}")

    if not path.exists():
        raise MemoryFileNotFoundError(f"Memory file not found: {path}")
    path.unlink()
