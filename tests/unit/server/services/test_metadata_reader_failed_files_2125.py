"""Bug #2125 (B): the refresh reads what the child `cidx index` recorded as
failed in its provider metadata, even when the child exited 0.

- ``failed_files``: what the run reports (the union of recorded paths, or a
  larger counter -- a counter the child does not always keep current);
- ``retryable_paths``: recorded paths that still exist under the clone --
  ONLY these justify an automatic retry (a stale counter or a vanished
  file would otherwise be retried forever);
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


def test_a_stale_counter_reports_but_is_not_retryable(tmp_path: Path) -> None:
    _write(
        tmp_path,
        "metadata-voyage-ai.json",
        {"status": "completed", "failed_files": 5, "failed_file_paths": []},
    )

    result = read_recorded_failures(tmp_path)

    assert result.failed_files == 5
    assert result.retryable_paths == 0


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
