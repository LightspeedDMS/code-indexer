"""
Shared helpers for DepMapRepairExecutor fork/join RED-phase tests
(Story #876 Phase B-2 Deliverable 2).

This module exists purely to eliminate duplicated test setup across the
seven fork/join tests in ``test_dep_map_repair_executor_fork_join.py``.

Scope (Operation 1 of the split helper creation):
  - ``_make_healthy_output_dir`` : materialise a minimal healthy dep-map
    output directory under ``tmp_path/depmap``.
  - ``_make_golden_repos_dir``   : materialise an empty ``tmp_path/golden``
    directory used as the executor's ``golden_repos_dir`` kwarg.
  - ``_setup_lifecycle_context`` : build (output_dir, HealthReport) where
    status flips to ``"needs_repair"`` whenever EITHER anomalies or
    lifecycle is non-empty, so ``is_healthy`` is False and the executor
    actually enters the fork/join path.

The remaining helpers (progress capture, executor wiring, progress-event
parser, skip-result assertion) arrive in subsequent operations to respect
the per-operation cap on new method count.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List, Optional, Sequence, Tuple

from code_indexer.server.services.dep_map_health_detector import (
    Anomaly,
    DepMapHealthDetector,
    HealthReport,
)
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import (
    DepMapRepairExecutor,
    RepairResult,
)

# Sibling test module's healthy-directory factory (reused to avoid
# duplicating the ~50-line fixture here).
from tests.unit.server.services.test_dep_map_health_detector import (
    make_healthy_output_dir as _sibling_make_healthy_output_dir,
)


def _make_healthy_output_dir(tmp_path: Path) -> Path:
    """Materialise a minimal healthy dep-map output directory and return it."""
    output_dir = tmp_path / "depmap"
    output_dir.mkdir(parents=True, exist_ok=True)
    _sibling_make_healthy_output_dir(output_dir)
    return output_dir


def _make_golden_repos_dir(tmp_path: Path) -> Path:
    """Materialise an empty golden-repos directory and return it."""
    golden_repos_dir = tmp_path / "golden"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    return golden_repos_dir


def _setup_lifecycle_context(
    tmp_path: Path,
    *,
    lifecycle: Sequence[str],
    anomalies: Optional[Sequence[Anomaly]] = None,
) -> Tuple[Path, HealthReport]:
    """
    Build (output_dir, HealthReport) for fork/join tests.

    ``status`` flips to ``"needs_repair"`` whenever EITHER anomalies or
    lifecycle is non-empty, so ``is_healthy`` is False and the executor
    actually enters the fork/join path instead of short-circuiting on
    ``nothing_to_repair``.
    """
    output_dir = _make_healthy_output_dir(tmp_path)
    anomaly_list = list(anomalies) if anomalies else []
    status = "needs_repair" if (anomaly_list or lifecycle) else "healthy"
    return output_dir, HealthReport(
        status=status,
        anomalies=anomaly_list,
        repairable_count=len(anomaly_list),
        output_dir=output_dir,
        lifecycle=list(lifecycle),
    )


def _capture_progress() -> Tuple[Callable[[int, str], None], List[Tuple[int, str]]]:
    """Return (callback, captured) matching the executor's progress signature."""
    captured: List[Tuple[int, str]] = []

    def callback(progress: int, info: str = "") -> None:
        captured.append((progress, info))

    return callback, captured


def _make_skip_executor(
    *,
    golden_repos_dir: Optional[Path] = None,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> DepMapRepairExecutor:
    """
    Executor wired so the lifecycle branch MUST take the skip path.

    ``lifecycle_invoker=None`` forces branch B to skip regardless of the
    lifecycle list; ``golden_repos_dir`` may optionally be None to exercise
    the other skip trigger.
    """
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        progress_callback=progress_callback,
        lifecycle_invoker=None,
        golden_repos_dir=golden_repos_dir,
    )


def _make_wired_executor(
    golden_repos_dir: Path,
    *,
    progress_callback: Optional[Callable[[int, str], None]] = None,
) -> DepMapRepairExecutor:
    """
    Executor wired so the lifecycle branch should spawn LifecycleBatchRunner.

    ``lifecycle_invoker`` is a benign no-op lambda (real invocation is
    short-circuited by the test's ``patch(_RUNNER_PATCH_TARGET)`` block),
    and ``golden_repos_dir`` is mandatory.
    """
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        progress_callback=progress_callback,
        lifecycle_invoker=lambda *a, **kw: None,
        golden_repos_dir=golden_repos_dir,
    )


# Progress sentinel used by the executor to flag a branch-separated event.
# The numeric ``percent`` slot is -1 (the sentinel) so a scalar dashboard
# cannot accidentally render it as a percentage -- consumers MUST inspect
# ``info`` (the JSON payload) instead.
BRANCH_PROGRESS_SENTINEL: int = -1


# -----------------------------------------------------------------------
# Shared IMMUTABLE test-data constants.
#
# Strings and tuples so mutations in one test cannot leak into another.
# ``Anomaly`` is NOT frozen, so we expose only the type string and each
# test builds a fresh ``Anomaly(type=MISSING_INDEX_ANOMALY_TYPE)`` locally.
# ``_setup_lifecycle_context`` copies ``lifecycle`` and ``anomalies`` into
# private lists internally, so tuple inputs convert safely.
# -----------------------------------------------------------------------

SINGLE_LIFECYCLE_ALIAS: str = "repo-a"
SINGLE_LIFECYCLE: Tuple[str, ...] = (SINGLE_LIFECYCLE_ALIAS,)
EMPTY_LIFECYCLE: Tuple[str, ...] = ()
MULTI_LIFECYCLE: Tuple[str, ...] = ("repo-a", "repo-b")
MISSING_INDEX_ANOMALY_TYPE: str = "missing_index"


def _branch_progress_events(captured: List[Tuple[int, str]]) -> List[dict]:
    """
    Extract branch-separated progress payloads (progress == sentinel).

    Fails LOUDLY (AssertionError) when a sentinel-valued event carries an
    empty ``info``, a non-JSON ``info``, or a non-dict JSON value.  Silent
    discard would mask a contract regression (Messi Rule #13).
    """
    events: List[dict] = []
    for progress, info in captured:
        if progress != BRANCH_PROGRESS_SENTINEL:
            continue
        assert info, (
            "branch-separated progress event carried empty info; "
            "expected a JSON payload with dep_map/lifecycle keys"
        )
        try:
            payload = json.loads(info)
        except json.JSONDecodeError as exc:
            raise AssertionError(
                f"branch-separated progress info is not valid JSON: {info!r} ({exc})"
            ) from exc
        assert isinstance(payload, dict), (
            f"branch-separated progress payload must be a JSON object, "
            f"got {type(payload).__name__}: {payload!r}"
        )
        events.append(payload)
    return events


def _assert_graceful_skip_result(result: RepairResult) -> None:
    """
    Shared assertion: skip-lifecycle-branch tests end with a RepairResult
    whose status is a graceful terminal state with no lifecycle-attributable
    error in ``result.errors``.
    """
    assert isinstance(result, RepairResult), (
        f"expected RepairResult, got {type(result).__name__}"
    )
    assert result.status in {"completed", "partial", "nothing_to_repair"}, (
        f"unexpected RepairResult.status {result.status!r}"
    )
    assert not any("lifecycle" in e.lower() for e in result.errors), (
        f"expected no lifecycle-related errors, got {result.errors!r}"
    )
