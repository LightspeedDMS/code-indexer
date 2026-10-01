"""
Shared subprocess-failure diagnostic formatting (Bug #1810, Bug #1832).

Across the codebase, subprocess failures were reported using ONLY
`result.stderr` (or `CalledProcessError.stderr`) -- discarding the exit
code and `stdout`. When the failing tool's real diagnostic lands on
stdout instead (common for CLI tools, and the norm for several this
codebase shells out to), the resulting message degrades to a bare prefix
with an EMPTY body, e.g. `"Semantic indexing failed: "`.

Bug #1810 first fixed this for one call site
(`refresh_scheduler._format_called_process_error_diagnostic`). Bug #1832
is the sweep: this module is the ONE shared formatter every call site
across the codebase must reuse (AC3) -- never a per-call-site
reimplementation, never a second competing format.

Two subprocess-failure shapes exist in this codebase:
  - `subprocess.run(..., check=True)` raising `CalledProcessError` ->
    `format_called_process_error_diagnostic`.
  - `subprocess.run(...)` (no `check=True`) or `run_cancellable_subprocess`
    returning a `CompletedProcess`-like result that the caller inspects
    via `result.returncode != 0` -> `format_completed_process_diagnostic`.

Both delegate to the same core, `format_subprocess_failure_diagnostic`,
which names the command, exit code, and BOTH captured streams, each
length-capped (AC4) so an unbounded stdout/stderr blob can never flood a
log line or an API error field.

Placement: `code_indexer.utils` (mirrors the `utils/subprocess_env.py`
precedent from Story #1328) so both `global_repos/` and `server/` code can
import it without a layering violation -- this module has zero
dependencies beyond the stdlib, so it carries none of the CLI/solo-path
vs. server-only import-direction risk called out for Bug #1467/#1468.
"""

from __future__ import annotations

import subprocess
from collections.abc import Sequence as _Sequence
from typing import Optional, Sequence, Union

# Bug #1832: dependency_map_analyzer.py:2813 already established a 1000-char
# cap (`(result.stderr or "")[:1000]`) as the in-tree precedent -- reuse
# that limit rather than inventing a new one.
DEFAULT_DIAGNOSTIC_MAX_CHARS = 1000


def _cap(text: Optional[str], max_chars: int) -> str:
    """Bound a possibly-huge stream to at most max_chars characters.

    max_chars is clamped to >= 0 before slicing: a negative value would
    otherwise silently slice from the end (`text[:-1]`) instead of
    capping, which is not what any caller of this "length-capped" helper
    intends.
    """
    if not text:
        return ""
    return text[: max(0, max_chars)]


def _format_cmd(cmd: Union[str, Sequence[str], None]) -> str:
    """cmd mirrors Popen's args and may be a str, a non-str/bytes
    sequence, or (for a genuinely unknown command) None. Only join real
    sequences -- joining a str's characters would corrupt it into
    space-separated letters."""
    if cmd is None:
        return "<unknown>"
    if isinstance(cmd, _Sequence) and not isinstance(cmd, (str, bytes)):
        return " ".join(str(part) for part in cmd)
    return str(cmd)


def format_subprocess_failure_diagnostic(
    cmd: Union[str, Sequence[str], None],
    returncode: Optional[int],
    stdout: Optional[str],
    stderr: Optional[str],
    *,
    max_chars: int = DEFAULT_DIAGNOSTIC_MAX_CHARS,
) -> str:
    """Build a diagnostic naming the command, exit code, and BOTH captured
    streams (each capped at max_chars) -- never rely on stderr alone,
    which can be empty when the failing command's real diagnostic lands
    on stdout instead (Bug #1832)."""
    return (
        f"command='{_format_cmd(cmd)}' exit_code={returncode} "
        f"stdout={_cap(stdout, max_chars)!r} stderr={_cap(stderr, max_chars)!r}"
    )


def format_called_process_error_diagnostic(
    e: subprocess.CalledProcessError,
    *,
    max_chars: int = DEFAULT_DIAGNOSTIC_MAX_CHARS,
) -> str:
    """Bug #1810 / Bug #1832: diagnostic for a raised CalledProcessError
    (the `subprocess.run(..., check=True)` failure shape)."""
    return f"{type(e).__name__}: " + format_subprocess_failure_diagnostic(
        e.cmd, e.returncode, e.stdout, e.stderr, max_chars=max_chars
    )


def format_completed_process_diagnostic(
    result: "subprocess.CompletedProcess[str]",
    *,
    max_chars: int = DEFAULT_DIAGNOSTIC_MAX_CHARS,
) -> str:
    """Bug #1832: diagnostic for a non-raising CompletedProcess-like result
    inspected via `result.returncode != 0` (the dominant shape across this
    sweep, as opposed to a raised CalledProcessError). `.args` is read
    defensively since not every caller's mock/result guarantees it."""
    return format_subprocess_failure_diagnostic(
        getattr(result, "args", None),
        result.returncode,
        result.stdout,
        result.stderr,
        max_chars=max_chars,
    )
