"""Shared per-commit document aggregator (Story #1290 / Epic #1289).

Collapses the legacy per-file-diff-vector layout into ONE aggregated document
per commit: the commit message once at the head, followed by each changed
file's diff prefixed with a ``--- <path> ---`` header. Binary files and PURE
renames (no content change) are skipped; renames WITH content changes are
included under a rename-aware header (the new path). The diff SOURCE is
selected uniformly by walking to the first parent (root commits fall back to
git's empty-tree sentinel, so a root commit's diff is its full initial tree;
a normal commit's diff is vs its single parent; a merge/octopus commit's
diff is vs its FIRST parent only) -- this single rule satisfies all three
commit-kind cases in the story without special-casing.

A section-range provenance map (start/end char offsets in the aggregated
text) is produced alongside the document so a caller that later chunks the
text can attribute each chunk's overlapping file paths (see
contextual_chunker.py).
"""

import logging
import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional, Tuple

from ...utils.never_indexed_paths import (
    SkippedPathReporter,
    escaped_path,
    is_never_indexed,
    never_indexed_reason,
)
from .models import CommitInfo

logger = logging.getLogger(__name__)

# Git's well-known empty-tree object hash -- diffing against it yields the
# full initial tree for a root commit (no special-casing needed vs a normal
# commit's single-parent diff).
EMPTY_TREE_SHA = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"


@dataclass(frozen=True)
class FileChange:
    """One changed file's diff within a commit's aggregated document.

    Attributes:
        path: Current (new) path of the file.
        diff_type: "added" | "deleted" | "modified" | "renamed" | "binary".
        diff_text: Unified-diff hunk text (empty string for pure renames and
            binary files -- these are filtered out before aggregation).
        old_path: Previous path, populated only for renames.
    """

    path: str
    diff_type: str
    diff_text: str
    old_path: Optional[str] = None


@dataclass(frozen=True)
class ProvenanceSection:
    """One contiguous span of the aggregated document's text.

    `path` is None for the message-head section (the commit message has no
    associated file path).
    """

    start: int
    end: int
    path: Optional[str]


@dataclass(frozen=True)
class AggregatedCommitDocument:
    """Result of aggregating one commit's message + changed-file diffs."""

    text: str
    provenance: List[ProvenanceSection] = field(default_factory=list)
    file_paths: List[str] = field(default_factory=list)


def commit_kind(commit: CommitInfo) -> str:
    """Classify a commit as "root" (no parents), "normal" (1 parent), or
    "merge" (2+ parents, includes octopus merges -- both use first-parent diff
    identically, so no further distinction is needed).
    """
    parents = commit.parent_hashes.split()
    if not parents:
        return "root"
    if len(parents) == 1:
        return "normal"
    return "merge"


def _diff_base_ref(commit: CommitInfo) -> str:
    """Return the ref to diff against: first parent, or the empty tree for a root commit."""
    parents = commit.parent_hashes.split()
    return parents[0] if parents else EMPTY_TREE_SHA


def get_file_changes(
    codebase_dir: Path,
    commit: CommitInfo,
    diff_context_lines: int = 5,
    *,
    skipped_paths: Optional[SkippedPathReporter] = None,
) -> List[FileChange]:
    """Retrieve the changed files for `commit` via a single `git diff` call.

    Diffs against the first parent (or the empty tree for a root commit) --
    the single rule that correctly implements root=full-initial-tree,
    normal=vs-parent, and merge/octopus=first-parent (AC4, AC25).

    Args:
        codebase_dir: Repository working directory.
        commit: Commit to retrieve changes for.
        diff_context_lines: Unified-diff context line count.

    Returns:
        Ordered list of FileChange, in the order git emits them.
    """
    base_ref = _diff_base_ref(commit)
    result = subprocess.run(
        [
            "git",
            "-c",
            "core.quotePath=false",
            "diff",
            "-M",  # plain `git diff` does not detect renames by default (AC24)
            f"-U{diff_context_lines}",
            "--full-index",
            "--src-prefix=a/",
            "--dst-prefix=b/",
            base_ref,
            commit.hash,
        ],
        cwd=codebase_dir,
        capture_output=True,
        check=True,
    )
    reporter = skipped_paths or new_temporal_skip_reporter()
    kept: List[FileChange] = []
    for change in _parse_diff_output(_decode_diff_output(result.stdout)):
        if is_never_indexed(change.path):
            reporter.report(change.path, never_indexed_reason(change.path))
        else:
            kept.append(change)
    if skipped_paths is None:
        reporter.summarize()
    return kept


