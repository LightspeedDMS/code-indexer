import json
from pathlib import Path
from typing import Any, Dict, Optional

import pytest

from perf_suite import SUITE_VERSION, cli, report


def _scenario(
    p50: float,
    mad: float = 1.0,
    wrong: int = 0,
    errors: int = 0,
    aborted: Optional[str] = None,
) -> Dict[str, Any]:
    stats = {
        "requests": 10,
        "served": 10,
        "rps": 20.0,
        "p50_ms": p50,
        "p95_ms": p50 * 2,
        "p99_ms": p50 * 3,
        "errors": errors,
        "wrong": wrong,
    }
    spread = {k: 0.0 for k in stats}
    spread["p50_ms"] = mad
    return {
        "concurrency": 1,
        "warmup_ms": [p50],
        "repetitions": [] if aborted else [dict(stats)],
        "aborted": aborted,
        "median": None if aborted else stats,
        "mad": None if aborted else spread,
        "error_samples": [],
    }


def _report(p50: float = 100.0, **kw: Any) -> Dict[str, Any]:
    scenarios = {
        "fts": {"rest": {"1": _scenario(p50, **kw)}, "mcp": {"1": _scenario(100.0)}},
        "file_read": {"rest": None, "mcp": {"1": _scenario(5.0)}},
    }
    rep: Dict[str, Any] = report.build_report(
        label="example-box",
        server_version="1.2.3",
        registered_repo_count=42,
        corpus_repos=[
            {"url": "https://github.com/pallets/click", "alias": "click-global"}
        ],
        health_before="healthy",
        health_after="degraded",
        settings={"repetitions": 5},
        duration_s=12.5,
        scenarios=scenarios,
        request_tally={"POST /auth/login": 1, "MCP search_code": 30},
    )
    return rep


def test_report_records_run_metadata():
    rep = _report()
    assert rep["suite_version"] == SUITE_VERSION
    assert rep["server_version"] == "1.2.3"
    assert rep["registered_repo_count"] == 42
    assert rep["health_before"] == "healthy"
    assert rep["health_after"] == "degraded"
    assert rep["label"] == "example-box"
    assert rep["corpus_repos"][0]["alias"] == "click-global"
    assert rep["timestamp"]
    assert rep["request_tally"]["MCP search_code"] == 30


def test_compare_flags_regression_beyond_tolerance_and_spread():
    base = report.baseline_subset(_report(p50=100.0))
    slow = report.compare_to_baseline(_report(p50=150.0), base, tolerance_pct=10.0)
    assert [r["scenario"] for r in slow["regressions"]] == ["fts/rest/c1"]
    assert slow["baseline_repo_count"] == 42
    assert slow["current_repo_count"] == 42
    same = report.compare_to_baseline(_report(p50=105.0), base, tolerance_pct=10.0)
    assert same["regressions"] == [] and same["improvements"] == []
    fast = report.compare_to_baseline(_report(p50=50.0), base, tolerance_pct=10.0)
    assert [r["scenario"] for r in fast["improvements"]] == ["fts/rest/c1"]


def test_compare_uses_three_times_baseline_spread_when_wider_than_tolerance():
    base = report.baseline_subset(_report(p50=100.0, mad=20.0))
    result = report.compare_to_baseline(_report(p50=150.0), base, tolerance_pct=10.0)
    assert result["regressions"] == []


def test_write_baseline_is_sanitized_and_refuses_overwrite(tmp_path: Path):
    target = tmp_path / "base.json"
    report.write_baseline(_report(), target, force=False)
    text = target.read_text()
    assert "192.0.2.10" not in text and "target_url" not in text
    data = json.loads(text)
    assert set(data) == {
        "label",
        "server_version",
        "registered_repo_count",
        "suite_version",
        "scenarios",
    }
    with pytest.raises(FileExistsError):
        report.write_baseline(_report(), target, force=False)
    report.write_baseline(_report(), target, force=True)


def test_verdict_fails_on_wrong_errors_aborts_and_regressions():
    assert report.verdict(_report(), None)["passed"] is True
    assert "1 wrong result" in report.verdict(_report(wrong=1), None)["reasons"][0]
    assert report.verdict(_report(errors=2), None)["passed"] is False
    assert report.verdict(_report(aborted="no fts_results"), None)["passed"] is False
    base = report.baseline_subset(_report(p50=100.0))
    comparison = report.compare_to_baseline(
        _report(p50=200.0), base, tolerance_pct=10.0
    )
    assert report.verdict(_report(p50=200.0), comparison)["passed"] is False


def test_summary_shows_doors_side_by_side_verdict_tally_and_path():
    rep = _report()
    rep["verdict"] = report.verdict(rep, None)
    text = report.render_summary(
        rep, Path("/x/perf.json"), Path("/x/perf.md"), color=False
    )
    assert "\x1b[" not in text
    assert "REST" in text and "MCP" in text
    fts_line = next(
        line
        for line in text.splitlines()
        if line.lstrip().startswith("c=1") and "100.0" in line
    )
    assert fts_line.count("100.0") == 2
    file_read = text.split("file_read", 1)[1]
    assert "n/a" in file_read.splitlines()[3]
    assert "VERDICT: PASS" in text
    assert "MCP search_code" in text and "30" in text
    assert "/x/perf.json" in text
    assert "1.2.3" in text and "42" in text and "example-box" in text


def test_summary_with_baseline_shows_delta_and_regressions():
    rep = _report(p50=150.0)
    comparison = report.compare_to_baseline(
        rep, report.baseline_subset(_report()), 10.0
    )
    rep["comparison"] = comparison
    rep["verdict"] = report.verdict(rep, comparison)
    text = report.render_summary(rep, Path("a.json"), Path("a.md"), color=True)
    assert "+50.0%" in text
    assert "Regressions" in text and "fts/rest/c1" in text
    assert "VERDICT: FAIL" in text
    assert "\x1b[" in text


