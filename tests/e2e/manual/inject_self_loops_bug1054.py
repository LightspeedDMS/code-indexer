#!/usr/bin/env python3
"""Inject self-loop edges into dep-map domain .md files to force aggregation.

Bug #1054 E2E rehearsal helper. The hygiene parser aggregates any anomaly type
whose total count exceeds threshold=5 (strictly greater), so to force
SELF_LOOP aggregation we inject >=6 self-loop rows distributed across the
existing domain .md files.

A self-loop is an Outgoing Dependencies row whose "Target Domain" column
equals the source file's domain (the YAML frontmatter `domain:` field).

Usage
-----

    python3 inject_self_loops_bug1054.py --dep-map-dir /path/to/cidx-meta/dependency-map
    python3 inject_self_loops_bug1054.py --dep-map-dir <dir> --count 6 --dry-run

The script idempotently removes any prior injection (markers fenced between
`<!-- BUG1054_SELF_LOOP_INJECTION_START -->` and matching END) before adding
fresh rows, so repeated runs do not stack rows.
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from typing import List, Tuple

START_MARKER = "<!-- BUG1054_SELF_LOOP_INJECTION_START -->"
END_MARKER = "<!-- BUG1054_SELF_LOOP_INJECTION_END -->"

OUTGOING_TABLE_HEADER = "### Outgoing Dependencies"

FRONTMATTER_DOMAIN_RE = re.compile(r"^domain:\s*(\S+)\s*$", re.MULTILINE)


def parse_frontmatter_domain(md_text: str) -> str:
    if not md_text.startswith("---\n"):
        raise ValueError("missing YAML frontmatter")
    end = md_text.index("\n---\n", 4)
    front = md_text[4:end]
    m = FRONTMATTER_DOMAIN_RE.search(front)
    if not m:
        raise ValueError("no `domain:` field in frontmatter")
    return m.group(1).strip("'\"")


def strip_prior_injection(md_text: str) -> str:
    """Remove any prior injection block (idempotency)."""
    pattern = re.compile(
        re.escape(START_MARKER) + r".*?" + re.escape(END_MARKER) + r"\n?",
        re.DOTALL,
    )
    return pattern.sub("", md_text)


def build_injection_block(domain: str, n_rows: int) -> str:
    rows = "\n".join(
        f"| injected-repo-{i} | injected-repo-{i} | {domain} | code-level | "
        f"bug1054 self-loop injection #{i} | bug1054 E2E rehearsal |"
        for i in range(1, n_rows + 1)
    )
    return f"{START_MARKER}\n{rows}\n{END_MARKER}\n"


def inject_into_file(md_path: Path, n_rows: int) -> Tuple[bool, str, int]:
    """Inject n_rows self-loop rows into md_path's outgoing dependencies table.

    Returns (changed, domain, injected_count).
    """
    original = md_path.read_text()
    domain = parse_frontmatter_domain(original)
    text = strip_prior_injection(original)

    # Find the Outgoing Dependencies table header.
    pos = text.find(OUTGOING_TABLE_HEADER)
    if pos == -1:
        return False, domain, 0

    # Find the end of the table block: scan forward until the next section
    # heading (`##` or `###`) OR end of file.
    block_start = pos
    next_section = text.find("\n## ", block_start + 1)
    next_h3 = text.find("\n### ", block_start + 1)
    candidates = [x for x in (next_section, next_h3) if x != -1]
    block_end = min(candidates) if candidates else len(text)

    block = build_injection_block(domain, n_rows)
    insertion_point = block_end
    new_text = (
        text[:insertion_point].rstrip() + "\n\n" + block + "\n" + text[insertion_point:]
    )

    if new_text == original:
        return False, domain, 0

    md_path.write_text(new_text)
    return True, domain, n_rows


def main(argv: List[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dep-map-dir", required=True, type=Path)
    ap.add_argument(
        "--count",
        type=int,
        default=6,
        help="total self-loop rows to inject across files (default 6, > threshold 5)",
    )
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--remove", action="store_true", help="strip injection only")
    args = ap.parse_args(argv)

    dep_map_dir: Path = args.dep_map_dir
    if not dep_map_dir.is_dir():
        print(f"ERROR: not a directory: {dep_map_dir}", file=sys.stderr)
        return 2

    domain_files = sorted(
        f for f in dep_map_dir.glob("*.md") if not f.name.startswith("_")
    )
    if not domain_files:
        print(f"ERROR: no domain .md files in {dep_map_dir}", file=sys.stderr)
        return 2

    if args.remove:
        cleaned = 0
        for f in domain_files:
            t = f.read_text()
            stripped = strip_prior_injection(t)
            if stripped != t:
                if not args.dry_run:
                    f.write_text(stripped)
                cleaned += 1
                print(f"cleaned: {f.name}")
        print(f"\ntotal cleaned: {cleaned}")
        return 0

    per_file: dict = {f: 0 for f in domain_files}
    for i in range(args.count):
        per_file[domain_files[i % len(domain_files)]] += 1

    total_injected = 0
    for f, n in per_file.items():
        if n == 0:
            continue
        if args.dry_run:
            print(f"[dry-run] would inject {n} into {f.name}")
            total_injected += n
            continue
        changed, domain, injected = inject_into_file(f, n)
        if changed:
            print(
                f"injected {injected} self-loop row(s) into {f.name} (domain={domain})"
            )
            total_injected += injected
        else:
            print(f"skipped {f.name} (no Outgoing Dependencies table)")

    print(f"\ntotal injected: {total_injected}")
    if total_injected <= 5:
        print(
            "WARNING: injected <= 5 -> parser will NOT aggregate; "
            "use --count >= 6 to force aggregation",
            file=sys.stderr,
        )
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
