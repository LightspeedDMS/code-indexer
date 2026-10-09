"""Serve one indexing pass's file bytes from a content backend, a batch at a time.

The high-throughput pass submits every file to a thread pool up front and each
worker then wants ONE file's bytes. Asking the backend per file is the failure
this seam exists to prevent -- a request (and, for the delegated plugin, a TCP
connection) per file, see ``fetch_planner``. Fetching everything up front holds a
repository's worth of bytes. This sits between the two:

* The pass's paths are planned into byte-budgeted batches once, in submission
  order, by ``plan_fetch_batches``.
* The first worker to want a path from a batch fetches the WHOLE batch; workers
  wanting other paths of the same batch wait for it rather than fetching again.
* Each path's bytes are dropped the moment its worker takes them, and a batch is
  forgotten once every path in it has been taken.

Workers drain the pool in roughly submission order, so only the one or two
batches straddling the workers' current position are ever held: peak memory is
about ``budget_bytes`` times that, whatever the repository's size. Calls are
exactly one per planned batch -- ``ceil(selected bytes / budget)`` in practice --
and never one per file.

A file that changed since the manifest is REFUSED, not indexed. The manifest's
blob hash has already gone into the file's metadata by the time its bytes are
fetched; indexing different bytes under it would store a payload describing
content that was never chunked. The next pass sees the new content in its own
manifest and indexes it then.
"""

from __future__ import annotations

import errno
import logging
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence

from ..server.storage.shared.content_backend import ContentBackend, FetchedFile
from .fetch_planner import (
    DEFAULT_FETCH_BUDGET_BYTES,
    DEFAULT_FETCH_BUDGET_FILES,
    DEFAULT_FETCH_BUDGET_REQUEST_BYTES,
    plan_fetch_batches,
)

logger = logging.getLogger(__name__)

#: Error strings meaning "this file is gone". The local backend reports
#: ``os.strerror(errno)``; workspace-mcp reports the bare errno name.
_VANISHED_ERRORS = frozenset({"ENOENT", os.strerror(errno.ENOENT)})

#: Longest transport-failure reason kept, in the log and on each failed file.
_MAX_REASON_CHARS = 300

#: Recorded for a file whose fetched bytes no longer hash to the manifest's value.
CHANGED_SINCE_MANIFEST = "changed since the manifest was taken"


@dataclass(frozen=True)
class PrefetchedContent:
    """One file's bytes for a worker, or why it cannot have them."""

    data: Optional[bytes]
    error: Optional[str] = None

    @property
    def vanished(self) -> bool:
        """The file is gone -- the pass's benign skip, not a failure."""
        return self.error in _VANISHED_ERRORS


class _Batch:
    __slots__ = ("paths", "lock", "files", "failure")

    def __init__(self, paths: List[str]) -> None:
        self.paths = paths
        self.lock = threading.Lock()
        self.files: Optional[Dict[str, FetchedFile]] = None
        self.failure: Optional[str] = None


class ContentPrefetcher:
    """Hands each worker its file's bytes, fetched a batch at a time."""

    def __init__(
        self,
        backend: ContentBackend,
        repo_root: Path,
        rel_paths: Sequence[str],
        size_of: Callable[[str], Optional[int]],
        expected_blob_sha: Callable[[str], Optional[str]],
        *,
        budget_bytes: int = DEFAULT_FETCH_BUDGET_BYTES,
        budget_files: int = DEFAULT_FETCH_BUDGET_FILES,
        budget_request_bytes: int = DEFAULT_FETCH_BUDGET_REQUEST_BYTES,
    ) -> None:
        self._backend = backend
        self._root = Path(repo_root)
        self._expected_blob_sha = expected_blob_sha
        self._batch_of: Dict[str, _Batch] = {}
        for paths in plan_fetch_batches(
            rel_paths,
            size_of,
            budget_bytes=budget_bytes,
            budget_files=budget_files,
            budget_request_bytes=budget_request_bytes,
        ):
            batch = _Batch(paths)
            for path in paths:
                self._batch_of[path] = batch
        # Guards _batch_of only; each batch's fetch is under its own lock so
        # workers on different batches never wait on each other.
        self._index_lock = threading.Lock()

    def relative(self, file_path: Path) -> Optional[str]:
        """The repo-relative POSIX path the backend speaks, or None if outside."""
        try:
            return Path(file_path).relative_to(self._root).as_posix()
        except ValueError:
            return None

    def take(self, file_path: Path) -> PrefetchedContent:
        """This file's bytes, once. A second take of the same path is refused."""
        rel = self.relative(file_path)
        if rel is None:
            return PrefetchedContent(None, "outside the repository root")
        with self._index_lock:
            batch = self._batch_of.pop(rel, None)
        if batch is None:
            return PrefetchedContent(None, "not planned for this pass")

        with batch.lock:
            if batch.files is None and batch.failure is None:
                self._load(batch)
            if batch.failure is not None:
                return PrefetchedContent(None, batch.failure)
            assert batch.files is not None  # _load sets one or the other
            fetched = batch.files.pop(rel, None)

        if fetched is None:
            return PrefetchedContent(None, "the backend returned no record")
        if fetched.error is not None:
            return PrefetchedContent(None, fetched.error)
        expected = self._expected_blob_sha(rel)
        if expected is not None and fetched.blob_sha != expected:
            return PrefetchedContent(None, CHANGED_SINCE_MANIFEST)
        return PrefetchedContent(fetched.data)

    def _load(self, batch: _Batch) -> None:
        """Fetch a whole batch. A transport failure fails every file in it."""
        try:
            batch.files = {
                record.path: record
                for record in self._backend.fetch(self._root, batch.paths)
            }
        except Exception as exc:  # the backend's transport, whatever it is
            # The message, not just the type: "ContentMcpError" alone cannot
            # tell a 503 from a timeout from a refused connection. The plugin's
            # messages carry the route, status and a capped body excerpt --
            # never the token -- and are capped again here.
            reason = f"{type(exc).__name__}: {exc}"[:_MAX_REASON_CHARS]
            logger.error(
                "Content fetch failed for a batch of %d files: %s",
                len(batch.paths),
                reason,
            )
            batch.failure = f"content fetch failed: {reason}"
