from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any, List
from unittest.mock import MagicMock

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer

_GIT = ["git", "-c", "user.email=test@example.com", "-c", "user.name=test"]
# ^ external git C-quotes non-ASCII names in text output ("caf\303\251.py") unless -z; core.quotePath default
_UTF8_NAME = "café.py"
_RAW_NAME = os.fsdecode(b"bad\xff.py")


def _git(repo: Path, *args: str) -> str:
    proc = subprocess.run([*_GIT, *args], cwd=repo, check=True, capture_output=True)
    return os.fsdecode(proc.stdout).strip()


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "master")
    for name in ("good.py", _UTF8_NAME, _RAW_NAME):
        (repo / name).write_text(f"def f():\n    return {name!r}\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "base")
    return repo


def _indexer(repo: Path, tmp_path: Path) -> SmartIndexer:
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    return SmartIndexer(
        config=Config(codebase_dir=repo),
        embedding_provider=MagicMock(),
        vector_store_client=store,
        metadata_path=tmp_path / "metadata.json",
    )


def _blob(repo: Path, name: str) -> str:
    return _git(repo, "rev-parse", f"HEAD:{name}")


def test_head_blob_map_keeps_batching_with_a_non_utf8_committed_name(
    tmp_path: Path,
) -> None:
    repo = _repo(tmp_path)

    mapping = _indexer(repo, tmp_path)._get_head_blob_hash_map()

    assert mapping.get("good.py") == _blob(repo, "good.py")
    assert mapping.get(_UTF8_NAME) == _blob(repo, _UTF8_NAME)
    assert _RAW_NAME in mapping


def test_git_delta_reports_deleted_names_unquoted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    for name in ("good.py", _UTF8_NAME, _RAW_NAME):
        (repo / name).unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove")
    head = _git(repo, "rev-parse", "HEAD")

    delta = _indexer(repo, tmp_path)._get_git_deltas_since_commit(base, head)

    assert "good.py" in delta.deleted
    assert _UTF8_NAME in delta.deleted
    assert not [path for path in delta.deleted if path.startswith('"')], delta


def test_modified_files_set_lists_non_ascii_names_unquoted(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    (repo / _UTF8_NAME).write_text("def f():\n    return 'edited'\n")

    modified = _indexer(repo, tmp_path)._get_modified_files_set()

    assert modified == {_UTF8_NAME}, modified


def test_git_delta_passes_revisions_after_end_of_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.services import smart_indexer as module

    repo = _repo(tmp_path)
    base = _git(repo, "rev-parse", "HEAD")
    (repo / "good.py").unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove")
    head = _git(repo, "rev-parse", "HEAD")
    calls: List[List[str]] = []
    real_run = subprocess.run

    def recording_run(cmd: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(list(cmd))
        return real_run(cmd, *args, **kwargs)

    monkeypatch.setattr(module.subprocess, "run", recording_run)

    delta = _indexer(repo, tmp_path)._get_git_deltas_since_commit(base, head)

    diffs = [cmd for cmd in calls if cmd[:2] == ["git", "diff"]]
    assert len(diffs) == 1, calls
    revision = f"{base}..{head}"
    assert diffs[0].index("--end-of-options") < diffs[0].index(revision), diffs
    assert delta.deleted == ["good.py"]


def test_only_a_full_commit_id_is_stored_as_watermark(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    indexer = _indexer(repo, tmp_path)
    head = _git(repo, "rev-parse", "HEAD")
    status = {"git_available": True, "current_branch": "master"}

    indexer._advance_commit_watermark({**status, "current_commit": "HEAD~1"})
    assert indexer.progressive_metadata.get_last_indexed_commit("master") is None

    indexer._advance_commit_watermark({**status, "current_commit": head})
    assert indexer.progressive_metadata.get_last_indexed_commit("master") == head


def test_commit_object_exists_tells_missing_from_present(tmp_path: Path) -> None:
    repo = _repo(tmp_path)
    indexer = _indexer(repo, tmp_path)

    assert indexer._commit_object_exists(_git(repo, "rev-parse", "HEAD")) is True
    assert indexer._commit_object_exists("0123456789abcdef" * 2 + "01234567") is False
    assert indexer._commit_object_exists("unknown") is False


def test_commit_object_check_raises_on_a_git_failure(tmp_path: Path) -> None:
    plain = tmp_path / "not-a-repository"
    plain.mkdir()
    indexer = _indexer(plain, tmp_path)

    with pytest.raises(RuntimeError):
        indexer._commit_object_exists("0123456789abcdef" * 2 + "01234567")


def test_unknown_commit_sentinel_logs_at_debug_and_other_non_commits_warn(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    indexer = _indexer(_repo(tmp_path), tmp_path)
    status = {"git_available": True, "current_branch": "master"}

    with caplog.at_level(logging.DEBUG, logger="code_indexer.services.smart_indexer"):
        indexer._advance_commit_watermark({**status, "current_commit": "unknown"})
    assert [r.levelno for r in caplog.records if "watermark" in r.getMessage()] == [
        logging.DEBUG
    ]
    caplog.clear()

    with caplog.at_level(logging.DEBUG, logger="code_indexer.services.smart_indexer"):
        indexer._advance_commit_watermark({**status, "current_commit": "HEAD~1"})
    assert [r.levelno for r in caplog.records if "watermark" in r.getMessage()] == [
        logging.WARNING
    ]
    assert indexer.progressive_metadata.get_last_indexed_commit("master") is None


def test_git_incremental_summarizes_skipped_never_indexed_names(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging
    import time

    repo = _repo(tmp_path)
    doomed = [repo / os.fsdecode(b"skip%d\xff.py" % i) for i in range(6)]
    for path in doomed:
        path.write_text("x = 1\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "unstorable names")
    base = _git(repo, "rev-parse", "HEAD")
    # ^ why delete them; names only in the git delta reach the reporter through _should_index_file, never the walk
    for path in doomed:
        path.unlink()
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "remove unstorable names")
    head = _git(repo, "rev-parse", "HEAD")
    indexer = _indexer(repo, tmp_path)
    store = indexer.vector_store_client
    store.collection_exists.return_value = False
    store.count_points.return_value = 0
    store.get_pending_self_heal_reprocess_paths.return_value = set()
    meta = indexer.progressive_metadata.metadata
    meta["status"] = "completed"
    # ^ why a future timestamp; the mtime track must select nothing so only the git delta reports names
    meta["last_index_timestamp"] = time.time() + 3600
    meta["branch_commit_watermarks"] = {"master": base}
    status = {"git_available": True, "current_branch": "master", "current_commit": head}

    with caplog.at_level(logging.WARNING):
        indexer._do_incremental_index(
            batch_size=50,
            progress_callback=None,
            git_status=status,
            provider_name="voyage-ai",
            model_name="voyage-code-3",
            safety_buffer_seconds=0,
        )

    messages = [r.getMessage() for r in caplog.records]
    summaries = [m for m in messages if m.startswith("Skipped ")]
    assert len(summaries) == 1, messages
    assert summaries[0].startswith("Skipped 7 "), summaries


@pytest.mark.timeout(300)
def test_reconcile_with_a_committed_non_utf8_name_succeeds(tmp_path: Path) -> None:
    from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
    from tests.utils.fake_voyage_provider import running_fake_voyage_server

    with running_fake_voyage_server("nul-paths-fake-key") as server:
        repo = FakeProviderRepo(tmp_path, server, "nul-paths-fake-key")
        repo.write_files("kept", 3)
        (repo.repo / "src" / _RAW_NAME).write_text("def raw():\n    return 1\n")
        (repo.repo / ".gitignore").write_text(".code-indexer/\n")
        _git(repo.repo, "init", "-q", "-b", "master")
        _git(repo.repo, "add", "-A")
        _git(repo.repo, "commit", "-qm", "base")

        indexed = repo.cidx(*repo.index_args())
        assert indexed.returncode == 0, indexed.stdout[-3000:]
        reconciled = repo.cidx(*repo.index_args(), "--reconcile")

        assert reconciled.returncode == 0, reconciled.stdout[-3000:]
        hits = repo.query("unique marker text", "--limit", "50")
        assert hits.returncode == 0, hits.stderr[-3000:]
        assert "kept_000.py" in hits.stdout, hits.stdout
