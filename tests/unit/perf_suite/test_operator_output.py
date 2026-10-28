import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from perf_suite import cli, measure, report

from .conftest import PROJECT_ROOT

RUN_PY = PROJECT_ROOT / "scripts" / "analysis" / "perf_suite" / "run.py"
UNROUTABLE_URL = "http://192.0.2.1:9"
FOOTNOTE = "n can exceed the query count"


def _ok() -> Optional[str]:
    return None


def _report(items: int, repetitions: int) -> Dict[str, Any]:
    cell = measure.run_scenario([_ok] * items, concurrency=1, repetitions=repetitions)
    rep: Dict[str, Any] = report.build_report(
        label="box-a",
        server_version="1.2.3",
        registered_repo_count=5,
        corpus_repos=[],
        health_before="healthy",
        health_after="healthy",
        settings={},
        duration_s=1.0,
        scenarios={"fanout_fts": {"rest": {"1": cell}, "mcp": {"1": cell}}},
        request_tally={"POST /auth/login": 1},
    )
    return rep


@pytest.mark.parametrize("too_few", [["--quick"], ["--repetitions", "2"]])
def test_baseline_with_too_few_repetitions_is_refused_before_login(
    tmp_path: Path, too_few: List[str]
):
    out_dir = tmp_path / "out"
    done = subprocess.run(
        [sys.executable, str(RUN_PY), "--url", UNROUTABLE_URL, "--username", "u"]
        + ["--password", "example-pass", "--output-dir", str(out_dir)]
        + ["--write-baseline", str(tmp_path / "base.json"), *too_few],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert done.returncode == 2
    lines = done.stderr.strip().splitlines()
    assert len(lines) == 1 and lines[0].startswith("perf suite: ")
    assert "--write-baseline needs at least 3 repetitions" in lines[0]
    assert "requests sent" not in done.stderr and done.stdout == ""
    assert not out_dir.exists() and not (tmp_path / "base.json").exists()


def test_late_baseline_refusal_is_stated_before_the_verdict(tmp_path: Path):
    code, summary = cli.conclude(
        _report(4, 1), None, 10.0, tmp_path, tmp_path / "base.json", False, False
    )
    assert code == cli.EXIT_CONFIG
    lines = summary.splitlines()
    verdict_at = next(i for i, line in enumerate(lines) if "VERDICT:" in line)
    refusal_at = next(
        i for i, line in enumerate(lines) if "baseline not written" in line
    )
    assert refusal_at < verdict_at
    assert "exit code 2" in lines[refusal_at]
    assert lines[verdict_at] == "VERDICT: PASS"


def test_round_robin_footnote_only_when_requests_exceed_queries(tmp_path: Path):
    reused = _report(3, 1)
    reused["verdict"] = report.verdict(reused, None)
    _, md_path, text = report.write_reports(reused, tmp_path / "a", color=False)
    assert FOOTNOTE in text and FOOTNOTE in md_path.read_text()
    assert f"at least {measure.MIN_REQUESTS_PER_WORKER} requests" in text
    exact = _report(4, 1)
    exact["verdict"] = report.verdict(exact, None)
    _, _, plain = report.write_reports(exact, tmp_path / "b", color=False)
    assert FOOTNOTE not in plain
