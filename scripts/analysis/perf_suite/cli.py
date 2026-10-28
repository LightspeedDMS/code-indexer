import argparse
import getpass
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from perf_suite import measure, operations, report
from perf_suite.client import (
    LoginRejectedError,
    PerfClient,
    RequestFailedError,
    ShapeError,
    TargetUnreachableError,
    TransportError,
)
from perf_suite.corpus import (
    DEFAULT_CORPUS_PATH,
    Corpus,
    CorpusError,
    load_corpus,
    resolve_aliases,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_OUTPUT_DIR = PROJECT_ROOT / ".analysis" / "perf"
PASSWORD_ENV = "CIDX_PERF_PASSWORD"
MAX_LABEL_LENGTH = 64
SAFE_LABEL = re.compile(rf"^[A-Za-z0-9._-]{{0,{MAX_LABEL_LENGTH}}}$")
CONCURRENCY_LEVELS = (1, 4, 16)
XRAY_MAX_CONCURRENCY = 2
DEFAULT_REPETITIONS = 5
DEFAULT_TOLERANCE_PCT = 10.0
EXIT_PASS, EXIT_FAIL, EXIT_CONFIG = 0, 1, 2
NOT_RUN_UNREACHABLE = "not run (target unreachable)"
_UNREADABLE = {
    FileNotFoundError: "file not found",
    PermissionError: "permission denied",
    IsADirectoryError: "is a directory",
}

USAGE_EXAMPLE = """\
usage (the normal case):
  python3 scripts/analysis/perf_suite/run.py --url URL --username USER --password PASS

Read-only against the target: it never registers, refreshes or configures anything.
Every corpus repository must already be registered and indexed on the target.
Without --password the password is read from CIDX_PERF_PASSWORD, then prompted for.
Exit codes: 0 pass, 1 regression or wrong results/errors, 2 guard abort or configuration error.
"""


class GuardError(Exception):
    pass


def parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="Pinned-corpus performance regression suite (REST and MCP).",
        epilog=USAGE_EXAMPLE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--url", required=True, help="target server base URL")
    parser.add_argument("--username", required=True, help="user to log in as")
    parser.add_argument(
        "--password", help=f"password (else ${PASSWORD_ENV}, else prompt)"
    )
    parser.add_argument(
        "--label", default="", help="free-text tag recorded in the report"
    )
    parser.add_argument(
        "--quick", action="store_true", help="one repetition at concurrency 1"
    )
    parser.add_argument(
        "--repetitions",
        type=int,
        default=DEFAULT_REPETITIONS,
        help=f"repetitions per scenario (default {DEFAULT_REPETITIONS})",
    )
    parser.add_argument(
        "--include-cold-semantic",
        action="store_true",
        help="also measure cache-cold semantic search (calls the paid embedding provider)",
    )
    parser.add_argument(
        "--baseline", type=Path, help="compare against this baseline JSON"
    )
    parser.add_argument(
        "--write-baseline", type=Path, help="write a sanitized baseline to this file"
    )
    parser.add_argument(
        "--force", action="store_true", help="allow --write-baseline to overwrite"
    )
    parser.add_argument(
        "--tolerance-pct",
        type=float,
        default=DEFAULT_TOLERANCE_PCT,
        help=f"regression tolerance in percent (default {DEFAULT_TOLERANCE_PCT:g})",
    )
    parser.add_argument(
        "--corpus",
        type=Path,
        default=DEFAULT_CORPUS_PATH,
        help="advanced: corpus YAML (default: the bundled corpus)",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=DEFAULT_OUTPUT_DIR,
        help="advanced: report directory (default .analysis/perf)",
    )
    parser.add_argument(
        "--http-timeout",
        type=float,
        default=None,
        help="advanced: per-request HTTP timeout in seconds (default none)",
    )
    parser.add_argument(
        "--xray-poll-deadline",
        type=float,
        default=operations.DEFAULT_XRAY_POLL_DEADLINE_S,
        help=(
            "advanced: give up polling one X-Ray job after this many seconds and count "
            f"it as an error (default {operations.DEFAULT_XRAY_POLL_DEADLINE_S}: the "
            f"server's {operations.SERVER_XRAY_JOB_TIMEOUT_S} s job timeout plus "
            f"{operations.XRAY_POLL_MARGIN_S} s)"
        ),
    )
    args = parser.parse_args(argv)
    if args.repetitions < 1:
        parser.error("--repetitions must be >= 1")
    # ^ why chained comparison, not `<= 0`; it is False for NaN, so NaN and +-inf are rejected too
    if not 0 < args.xray_poll_deadline < float("inf"):
        parser.error("--xray-poll-deadline must be a finite number > 0")
    # ^ hazard NaN tolerance => every regression comparison is False => any slowdown passes
    if not 0 <= args.tolerance_pct < float("inf"):
        parser.error("--tolerance-pct must be a finite number >= 0")
    return args


def resolve_password(
    flag: Optional[str], environ: Mapping[str, str], prompt: Callable[[str], str]
) -> str:
    if flag:
        return flag
    if environ.get(PASSWORD_ENV):
        return environ[PASSWORD_ENV]
    return prompt("Password: ")


def _fail(message: str) -> int:
    print(f"perf suite: {message}", file=sys.stderr)
    return EXIT_CONFIG


def _health(perf: PerfClient, breaker: bool = True) -> Tuple[str, str]:
    payload = perf.rest("GET", "/health", breaker=breaker)
    status, version = payload.get("status"), payload.get("version")
    if not isinstance(status, str) or not isinstance(version, str):
        raise ShapeError("GET /health has no status/version")
    # ^ security server text is sanitized where it is read; it feeds terminal and reports
    return perf.sanitize(status), perf.sanitize(version)


def health_after_status(perf: PerfClient) -> str:
    # ^ why record, not raise, and bypass the breaker; one closing check even after the target vanished
    try:
        return _health(perf, breaker=False)[0]
    except (
        RequestFailedError,
        TransportError,
        ShapeError,
        TargetUnreachableError,
    ) as exc:
        return f"unavailable ({type(exc).__name__}: {perf.sanitize(str(exc))})"


def run_guard(perf: PerfClient, corpus: Corpus) -> Tuple[Dict[str, str], int]:
    listing = perf.mcp("list_global_repos", {})
    repos = listing.get("repos")
    if not isinstance(repos, list):
        raise ShapeError("MCP list_global_repos has no repos list")
    aliases, missing = resolve_aliases([r.url for r in corpus.repos], repos)
    problems = [f"{url} (not registered)" for url in missing]
    for repo in corpus.repos:
        if repo.url not in aliases:
            continue
        body = {
            "query_text": repo.probe,
            "repository_alias": aliases[repo.url],
            "search_mode": "fts",
            "limit": 1,
        }
        try:
            hits = operations.rest_query_paths(
                perf.rest("POST", "/api/query", json=body), "fts"
            )
        except (RequestFailedError, TransportError) as exc:
            problems.append(f"{repo.url} (probe query failed: {exc})")
            continue
        if not hits:
            problems.append(f"{repo.url} (probe query returned no results)")
    if problems:
        raise GuardError(
            "corpus repositories missing or not queryable on the target:\n  - "
            + "\n  - ".join(problems)
            + "\nThe developer must register and index these repositories on the target"
            " (as golden repositories); this suite never registers anything."
        )
    return aliases, len(repos)


def _levels(operation: str, quick: bool) -> List[int]:
    if quick:
        return [1]
    if operation == "xray":
        return [1, XRAY_MAX_CONCURRENCY]
    return list(CONCURRENCY_LEVELS)


def run_scenarios(
    perf: PerfClient,
    corpus: Corpus,
    aliases: Mapping[str, str],
    args: argparse.Namespace,
) -> Dict[str, Any]:
    scenarios: Dict[str, Any] = {}
    repetitions = 1 if args.quick else args.repetitions
    for op in operations.OPERATIONS:
        if op == "semantic_cold" and not args.include_cold_semantic:
            continue
        doors: Dict[str, Any] = {}
        for door in operations.DOORS:
            if not operations.door_available(op, door):
                doors[door] = None
                continue
            items = operations.build_items(
                perf, corpus, aliases, op, door, args.xray_poll_deadline
            )
            if not items:
                continue
            doors[door] = {
                str(conc): _measure_cell(perf, op, door, conc, items, repetitions)
                for conc in _levels(op, args.quick)
            }
        if any(doors.values()):
            scenarios[op] = doors
    return scenarios


def _measure_cell(
    perf: PerfClient,
    op: str,
    door: str,
    conc: int,
    items: List[measure.Item],
    repetitions: int,
) -> Dict[str, Any]:
    result: Dict[str, Any]
    if perf.unreachable is not None:
        result = measure.not_run(conc, NOT_RUN_UNREACHABLE)
        return result
    print(f"  measuring {op:<13} {door:<4} c={conc} ({len(items)} queries)", flush=True)
    result = measure.run_scenario(items, conc, repetitions, perf.sanitize)
    return result


def _load_baseline(path: Optional[Path]) -> Optional[Dict[str, Any]]:
    if path is None:
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            text = handle.read()
    except OSError as exc:
        reason = _UNREADABLE.get(
            type(exc), (exc.strerror or type(exc).__name__).lower()
        )
        raise ValueError(
            f"{path} cannot be read as a perf baseline: {reason}"
        ) from None
    try:
        data = json.loads(text)
    except ValueError:
        raise ValueError(f"{path} is not valid JSON (empty or malformed)") from None
    problems = report.baseline_problems(data)
    if problems:
        raise ValueError(f"{path} is not a valid perf baseline: " + "; ".join(problems))
    return dict(data)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)
    if args.write_baseline is not None and not SAFE_LABEL.match(args.label or ""):
        return _fail(
            "--label must be letters, digits, '.', '-', '_' and at most "
            f"{MAX_LABEL_LENGTH} characters when writing a baseline; a baseline "
            "must not carry host, user or URL text"
        )
    repetitions = 1 if args.quick else args.repetitions
    if (
        args.write_baseline is not None
        and repetitions < report.MIN_BASELINE_REPETITIONS
    ):
        source = " from --quick" if args.quick else ""
        return _fail(
            f"--write-baseline needs at least {report.MIN_BASELINE_REPETITIONS} "
            f"repetitions per scenario (got {repetitions}{source}); write baselines "
            "from a full run, never --quick"
        )
    if (
        args.write_baseline is not None
        and args.write_baseline.exists()
        and not args.force
    ):
        return _fail(f"{args.write_baseline} exists; pass --force to overwrite it")
    try:
        baseline = _load_baseline(args.baseline)
        corpus = load_corpus(args.corpus)
    except (OSError, ValueError, CorpusError) as exc:
        return _fail(str(exc))
    try:
        password = resolve_password(args.password, os.environ, getpass.getpass)
    except EOFError:
        password = ""
    if not password:
        return _fail(
            "a non-empty password is required (--password, "
            f"{PASSWORD_ENV} or the prompt)"
        )
    perf = PerfClient(args.url, args.username, password, http_timeout=args.http_timeout)
    try:
        return _run(perf, corpus, baseline, args)
    except (
        LoginRejectedError,
        GuardError,
        ShapeError,
        RequestFailedError,
        TransportError,
        TargetUnreachableError,
    ) as exc:
        print(f"perf suite: aborted: {perf.redact(str(exc))}", file=sys.stderr)
        print(
            "requests sent: " + json.dumps(perf.tally(), sort_keys=True),
            file=sys.stderr,
        )
        return EXIT_CONFIG
    finally:
        perf.close()


