import json
from datetime import datetime, timezone
from pathlib import Path, PurePath
from typing import Any, Dict, List, Mapping, Optional, Tuple
from urllib.parse import urlsplit

from perf_suite import SUITE_VERSION, measure

SPREAD_FACTOR = 3.0
MIN_BASELINE_REPETITIONS = 3
# ^ invariant nearest-rank p95 = max below 20 samples, p99 = max below 100 samples
P95_MAX_BELOW = 20
P99_MAX_BELOW = 100
BLOCK_WIDTH = 59
ROW_LABEL_WIDTH = 8
ERROR_SAMPLE_CHARS = 160
FILES_CAVEAT = (
    "Note: files_list and file_read are MCP-only: no REST door serves global "
    "(-global) repositories."
)
SAMPLE_NOTE = (
    "n = served responses per repetition (median); req/s and latency use served "
    "responses only. * p95/p99 = max sample (p95 < 20, p99 < 100 samples)"
)
GREEN = "\x1b[32m"
RED = "\x1b[31m"
RESET = "\x1b[0m"


def safe_repo_label(url: str) -> str:
    # ^ security a local-path corpus entry is shown by basename, never as an absolute local path
    parts = urlsplit(url)
    if url.startswith("git@") or parts.scheme not in ("", "file"):
        return url
    return PurePath(parts.path or url).name or url


def build_report(
    *,
    label: str,
    server_version: str,
    registered_repo_count: int,
    corpus_repos: List[Dict[str, str]],
    health_before: str,
    health_after: str,
    settings: Mapping[str, Any],
    duration_s: float,
    scenarios: Mapping[str, Any],
    request_tally: Mapping[str, int],
) -> Dict[str, Any]:
    # ^ security no target URL, host or username in a report; label identifies the hardware class
    return {
        "suite_version": SUITE_VERSION,
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "label": label,
        "server_version": server_version,
        "registered_repo_count": registered_repo_count,
        "corpus_repos": [
            {"url": safe_repo_label(r["url"]), "alias": r["alias"]}
            for r in corpus_repos
        ],
        "health_before": health_before,
        "health_after": health_after,
        "settings": dict(settings),
        "duration_s": duration_s,
        "scenarios": scenarios,
        "request_tally": dict(sorted(request_tally.items())),
        "comparison": None,
        "verdict": None,
    }


def _measured(rep: Mapping[str, Any]) -> List[Tuple[str, Dict[str, Any]]]:
    rows = []
    for op, doors in rep["scenarios"].items():
        for door, by_conc in doors.items():
            for conc, result in (by_conc or {}).items():
                if result is not None:
                    rows.append((f"{op}/{door}/c{conc}", result))
    return rows


def _served(result: Mapping[str, Any]) -> bool:
    return result["median"] is not None and result["median"]["p50_ms"] is not None


def _total(result: Mapping[str, Any], name: str) -> int:
    if result["repetitions"]:
        return int(sum(r[name] for r in result["repetitions"]))
    if result["median"] is None:
        return 0
    return int(result["median"][name])


def min_repetitions(rep: Mapping[str, Any]) -> int:
    counts = [
        len(r["repetitions"]) for _, r in _measured(rep) if r["median"] is not None
    ]
    return min(counts) if counts else 0


def baseline_subset(rep: Mapping[str, Any]) -> Dict[str, Any]:
    scenarios = {
        key: {
            "p50_ms": result["median"]["p50_ms"],
            "p50_mad": result["mad"]["p50_ms"],
            "rps": result["median"]["rps"],
            "repetitions": len(result["repetitions"]),
        }
        for key, result in _measured(rep)
        if _served(result)
    }
    return {
        "label": rep["label"],
        "server_version": rep["server_version"],
        "registered_repo_count": rep["registered_repo_count"],
        "suite_version": rep["suite_version"],
        "scenarios": scenarios,
    }


def write_baseline(rep: Mapping[str, Any], path: Path, force: bool) -> None:
    if path.exists() and not force:
        raise FileExistsError(f"{path} exists; pass --force to overwrite")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(baseline_subset(rep), indent=2) + "\n", encoding="utf-8")


class BaselineMismatchError(Exception):
    pass


BASELINE_FIELDS = ("p50_ms", "p50_mad", "rps")


