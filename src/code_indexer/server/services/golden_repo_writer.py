"""
Golden-repo file writes through the content writer, on a pod without the repository.

Under a delegated content backend the golden repositories are not on this pod:
reads come from the content backend and writes go to the content writer (the
``code_indexer.content_writer`` seam, workspace-mcp's ``POST /content/write``),
both at the repository's absolute path. ``FileCRUDService`` uses this module
for its write-exception (golden) repos ONLY when the running server's content
backend is non-local; under the local default it is never consulted and the
service writes the disk exactly as before.

What this module owns:

* WHERE: the root the content backend serves (a pod-local snapshot maps to its
  golden repo's base clone, exactly as the read tools map it), and the
  lexically confined repo-relative path (there is no local tree to resolve
  symlinks against; the writer refuses symlinks, ``.git`` and paths outside
  its write roots itself).
* READ-FOR-CAS: the current bytes come from the content backend; the git blob
  sha of exactly those bytes is the writer's ``expected_blob_sha``, so a
  concurrent writer between the read and the write is a conflict, never a
  lost update.
* ERRORS: the writer's ``code`` becomes the exception type the CRUD front
  doors (MCP and REST) already handle, so callers see today's shapes.
"""

from __future__ import annotations

import errno
import os
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Dict, Optional, Union

from code_indexer.server.services.file_crud_service import (
    CRUDOperationError,
    HashMismatchError,
)
from code_indexer.server.services.file_service import (
    ContentSeamSource,
    content_seam_source,
    fetch_one,
)
from code_indexer.utils.path_confinement import confine_relative_path

#: Fetch errors that mean "the file is there but could not be read": a
#: directory, or a file the backend will not serve. Every OTHER fetch error
#: reads as "not found", the rule the read tools already apply (workspace-mcp
#: answers EACCES, not ENOENT, for a file under a missing directory).
_UNREADABLE_FETCH_ERRORS = frozenset(
    {"EISDIR", os.strerror(errno.EISDIR), "EFBIG", "ESIZECHANGED"}
)

#: Writer codes by the exception the CRUD front doors already map.
_PERMISSION_CODES = frozenset({"EFORBIDDEN"})
_INVALID_CODES = frozenset({"EINVAL", "ETOOLARGE"})
ECONFLICT = "ECONFLICT"
ESECRET = "ESECRET"

#: An ``expected_blob_sha`` of "no precondition": the kwarg is not sent at all,
#: so this module never needs the plugin's UNSET sentinel.
UNCONDITIONAL = object()
Expected = Union[None, str, object]

_CONCURRENT_EDIT = "File was modified by another writer while this change was applied."


@dataclass(frozen=True)
class GoldenRepoWriteTarget:
    """A golden repo whose files are read through the content backend and
    written through the content writer."""

    source: ContentSeamSource
    writer: Any

    def confine(self, file_path: str) -> str:
        """The repo-relative path the seam speaks; PermissionError if refused."""
        rel = confine_relative_path(file_path)
        if not rel:
            raise PermissionError("Access denied")
        return rel

    def absolute(self, rel: str) -> str:
        """The absolute path the writer takes: the backend's root plus *rel*."""
        return str(PurePosixPath(self.source.root) / rel)

    def read(self, rel: str, file_path: str) -> bytes:
        """The file's current bytes, or today's not-found / read-failure error."""
        fetched = fetch_one(self.source, rel)
        if fetched.error is None:
            return fetched.data
        if fetched.error in _UNREADABLE_FETCH_ERRORS:
            raise CRUDOperationError(
                f"Failed to read file '{file_path}': {fetched.error}"
            )
        raise FileNotFoundError(f"File not found: {file_path}")

    def put(
        self,
        rel: str,
        data: bytes,
        expected: Expected,
        *,
        file_path: str,
        operation: str,
    ) -> None:
        """Write *data* atomically; *expected* is the CAS precondition."""
        try:
            self.writer.put(self.absolute(rel), data, **_cas(expected))
        except Exception as exc:
            raise writer_error(exc, file_path=file_path, operation=operation) from exc

    def delete(
        self, rel: str, expected: Expected, *, file_path: str, operation: str
    ) -> bool:
        """Delete the file; False when it was already absent."""
        try:
            return bool(self.writer.delete(self.absolute(rel), **_cas(expected)))
        except Exception as exc:
            raise writer_error(exc, file_path=file_path, operation=operation) from exc


def golden_repo_write_target(
    repo_path: Union[str, Path],
) -> Optional[GoldenRepoWriteTarget]:
    """The delegated target for a golden repo, or None to write the local disk.

    None -- today's behaviour -- unless the running server's content backend is
    non-local. Then a writer MUST be parked: startup refuses a delegated
    backend without one, and this refuses again rather than writing a disk
    that holds no repository.
    """
    from code_indexer.server.startup.content_writer_wiring import (
        server_content_writer,
    )

    source = content_seam_source(str(repo_path))
    if source is None:
        return None
    writer = server_content_writer()
    if writer is None:
        raise CRUDOperationError(
            "Repository files are not on this server (delegated content backend) "
            "and no content writer is configured; refusing to write the local disk."
        )
    return GoldenRepoWriteTarget(source=source, writer=writer)


def writer_error(exc: Exception, *, file_path: str, operation: str) -> Exception:
    """The CRUD exception for a writer failure, by its ``code``.

    ECONFLICT is the caller's precondition failing: "already exists" for a
    create, a hash mismatch for an edit or a conditional delete. ESECRET says
    the content was refused for a secret; the writer's message names the rule,
    never the value, and nothing here adds the content.
    """
    code = getattr(exc, "code", None)
    detail = _detail(exc, code)
    if code == ECONFLICT:
        if operation == "create_file":
            return FileExistsError(f"File already exists: {file_path}")
        return HashMismatchError(
            f"Content hash mismatch for '{file_path}'. {_CONCURRENT_EDIT}"
        )
    if code == ESECRET:
        return ValueError(
            f"{operation} refused: the content of '{file_path}' appears to contain "
            f"a secret ({detail}). Remove it and retry."
        )
    if code in _PERMISSION_CODES:
        return PermissionError(f"{operation} blocked: {detail}")
    if code in _INVALID_CODES:
        return ValueError(f"{operation} refused for '{file_path}': {detail}")
    return CRUDOperationError(f"Failed to write file '{file_path}': {detail}")


def _detail(exc: Exception, code: Optional[str]) -> str:
    """The writer's message without its ``CODE: `` prefix."""
    text = str(exc)
    prefix = f"{code}: "
    return text[len(prefix) :] if code and text.startswith(prefix) else text


def _cas(expected: Expected) -> Dict[str, Any]:
    """The writer kwargs for *expected*: none at all when unconditional."""
    if expected is UNCONDITIONAL:
        return {}
    return {"expected_blob_sha": expected}
