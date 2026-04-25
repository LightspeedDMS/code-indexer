"""
Phase 5 positive-control test: AC2 (dual-provider indexed golden repo) +
AC3 (authenticated CLI workspace) end-to-end validation.

When this test runs, the indexed_golden_repo fixture causes:
  - markupsafe to be registered as a golden repo via POST /api/admin/golden-repos
  - The background indexing job to be polled to completion
  - markupsafe to be activated in fault_workspace
And fault_workspace causes:
  - A git-backed temp workspace to be created
  - cidx init --remote http://... to be run there with admin credentials

The clear_all_faults autouse fixture ensures no faults are installed when this
test runs, so cidx query must return ranked results from the dual-provider
RRF coalesced result set.
"""

from __future__ import annotations

import os
from pathlib import Path

from tests.e2e.helpers import run_cidx


def _build_test_cli_env() -> dict[str, str]:
    """Build a subprocess environment with PYTHONPATH and VOYAGE_API_KEY set.

    Mirrors the logic in conftest._build_cli_env so the CLI subprocess can
    import code_indexer and reach the embedding provider.
    """
    src_dir = str(Path(__file__).parent.parent.parent.parent / "src")
    existing = os.environ.get("PYTHONPATH", "")
    pythonpath = f"{src_dir}:{existing}" if existing else src_dir

    env = dict(os.environ)
    env["PYTHONPATH"] = pythonpath

    voyage_api_key = os.environ.get("E2E_VOYAGE_API_KEY") or os.environ.get(
        "VOYAGE_API_KEY"
    )
    if voyage_api_key:
        env["VOYAGE_API_KEY"] = voyage_api_key

    return env


def test_query_returns_results_when_no_faults_installed(
    indexed_golden_repo: str,
    fault_workspace: Path,
) -> None:
    """AC2 + AC3 positive control: with markupsafe indexed via dual-provider
    and a clean CLI workspace, `cidx query` returns ranked results when no
    faults are installed.

    indexed_golden_repo is the alias string returned by the fixture ("markupsafe").
    fault_workspace is the Path to the temp workspace initialised with cidx init --remote.
    The clear_all_faults autouse fixture (session-scope) guarantees a clean
    fault baseline before this test body executes.
    """
    result = run_cidx(
        "query",
        "escape",
        "--repos",
        indexed_golden_repo,
        "--quiet",
        cwd=str(fault_workspace),
        env=_build_test_cli_env(),
    )

    assert result.returncode == 0, (
        f"cidx query exit {result.returncode}; stderr:\n{result.stderr}"
    )
    assert result.stdout.strip(), (
        f"cidx query returned empty stdout for repo '{indexed_golden_repo}'; "
        f"stderr:\n{result.stderr}"
    )