def new_temporal_skip_reporter() -> SkippedPathReporter:
    return SkippedPathReporter(
        logger,
        where=" in commit history",
        noun="path(s)",
        summary_subject="never-indexed path(s) in this commit batch",
    )


_PATH_LINE_PREFIXES = (b"diff --git ", b"rename from ", b"rename to ")


# ^ why paths via os.fsdecode, not errors="replace"; a replaced byte names no real file and dodges is_never_indexed
def _decode_diff_output(raw: bytes) -> str:
    lines = raw.replace(b"\r\n", b"\n").replace(b"\r", b"\n").split(b"\n")
    return "\n".join(
        os.fsdecode(line)
        if line.startswith(_PATH_LINE_PREFIXES)
        else line.decode("utf-8", "replace")
        for line in lines
    )


_DIFF_PREFIX_LEN = len(b"a/")
_HEADER_SEPARATOR = b" b/"
_OCTAL_ESCAPE_LEN = len(b"\\303")
_C_ESCAPES = {
    ord("a"): 7,
    ord("b"): 8,
    ord("t"): 9,
    ord("n"): 10,
    ord("v"): 11,
    ord("f"): 12,
    ord("r"): 13,
}


# ^ external git C-quotes a path holding '"', '\\' or control chars even with core.quotePath=false
def _read_quoted(raw: bytes) -> Tuple[bytes, bytes]:
    out = bytearray()
    index = 1
    while index < len(raw):
        byte = raw[index]
        if byte == ord('"'):
            return bytes(out), raw[index + 1 :]
        if byte == ord("\\") and index + 1 < len(raw):
            escaped = raw[index + 1]
            if ord("0") <= escaped <= ord("7"):
                out.append(int(raw[index + 1 : index + _OCTAL_ESCAPE_LEN], 8))
                index += _OCTAL_ESCAPE_LEN
                continue
            out.append(_C_ESCAPES.get(escaped, escaped))
            index += 2
            continue
        out.append(byte)
        index += 1
    raise ValueError(f"unterminated quoted path in git diff: {raw!r}")


def _git_path(field_text: str) -> str:
    raw = os.fsencode(field_text)
    return os.fsdecode(_read_quoted(raw)[0] if raw.startswith(b'"') else raw)


# ^ external git-diff(1) "a/ and b/ filenames are the same unless rename/copy"; renames carry rename from/to lines
# ^ hazard splitting an unquoted header on whitespace or " b/" => a path with spaces is cut; test:test_temporal_paths_with_spaces_are_parsed_whole
def _header_paths(rest_text: str) -> Tuple[Optional[str], Optional[str]]:
    rest = os.fsencode(rest_text)
    if rest.startswith(b'"'):
        old, tail = _read_quoted(rest)
        tail = tail[1:]
        new = _read_quoted(tail)[0] if tail.startswith(b'"') else tail
        return (
            os.fsdecode(old[_DIFF_PREFIX_LEN:]),
            os.fsdecode(new[_DIFF_PREFIX_LEN:]),
        )
    half = (len(rest) - 1) // 2
    separator_end = half + len(_HEADER_SEPARATOR)
    if (
        len(rest) % 2 == 1
        and rest.startswith(b"a/")
        and rest[half:separator_end] == _HEADER_SEPARATOR
        and rest[_DIFF_PREFIX_LEN:half] == rest[separator_end:]
    ):
        path = os.fsdecode(rest[_DIFF_PREFIX_LEN:half])
        return path, path
    return None, None


