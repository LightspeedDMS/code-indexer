from __future__ import annotations

import logging
import os
import subprocess
from pathlib import Path

import pytest

from code_indexer.config import Config, OverrideConfig
from code_indexer.global_repos.local_source_manifest import (
    record_source_manifest,
    source_change_reason,
)
from code_indexer.indexing.file_finder import FileFinder
from code_indexer.services.override_filter_service import OverrideFilterService
from code_indexer.utils.never_indexed_paths import escaped_path, is_never_indexed

_BAD = "src/bad\udcff.py"


def _bad_file(directory: Path, name: bytes = b"bad\xff.py") -> None:
    target = os.path.join(os.fsencode(directory), name)
    try:
        with open(target, "wb") as handle:
            handle.write(b"def bad_name_function():\n    return 1\n")
    except OSError as exc:
        pytest.skip(f"filesystem refuses a non-UTF-8 file name: {exc}")


@pytest.mark.parametrize(
    "path, expected",
    [
        (_BAD, True),
        ("dir\udc80/ok.py", True),
        (".snapshot-reader-leases/abc-lease.json", True),
        ("nested/.snapshot-reader-leases/abc.json", True),
        ("src/ok.py", False),
        ("src/café.py", False),
        ("snapshot-reader-leases/notes.md", False),
    ],
)
def test_is_never_indexed(path: str, expected: bool) -> None:
    assert is_never_indexed(path) is expected


def test_escaped_path_is_printable_and_names_the_byte() -> None:
    assert escaped_path(_BAD) == "src/bad\\xff.py"
    assert escaped_path("a\nb\x1b.py") == "a\\nb\\x1b.py"
    assert escaped_path("src/café.py") == "src/café.py"


def test_override_patterns_never_rescue_a_never_indexed_path() -> None:
    service = OverrideFilterService(
        OverrideConfig(
            add_extensions=[".py", ".json"],
            remove_extensions=[],
            add_exclude_dirs=[],
            add_include_dirs=["src", ".snapshot-reader-leases"],
            force_include_patterns=["**"],
            force_exclude_patterns=[],
        )
    )
    assert service.should_include_file(Path(_BAD), True) is False
    lease = Path(".snapshot-reader-leases/abc.json")
    assert service.should_include_file(lease, True) is False
    assert service.should_include_file(Path("src/ok.py"), False) is True


