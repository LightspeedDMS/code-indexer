"""Grep-enforceable single-source-of-truth test (Bug #1084 Phase B, AC #7).

The canonical versioned-snapshot predicate lives in exactly one module
(``snapshot_paths.py``). Bug #1084 Phase A introduced it; Phase B routes every
*secondary* consumer through it. This test is the regression guard for AC #7:

    "the ``.versioned`` substring tests ... are removed from all
     decision/discovery paths; the canonical predicate + discovery API are the
     only authorities (grep-enforceable assertion in tests)."

It scans the specific Phase A + Phase B *decision-path* source files for a
``".versioned" in`` / ``'.versioned' in`` membership-DECISION pattern and
asserts ZERO matches. Path *construction* of the canonical layout (the backends
and the snapshot manager's CoW path-builder) is explicitly allow-listed, as are
the canonical predicate module itself and tests.

Tight enough to fail if anyone reintroduces a substring decision in a consumer;
loose enough not to fight comments/docstrings (the regex matches only the
`<literal> in` operator form, not prose mentioning ``.versioned``).
"""

from __future__ import annotations

import re
from pathlib import Path

# Repo root: tests/unit/server/storage/shared/<this file> -> up 5 = repo root.
_REPO_ROOT = Path(__file__).resolve().parents[5]
_SRC = _REPO_ROOT / "src" / "code_indexer"

# The decision-path files that MUST route through the canonical predicate.
# (Phase B consumers + Phase A cleanup gates, all relative to src/code_indexer.)
_DECISION_PATH_FILES = [
    "server/mcp/handlers/repos.py",
    "server/services/scip_query_service.py",
    "server/services/dependency_map_service.py",
    "server/services/query_path_cache.py",
    "server/mcp/handlers/_legacy.py",
    "global_repos/refresh_scheduler.py",
    "server/repositories/golden_repo_manager.py",
    "global_repos/cleanup_manager.py",
]

# Matches a membership/decision test on the literal ``.versioned`` string:
#   ".versioned" in <something>      or      '.versioned' in <something>
# Does NOT match prose/comments that merely name ``.versioned`` without the
# ``in`` operator, and does NOT match path-construction (``/ ".versioned" /``).
_DECISION_RE = re.compile(r"""["']\.versioned["']\s+in\b""")


def _strip_comment(line: str) -> str:
    """Naively drop a trailing ``# ...`` comment so prose never trips the guard.

    Adequate for this guard: the decision pattern we hunt is executable code
    (``".versioned" in x``), never a string-literal argument, so a simplistic
    "cut at the first ``#`` outside the obvious quoted-literal cases" is fine.
    Full-line comments collapse to empty. We DO keep the leading code, so an
    inline ``code  # comment mentioning ".versioned" in parts`` reduces to the
    code only.
    """
    hash_idx = line.find("#")
    if hash_idx == -1:
        return line
    return line[:hash_idx]


def _offending_lines(path: Path) -> list[tuple[int, str]]:
    """Return [(lineno, text), ...] for code lines containing a .versioned decision.

    Comments (full-line and inline) are stripped first so the guard enforces the
    single-source rule on executable code only and does not fight documentation.
    """
    out: list[tuple[int, str]] = []
    text = path.read_text(encoding="utf-8")
    for i, line in enumerate(text.splitlines(), start=1):
        code = _strip_comment(line)
        if _DECISION_RE.search(code):
            out.append((i, line.strip()))
    return out


def test_no_versioned_substring_decision_in_consumer_paths():
    """No ``".versioned" in`` membership decision survives in any consumer path."""
    violations: dict[str, list[tuple[int, str]]] = {}
    for rel in _DECISION_PATH_FILES:
        path = _SRC / rel
        assert path.exists(), f"decision-path file missing: {path}"
        hits = _offending_lines(path)
        if hits:
            violations[rel] = hits

    assert not violations, (
        "AC #7 violated: a `.versioned` substring DECISION remains in a consumer "
        "path (route it through snapshot_paths.is_versioned_snapshot / the "
        "discovery API instead):\n"
        + "\n".join(
            f"  {rel}:\n" + "\n".join(f"    line {ln}: {txt}" for ln, txt in hits)
            for rel, hits in violations.items()
        )
    )


def test_canonical_module_is_the_authority():
    """The canonical predicate module exists and exposes is_versioned_snapshot."""
    from code_indexer.server.storage.shared.snapshot_paths import (
        is_versioned_snapshot,
    )

    # Sanity: the canonical predicate recognizes the canonical shape and rejects
    # a base clone — proving the single authority is callable from here.
    assert is_versioned_snapshot("/data/golden-repos/.versioned/flask/v_1") is True
    assert is_versioned_snapshot("/data/golden-repos/flask") is False
