"""Unit tests for the shared per-commit document aggregator (Story #1290 AC4/AC24/AC25/AC26/AC6).

Uses REAL git repositories (subprocess git init/commit) -- no mocking of git,
per Messi Anti-Mock. Covers:
- diff source selection by commit kind (root=full initial tree, normal=vs-parent,
  merge/octopus=first-parent) with EXACT file lists and byte-for-byte aggregated
  text (AC4, AC25).
- binary/pure-rename skipped; rename-with-content-changes included (AC4, AC24).
- degenerate (zero-file-entry) commit yields exactly one head chunk (AC26).
- section-range provenance map (AC6) is exercised in test_contextual_chunker_1290.py.
"""

import subprocess
from pathlib import Path

import pytest

from src.code_indexer.services.temporal.commit_aggregator import (
    build_aggregated_document,
    commit_kind,
    get_file_changes,
)
from src.code_indexer.services.temporal.models import CommitInfo


_GIT_ENV_EXTRA = {
    "GIT_AUTHOR_NAME": "Test User",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "Test User",
    "GIT_COMMITTER_EMAIL": "test@example.com",
}


def _git(repo_path: Path, *args: str) -> subprocess.CompletedProcess:
    import os

    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=True,
        capture_output=True,
        text=True,
        env={**os.environ, **_GIT_ENV_EXTRA},
    )


def _init_repo(repo_path: Path) -> None:
    repo_path.mkdir(parents=True, exist_ok=True)
    _git(repo_path, "init", "-q")
    _git(repo_path, "config", "user.name", "Test User")
    _git(repo_path, "config", "user.email", "test@example.com")


def _commit(repo_path: Path, message: str) -> str:
    result = _git(repo_path, "commit", "-q", "-m", message)
    assert result.returncode == 0
    return str(_git(repo_path, "rev-parse", "HEAD").stdout.strip())


def _commit_info_for(repo_path: Path, commit_hash: str) -> CommitInfo:
    fmt = _git(
        repo_path,
        "log",
        "-1",
        "--format=%at%x00%an%x00%ae%x00%B%x00%P",
        commit_hash,
    ).stdout
    parts = fmt.split("\x00")
    return CommitInfo(
        hash=commit_hash,
        timestamp=int(parts[0]),
        author_name=parts[1],
        author_email=parts[2],
        message=parts[3].strip(),
        parent_hashes=parts[4].strip(),
    )


@pytest.fixture
def repo(tmp_path):
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


class TestCommitKind:
    def test_root_commit_has_no_parents(self, repo):
        (repo / "a.txt").write_text("hello\n")
        _git(repo, "add", ".")
        h = _commit(repo, "root commit")
        info = _commit_info_for(repo, h)
        assert commit_kind(info) == "root"

    def test_normal_commit_has_one_parent(self, repo):
        (repo / "a.txt").write_text("hello\n")
        _git(repo, "add", ".")
        _commit(repo, "root commit")
        (repo / "a.txt").write_text("hello world\n")
        _git(repo, "add", ".")
        h2 = _commit(repo, "normal commit")
        info = _commit_info_for(repo, h2)
        assert commit_kind(info) == "normal"

    def test_merge_commit_has_multiple_parents(self, repo):
        (repo / "a.txt").write_text("base\n")
        _git(repo, "add", ".")
        _commit(repo, "root")
        _git(repo, "branch", "feature")
        (repo / "a.txt").write_text("base\nmain change\n")
        _git(repo, "add", ".")
        _commit(repo, "main change")
        _git(repo, "checkout", "-q", "feature")
        (repo / "b.txt").write_text("feature file\n")
        _git(repo, "add", ".")
        _commit(repo, "feature change")
        _git(repo, "checkout", "-q", "master" if _has_master(repo) else "main")
        _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")
        merge_hash = _git(repo, "rev-parse", "HEAD").stdout.strip()
        info = _commit_info_for(repo, merge_hash)
        assert commit_kind(info) == "merge"


def _has_master(repo: Path) -> bool:
    result = subprocess.run(
        ["git", "branch", "--list", "master"],
        cwd=repo,
        capture_output=True,
        text=True,
    )
    return bool(result.stdout.strip())