def test_file_finder_reports_each_unstorable_name_once_with_a_capped_count(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    finder = FileFinder(Config(codebase_dir=tmp_path))
    names = [f"src/bad{i}\udcff.py" for i in range(7)]

    with caplog.at_level(logging.WARNING, logger="code_indexer.indexing.file_finder"):
        for name in names + names:
            assert finder.matches_exclude_pattern(name) is True

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 5, messages
    assert "src/bad0\\xff.py" in messages[0] and "(1 such file(s)" in messages[0]
    assert "src/bad4\\xff.py" in messages[4] and "(5 such file(s)" in messages[4]
    assert all("\udcff" not in message for message in messages)


def test_file_finder_walk_ends_with_one_count_of_unnamed_unstorable_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (tmp_path / "src").mkdir()
    for i in range(7):
        _bad_file(tmp_path / "src", f"bad{i}\xff.py".encode("latin-1"))
    finder = FileFinder(Config(codebase_dir=tmp_path))

    with caplog.at_level(logging.WARNING, logger="code_indexer.indexing.file_finder"):
        assert list(finder.find_files()) == []
        first_walk = [r.getMessage() for r in caplog.records]
        caplog.clear()
        assert list(finder.find_files()) == []
        second_walk = [r.getMessage() for r in caplog.records]

    assert len(first_walk) == 6, first_walk
    summary = first_walk[5]
    assert "Skipped 7 file(s) in this run whose names are not valid UTF-8" in summary
    assert second_walk == []


def test_file_finder_summarize_skipped_paths_counts_names_seen_outside_a_walk(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    finder = FileFinder(Config(codebase_dir=tmp_path))
    for i in range(7):
        assert finder.matches_exclude_pattern(f"src/bad{i}\udcff.py") is True

    with caplog.at_level(logging.WARNING, logger="code_indexer.indexing.file_finder"):
        caplog.clear()
        finder.summarize_skipped_paths()
        first = [r.getMessage() for r in caplog.records]
        caplog.clear()
        finder.summarize_skipped_paths()
        second = [r.getMessage() for r in caplog.records]

    assert len(first) == 1, first
    assert "Skipped 7 file(s) in this run whose names are not valid UTF-8" in first[0]
    assert second == []


def test_file_finder_retains_a_bounded_set_of_unstorable_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    finder = FileFinder(Config(codebase_dir=tmp_path))

    with caplog.at_level(logging.WARNING, logger="code_indexer.indexing.file_finder"):
        for i in range(20_000):
            assert finder.matches_exclude_pattern(f"src/b{i}\udcff.py") is True
        assert list(finder.find_files()) == []

    assert finder._unstorable_reporter.tracked_count <= 10_000
    assert "Skipped 10000 or more file(s)" in caplog.records[-1].getMessage()


def test_file_finder_walk_skips_never_indexed_files_even_when_force_included(
    tmp_path: Path,
) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "ok.py").write_text("def ok():\n    return 1\n")
    _bad_file(tmp_path / "src")
    leases = tmp_path / ".snapshot-reader-leases"
    leases.mkdir()
    (leases / "abc-lease.json").write_text('{"snapshot_path": "/x"}')
    config = Config(codebase_dir=tmp_path)
    config.override_config = OverrideConfig(
        add_extensions=[],
        remove_extensions=[],
        add_exclude_dirs=[],
        add_include_dirs=[".snapshot-reader-leases"],
        force_include_patterns=["**/*.json", "**/*.py"],
        force_exclude_patterns=[],
    )

    found = {
        str(path.relative_to(tmp_path)) for path in FileFinder(config).find_files()
    }

    assert found == {"src/ok.py"}, found


def _git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_branch_change_analysis_never_plans_a_lease_file(tmp_path: Path) -> None:
    from code_indexer.services.git_topology_service import GitTopologyService

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    (tmp_path / ".snapshot-reader-leases").mkdir()
    (tmp_path / ".snapshot-reader-leases" / "abc-lease.json").write_text("{}")
    (tmp_path / "new.py").write_text("def new():\n    return 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feature")

    analysis = GitTopologyService(
        tmp_path, Config(codebase_dir=tmp_path)
    ).analyze_branch_change("main", "feature")

    assert analysis.files_to_reindex == ["new.py"], analysis.files_to_reindex
    assert analysis.files_to_update_metadata == ["ok.py"], (
        analysis.files_to_update_metadata
    )


def test_branch_change_analysis_reads_renames_and_spaced_names_exactly(
    tmp_path: Path,
) -> None:
    from code_indexer.services.git_topology_service import GitTopologyService

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    (tmp_path / "old name.py").write_text("def renamed():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    _git(tmp_path, "mv", "old name.py", "new name.py")
    (tmp_path / "with space.py").write_text("def spaced():\n    return 2\n")
    (tmp_path / "tab\there.py").write_text("def tabbed():\n    return 3\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feature")

    analysis = GitTopologyService(
        tmp_path, Config(codebase_dir=tmp_path)
    ).analyze_branch_change("main", "feature")

    assert sorted(analysis.files_to_reindex) == [
        "new name.py",
        "tab\there.py",
        "with space.py",
    ], analysis.files_to_reindex
    assert analysis.files_to_update_metadata == ["ok.py"]


def test_branch_change_analysis_decodes_raw_byte_names_and_skips_them(
    tmp_path: Path,
) -> None:
    from code_indexer.services.git_topology_service import GitTopologyService

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    _bad_file(tmp_path)
    (tmp_path / "new.py").write_text("def new():\n    return 2\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feature")

    analysis = GitTopologyService(
        tmp_path, Config(codebase_dir=tmp_path)
    ).analyze_branch_change("main", "feature")

    assert analysis.files_to_reindex == ["new.py"], analysis.files_to_reindex
    assert analysis.files_to_update_metadata == ["ok.py"], (
        analysis.files_to_update_metadata
    )


def test_branch_change_analysis_reports_skipped_paths_escaped_then_counts(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.services.git_topology_service import GitTopologyService

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    _git(tmp_path, "checkout", "-q", "-b", "feature")
    for i in range(6):
        _bad_file(tmp_path, f"bad{i}\xff.py".encode("latin-1"))
    (tmp_path / ".snapshot-reader-leases").mkdir()
    (tmp_path / ".snapshot-reader-leases" / "abc-lease.json").write_text("{}")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "feature")
    service = GitTopologyService(tmp_path, Config(codebase_dir=tmp_path))

    logger_name = "code_indexer.services.git_topology_service"
    with caplog.at_level(logging.WARNING, logger=logger_name):
        analysis = service.analyze_branch_change("main", "feature")

    assert analysis.files_to_reindex == []
    messages = [r.getMessage() for r in caplog.records if r.name == logger_name]
    assert len(messages) == 6, messages
    assert ".snapshot-reader-leases/abc-lease.json" in messages[0], messages
    assert "bad0\\xff.py" in messages[1], messages
    assert "Skipped 7 never-indexed path(s) in this branch change" in messages[5]
    assert all("\udcff" not in message for message in messages)


def test_temporal_commit_document_skips_never_indexed_paths(tmp_path: Path) -> None:
    from code_indexer.services.temporal.commit_aggregator import (
        build_aggregated_document,
        get_file_changes,
    )
    from code_indexer.services.temporal.models import CommitInfo

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    parent = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    _bad_file(tmp_path)
    (tmp_path / ".snapshot-reader-leases").mkdir()
    (tmp_path / ".snapshot-reader-leases" / "abc-lease.json").write_text("{}\n")
    (tmp_path / "ok.py").write_bytes(b"def ok():\n    return '\xff caf\xc3\xa9'\n")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "second")
    head = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=tmp_path, capture_output=True, text=True
    ).stdout.strip()
    commit = CommitInfo(
        hash=head,
        timestamp=0,
        author_name="Example",
        author_email="dev@example.com",
        message="second",
        parent_hashes=parent,
    )

    doc = build_aggregated_document(commit, get_file_changes(tmp_path, commit))

    assert doc.file_paths == ["ok.py"], doc.file_paths
    assert "bad" not in doc.text and "snapshot-reader-leases" not in doc.text
    assert "café" in doc.text and "�" in doc.text
    doc.text.encode("utf-8")