def baseline_problems(data: Any) -> List[str]:
    if not isinstance(data, dict):
        return ["baseline is not a JSON object"]
    scenarios = data.get("scenarios")
    if not isinstance(scenarios, dict):
        return ["baseline has no 'scenarios' object"]
    problems = []
    for key, entry in scenarios.items():
        if not isinstance(entry, dict):
            problems.append(f"{key} (entry is not an object)")
            continue
        for name in BASELINE_FIELDS:
            value = entry.get(name)
            if (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not float("-inf") < value < float("inf")
            ):
                problems.append(f"{key} ({name} missing or not a finite number)")
        # ^ why optional; baselines written before repetitions was recorded stay readable
        repetitions = entry.get("repetitions", 1)
        if (
            isinstance(repetitions, bool)
            or not isinstance(repetitions, int)
            or repetitions < 1
        ):
            problems.append(f"{key} (repetitions not a positive integer)")
    return problems


def _baseline_offenders(
    measured: Mapping[str, Dict[str, Any]], baseline: Mapping[str, Any]
) -> List[str]:
    offenders = []
    for key, base in baseline["scenarios"].items():
        if key not in measured:
            offenders.append(f"{key} (absent from this run)")
        elif measured[key]["median"] is None:
            offenders.append(f"{key} (no median in this run: aborted)")
        elif base["p50_ms"] <= 0:
            offenders.append(f"{key} (non-positive baseline p50)")
    # ^ invariant a run scenario the baseline lacks is a mismatch, never a silent skip
    for key in sorted(measured):
        if key not in baseline["scenarios"]:
            offenders.append(f"{key} (missing from the baseline)")
    return offenders


def _has_no_spread(base: Mapping[str, Any]) -> bool:
    repetitions = base.get("repetitions")
    if repetitions is None:
        return bool(base["p50_mad"] == 0)
    return bool(repetitions < 2)


def _judge(
    key: str, current: float, base: Mapping[str, Any], tolerance_pct: float
) -> Dict[str, Any]:
    delta_pct = (current - base["p50_ms"]) / base["p50_ms"] * 100.0
    spread_pct = base["p50_mad"] / base["p50_ms"] * 100.0
    return {
        "scenario": key,
        "baseline_p50_ms": base["p50_ms"],
        "p50_ms": current,
        "delta_pct": delta_pct,
        "threshold_pct": max(tolerance_pct, SPREAD_FACTOR * spread_pct),
    }


def compare_to_baseline(
    rep: Mapping[str, Any], baseline: Mapping[str, Any], tolerance_pct: float
) -> Dict[str, Any]:
    problems = baseline_problems(baseline)
    if problems:
        raise BaselineMismatchError("malformed baseline: " + "; ".join(problems))
    measured = dict(_measured(rep))
    offenders = _baseline_offenders(measured, baseline)
    if not any(key in baseline["scenarios"] for key in measured):
        raise BaselineMismatchError(
            "baseline shares no scenario with this run; nothing was compared: "
            + "; ".join(offenders)
        )
    if offenders:
        raise BaselineMismatchError(
            "baseline does not match this run: " + "; ".join(offenders)
        )
    out: Dict[str, Any] = {
        "baseline_label": baseline.get("label"),
        "baseline_server_version": baseline.get("server_version"),
        "baseline_repo_count": baseline.get("registered_repo_count"),
        "current_repo_count": rep["registered_repo_count"],
        "tolerance_pct": tolerance_pct,
        "deltas": {},
        "regressions": [],
        "improvements": [],
        "no_spread": [],
        "no_success": [],
    }
    for key, result in sorted(measured.items()):
        base = baseline["scenarios"][key]
        # ^ invariant a cell with no served response is failed, never a speed-up
        if not _served(result):
            out["no_success"].append(key)
            continue
        if _has_no_spread(base):
            out["no_spread"].append(key)
        entry = _judge(key, result["median"]["p50_ms"], base, tolerance_pct)
        out["deltas"][key] = entry["delta_pct"]
        if entry["delta_pct"] > entry["threshold_pct"]:
            out["regressions"].append(entry)
        elif entry["delta_pct"] < -entry["threshold_pct"]:
            out["improvements"].append(entry)
    return out


def _plural(count: int, noun: str) -> str:
    return f"{count} {noun}" + ("" if count == 1 else "s")


def _counted(count: int, warmup: int, noun: str) -> str:
    text = _plural(count, noun)
    return f"{text} incl. warmup" if warmup else text