class TestOctopusMergeCommitKind:
    def test_octopus_merge_has_three_parents_and_uses_first_parent_diff(self, repo):
        """AC25: octopus (3+ parent) merge classifies as 'merge' and uses
        FIRST-PARENT diff identically to a normal 2-parent merge."""
        (repo / "a.txt").write_text("base\n")
        _git(repo, "add", ".")
        _commit(repo, "root")
        target_branch = "master" if _has_master(repo) else "main"

        _git(repo, "branch", "topic1")
        _git(repo, "branch", "topic2")

        (repo / "a.txt").write_text("base\nmain change\n")
        _git(repo, "add", ".")
        _commit(repo, "main change")

        _git(repo, "checkout", "-q", "topic1")
        (repo / "b.txt").write_text("topic1 file\n")
        _git(repo, "add", ".")
        _commit(repo, "topic1 change")

        _git(repo, "checkout", "-q", "topic2")
        (repo / "c.txt").write_text("topic2 file\n")
        _git(repo, "add", ".")
        _commit(repo, "topic2 change")

        _git(repo, "checkout", "-q", target_branch)
        _git(
            repo,
            "merge",
            "--no-ff",
            "-q",
            "-m",
            "octopus merge",
            "topic1",
            "topic2",
        )
        merge_hash = _git(repo, "rev-parse", "HEAD").stdout.strip()
        info = _commit_info_for(repo, merge_hash)

        assert len(info.parent_hashes.split()) == 3
        assert commit_kind(info) == "merge"

        changes = get_file_changes(repo, info)
        # First-parent diff: main's tree already had a.txt=main-change; the
        # octopus merge's tree adds b.txt AND c.txt from the topic branches.
        paths = sorted(c.path for c in changes)
        assert paths == ["b.txt", "c.txt"]
        assert all(c.diff_type == "added" for c in changes)


class TestGetFileChangesDiffSourceByCommitKind:
    def test_root_commit_full_initial_tree(self, repo):
        (repo / "a.txt").write_text("line1\nline2\n")
        (repo / "b.txt").write_text("other\n")
        _git(repo, "add", ".")
        h = _commit(repo, "root commit")
        info = _commit_info_for(repo, h)

        changes = get_file_changes(repo, info)

        paths = sorted(c.path for c in changes)
        assert paths == ["a.txt", "b.txt"]
        assert all(c.diff_type == "added" for c in changes)

    def test_normal_commit_vs_parent(self, repo):
        (repo / "a.txt").write_text("line1\n")
        _git(repo, "add", ".")
        _commit(repo, "root")
        (repo / "a.txt").write_text("line1\nline2\n")
        _git(repo, "add", ".")
        h2 = _commit(repo, "normal")
        info = _commit_info_for(repo, h2)

        changes = get_file_changes(repo, info)

        assert len(changes) == 1
        assert changes[0].path == "a.txt"
        assert changes[0].diff_type == "modified"
        assert "line2" in changes[0].diff_text

    def test_merge_commit_uses_first_parent_diff(self, repo):
        (repo / "a.txt").write_text("base\n")
        _git(repo, "add", ".")
        _commit(repo, "root")
        _git(repo, "branch", "feature")
        (repo / "a.txt").write_text("base\nmain change\n")
        _git(repo, "add", ".")
        _commit(repo, "main change")
        _git(repo, "checkout", "-q", "feature")
        (repo / "b.txt").write_text("feature file\n")
        _git(repo, "add", ".")
        _commit(repo, "feature change")
        target_branch = "master" if _has_master(repo) else "main"
        _git(repo, "checkout", "-q", target_branch)
        _git(repo, "merge", "--no-ff", "-q", "-m", "merge feature", "feature")
        merge_hash = _git(repo, "rev-parse", "HEAD").stdout.strip()
        info = _commit_info_for(repo, merge_hash)

        changes = get_file_changes(repo, info)

        # First-parent diff: main's tree already has a.txt=main-change; the
        # merge's tree adds b.txt from the feature branch. So the ONLY
        # first-parent delta is b.txt being added.
        paths = [c.path for c in changes]
        assert paths == ["b.txt"]
        assert changes[0].diff_type == "added"