def test_closing_health_unavailable_or_unhealthy_fails_degraded_does_not():
    rep = _report()
    assert report.verdict(rep, None)["passed"] is True
    rep["health_after"] = "unavailable (RequestFailedError: GET /health HTTP 500)"
    failed = report.verdict(rep, None)
    assert failed["passed"] is False
    assert any("closing health" in r for r in failed["reasons"])
    rep["health_after"] = "unhealthy"
    assert report.verdict(rep, None)["passed"] is False


def test_baseline_mismatch_names_every_offending_key():
    base = report.baseline_subset(_report())
    base["scenarios"]["regex/rest/c1"] = {"p50_ms": 5.0, "p50_mad": 0.1, "rps": 1.0}
    base["scenarios"]["file_read/mcp/c1"]["p50_ms"] = 0.0
    current = _report(aborted="no fts_results")
    with pytest.raises(report.BaselineMismatchError) as raised:
        report.compare_to_baseline(current, base, tolerance_pct=10.0)
    message = str(raised.value)
    assert "regex/rest/c1" in message
    assert "fts/rest/c1" in message
    assert "file_read/mcp/c1" in message


def test_verdict_counts_warmup_errors_and_wrong_results():
    rep = _report()
    rep["scenarios"]["fts"]["rest"]["1"]["warmup_wrong"] = 1
    rep["scenarios"]["fts"]["mcp"]["1"]["warmup_errors"] = 2
    result = report.verdict(rep, None)
    assert result["passed"] is False
    assert "1 wrong result" in result["reasons"][0]
    assert "2 errors" in result["reasons"][1]


def test_summary_lists_wrong_samples_and_door_caveats():
    rep = _report(wrong=1)
    rep["scenarios"]["fts"]["rest"]["1"]["wrong_samples"] = [
        "click-global fts 'EchoingStdin': expected src/click/testing.py; top ['x.py']"
    ]
    rep["verdict"] = report.verdict(rep, None)
    text = report.render_summary(rep, Path("a.json"), Path("a.md"), color=False)
    assert "expected src/click/testing.py" in text
    assert "different engines" not in text
    assert "MCP-only" in text


def test_compare_rejects_malformed_baseline_without_crashing():
    no_p50_mad = {"scenarios": {"fts/rest/c1": {"p50_ms": 10.0, "rps": 1.0}}}
    with pytest.raises(report.BaselineMismatchError, match="p50_mad"):
        report.compare_to_baseline(_report(), no_p50_mad, tolerance_pct=10.0)
    with pytest.raises(report.BaselineMismatchError, match="scenarios"):
        report.compare_to_baseline(_report(), {"scenarios": []}, tolerance_pct=10.0)


def test_local_path_corpus_entry_never_reaches_reports(tmp_path: Path):
    rep = _report()
    rep["corpus_repos"] = report.build_report(
        **{k: rep[k] for k in ("label", "server_version", "registered_repo_count")},
        corpus_repos=[
            {"url": str(tmp_path / "local-repo"), "alias": "local-global"},
            {"url": "https://github.com/pallets/click", "alias": "click-global"},
        ],
        health_before="healthy",
        health_after="healthy",
        settings={},
        duration_s=1.0,
        scenarios={},
        request_tally={},
    )["corpus_repos"]
    rep["verdict"] = report.verdict(rep, None)
    json_path, md_path, _ = report.write_reports(rep, tmp_path / "out", color=False)
    written = json_path.read_text() + md_path.read_text()
    local_paths = written.count(str(tmp_path))
    assert local_paths == 0
    assert "local-repo (local-global)" in md_path.read_text()
    assert "https://github.com/pallets/click (click-global)" in md_path.read_text()


def test_zero_overlap_baseline_exits_2_naming_every_key(tmp_path: Path):
    base = report.baseline_subset(_report())
    entry = base["scenarios"]["fts/rest/c1"]
    base["scenarios"] = {"xray/rest/c1": entry, "xray/mcp/c1": dict(entry)}
    code, summary = cli.conclude(_report(), base, 10.0, tmp_path, None, False, False)
    assert code == cli.EXIT_CONFIG
    message = summary.splitlines()[-1]
    assert "shares no scenario" in message
    for key in ("fts/rest/c1", "fts/mcp/c1", "file_read/mcp/c1"):
        assert f"{key} (missing from the baseline)" in message
    for key in ("xray/rest/c1", "xray/mcp/c1"):
        assert f"{key} (absent from this run)" in message


def test_baseline_lacking_a_run_scenario_exits_2_naming_every_key(tmp_path: Path):
    base = report.baseline_subset(_report())
    base["scenarios"]["xray/rest/c1"] = dict(base["scenarios"]["fts/rest/c1"])
    later = _report()
    later["scenarios"]["semantic_cold"] = {
        "rest": {"1": _scenario(400.0)},
        "mcp": {"1": _scenario(420.0)},
    }
    code, summary = cli.conclude(later, base, 10.0, tmp_path, None, False, False)
    assert code == cli.EXIT_CONFIG
    message = summary.splitlines()[-1]
    for key in ("semantic_cold/mcp/c1", "semantic_cold/rest/c1"):
        assert f"{key} (missing from the baseline)" in message
    assert "xray/rest/c1 (absent from this run)" in message
    assert later["baseline_mismatch"] in message
    assert later["comparison"] is None
    assert len(list(tmp_path.glob("perf_*.json"))) == 1