def _run(
    perf: PerfClient,
    corpus: Corpus,
    baseline: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    perf.login()
    health_before, server_version = _health(perf)
    aliases, registered = run_guard(perf, corpus)
    print(
        f"perf suite: target {server_version}, {registered} registered repos, "
        f"{len(aliases)} corpus repos resolved",
        flush=True,
    )
    return measure_and_report(
        perf, corpus, aliases, registered, health_before, server_version, baseline, args
    )


def _shown_path(path: Path) -> str:
    # ^ security reports never carry an absolute local path
    try:
        return str(path.resolve().relative_to(PROJECT_ROOT))
    except ValueError:
        return path.name


def measure_and_report(
    perf: PerfClient,
    corpus: Corpus,
    aliases: Mapping[str, str],
    registered: int,
    health_before: str,
    server_version: str,
    baseline: Optional[Dict[str, Any]],
    args: argparse.Namespace,
) -> int:
    started = time.monotonic()
    scenarios = run_scenarios(perf, corpus, aliases, args)
    health_after = health_after_status(perf)
    rep = report.build_report(
        label=args.label,
        server_version=server_version,
        registered_repo_count=registered,
        corpus_repos=[{"url": url, "alias": alias} for url, alias in aliases.items()],
        health_before=health_before,
        health_after=health_after,
        settings={
            "quick": args.quick,
            "repetitions": 1 if args.quick else args.repetitions,
            "include_cold_semantic": args.include_cold_semantic,
            "tolerance_pct": args.tolerance_pct,
            "corpus": _shown_path(args.corpus),
        },
        duration_s=time.monotonic() - started,
        scenarios=scenarios,
        request_tally=perf.tally(),
    )
    rep["target_unreachable"] = perf.unreachable
    code, summary = conclude(
        rep,
        baseline,
        args.tolerance_pct,
        args.output_dir,
        args.write_baseline,
        args.force,
        sys.stdout.isatty(),
    )
    print(perf.redact(summary))
    return code


def conclude(
    rep: Dict[str, Any],
    baseline: Optional[Dict[str, Any]],
    tolerance_pct: float,
    output_dir: Path,
    write_baseline: Optional[Path],
    force: bool,
    color: bool,
) -> Tuple[int, str]:
    mismatch: Optional[str] = None
    # ^ why no comparison once the target vanished; unmeasured cells would read as a mismatch (exit 2)
    if baseline is not None and not rep.get("target_unreachable"):
        try:
            rep["comparison"] = report.compare_to_baseline(rep, baseline, tolerance_pct)
        except report.BaselineMismatchError as exc:
            mismatch = str(exc)
            rep["baseline_mismatch"] = mismatch
    rep["verdict"] = report.verdict(rep, rep.get("comparison"))
    _, _, summary = report.write_reports(rep, output_dir, color=color)
    if mismatch is not None:
        return EXIT_CONFIG, f"{summary}\nperf suite: {mismatch}"
    if write_baseline is not None and not rep["verdict"]["passed"]:
        reasons = ", ".join(rep["verdict"]["reasons"])
        return _refuse_baseline(summary, f"run verdict FAIL ({reasons})")
    fewest = report.min_repetitions(rep)
    if write_baseline is not None and fewest < report.MIN_BASELINE_REPETITIONS:
        return _refuse_baseline(
            summary,
            f"{fewest} repetitions per scenario; a baseline needs at least "
            f"{report.MIN_BASELINE_REPETITIONS} (write baselines from a full run)",
        )
    if write_baseline is not None:
        report.write_baseline(rep, write_baseline, force=force)
        summary += f"\nBaseline written: {write_baseline}"
    return (EXIT_PASS if rep["verdict"]["passed"] else EXIT_FAIL), summary


def _refuse_baseline(summary: str, reason: str) -> Tuple[int, str]:
    lines = summary.splitlines()
    verdict_at = next(i for i, line in enumerate(lines) if "VERDICT:" in line)
    lines.insert(
        verdict_at,
        f"baseline not written: {reason}; exit code 2 is this baseline refusal, "
        "the verdict below reports the measurements",
    )
    return EXIT_CONFIG, "\n".join(lines)
