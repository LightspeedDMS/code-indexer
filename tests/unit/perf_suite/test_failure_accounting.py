import json
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

from perf_suite import client, measure, report

URL_OR_IP = re.compile(r"https?://|\b\d{1,3}(\.\d{1,3}){3}\b")


def _refused() -> Optional[str]:
    raise client.TransportError(
        "POST /api/query: ConnectError: [Errno 111] Connection refused",
        kind="ConnectError",
        connection_lost=True,
    )


def test_failed_requests_never_count_as_throughput_or_latency():
    stats = measure.run_closed_loop([_refused], concurrency=2)
    assert stats.requests == 8 and stats.errors == 8
    assert stats.served == 0
    assert stats.rps is None
    assert (stats.p50_ms, stats.p95_ms, stats.p99_ms) == (None, None, None)
    result = measure.run_scenario([_refused], concurrency=1, repetitions=3)
    assert result["median"]["served"] == 0
    assert result["median"]["p50_ms"] is None and result["median"]["rps"] is None
    assert result["median"]["errors"] == 4


def test_latency_is_computed_from_served_responses_only():
    def slow_ok() -> Optional[str]:
        time.sleep(0.05)
        return None

    def wrong() -> Optional[str]:
        time.sleep(0.05)
        return "missed"

    items: List[Callable[[], Optional[str]]] = [slow_ok, _refused, wrong, _refused]
    stats = measure.run_closed_loop(items, concurrency=1)
    assert stats.requests == 4 and stats.errors == 2 and stats.wrong == 1
    assert stats.served == 2
    assert stats.p50_ms is not None and stats.p50_ms >= 50.0
    assert stats.rps is not None and stats.rps < 2 / 0.1 + 1


def test_errors_are_counted_per_class_with_one_sanitized_sample():
    def leaky() -> Optional[str]:
        raise client.RequestFailedError(
            "MCP search_code HTTP 503: upstream http://192.0.2.7:8000/mcp down",
            kind="HTTP 503",
        )

    state = {"calls": 0}

    def one_error() -> Optional[str]:
        state["calls"] += 1
        if state["calls"] == 3:
            return leaky()
        return None

    result = measure.run_scenario([one_error], concurrency=1, repetitions=2)
    assert [r["errors"] for r in result["repetitions"]] == [1, 0]
    classes = result["errors_by_class"]
    assert list(classes) == ["HTTP 503"]
    assert classes["HTTP 503"]["count"] == 1
    sample = classes["HTTP 503"]["sample"]
    assert "upstream" in sample and not URL_OR_IP.search(sample)
    assert not any(
        URL_OR_IP.search(s) for s in result["repetitions"][0]["error_samples"]
    )


def _ok() -> Optional[str]:
    return None


def _report_for(rest: Dict[str, Any], mcp: Dict[str, Any]) -> Dict[str, Any]:
    rep: Dict[str, Any] = report.build_report(
        label="box-a",
        server_version="1.2.3",
        registered_repo_count=5,
        corpus_repos=[],
        health_before="healthy",
        health_after="healthy",
        settings={},
        duration_s=1.0,
        scenarios={"fts": {"rest": {"1": rest}, "mcp": {"1": mcp}}},
        request_tally={"POST /auth/login": 1},
    )
    return rep


def _row(text: str) -> str:
    return next(line for line in text.splitlines() if line.startswith("  c=1"))


def _render(rep: Dict[str, Any], comparison: Any = None) -> str:
    rep["comparison"] = comparison
    rep["verdict"] = report.verdict(rep, comparison)
    summary: str = report.render_summary(rep, Path("a.json"), Path("a.md"), color=False)
    return summary


def test_zero_success_cell_is_na_never_baselined_never_an_improvement():
    dead = measure.run_scenario([_refused], concurrency=1, repetitions=3)
    good = measure.run_scenario([_ok], concurrency=1, repetitions=3)
    rep = _report_for(dead, good)
    rest_cell = _row(_render(rep)).split(" | ")[0]
    assert rest_cell.split()[1:3] == ["n/a", "n/a"]
    assert "fts/rest/c1" not in report.baseline_subset(rep)["scenarios"]
    old = report.baseline_subset(_report_for(good, good))
    comparison = report.compare_to_baseline(rep, old, 10.0)
    assert comparison["improvements"] == []
    assert comparison["no_success"] == ["fts/rest/c1"]
    assert "fts/rest/c1" not in comparison["deltas"]
    text = _render(rep, comparison)
    assert "not compared (no successful response): fts/rest/c1" in text
    assert not rep["verdict"]["passed"]


def test_one_error_in_one_repetition_is_visible_in_table_and_listing():
    state = {"calls": 0}

    def one_error() -> Optional[str]:
        state["calls"] += 1
        if state["calls"] == 3:
            raise client.RequestFailedError(
                "MCP search_code HTTP 503: from http://192.0.2.7/mcp", kind="HTTP 503"
            )
        return None

    flaky = measure.run_scenario([one_error], concurrency=1, repetitions=3)
    assert flaky["median"]["errors"] == 0
    good = measure.run_scenario([_ok], concurrency=1, repetitions=3)
    text = _render(_report_for(good, flaky))
    mcp_cell = _row(text).split(" | ")[1].split()
    assert mcp_cell[4] == "1"
    listing = [line for line in text.splitlines() if "HTTP 503" in line]
    assert len(listing) == 1
    assert all(w in listing[0] for w in ("fts", "mcp", "c=1", "1x"))
    assert "MCP search_code HTTP 503" in listing[0]
    assert not URL_OR_IP.search(listing[0])


def test_sample_count_column_in_table_markdown_and_json(tmp_path: Path):
    good = measure.run_scenario([_ok] * 6, concurrency=1, repetitions=3)
    assert good["median"]["served"] == 6
    rep = _report_for(good, good)
    rep["verdict"] = report.verdict(rep, None)
    json_path, md_path, text = report.write_reports(rep, tmp_path, color=False)
    header = next(line for line in text.splitlines() if "req/s" in line)
    assert header.split(" | ")[0].split()[-1] == "n"
    assert _row(text).split(" | ")[0].split()[-1] == "6"
    assert "n = served responses per repetition" in text
    assert _row(text) in md_path.read_text()
    saved = json.loads(json_path.read_text())
    assert saved["scenarios"]["fts"]["rest"]["1"]["median"]["served"] == 6
