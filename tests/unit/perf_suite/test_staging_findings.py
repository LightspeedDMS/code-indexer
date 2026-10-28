import inspect
import re
from pathlib import Path
from typing import Any, Dict, List

from perf_suite import cli, report

URL_OR_IP = re.compile(r"https?://|\b\d{1,3}(\.\d{1,3}){3}\b")
USERNAME = "example-user"


def _scenario(
    reps: int, requests: int, wrong: int = 0, warmup_wrong: int = 0
) -> Dict[str, Any]:
    stats = {
        "requests": requests,
        "served": requests,
        "rps": 10.0,
        "p50_ms": 20.0,
        "p95_ms": 30.0,
        "p99_ms": 40.0,
        "errors": 0,
        "wrong": wrong,
    }
    rep_rows: List[Dict[str, Any]] = [
        dict(stats, error_samples=[], wrong_samples=[])
    ] * reps
    return {
        "concurrency": 1,
        "warmup_ms": [25.0],
        "warmup_errors": 0,
        "warmup_wrong": warmup_wrong,
        "wrong_samples": [],
        "repetitions": rep_rows,
        "median": stats,
        "mad": {k: 0.0 for k in stats},
        "aborted": None,
    }


def _report(reps: int = 3, requests: int = 200, **kw: Any) -> Dict[str, Any]:
    rep: Dict[str, Any] = report.build_report(
        label="box-a",
        server_version="1.2.3",
        registered_repo_count=5,
        corpus_repos=[
            {"url": "https://github.com/pallets/click", "alias": "click-global"}
        ],
        health_before="healthy",
        health_after="healthy",
        settings={"repetitions": reps},
        duration_s=1.0,
        scenarios={
            "fts": {
                "rest": {"1": _scenario(reps, requests, **kw)},
                "mcp": {"1": _scenario(reps, requests)},
            }
        },
        request_tally={"POST /auth/login": 1},
    )
    return rep


def test_quick_run_never_writes_a_baseline(tmp_path: Path):
    target = tmp_path / "base.json"
    code, summary = cli.conclude(
        _report(reps=1), None, 10.0, tmp_path, target, False, False
    )
    assert code == 2
    assert not target.exists()
    assert "repetitions" in summary


def test_baseline_records_repetitions():
    base = report.baseline_subset(_report(reps=5))
    assert base["scenarios"]["fts/rest/c1"]["repetitions"] == 5


def test_single_repetition_baseline_reports_no_spread_estimate(tmp_path: Path):
    old = report.baseline_subset(_report(reps=1))
    for entry in old["scenarios"].values():
        del entry["repetitions"]
    rep = _report()
    comparison = report.compare_to_baseline(rep, old, 10.0)
    assert comparison["no_spread"] == ["fts/mcp/c1", "fts/rest/c1"]
    rep["comparison"] = comparison
    rep["verdict"] = report.verdict(rep, comparison)
    text = report.render_summary(
        rep, tmp_path / "a.json", tmp_path / "a.md", color=False
    )
    assert "baseline has no spread estimate" in text


def test_report_carries_no_url_host_or_username(tmp_path: Path):
    assert "target_url" not in inspect.signature(report.build_report).parameters
    rep = _report()
    rep["corpus_repos"] = [{"url": "github.com/pallets/click", "alias": "click-global"}]
    json_path, md_path, _ = report.write_reports(
        dict(rep, verdict=report.verdict(rep, None)), tmp_path, color=False
    )
    for path in (json_path, md_path):
        text = path.read_text()
        assert not URL_OR_IP.search(text), path.name
        assert USERNAME not in text


def _separator_columns(text: str) -> List[int]:
    return [
        line.index(" | ")
        for line in text.splitlines()
        if line.startswith(" ") and " | " in line
    ]


def test_table_separator_aligned_with_and_without_baseline(tmp_path: Path):
    plain = _report()
    plain["verdict"] = report.verdict(plain, None)
    without = report.render_summary(plain, tmp_path / "a", tmp_path / "b", color=False)
    with_base = _report()
    with_base["comparison"] = report.compare_to_baseline(
        with_base, report.baseline_subset(_report()), 10.0
    )
    with_base["verdict"] = report.verdict(with_base, with_base["comparison"])
    delta_text = report.render_summary(
        with_base, tmp_path / "a", tmp_path / "b", color=False
    )
    for text in (without, delta_text):
        columns = _separator_columns(text)
        assert len(set(columns)) == 1, columns
    assert set(_separator_columns(without)) == set(_separator_columns(delta_text))


def test_verdict_says_warmup_outcomes_are_included():
    rep = _report(wrong=1, warmup_wrong=1)
    reasons = report.verdict(rep, None)["reasons"]
    assert any("incl. warmup" in r for r in reasons)


def test_small_sample_percentiles_are_marked(tmp_path: Path):
    rep = _report(requests=10)
    rep["verdict"] = report.verdict(rep, None)
    text = report.render_summary(rep, tmp_path / "a", tmp_path / "b", color=False)
    row = next(line for line in text.splitlines() if line.lstrip().startswith("c=1"))
    assert row.count("*") == 4
    assert "p95/p99 = max" in text
    big = _report(requests=200)
    big["verdict"] = report.verdict(big, None)
    big_text = report.render_summary(big, tmp_path / "a", tmp_path / "b", color=False)
    big_row = next(
        line for line in big_text.splitlines() if line.lstrip().startswith("c=1")
    )
    assert "*" not in big_row
