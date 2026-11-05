"""Files' text for full-text indexing: read from disk, or fetched a batch at a time.

The FTS builders read every discovered file as text. Under a delegated content
backend the repository is not on this container's disk, so the bytes come from
the backend instead -- planned into byte-budgeted batches the way the indexing
pass plans them, never one request per file.

Decoding is the SAME machinery ``open(path, "r", encoding="utf-8", errors=...)``
uses -- ``io.TextIOWrapper`` with universal newlines -- applied to the fetched
bytes, so a delegated build stores exactly the text a local one would: CRLF and
lone CR become LF, a BOM is kept, and invalid bytes follow the caller's
``errors`` policy. A hand-rolled ``bytes.decode`` would not translate newlines,
and the stored documents would differ.

A per-file failure is yielded, not raised: the callers count and skip it, as
they always have. There is no fallback to the disk under a backend.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, NamedTuple, Optional

from ..server.storage.shared.content_backend import NOT_FOUND_FETCH_ERRORS
from .fetch_planner import plan_fetch_batches

if TYPE_CHECKING:
    from .repo_manifest import RepoManifest


class FileText(NamedTuple):
    path: Path
    text: Optional[str]
    error: Optional[str]


def decode_like_text_mode(data: bytes, errors: str) -> str:
    """Bytes to text exactly as a text-mode ``open(..., encoding="utf-8")`` read."""
    with io.TextIOWrapper(
        io.BytesIO(data), encoding="utf-8", errors=errors, newline=None
    ) as stream:
        return stream.read()


def iter_file_texts(
    files: List[Path],
    codebase_dir: Path,
    *,
    errors: str,
    manifest: Optional["RepoManifest"] = None,
    backend: Optional[Any] = None,
) -> Iterator[FileText]:
    """Yield each file's text in order: from ``backend`` when given, else disk."""
    if backend is None:
        for file_path in files:
            try:
                with open(file_path, "r", encoding="utf-8", errors=errors) as f:
                    text = f.read()
            except Exception as exc:  # each caller skips and counts a failure
                yield FileText(file_path, None, str(exc))
                continue
            yield FileText(file_path, text, None)
        return

    for record in iter_file_bytes(
        files, codebase_dir, manifest=manifest, backend=backend
    ):
        if record.data is None:
            yield FileText(record.path, None, record.error)
            continue
        yield FileText(record.path, decode_like_text_mode(record.data, errors), None)


class FileBytes(NamedTuple):
    """One file's bytes from a content backend, or why there are none.

    ``missing``: the backend answered "no such file" (``NOT_FOUND_FETCH_ERRORS``)
    rather than failing to read it -- a caller that must tell an absent file
    from an unreadable one (the FTS retry of a file deleted mid-run) asks this.
    """

    path: Path
    data: Optional[bytes]
    error: Optional[str]
    missing: bool = False


def iter_file_bytes(
    files: List[Path],
    codebase_dir: Path,
    *,
    manifest: Optional["RepoManifest"],
    backend: Any,
) -> Iterator[FileBytes]:
    """Yield each file's bytes from ``backend``, fetched in byte-budgeted
    batches planned from ``manifest``'s sizes. A per-file failure is yielded,
    never raised, and never answered from the local disk."""
    if manifest is None:
        # Without sizes the planner sends every file alone: one request (and,
        # for the plugin, one connection) per file -- the failure batching
        # exists to prevent. Refused rather than quietly degraded.
        raise ValueError("a delegated read needs the pass's manifest for sizes")
    root = Path(codebase_dir)
    by_rel: Dict[str, Path] = {}
    for file_path in files:
        try:
            by_rel[Path(file_path).relative_to(root).as_posix()] = file_path
        except ValueError:
            yield FileBytes(file_path, None, "outside the repository root")
    for batch in plan_fetch_batches(list(by_rel), manifest.size):
        try:
            records = {r.path: r for r in backend.fetch(root, batch)}
        except Exception as exc:  # the backend's transport, whatever it is
            for rel in batch:
                yield FileBytes(by_rel[rel], None, f"content fetch failed: {exc}")
            continue
        for rel in batch:
            record = records.get(rel)
            if record is None or record.error is not None:
                reason = "no record" if record is None else record.error
                yield FileBytes(
                    by_rel[rel],
                    None,
                    reason,
                    missing=record is not None and reason in NOT_FOUND_FETCH_ERRORS,
                )
                continue
            yield FileBytes(by_rel[rel], record.data, None)