def _head(repo: Path) -> str:
    return subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=repo, capture_output=True, text=True
    ).stdout.strip()


def _commit_info(repo: Path, parent: str):
    from code_indexer.services.temporal.models import CommitInfo

    return CommitInfo(
        hash=_head(repo),
        timestamp=0,
        author_name="Example",
        author_email="dev@example.com",
        message="change",
        parent_hashes=parent,
    )


_HUNK = "@@ -0,0 +1 @@\n+x = 1\n"


@pytest.mark.parametrize(
    "diff, path, old_path",
    [
        ("diff --git a/x y.py b/x y.py\n" + _HUNK, "x y.py", None),
        ('diff --git "a/q\\"t.py" "b/q\\"t.py"\n' + _HUNK, 'q"t.py', None),
        (
            'diff --git "a/tab\\there.py" "b/tab\\there.py"\n' + _HUNK,
            "tab\there.py",
            None,
        ),
        (
            'diff --git "a/caf\\303\\251.py" "b/caf\\303\\251.py"\n' + _HUNK,
            "café.py",
            None,
        ),
        (
            "diff --git a/old one.py b/new two.py\nsimilarity index 90%\n"
            'rename from old one.py\nrename to "new\\ttwo.py"\n' + _HUNK,
            "new\ttwo.py",
            "old one.py",
        ),
    ],
)
def test_temporal_diff_header_paths_are_decoded_exactly(
    diff: str, path: str, old_path: object
) -> None:
    from code_indexer.services.temporal.commit_aggregator import _parse_diff_output

    (change,) = _parse_diff_output(diff)

    assert (change.path, change.old_path) == (path, old_path)
    assert change.diff_text == _HUNK.strip("\n")


def test_temporal_diff_unterminated_quote_raises() -> None:
    from code_indexer.services.temporal.commit_aggregator import _parse_diff_output

    with pytest.raises(ValueError):
        _parse_diff_output('diff --git "a/open.py b/open.py\n' + _HUNK)


def test_temporal_diff_unparseable_non_rename_header_raises() -> None:
    from code_indexer.services.temporal.commit_aggregator import _parse_diff_output

    with pytest.raises(ValueError):
        _parse_diff_output("diff --git a/one.py b/two.py\n" + _HUNK)