def verdict(
    rep: Mapping[str, Any], comparison: Optional[Mapping[str, Any]]
) -> Dict[str, Any]:
    wrong = errors = warmup_wrong = warmup_errors = 0
    aborted: List[str] = []
    for key, result in _measured(rep):
        if result["aborted"] or result["median"] is None:
            aborted.append(f"{key}: {result['aborted'] or 'no measurement'}")
            continue
        wrong += _total(result, "wrong")
        errors += _total(result, "errors")
        warmup_wrong += int(result.get("warmup_wrong", 0))
        warmup_errors += int(result.get("warmup_errors", 0))
    unreachable = rep.get("target_unreachable")
    reasons = [str(unreachable)] if unreachable else []
    if wrong + warmup_wrong:
        reasons.append(_counted(wrong + warmup_wrong, warmup_wrong, "wrong result"))
    if errors + warmup_errors:
        reasons.append(_counted(errors + warmup_errors, warmup_errors, "error"))
    if aborted:
        reasons.append(_plural(len(aborted), "aborted scenario"))
    closing = str(rep["health_after"])
    if closing.startswith("unavailable") or closing == "unhealthy":
        reasons.append(f"closing health {closing}")
    if comparison and comparison["regressions"]:
        reasons.append(_plural(len(comparison["regressions"]), "regression"))
    return {"passed": not reasons, "reasons": reasons, "aborted": aborted}


def _cell(value: Optional[float], samples: int, max_below: int = 0) -> str:
    if value is None:
        return f"{'n/a':>7}  "
    return f"{value:7.1f}" + ("* " if samples < max_below else "  ")


def _block(result: Optional[Mapping[str, Any]], delta: Optional[float]) -> str:
    if result is None:
        return "n/a".ljust(BLOCK_WIDTH)
    m = result["median"]
    if result["aborted"] or m is None:
        return "ABORTED".ljust(BLOCK_WIDTH)
    served = int(m["served"])
    text = (
        f"{_cell(m['rps'], served)}{_cell(m['p50_ms'], served)}"
        f"{_cell(m['p95_ms'], served, P95_MAX_BELOW)}"
        f"{_cell(m['p99_ms'], served, P99_MAX_BELOW)}"
        f"{_total(result, 'errors'):3d} {_total(result, 'wrong'):5d} {served:5d}"
    )
    if delta is not None:
        text += f" {delta:+6.1f}%"
    return text.ljust(BLOCK_WIDTH)


def _columns(with_delta: bool) -> str:
    text = (
        f"{'req/s':>7}  {'p50':>7}  {'p95':>7}  {'p99':>7}  "
        f"{'err':>3} {'wrong':>5} {'n':>5}"
    )
    if with_delta:
        text += f" {'delta':>7}"
    return text.ljust(BLOCK_WIDTH)


def _operation_table(
    op: str, doors: Mapping[str, Any], deltas: Mapping[str, float], with_delta: bool
) -> List[str]:
    columns = _columns(with_delta)
    pad = " " * ROW_LABEL_WIDTH
    lines = [
        op,
        pad + "REST".center(BLOCK_WIDTH) + " | " + "MCP".center(BLOCK_WIDTH),
        pad + columns + " | " + columns,
    ]
    concs = sorted({int(c) for door in doors.values() for c in (door or {})})
    for conc in concs:
        cells = []
        for door in ("rest", "mcp"):
            by_conc = doors.get(door)
            result = None if by_conc is None else by_conc.get(str(conc))
            cells.append(_block(result, deltas.get(f"{op}/{door}/c{conc}")))
        lines.append(f"  c={conc}".ljust(ROW_LABEL_WIDTH) + cells[0] + " | " + cells[1])
    return lines


def _error_listing(rep: Mapping[str, Any]) -> List[str]:
    lines = []
    for key, result in _measured(rep):
        op, door, conc = key.split("/")
        for kind, entry in (result.get("errors_by_class") or {}).items():
            sample = entry["sample"][:ERROR_SAMPLE_CHARS]
            lines.append(
                f"  {op} {door} c={conc[1:]}: {kind} {entry['count']}x: {sample}"
            )
    if not lines:
        return []
    return ["Errors (warmup included):"] + lines + [""]


def _comparison_lines(comparison: Mapping[str, Any]) -> List[str]:
    lines = [
        f"Baseline: label {comparison['baseline_label']} | server "
        f"{comparison['baseline_server_version']} | registered repos "
        f"{comparison['baseline_repo_count']} (now {comparison['current_repo_count']})"
    ]
    if comparison["no_spread"]:
        lines.append(
            f"  baseline has no spread estimate for {len(comparison['no_spread'])} "
            "scenarios (single repetition): threshold is --tolerance-pct only"
        )
    for key in comparison["no_success"]:
        lines.append(f"  not compared (no successful response): {key}")
    for title in ("regressions", "improvements"):
        entries = comparison[title]
        lines.append(f"{title.capitalize()}: {len(entries)}")
        for e in entries:
            lines.append(
                f"  {e['scenario']}: p50 {e['baseline_p50_ms']:.1f} -> "
                f"{e['p50_ms']:.1f} ms ({e['delta_pct']:+.1f}%, threshold {e['threshold_pct']:.1f}%)"
            )
    return lines + [""]


