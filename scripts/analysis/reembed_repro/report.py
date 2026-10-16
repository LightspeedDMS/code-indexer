"""Text rendering of the per-run table and the verdict."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from invariant import Verdict

Row = Dict[str, Any]


def _dash(value: Any) -> str:
    return "-" if value is None else str(value)


COLUMNS: List[Tuple[str, Callable[[Row], str]]] = [
    ("run", lambda r: r["label"]),
    ("kind", lambda r: r["kind"]),
    ("node", lambda r: _dash(r.get("node"))),
    ("ver", lambda r: _dash(r.get("version"))),
    ("cmd", lambda r: "reconcile" if r["reconcile"] else "incr"),
    ("interrupt", lambda r: _dash(r["interrupt"])),
    ("exit", lambda r: _dash(r["exit_code"])),
    ("secs", lambda r: _dash(r["duration_s"])),
    ("idx_pts", lambda r: f"{r['index_points_before']}>{r['index_points_after']}"),
    ("unindexed", lambda r: _dash(r["unindexed_at_start"])),
    (
        "planned(prog/meta)",
        lambda r: f"{_dash(r['planned_progress'])}/{_dash(r['planned_metadata'])}",
    ),
    ("sent", lambda r: _dash(r["chunks_sent"])),
    ("dup_prior", lambda r: _dash(r["dup_prior_runs"])),
    ("dup_same", lambda r: _dash(r["dup_same_run"])),
    ("re_idx", lambda r: _dash(r.get("reembedded_already_indexed"))),
    ("inflight", lambda r: _dash(r.get("inflight_keys"))),
    ("new", lambda r: _dash(r["new_unique"])),
    ("status_after", lambda r: _dash(r["status_after"])),
    ("missing", lambda r: _dash(r.get("content_missing_after"))),
]


def format_run_table(rows: List[Row]) -> str:
    cells = [[name for name, _ in COLUMNS]] + [
        [fn(row) for _, fn in COLUMNS] for row in rows
    ]
    widths = [max(len(line[i]) for line in cells) for i in range(len(COLUMNS))]
    rendered = [
        "  ".join(cell.ljust(w) for cell, w in zip(line, widths)).rstrip()
        for line in cells
    ]
    rendered.insert(1, "  ".join("-" * w for w in widths))
    return "\n".join(rendered)


def format_verdict(verdict: Verdict) -> str:
    headline = f"VERDICT: {verdict.status}"
    if verdict.skipped:
        headline += f" ({verdict.skipped} checks SKIPPED, not passed)"
    lines = [headline]
    for check in verdict.checks:
        name = f"{check.name} {check.title}" if check.title else check.name
        lines.append(f"  {check.status}  {name}: {check.detail}")
    return "\n".join(lines)


def format_report(
    header_lines: List[str],
    rows: List[Row],
    boundaries: Dict[str, Any],
    verdict: Verdict,
    problems: List[str],
) -> str:
    """The whole report.txt: header, run table, provider boundary, verdict, guards."""
    text = "\n".join(
        [
            *header_lines,
            "",
            format_run_table(rows),
            "",
            "provider boundary at each kill (per key: durable or in flight = not in store/pending"
            " after exit; response delivery per request reported separately):",
            *(f"  {label}: {data['summary']}" for label, data in boundaries.items()),
            "",
            format_verdict(verdict),
        ]
    )
    if problems:
        text += "\nGUARD FAILURE (result invalid):\n  " + "\n  ".join(problems)
    return text


#: A home prefix is redacted only when one of these (or the end) follows it.
_PATH_DELIMITERS = "/\"' \n\t:,)]}"


def redact_home(text: str, home: str) -> str:
    """Replace the home directory prefix with '~' (never a look-alike such
    as '<home>2'), so a report can be shared without the user's home path."""
    if not home:
        raise ValueError("home must not be empty")
    out: List[str] = []
    i = 0
    while True:  # each pass consumes len(home) >= 1 characters: terminates
        j = text.find(home, i)
        if j < 0:
            out.append(text[i:])
            return "".join(out)
        follows = text[j + len(home) : j + len(home) + 1]
        out.append(text[i:j])
        out.append("~" if follows == "" or follows in _PATH_DELIMITERS else home)
        i = j + len(home)


def write_reports(
    work: Path,
    header_lines: List[str],
    rows: List[Row],
    extra: Dict[str, Any],
    boundaries: Dict[str, Any],
    verdict: Verdict,
    problems: List[str],
    home: Optional[str] = None,
) -> str:
    """Write report.txt and report.json into ``work`` with the home prefix
    redacted; return the (redacted) text."""
    home = home or str(Path.home())
    text = redact_home(
        format_report(header_lines, rows, boundaries, verdict, problems), home
    )
    (work / "report.txt").write_text(text + "\n")
    json_text = json.dumps(  # the serialised report.json string
        {
            **extra,
            "rows": rows,
            "boundaries": boundaries,
            "guard_problems": problems,
            "verdict": {
                "status": verdict.status,
                "exit_code": verdict.exit_code,
                "reproduced": verdict.reproduced,
                "skipped": verdict.skipped,
                "checks": [
                    {
                        "name": c.name,
                        "title": c.title,
                        "status": c.status,
                        "detail": c.detail,
                    }
                    for c in verdict.checks
                ],
            },
        },
        indent=1,
        default=str,
    )
    (work / "report.json").write_text(redact_home(json_text, home))
    return text  # report.txt content, already redacted above
