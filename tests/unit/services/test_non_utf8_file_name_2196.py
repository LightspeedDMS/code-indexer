from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Iterator, List

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo, unique_python_text
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2196-fake-key"
_BAD_NAME_BYTES = b"bad\xff.py"
_REPORTED_NAME = "bad\\xff.py"
_PLAIN_MARKER = "zebrafinchword_plain_handler"

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def fake_provider() -> Iterator[object]:
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _write_bad_file(directory: Path) -> Path:
    target = os.path.join(os.fsencode(directory), _BAD_NAME_BYTES)
    try:
        with open(target, "wb") as handle:
            handle.write(unique_python_text("bad_name_module", 8).encode())
    except OSError as exc:
        pytest.skip(f"filesystem refuses a non-UTF-8 file name: {exc}")
    return Path(os.fsdecode(target))


def _write_plain_file(repo: FakeProviderRepo) -> None:
    (repo.repo / "src" / "plain.py").write_text(
        f"def {_PLAIN_MARKER}(value):\n    return value * 7\n"
        + unique_python_text("plain_module", 8)
    )


def _repo_with_bad_name(tmp_path: Path, server) -> FakeProviderRepo:
    repo = FakeProviderRepo(tmp_path, server, _SENTINEL_KEY)
    _write_plain_file(repo)
    _write_bad_file(repo.repo / "src")
    return repo


def _assert_clean_run(
    repo: FakeProviderRepo, proc: "subprocess.CompletedProcess[str]"
) -> None:
    output = proc.stdout + proc.stderr
    assert proc.returncode == 0, (proc.returncode, output[-3000:])
    metadata = repo.metadata()
    assert not metadata.get("failed_file_paths"), metadata.get("failed_file_paths")
    stored_paths = set(repo.stored_rows().values())
    assert any(path.endswith("plain.py") for path in stored_paths), stored_paths
    assert not any("bad" in path for path in stored_paths), stored_paths


def _assert_plain_searchable(repo: FakeProviderRepo) -> None:
    fts = repo.query(_PLAIN_MARKER, "--fts")
    assert fts.returncode == 0, fts.stderr[-2000:]
    assert "plain.py" in fts.stdout, fts.stdout[-2000:]
    semantic = repo.query(f"def {_PLAIN_MARKER}(value)", "--limit", "5")
    assert semantic.returncode == 0, semantic.stderr[-2000:]
    assert "plain.py" in semantic.stdout, semantic.stdout[-2000:]


def test_non_git_full_index_skips_the_name_and_indexes_the_rest(
    tmp_path: Path, fake_provider
) -> None:
    repo = _repo_with_bad_name(tmp_path, fake_provider)

    proc = repo.cidx(*repo.index_args(), "--fts")

    _assert_clean_run(repo, proc)
    output = proc.stdout + proc.stderr
    assert _REPORTED_NAME in output, output[-3000:]
    assert "not valid UTF-8" in output, output[-3000:]
    _assert_plain_searchable(repo)


def test_large_non_utf8_named_file_never_aborts_discovery(
    tmp_path: Path, fake_provider
) -> None:
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    _write_plain_file(repo)
    large = os.path.join(os.fsencode(repo.repo / "src"), b"big\xff.py")
    try:
        with open(large, "wb") as handle:
            handle.write(b"# filler line for a large file\n" * 20_000)
    except OSError as exc:
        pytest.skip(f"filesystem refuses a non-UTF-8 file name: {exc}")

    proc = repo.cidx(*repo.index_args(), "--fts")

    _assert_clean_run(repo, proc)
    assert "big\\xff.py" in proc.stdout + proc.stderr, proc.stdout[-3000:]


@pytest.mark.parametrize(
    "route", [["--reconcile"], ["--detect-deletions"], []], ids=str
)
def test_non_git_follow_up_runs_stay_clean(
    tmp_path: Path, fake_provider, route: List[str]
) -> None:
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    _write_plain_file(repo)
    first = repo.cidx(*repo.index_args(), "--fts")
    assert first.returncode == 0, first.stderr[-3000:]
    _write_bad_file(repo.repo / "src")

    proc = repo.cidx(*repo.index_args(), "--fts", *route)

    _assert_clean_run(repo, proc)
    _assert_plain_searchable(repo)


def _git(repo: FakeProviderRepo, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com", *args],
        cwd=repo.repo,
        check=True,
        capture_output=True,
    )


def _git_repo(tmp_path: Path, server) -> FakeProviderRepo:
    repo = FakeProviderRepo(tmp_path, server, _SENTINEL_KEY)
    (repo.repo / ".gitignore").write_text(".code-indexer/\n")
    _git(repo, "init", "-q", "-b", "main")
    return repo


def test_git_full_index_with_committed_bad_name(tmp_path: Path, fake_provider) -> None:
    repo = _git_repo(tmp_path, fake_provider)
    _write_plain_file(repo)
    _write_bad_file(repo.repo / "src")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")

    proc = repo.cidx(*repo.index_args(), "--fts")

    _assert_clean_run(repo, proc)
    assert _REPORTED_NAME in proc.stdout + proc.stderr
    _assert_plain_searchable(repo)


def test_git_incremental_with_newly_committed_bad_name(
    tmp_path: Path, fake_provider
) -> None:
    repo = _git_repo(tmp_path, fake_provider)
    _write_plain_file(repo)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "initial")
    first = repo.cidx(*repo.index_args(), "--fts")
    assert first.returncode == 0, first.stderr[-3000:]

    _write_bad_file(repo.repo / "src")
    (repo.repo / "src" / "second.py").write_text(unique_python_text("second", 6))
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "add files")

    proc = repo.cidx(*repo.index_args(), "--fts")

    _assert_clean_run(repo, proc)
    assert _REPORTED_NAME in proc.stdout + proc.stderr
    assert any(p.endswith("second.py") for p in repo.stored_rows().values())
    _assert_plain_searchable(repo)

    _git(repo, "rm", "-q", "--cached", os.fsdecode(b"src/" + _BAD_NAME_BYTES))
    _git(repo, "commit", "-q", "-m", "drop bad name")
    proc = repo.cidx(*repo.index_args(), "--fts")
    _assert_clean_run(repo, proc)
