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

import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

from .models import CommitInfo

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
    codebase_dir: Path, commit: CommitInfo, diff_context_lines: int = 5
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
            "diff",
            "-M",  # plain `git diff` does not detect renames by default (AC24)
            f"-U{diff_context_lines}",
            "--full-index",
            base_ref,
            commit.hash,
        ],
        cwd=codebase_dir,
        capture_output=True,
        text=True,
        errors="replace",
        check=True,
    )
    return _parse_diff_output(result.stdout)


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
        if current_path is None or current_diff_type is None:
            return
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
            parts = line.split()
            old_path = parts[2][2:] if parts[2].startswith("a/") else parts[2]
            new_path = parts[3][2:] if parts[3].startswith("b/") else parts[3]
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
            current_old_path = line.split("rename from ", 1)[1]
        elif line.startswith("rename to "):
            current_diff_type = "renamed"
            current_path = line.split("rename to ", 1)[1]
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