def test_temporal_paths_with_spaces_are_parsed_whole(tmp_path: Path) -> None:
    from code_indexer.services.temporal.commit_aggregator import (
        build_aggregated_document,
        get_file_changes,
    )

    (tmp_path / "old name.py").write_text("def renamed():\n    return 1\n" * 3)
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    parent = _head(tmp_path)
    leases = tmp_path / ".snapshot-reader-leases"
    leases.mkdir()
    (leases / "lease file.json").write_text('{"token": "wombatleasetoken"}\n')
    _bad_file(tmp_path, b"first second \xff.py")
    (tmp_path / "normal.py").write_text("def normal():\n    return 'otterword'\n")
    _git(tmp_path, "mv", "old name.py", "new name.py")
    (tmp_path / "new name.py").write_text(
        "def renamed():\n    return 1\n" * 3 + "def added():\n    return 2\n"
    )
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "change")
    commit = _commit_info(tmp_path, parent)

    doc = build_aggregated_document(commit, get_file_changes(tmp_path, commit))

    assert sorted(doc.file_paths) == ["new name.py", "normal.py"], doc.file_paths
    assert "wombatleasetoken" not in doc.text
    assert "bad_name_function" not in doc.text
    assert "--- normal.py ---\n@@ -0,0 +1,2 @@" in doc.text
    assert "+    return 'otterword'" in doc.text
    assert "+def added():" in doc.text


def test_temporal_skipped_paths_are_reported(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.services.temporal.commit_aggregator import get_file_changes

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    parent = _head(tmp_path)
    (tmp_path / ".snapshot-reader-leases").mkdir()
    (tmp_path / ".snapshot-reader-leases" / "lease file.json").write_text("{}\n")
    _bad_file(tmp_path, b"first second \xff.py")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "change")

    with caplog.at_level(logging.WARNING):
        changes = get_file_changes(tmp_path, _commit_info(tmp_path, parent))

    assert changes == []
    messages = "\n".join(r.getMessage() for r in caplog.records)
    assert "first second \\xff.py" in messages, messages
    assert ".snapshot-reader-leases/lease file.json" in messages, messages
    assert "\udcff" not in messages


def test_temporal_batch_reporter_names_five_then_counts_the_rest(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.services.temporal.commit_aggregator import (
        get_file_changes,
        new_temporal_skip_reporter,
    )

    (tmp_path / "ok.py").write_text("def ok():\n    return 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    commits = []
    for i in range(7):
        parent = _head(tmp_path)
        _bad_file(tmp_path, f"bad{i}\xff.py".encode("latin-1"))
        _git(tmp_path, "add", "-A")
        _git(tmp_path, "commit", "-q", "-m", f"bad {i}")
        commits.append(_commit_info(tmp_path, parent))
    reporter = new_temporal_skip_reporter()

    with caplog.at_level(logging.WARNING):
        for commit in commits:
            assert get_file_changes(tmp_path, commit, skipped_paths=reporter) == []
        reporter.summarize()

    messages = [r.getMessage() for r in caplog.records]
    assert len(messages) == 6, messages
    assert "bad4\\xff.py" in messages[4]
    assert "Skipped 7 never-indexed path(s) in this commit batch" in messages[5]


def test_temporal_binary_mode_and_deleted_sections_keep_whole_paths(
    tmp_path: Path,
) -> None:
    from code_indexer.services.temporal.commit_aggregator import get_file_changes

    (tmp_path / "mode me.sh").write_text("echo hi\n")
    (tmp_path / "gone file.py").write_text("x = 1\n")
    _git(tmp_path, "init", "-q", "-b", "main")
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "initial")
    parent = _head(tmp_path)
    (tmp_path / "img one.bin").write_bytes(b"\x00\x01\x02binary\x00")
    os.chmod(tmp_path / "mode me.sh", 0o755)
    (tmp_path / "gone file.py").unlink()
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "change")

    changes = get_file_changes(tmp_path, _commit_info(tmp_path, parent))

    assert sorted((c.path, c.diff_type) for c in changes) == [
        ("gone file.py", "deleted"),
        ("img one.bin", "binary"),
        ("mode me.sh", "modified"),
    ]


def test_local_source_manifest_carries_a_non_utf8_name_without_a_lasting_change(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    (source / ".code-indexer").mkdir(parents=True)
    (source / "ok.py").write_text("def ok():\n    return 1\n")
    _bad_file(source)
    snapshot = tmp_path / "snapshot"
    (snapshot / ".code-indexer").mkdir(parents=True)

    record_source_manifest(str(source), None)
    manifest = source / ".code-indexer" / "refresh_source_manifest.json"
    (snapshot / ".code-indexer" / manifest.name).write_bytes(manifest.read_bytes())
    os.utime(source / "ok.py", ns=(1_000_000_000, 1_000_000_000))
    os.utime(os.path.join(os.fsencode(source), b"bad\xff.py"), ns=(10**9, 10**9))
    record_source_manifest(str(source), None)
    (snapshot / ".code-indexer" / manifest.name).write_bytes(manifest.read_bytes())

    assert source_change_reason(str(snapshot), str(source)) is None
