# ^ invariant a never-indexed path is never planned, never a failed file, never rescued by an override; #2196 #2198
from __future__ import annotations

import logging
import threading
from pathlib import PurePath
from typing import FrozenSet, Set

LEGACY_LEASE_DIRECTORY_NAME = ".snapshot-reader-leases"
NEVER_INDEXED_DIRECTORIES: FrozenSet[str] = frozenset({LEGACY_LEASE_DIRECTORY_NAME})

_SURROGATE_ESCAPE_LOW = 0xDC80
_SURROGATE_ESCAPE_HIGH = 0xDCFF


# ^ external sqlite3 and tantivy refuse a str holding lone surrogates => a non-UTF-8 name cannot be stored
def has_unstorable_name(relative_path: str) -> bool:
    try:
        relative_path.encode("utf-8")
    except UnicodeEncodeError:
        return True
    return False


def in_never_indexed_directory(relative_path: str) -> bool:
    return not NEVER_INDEXED_DIRECTORIES.isdisjoint(PurePath(relative_path).parts)


def is_never_indexed(relative_path: str) -> bool:
    return has_unstorable_name(relative_path) or in_never_indexed_directory(
        relative_path
    )


def _escaped_char(char: str) -> str:
    code = ord(char)
    if _SURROGATE_ESCAPE_LOW <= code <= _SURROGATE_ESCAPE_HIGH:
        return f"\\x{code - 0xDC00:02x}"
    if char.isprintable():
        return char
    return ascii(char)[1:-1]


# ^ security escaped_path output is log-safe; an undecodable byte renders as \xNN, a control char escaped
def escaped_path(relative_path: str) -> str:
    return "".join(_escaped_char(char) for char in relative_path)


def never_indexed_reason(relative_path: str) -> str:
    if has_unstorable_name(relative_path):
        return "its name is not valid UTF-8 and cannot be stored"
    return "it is snapshot reader lease bookkeeping"


NAMED_SKIP_LIMIT = 5
TRACKED_SKIP_LIMIT = 10_000


class SkippedPathReporter:
    # ^ invariant state is per instance and retains <= TRACKED_SKIP_LIMIT names; never module-level
    def __init__(
        self, logger: logging.Logger, *, where: str, noun: str, summary_subject: str
    ) -> None:
        self._logger = logger
        self._where = where
        self._noun = noun
        self._summary_subject = summary_subject
        self._names: Set[str] = set()
        self._summarized = 0
        self._lock = threading.Lock()

    @property
    def tracked_count(self) -> int:
        with self._lock:
            return len(self._names)

    def report(self, relative_path: str, reason: str) -> None:
        with self._lock:
            if relative_path in self._names or len(self._names) >= TRACKED_SKIP_LIMIT:
                return
            self._names.add(relative_path)
            count = len(self._names)
        if count > NAMED_SKIP_LIMIT:
            return
        self._logger.warning(
            "Not indexing %s%s: %s (%d such %s skipped in this run%s)",
            escaped_path(relative_path),
            self._where,
            reason,
            count,
            self._noun,
            "; further ones are counted at the end"
            if count == NAMED_SKIP_LIMIT
            else "",
        )

    def summarize(self) -> None:
        with self._lock:
            count = len(self._names)
            if count <= NAMED_SKIP_LIMIT or count == self._summarized:
                return
            self._summarized = count
        self._logger.warning(
            "Skipped %d%s %s (%d named above)",
            count,
            " or more" if count >= TRACKED_SKIP_LIMIT else "",
            self._summary_subject,
            NAMED_SKIP_LIMIT,
        )
