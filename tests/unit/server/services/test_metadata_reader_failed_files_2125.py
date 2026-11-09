"""Bug #2125 (B): the refresh reads what the child `cidx index` recorded as
failed in its provider metadata, even when the child exited 0.

- ``failed_files``: what the run reports (the union of recorded paths, or a
  larger counter -- a counter the child does not always keep current);
- ``retryable_paths``: recorded paths that still exist under the clone --
  the files a path-based retry can reach (a vanished file would otherwise
  be retried forever);
- ``unattributed`` (issue-64616): per provider, failures counted above its
  recorded paths -- they owe a full reconcile, not a per-file retry;
- ``known``: False when a metadata file is unreadable or malformed (never
  read as "no failure").

Real metadata files on disk, in the shape the indexer writes them
(absolute ``str(path)`` entries)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict

from code_indexer.server.services.metadata_reader import read_recorded_failures


def _write(repo: Path, name: str, data: Dict[str, Any]) -> None:
    code_indexer_dir = repo / ".code-indexer"
    code_indexer_dir.mkdir(parents=True, exist_ok=True)
    (code_indexer_dir / name).write_text(json.dumps(data))


def _files(repo: Path, *names: str) -> list:
    paths = []
    for name in names:
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n")
        paths.append(str(path))
    return paths


def test_recorded_existing_paths_are_retryable(tmp_path: Path) -> None:
    paths = _files(tmp_path, "a.py", "b.py", "pkg/c.py")
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"status": "completed", "failed_files": 3, "failed_file_paths": paths},
    )

    result = read_recorded_failures(tmp_path)

    assert result.known is True
    assert result.failed_files == 3
    assert result.retryable_paths == 3


def test_clean_run_records_no_failure(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"status": "completed", "failed_files": 0, "failed_file_paths": []},
    )

    result = read_recorded_failures(tmp_path)

    assert (result.known, result.failed_files, result.retryable_paths) == (True, 0, 0)


def test_no_metadata_records_no_failure(tmp_path: Path) -> None:
    result = read_recorded_failures(tmp_path)

    assert (result.known, result.failed_files, result.retryable_paths) == (True, 0, 0)


def test_a_count_without_paths_is_unattributed_not_retryable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"status": "completed", "failed_files": 5, "failed_file_paths": []},
    )

    result = read_recorded_failures(tmp_path)

    assert result.failed_files == 5
    assert result.retryable_paths == 0
    assert result.unattributed == 5


def test_vanished_and_outside_paths_are_not_retryable(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    kept = _files(repo, "kept.py")
    outside = _files(tmp_path / "elsewhere", "secret.py")
    _write(
        repo,
        "metadata-voyage-ai.json",
        {
            "failed_files": 3,
            "failed_file_paths": kept + [str(repo / "gone.py")] + outside,
        },
    )

    result = read_recorded_failures(repo)

    assert result.failed_files == 3
    assert result.retryable_paths == 1


def test_a_stale_prefix_path_is_reanchored(tmp_path: Path) -> None:
    repo = tmp_path / "now" / "repo"
    _files(repo, "a.py")
    stale = str(tmp_path / "then" / "repo" / "a.py")  # another mount prefix
    _write(
        repo,
        "metadata-voyage-ai.json",
        {"failed_files": 1, "failed_file_paths": [stale]},
    )

    assert read_recorded_failures(repo).retryable_paths == 1


def test_providers_failing_different_files_are_united(tmp_path: Path) -> None:
    a, b, c = _files(tmp_path, "a.py", "b.py", "c.py")
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"failed_files": 2, "failed_file_paths": [a, b]},
    )
    _write(
        tmp_path,
        "metadata-cohere.json",
        {"failed_files": 2, "failed_file_paths": [b, c]},
    )

    result = read_recorded_failures(tmp_path)

    assert result.failed_files == 3
    assert result.retryable_paths == 3


def test_an_unreadable_metadata_file_is_unknown(tmp_path: Path) -> None:
    _write(tmp_path, "metadata-voyage-ai.json", {"failed_files": 0})
    (tmp_path / ".code-indexer" / "metadata-cohere.json").write_text("{not json")

    assert read_recorded_failures(tmp_path).known is False


def test_malformed_failure_fields_are_unknown(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"failed_files": "many", "failed_file_paths": "a.py"},
    )

    assert read_recorded_failures(tmp_path).known is False


def test_delegated_clone_counts_recorded_paths_without_the_disk(
    tmp_path: Path, monkeypatch
) -> None:
    """A mount-less pod holds only the index state: the recorded files are
    not on its disk. Asking the disk would report none retryable and the
    retry would never be scheduled; under a delegated content backend the
    recorded paths inside the repository count (the retry asks the pass
    manifest), and nothing outside it does."""
    from code_indexer.server.startup import content_backend_wiring

    monkeypatch.setattr(
        content_backend_wiring, "is_content_seam_delegated", lambda: True
    )
    recorded = [
        str(tmp_path / "a.py"),
        str(tmp_path / "pkg" / "c.py"),
        "/elsewhere/x.py",
    ]
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"status": "completed", "failed_files": 3, "failed_file_paths": recorded},
    )
    real_exists = Path.exists
    probed: list = []

    def recording_exists(self: Path, *a: Any, **k: Any) -> bool:
        if str(self).startswith(str(tmp_path)) and ".code-indexer" not in str(self):
            probed.append(self)
        return real_exists(self, *a, **k)

    monkeypatch.setattr(Path, "exists", recording_exists)
    real_is_file = Path.is_file

    def recording_is_file(self: Path) -> bool:
        probed.append(self)
        return real_is_file(self)

    monkeypatch.setattr(Path, "is_file", recording_is_file)

    result = read_recorded_failures(tmp_path)
    monkeypatch.undo()

    assert result.retryable_paths == 2
    assert probed == [], f"the delegated count probed this disk: {probed}"


def test_unattributed_is_counted_per_provider(tmp_path: Path) -> None:
    """A path another provider recorded does not attribute this provider's
    count: each provider's excess is its own (issue-64616)."""
    a, b = _files(tmp_path, "a.py", "b.py")
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"failed_files": 3, "failed_file_paths": [a]},
    )
    _write(
        tmp_path,
        "metadata-cohere.json",
        {"failed_files": 2, "failed_file_paths": [a, b, a]},
    )

    result = read_recorded_failures(tmp_path)

    assert result.unattributed == 2
    assert result.retryable_paths == 2
    assert result.failed_files == 3


def test_paths_covering_the_count_leave_nothing_unattributed(tmp_path: Path) -> None:
    paths = _files(tmp_path, "a.py", "b.py")
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"failed_files": 2, "failed_file_paths": paths},
    )

    assert read_recorded_failures(tmp_path).unattributed == 0
