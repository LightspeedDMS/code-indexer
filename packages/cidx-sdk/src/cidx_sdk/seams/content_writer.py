"""The content writer seam: write and delete a repository's files.

The write side of the content seam, selected by the SAME ``content_backend``
setting (reads and writes cannot disagree about where the files are);
entry-point group ``code_indexer.content_writer``. Under ``local`` the core
writes this pod's disk directly and resolves no writer; under ``delegated`` the
writer is the remote service's (the content service ``POST /content/write``).

Paths are ABSOLUTE as the content backend knows them (the seam root plus the
repo-relative path). ``expected_blob_sha`` is a compare-and-swap on git's blob
sha: leave it out for an unconditional write, pass ``None`` for "must not
exist", a sha for "must currently be exactly this blob". A failure raises an
exception carrying a string ``code`` attribute, one of :data:`WRITER_CODES`.
"""

from __future__ import annotations

from typing import Any, FrozenSet, Optional, Protocol, runtime_checkable

#: A failed ``expected_blob_sha`` precondition.
ECONFLICT = "ECONFLICT"
#: The path or request is invalid.
EINVAL = "EINVAL"
#: The service refuses to write there (for example under ``.git``).
EFORBIDDEN = "EFORBIDDEN"
#: The service cannot take writes at all (its write root is not usable).
EUNAVAILABLE = "EUNAVAILABLE"
#: The service could not be reached or answered something unreadable.
ETRANSPORT = "ETRANSPORT"
#: The payload exceeds the service's size limit.
ETOOLARGE = "ETOOLARGE"
#: The content was refused by the service's secret scanner.
ESECRET = "ESECRET"
#: Any other I/O failure.
EIO = "EIO"
WRITER_CODES: FrozenSet[str] = frozenset(
    {ECONFLICT, EINVAL, EFORBIDDEN, EUNAVAILABLE, ETRANSPORT, ETOOLARGE, ESECRET, EIO}
)


#: An ``expected_blob_sha`` that was not passed: an unconditional write. Use
#: it as the keyword's default; ``None`` means "must not exist".
UNSET: Any = object()


class ContentWriteError(Exception):
    """A writer failure carrying its ``code`` (one of :data:`WRITER_CODES`)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


@runtime_checkable
class ContentWriter(Protocol):
    """Writes and deletes files of a repository held by the content seam.

    ``expected_blob_sha`` is passed as a keyword only when the caller has a
    precondition; implementations must treat its ABSENCE as "unconditional",
    which is different from an explicit ``None`` ("must not exist").
    """

    def put(
        self, path: str, data: bytes, *, expected_blob_sha: Optional[str] = ...
    ) -> object:
        """Write *data* atomically to *path*; returns the new blob sha."""
        ...

    def delete(self, path: str, *, expected_blob_sha: Optional[str] = ...) -> bool:
        """Delete *path*; False when it was already absent (not an error)."""
        ...


def writer_error_code(exc: BaseException) -> Optional[str]:
    """The writer ``code`` *exc* carries, or None for any other exception."""
    code = getattr(exc, "code", None)
    return code if isinstance(code, str) else None
