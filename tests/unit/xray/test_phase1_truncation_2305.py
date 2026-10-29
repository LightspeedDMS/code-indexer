from __future__ import annotations

import time
from pathlib import Path
from typing import Any, Dict

import pytest

_FILE_COUNT = 40
_LINES_PER_FILE = 20
_CAP_SEAM = "code_indexer.xray.phase1_limits.PHASE1_MAX_MATCHES"
_CEILING_SEAM = "code_indexer.global_repos.regex_search._MAX_READ_BYTES"


@pytest.fixture
def engine():
    pytest.importorskip("tree_sitter_languages", reason="xray extras not installed")
    from code_indexer.xray.search_engine import XRaySearchEngine

    return XRaySearchEngine()


# ^ why .txt candidates, not code; no grammar => Phase 2 never spawns xray-cli, flags are set in run() either way
@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    for i in range(_FILE_COUNT):
        body = "".join(
            f"line {j} of file {i} with some padding text\n"
            for j in range(_LINES_PER_FILE)
        )
        (root / f"f{i:03d}.txt").write_text(body)
    return root


def _run(
    engine: Any,
    repo: Path,
    driver: str = ".*",
    target: str = "content",
    timeout: int = 60,
) -> Dict[str, Any]:
    return dict(
        engine.run(
            repo_path=repo,
            driver_regex=driver,
            evaluator_code="unused",
            search_target=target,
            timeout_seconds=timeout,
        )
    )


def _warning_types(result: Dict[str, Any]) -> list:
    return [w.get("type") for w in result.get("warnings") or []]


def test_read_limit_cut_marks_result_partial(engine, repo, monkeypatch):
    monkeypatch.setattr(_CEILING_SEAM, 4096)

    result = _run(engine, repo)

    assert result.get("partial") is True, result
    assert result.get("phase1_truncated") is True, result
    assert result.get("phase1_truncation_reason") == "read_limit", result
    assert result["files_total"] is None, result
    assert 0 < result["phase1_candidate_files"] < _FILE_COUNT, result
    assert "content_search_read_capped" in _warning_types(result), result


def test_match_cap_cut_marks_result_partial(engine, repo, monkeypatch):
    monkeypatch.setattr(_CAP_SEAM, 30)

    result = _run(engine, repo)

    assert result.get("partial") is True, result
    assert result.get("phase1_truncated") is True, result
    assert result.get("phase1_truncation_reason") == "match_cap", result
    assert result["files_total"] is None, result
    assert result["phase1_candidate_files"] == 2, result
    assert "phase1_match_cap_reached" in _warning_types(result), result


def test_matches_exactly_at_the_cap_stay_complete(engine, repo, monkeypatch):
    monkeypatch.setattr(_CAP_SEAM, 2 * _LINES_PER_FILE)

    result = _run(engine, repo, driver=r"of file [01] ")

    assert "partial" not in result, result
    assert "phase1_truncated" not in result, result
    assert result["files_total"] == 2, result


def test_both_cut_signals_report_read_limit_only(engine, repo, monkeypatch):
    monkeypatch.setattr(_CEILING_SEAM, 4096)
    monkeypatch.setattr(_CAP_SEAM, 5)

    result = _run(engine, repo)

    assert result.get("phase1_truncation_reason") == "read_limit", result
    types = _warning_types(result)
    assert "content_search_read_capped" in types, result
    assert "phase1_match_cap_reached" not in types, result


def test_uncut_phase1_is_complete(engine, repo):
    result = _run(engine, repo, driver=r"line 3 of file [0-4] ")

    assert "partial" not in result, result
    assert "phase1_truncated" not in result, result
    assert "phase1_truncation_reason" not in result, result
    assert result["files_total"] == 5, result
    assert result["phase1_candidate_files"] == 5, result


def test_phase1_failure_reports_zero_candidates(engine, repo):
    result = _run(engine, repo, driver="(")

    assert result.get("phase1_failed") is True, result
    assert result["phase1_candidate_files"] == 0, result


def test_phase1_timeout_reports_its_candidates(engine, repo, monkeypatch):
    real_is_file = Path.is_file

    # ^ why slow Path.is_file, not a mock walk; the real walker checks its deadline per rglob entry
    def _slow_is_file(self_path: Path, *args: Any, **kwargs: Any) -> bool:
        time.sleep(0.05)
        return real_is_file(self_path, *args, **kwargs)

    monkeypatch.setattr(Path, "is_file", _slow_is_file)

    result = _run(engine, repo, target="filename", timeout=1)

    assert result.get("timeout") is True, result
    assert result["files_total"] > 0, result
    assert result["phase1_candidate_files"] == result["files_total"], result
