"""
Story #919 AC5: DependencyMapService.run_graph_repair_dry_run() method.

Verifies that DependencyMapService.run_graph_repair_dry_run() drives the real
executor's _run_phase37(dry_run=True) and returns a dict with the 8 DryRunReport
field names (AC2/AC6 schema contract).

All four constructor collaborators (golden_repos_manager, config_manager,
tracking_backend, analyzer) are stubbed as MagicMocks; no internal service
methods are patched.

Tests (exhaustive list):
  test_service_run_graph_repair_dry_run_returns_required_schema
  test_service_run_graph_repair_dry_run_returns_mode_dry_run
  test_service_run_graph_repair_dry_run_no_output_dir_returns_fallback

Module-level helpers (exhaustive list):
  _REQUIRED_KEYS          -- frozenset of the 8 DryRunReport field names
  _make_context(tmp_path) -- returns (DependencyMapService, dep_map_dir: Path)
                             with empty dep-map dir and all collaborators stubbed
"""

import json
from pathlib import Path
from typing import Tuple
from unittest.mock import MagicMock

from code_indexer.server.services.dependency_map_service import DependencyMapService

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_REQUIRED_KEYS = frozenset(
    {
        "mode",
        "timestamp",
        "total_anomalies",
        "per_type_counts",
        "per_verdict_counts",
        "per_action_counts",
        "would_be_writes",
        "skipped",
        "errors",
    }
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_context(tmp_path: Path) -> Tuple[DependencyMapService, Path]:
    """Build a DependencyMapService with all collaborators stubbed.

    golden_repos_manager.golden_repos_dir → str(tmp_path)
    dep-map output dir → tmp_path/cidx-meta/dependency-map/ (created with empty _domains.json)

    Returns (service, dep_map_dir).
    """
    dep_map_dir = tmp_path / "cidx-meta" / "dependency-map"
    dep_map_dir.mkdir(parents=True)
    (dep_map_dir / "_domains.json").write_text(json.dumps([]), encoding="utf-8")

    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(tmp_path)

    svc = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
    )
    return svc, dep_map_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_service_run_graph_repair_dry_run_returns_required_schema(
    tmp_path: Path,
) -> None:
    """AC5: run_graph_repair_dry_run() returns a dict with all 9 DryRunReport keys."""
    svc, _ = _make_context(tmp_path)
    result = svc.run_graph_repair_dry_run()
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    missing = _REQUIRED_KEYS - set(result.keys())
    assert not missing, f"Missing keys: {missing}"


def test_service_run_graph_repair_dry_run_returns_mode_dry_run(
    tmp_path: Path,
) -> None:
    """AC5: run_graph_repair_dry_run() always returns mode='dry_run'."""
    svc, _ = _make_context(tmp_path)
    result = svc.run_graph_repair_dry_run()
    assert result["mode"] == "dry_run", (
        f"Expected mode='dry_run', got {result['mode']!r}"
    )


def test_service_run_graph_repair_dry_run_no_output_dir_returns_fallback(
    tmp_path: Path,
) -> None:
    """AC5/Blocker4: when dep-map dir does not exist, returns dict with non-empty errors[].

    Regression guard: the fallback must not return silent empty data — errors[] must
    contain an explanatory message about the missing directory (Messi Rule 13).
    """
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(tmp_path / "missing-root")
    svc = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=MagicMock(),
        tracking_backend=MagicMock(),
        analyzer=MagicMock(),
    )
    result = svc.run_graph_repair_dry_run()
    assert isinstance(result, dict), f"Expected dict, got {type(result)}"
    missing = _REQUIRED_KEYS - set(result.keys())
    assert not missing, f"Missing keys in fallback result: {missing}"
    assert result["mode"] == "dry_run"
    assert result["total_anomalies"] == 0
    assert result["would_be_writes"] == []
    assert isinstance(result["errors"], list), (
        f"Expected errors to be a list, got {type(result['errors'])}"
    )
    assert len(result["errors"]) > 0, (
        "errors[] must be non-empty when dep-map output directory does not exist "
        "(Messi Rule 13: anti-silent-failure)"
    )
    assert any(
        "dependency-map" in e or "not exist" in e or "missing" in e.lower()
        for e in result["errors"]
    ), f"errors[] must contain an explanatory message, got: {result['errors']}"
