"""Bug #1832: the four USER-FACING subprocess-diagnostic sites in
ActivatedRepoIndexManager.

Before the fix, `_execute_semantic_indexing`, `_execute_fts_indexing`,
`_execute_temporal_indexing`, and `_execute_scip_indexing` each built their
returned "error" field from `result.stderr` alone:
`f"<Operation> indexing failed: {result.stderr}"`. This value becomes the
operator-visible `error` field on a failed indexing job (the worst sites in
the whole sweep, per the issue). When the failing `cidx` subprocess writes
its real diagnostic to stdout instead of stderr, the message degrades to
`"<Operation> indexing failed: "` with an empty tail.

Discriminating case (AC5): stderr EMPTY, stdout NON-EMPTY. A test using a
non-empty stderr would pass before the fix and prove nothing.

All four sites share ONE parametrized runner (`_run_indexing_method`) that
patches the right subprocess seam and, for the temporal site only, the
config-service dependency -- avoiding four near-duplicate test bodies.
"""

from __future__ import annotations

import tempfile
import uuid
from contextlib import ExitStack
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.repositories.background_jobs import BackgroundJobManager
from code_indexer.server.services.activated_repo_index_manager import (
    ActivatedRepoIndexManager,
)
from code_indexer.server.utils.config_manager import ServerConfig

_DISCRIMINATING_STDOUT = (
    "ERROR: embedding provider rejected request: quota exceeded for voyage-code-3"
)

_MODULE = "code_indexer.server.services.activated_repo_index_manager"
_RUN_CANCELLABLE_PATCH = f"{_MODULE}.run_cancellable_subprocess"
_SUBPROCESS_RUN_PATCH = f"{_MODULE}.subprocess.run"


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def mock_background_job_manager():
    manager = Mock(spec=BackgroundJobManager)
    manager.submit_job = Mock(return_value=str(uuid.uuid4()))
    manager.list_jobs = Mock(return_value={"jobs": [], "total": 0})
    return manager


@pytest.fixture
def mock_activated_repo_manager(temp_data_dir):
    manager = Mock()
    repo_path = str(Path(temp_data_dir) / "activated-repos" / "testuser" / "test-repo")
    manager.get_activated_repo_path = Mock(return_value=repo_path)
    return manager


@pytest.fixture
def index_manager(
    temp_data_dir, mock_background_job_manager, mock_activated_repo_manager
):
    return ActivatedRepoIndexManager(
        data_dir=temp_data_dir,
        background_job_manager=mock_background_job_manager,
        activated_repo_manager=mock_activated_repo_manager,
    )


def _failing_result() -> Mock:
    """A subprocess failure whose real diagnostic landed on stdout, with
    EMPTY stderr -- the discriminating shape AC5 requires."""
    return Mock(
        args=["cidx", "index"],
        returncode=1,
        stderr="",
        stdout=_DISCRIMINATING_STDOUT,
    )


def _init_config_json(repo_path: Path) -> None:
    (repo_path / ".code-indexer").mkdir(exist_ok=True)
    (repo_path / ".code-indexer" / "config.json").write_text("{}")


def _run_indexing_method(
    index_manager: ActivatedRepoIndexManager,
    repo_path: Path,
    *,
    method_name: str,
    subprocess_patch_target: str,
    needs_config_json: bool,
    needs_config_service: bool,
) -> Dict[str, Any]:
    """Shared driver for all four sites: patches the right subprocess seam
    (and, for temporal only, the config-service dependency it reads for
    the floor date / env), then invokes the named method with a failure
    whose stderr is empty and whose stdout carries the real diagnostic."""
    if needs_config_json:
        _init_config_json(repo_path)

    with ExitStack() as stack:
        stack.enter_context(
            patch(subprocess_patch_target, return_value=_failing_result())
        )
        if needs_config_service:
            mock_get_cfg_svc = Mock()
            mock_get_cfg_svc.return_value.get_config.return_value = ServerConfig(
                # server_dir derived from this test's own tmp_path, not an
                # environment-specific literal.
                server_dir=str(repo_path / "server")
            )
            stack.enter_context(
                patch(f"{_MODULE}.get_config_service", new=mock_get_cfg_svc)
            )
            stack.enter_context(
                patch(
                    "code_indexer.server.services.config_service.get_config_service",
                    new=mock_get_cfg_svc,
                )
            )
        method = getattr(index_manager, method_name)
        # getattr() returns Any; the four dynamically-selected methods
        # (_execute_semantic_indexing/_execute_fts_indexing/
        # _execute_temporal_indexing/_execute_scip_indexing) are all
        # contractually known to return Dict[str, Any].
        return cast(Dict[str, Any], method(str(repo_path), False))


_SITES = [
    pytest.param(
        "_execute_semantic_indexing",
        _RUN_CANCELLABLE_PATCH,
        True,
        False,
        id="semantic",
    ),
    pytest.param(
        "_execute_fts_indexing", _RUN_CANCELLABLE_PATCH, True, False, id="fts"
    ),
    pytest.param(
        "_execute_temporal_indexing",
        _RUN_CANCELLABLE_PATCH,
        False,
        True,
        id="temporal",
    ),
    pytest.param(
        "_execute_scip_indexing", _SUBPROCESS_RUN_PATCH, False, False, id="scip"
    ),
]


@pytest.mark.parametrize(
    "method_name, subprocess_patch_target, needs_config_json, needs_config_service",
    _SITES,
)
def test_empty_stderr_nonempty_stdout_surfaces_in_error(
    method_name: str,
    subprocess_patch_target: str,
    needs_config_json: bool,
    needs_config_service: bool,
    index_manager: ActivatedRepoIndexManager,
    tmp_path: Path,
) -> None:
    result = _run_indexing_method(
        index_manager,
        tmp_path,
        method_name=method_name,
        subprocess_patch_target=subprocess_patch_target,
        needs_config_json=needs_config_json,
        needs_config_service=needs_config_service,
    )

    assert result["success"] is False
    assert _DISCRIMINATING_STDOUT in result["error"], (
        f"[{method_name}] stdout diagnostic missing from error: {result['error']!r}"
    )