def _queries_reused(rep: Mapping[str, Any]) -> bool:
    return any(
        r["median"] is not None
        and bool(r.get("queries"))
        and int(r["median"]["requests"]) > int(r["queries"])
        for _, r in _measured(rep)
    )


def _reuse_note() -> str:
    return (
        "n can exceed the query count: each worker sends at least "
        f"{measure.MIN_REQUESTS_PER_WORKER} requests per repetition; queries are "
        "reused round-robin"
    )


def render_summary(
    rep: Mapping[str, Any], json_path: Path, md_path: Path, color: bool
) -> str:
    comparison = rep.get("comparison")
    deltas = comparison["deltas"] if comparison else {}
    lines = [
        f"CIDX perf suite {rep['suite_version']} | server {rep['server_version']} | "
        f"registered repos {rep['registered_repo_count']} | duration {rep['duration_s']:.0f}s"
        f" | label {rep['label'] or '-'}",
        f"health before: {rep['health_before']}   after: {rep['health_after']}   "
        "(latencies in ms, medians across repetitions; err/wrong summed)",
        "",
    ]
    for op, doors in rep["scenarios"].items():
        lines.extend(_operation_table(op, doors, deltas, comparison is not None))
        lines.append("")
    lines.append(SAMPLE_NOTE)
    if _queries_reused(rep):
        lines.append(_reuse_note())
    lines.append(FILES_CAVEAT)
    lines.append("")
    lines.extend(_error_listing(rep))
    samples = [(k, r.get("wrong_samples") or []) for k, r in _measured(rep)]
    samples = [(k, s) for k, s in samples if s]
    if samples:
        lines.append("Wrong-result samples (warmup included):")
        for key, entries in samples:
            lines.extend(f"  {key}: {entry}" for entry in entries)
        lines.append("")
    if comparison:
        lines.extend(_comparison_lines(comparison))
    lines.append("Requests sent (read-only allow-list):")
    for key, count in rep["request_tally"].items():
        lines.append(f"  {key:<34} {count:>7d}")
    lines.append("")
    result = rep["verdict"]
    for item in result["aborted"]:
        lines.append(f"  aborted {item}")
    text = (
        "VERDICT: PASS"
        if result["passed"]
        else "VERDICT: FAIL (" + ", ".join(result["reasons"]) + ")"
    )
    if color:
        text = (GREEN if result["passed"] else RED) + text + RESET
    lines.append(text)
    lines.append(f"Report: {json_path}")
    lines.append(f"        {md_path}")
    return "\n".join(lines)


def _markdown(rep: Mapping[str, Any], summary: str) -> str:
    lines = [
        f"# CIDX perf run {rep['timestamp']}",
        "",
        f"- Suite version: {rep['suite_version']}",
        f"- Server version: {rep['server_version']}",
        f"- Registered repositories: {rep['registered_repo_count']}",
        f"- Label: {rep['label'] or '-'}",
        f"- Health before / after: {rep['health_before']} / {rep['health_after']}",
        f"- Settings: {json.dumps(rep['settings'], sort_keys=True)}",
        "- Corpus: "
        + ", ".join(f"{r['url']} ({r['alias']})" for r in rep["corpus_repos"]),
        "",
        "## Summary",
        "",
        "```",
        summary,
        "```",
        "",
        "## First-call (warmup) latency and error samples",
        "",
    ]
    for key, result in _measured(rep):
        warm = result.get("warmup_ms") or []
        first = f"{warm[0]:.1f} ms" if warm else "-"
        samples = [s for r in result["repetitions"] for s in r.get("error_samples", [])]
        samples += result.get("warmup_error_samples", [])
        lines.append(
            f"- {key}: first call {first}; errors: {'; '.join(samples[:3]) or 'none'}"
        )
    return "\n".join(lines) + "\n"


def write_reports(
    rep: Dict[str, Any], out_dir: Path, color: bool
) -> Tuple[Path, Path, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = rep["timestamp"].replace(":", "").replace("+0000", "Z")
    json_path = out_dir / f"perf_{stamp}.json"
    md_path = out_dir / f"perf_{stamp}.md"
    # ^ security the markdown names its siblings, never an absolute local path
    plain = render_summary(rep, Path(json_path.name), Path(md_path.name), color=False)
    json_path.write_text(json.dumps(rep, indent=2) + "\n", encoding="utf-8")
    md_path.write_text(_markdown(rep, plain), encoding="utf-8")
    return json_path, md_path, render_summary(rep, json_path, md_path, color=color)