def _parse_diff_output(diff_output: str) -> List[FileChange]:
    """Parse `git diff` unified output into FileChange entries.

    Unlike the legacy TemporalDiffScanner, this preserves actual hunk content
    for renamed files so a rename WITH content changes is not collapsed to a
    metadata-only string (AC24) -- `diff_type == "renamed"` with non-empty
    `diff_text` is a rename WITH changes; empty `diff_text` is a pure rename.
    """
    changes: List[FileChange] = []

    current_path: Optional[str] = None
    current_old_path: Optional[str] = None
    current_diff_type: Optional[str] = None
    current_hunks: List[str] = []
    in_hunk = False

    def _flush() -> None:
        if current_diff_type is None:
            return
        if current_path is None:
            raise ValueError(
                f"git diff section without a parseable path (old path "
                f"{escaped_path(current_old_path or '')!r})"
            )
        changes.append(
            FileChange(
                path=current_path,
                diff_type=current_diff_type,
                diff_text="\n".join(current_hunks).strip("\n"),
                old_path=current_old_path,
            )
        )

    for line in diff_output.split("\n"):
        if line.startswith("diff --git "):
            _flush()
            old_path, new_path = _header_paths(line[len("diff --git ") :])
            current_path = new_path
            current_old_path = old_path if old_path != new_path else None
            current_diff_type = "modified"
            current_hunks = []
            in_hunk = False
        elif line.startswith("new file mode"):
            current_diff_type = "added"
        elif line.startswith("deleted file mode"):
            current_diff_type = "deleted"
        elif line.startswith("rename from "):
            current_diff_type = "renamed"
            current_old_path = _git_path(line[len("rename from ") :])
        elif line.startswith("rename to "):
            current_diff_type = "renamed"
            current_path = _git_path(line[len("rename to ") :])
        elif line.startswith("Binary files"):
            current_diff_type = "binary"
            current_hunks = []
        elif line.startswith("@@"):
            in_hunk = True
            current_hunks.append(line)
        elif in_hunk:
            current_hunks.append(line)

    _flush()
    return changes


def _is_content_bearing(change: FileChange) -> bool:
    """True iff this change should be embedded (not binary, not a pure rename)."""
    if change.diff_type == "binary":
        return False
    if change.diff_type == "renamed" and not change.diff_text.strip():
        return False
    return True


def build_aggregated_document(
    commit: CommitInfo, file_changes: List[FileChange]
) -> AggregatedCommitDocument:
    """Build the per-commit aggregated document: message-once head + diffs.

    Args:
        commit: The commit whose message forms the head section.
        file_changes: FileChange entries from get_file_changes(); binary and
            pure-rename entries are skipped, renames-with-changes included
            under a rename-aware header (the new path).

    Returns:
        AggregatedCommitDocument with `text`, a provenance map of section
        (start, end, path) spans, and the ordered list of included file paths.
    """
    message = (commit.message or "").rstrip("\n")
    head_text = message + "\n"

    provenance: List[ProvenanceSection] = [
        ProvenanceSection(start=0, end=len(head_text), path=None)
    ]
    parts: List[str] = [head_text]
    cursor = len(head_text)
    included_paths: List[str] = []

    for change in file_changes:
        if not _is_content_bearing(change):
            continue
        header = f"--- {change.path} ---\n"
        body = change.diff_text
        if body and not body.endswith("\n"):
            body += "\n"
        section_text = header + body
        start = cursor
        end = start + len(section_text)
        provenance.append(ProvenanceSection(start=start, end=end, path=change.path))
        parts.append(section_text)
        cursor = end
        included_paths.append(change.path)

    return AggregatedCommitDocument(
        text="".join(parts), provenance=provenance, file_paths=included_paths
    )