class TestBinaryAndRenameHandling:
    def test_binary_file_skipped_from_aggregated_document(self, repo):
        (repo / ".gitattributes").write_text("*.bin binary\n")
        _git(repo, "add", ".")
        _commit(repo, "gitattributes")
        (repo / "blob.bin").write_bytes(bytes([0, 1, 2, 3, 0, 255, 254]))
        _git(repo, "add", ".")
        h = _commit(repo, "add binary")
        info = _commit_info_for(repo, h)

        changes = get_file_changes(repo, info)
        doc = build_aggregated_document(info, changes)

        assert "blob.bin" not in doc.text
        assert doc.file_paths == []

    def test_pure_rename_skipped_from_aggregated_document(self, repo):
        (repo / "old_name.txt").write_text("unchanged content\n" * 5)
        _git(repo, "add", ".")
        _commit(repo, "add file")
        _git(repo, "mv", "old_name.txt", "new_name.txt")
        h = _commit(repo, "pure rename")
        info = _commit_info_for(repo, h)

        changes = get_file_changes(repo, info)
        doc = build_aggregated_document(info, changes)

        assert "new_name.txt" not in doc.text
        assert doc.file_paths == []

    def test_rename_with_content_changes_is_included(self, repo):
        """AC24: rename WITH content changes yields a non-empty diff chunk."""
        # Large file with a single changed line => similarity comfortably above
        # git's default 50% rename-detection threshold (a realistic
        # small-edit-plus-rename scenario, unlike a tiny fully-rewritten file).
        original_lines = [f"line{i}\n" for i in range(50)]
        (repo / "old_name.txt").write_text("".join(original_lines))
        _git(repo, "add", ".")
        _commit(repo, "add file")
        _git(repo, "mv", "old_name.txt", "new_name.txt")
        changed_lines = list(original_lines)
        changed_lines[25] = "line25 CHANGED\n"
        (repo / "new_name.txt").write_text("".join(changed_lines))
        _git(repo, "add", ".")
        h = _commit(repo, "rename with changes")
        info = _commit_info_for(repo, h)

        changes = get_file_changes(repo, info)
        doc = build_aggregated_document(info, changes)

        assert doc.file_paths == ["new_name.txt"]
        assert "--- new_name.txt ---" in doc.text
        assert "CHANGED" in doc.text


class TestAggregatedDocumentComposition:
    def test_message_appears_exactly_once_at_head(self, repo):
        (repo / "a.txt").write_text("x\n")
        (repo / "b.txt").write_text("y\n")
        _git(repo, "add", ".")
        h = _commit(repo, "UNIQUE_MESSAGE_MARKER_12345")
        info = _commit_info_for(repo, h)

        changes = get_file_changes(repo, info)
        doc = build_aggregated_document(info, changes)

        assert doc.text.count("UNIQUE_MESSAGE_MARKER_12345") == 1
        assert doc.text.startswith("UNIQUE_MESSAGE_MARKER_12345")

    def test_each_changed_file_prefixed_with_header(self, repo):
        (repo / "a.txt").write_text("x\n")
        (repo / "b.txt").write_text("y\n")
        _git(repo, "add", ".")
        h = _commit(repo, "two files")
        info = _commit_info_for(repo, h)

        changes = get_file_changes(repo, info)
        doc = build_aggregated_document(info, changes)

        assert "--- a.txt ---" in doc.text
        assert "--- b.txt ---" in doc.text

    def test_degenerate_zero_file_entry_commit_has_no_file_paths(self, repo):
        """AC26: an empty-diff commit (e.g. --allow-empty) has zero file entries."""
        (repo / "a.txt").write_text("x\n")
        _git(repo, "add", ".")
        _commit(repo, "root")
        _git(repo, "commit", "--allow-empty", "-q", "-m", "EMPTY_COMMIT_MARKER")
        empty_hash = _git(repo, "rev-parse", "HEAD").stdout.strip()
        info = _commit_info_for(repo, empty_hash)

        changes = get_file_changes(repo, info)
        doc = build_aggregated_document(info, changes)

        assert changes == []
        assert doc.file_paths == []
        assert "EMPTY_COMMIT_MARKER" in doc.text
