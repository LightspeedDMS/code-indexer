"""Bug #1544: shared resolver for where perf-evidence test artifacts land.

A handful of unit tests measure real wall-clock timings and persist the
numbers to a JSON file so the measurement is reviewable. Writing those
numbers straight into a TRACKED file under `reports/perf/` on every test run
dirties `git status` with unrelated, non-deterministic churn on every gate
run -- exactly what Bug #1544 reports.

`perf_artifact_path(filename)` is the single place that decides where such a
file goes:

* Default (no env var set): a gitignored scratch location under
  `.tmp/perf/`, so a plain test run never touches tracked content.
* Opt-in (`CIDX_WRITE_PERF_ARTIFACTS` set to a truthy value): the real,
  tracked `reports/perf/<filename>` path -- for deliberately regenerating
  the committed evidence snapshot, the same way Story #1168's standalone
  throughput benchmark is invoked deliberately rather than as part of the
  suite.

The parent directory is always created, so callers can write immediately.
"""

from __future__ import annotations

import os
from pathlib import Path

PERF_ARTIFACT_ENV_VAR = "CIDX_WRITE_PERF_ARTIFACTS"

_TRUTHY_VALUES = {"1", "true", "yes", "on"}

# A bare filename must not be able to escape the resolved base directory.
_FORBIDDEN_BARE_NAMES = {"", ".", ".."}


def _repo_root() -> Path:
    # tests/utils/perf_artifact_paths.py -> tests/utils -> tests -> repo root
    return Path(__file__).resolve().parents[2]


def _write_opt_in_enabled() -> bool:
    return os.environ.get(PERF_ARTIFACT_ENV_VAR, "").strip().lower() in _TRUTHY_VALUES


def perf_artifact_path(filename: str) -> Path:
    """Resolve the path a perf-evidence test should write ``filename`` to.

    ``filename`` must be a non-empty bare filename (no directory components,
    and not ``"."``/``".."``) -- this resolver only ever picks the PARENT
    directory; callers do not get to escape it. The parent directory is
    created if missing.
    """
    if not isinstance(filename, str) or not filename:
        raise ValueError(
            f"perf_artifact_path expects a non-empty str filename, got {filename!r}"
        )

    safe_name = Path(filename).name
    if safe_name != filename or safe_name in _FORBIDDEN_BARE_NAMES:
        raise ValueError(
            f"perf_artifact_path expects a bare, non-empty filename with no "
            f"directory components, got {filename!r}"
        )

    root = _repo_root()
    if _write_opt_in_enabled():
        base_dir = root / "reports" / "perf"
    else:
        base_dir = root / ".tmp" / "perf"
    base_dir.mkdir(parents=True, exist_ok=True)
    return base_dir / safe_name
