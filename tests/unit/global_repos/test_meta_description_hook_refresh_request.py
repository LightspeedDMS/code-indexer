"""The golden-repo add/remove hooks request their catalog refresh through the
shared writer path: a deferral persisted by the failure backoff is left to
the refresh scheduler (never retried by the debouncer), and a submission
that failed transiently is owed to the debouncer (never dropped).

Real hooks, real files, real description write; only the Claude CLI
boundary and the server config are replaced.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Iterator, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos import meta_description_hook
from code_indexer.global_repos.refresh_failure_recovery import RefreshDeferredError

REPO = "example-repo"


class _Scheduler:
    def __init__(self, failure: BaseException) -> None:
        self.holder: Optional[str] = None
        self.triggers: List[str] = []
        self._failure = failure

    def acquire_write_lock(self, alias: str, owner_name: str) -> bool:
        if self.holder is not None:
            return False
        self.holder = owner_name
        return True

    def release_write_lock(self, alias: str, owner_name: str) -> bool:
        assert self.holder == owner_name
        self.holder = None
        return True

    def trigger_refresh_for_repo(self, alias_name: str) -> Optional[str]:
        self.triggers.append(alias_name)
        raise self._failure


class _CountingDebouncer:
    def __init__(self) -> None:
        self.signals = 0

    def signal_dirty(self) -> None:
        self.signals += 1


@pytest.fixture
def debouncer() -> Iterator[_CountingDebouncer]:
    counting = _CountingDebouncer()
    meta_description_hook.set_debouncer(counting)  # type: ignore[arg-type]
    meta_description_hook.set_tracking_backend(None)
    try:
        yield counting
    finally:
        meta_description_hook.set_debouncer(None)
        meta_description_hook.set_refresh_scheduler(None)


def _golden(tmp_path: Path) -> Path:
    golden = tmp_path / "golden-repos"
    (golden / "cidx-meta").mkdir(parents=True)
    return golden


def _remove(golden: Path) -> None:
    (golden / "cidx-meta" / f"{REPO}.md").write_text("# old\n", encoding="utf-8")
    meta_description_hook.on_repo_removed(REPO, str(golden))
    assert not (golden / "cidx-meta" / f"{REPO}.md").exists()


def _add(golden: Path) -> None:
    clone = golden / REPO
    clone.mkdir()
    cli_manager = MagicMock()
    cli_manager.check_cli_available.return_value = True
    server_config = MagicMock()
    server_config.claude_integration_config.dep_map_fact_check_enabled = False
    config_service = MagicMock()
    config_service.get_config.return_value = server_config
    with (
        patch.object(
            meta_description_hook,
            "get_claude_cli_manager",
            return_value=cli_manager,
        ),
        patch.object(
            meta_description_hook,
            "_generate_repo_description",
            return_value="# example-repo\n",
        ),
        patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=config_service,
        ),
    ):
        meta_description_hook.on_repo_added(
            REPO, "https://example.com/example-repo.git", str(clone), str(golden)
        )
    assert (golden / "cidx-meta" / f"{REPO}.md").is_file()


HOOKS = [pytest.param(_add, id="on_repo_added"), pytest.param(_remove, id="removed")]


@pytest.mark.parametrize("run_hook", HOOKS)
def test_deferred_refresh_is_left_to_the_scheduler(
    tmp_path: Path,
    debouncer: _CountingDebouncer,
    run_hook: Callable[[Path], None],
) -> None:
    scheduler = _Scheduler(RefreshDeferredError("cidx-meta-global", 2_000_000_000.0))
    meta_description_hook.set_refresh_scheduler(scheduler)

    run_hook(_golden(tmp_path))

    assert scheduler.triggers == ["cidx-meta-global"]
    assert debouncer.signals == 0
    assert scheduler.holder is None


@pytest.mark.parametrize("run_hook", HOOKS)
def test_transient_submission_failure_is_owed_to_the_debouncer(
    tmp_path: Path,
    debouncer: _CountingDebouncer,
    run_hook: Callable[[Path], None],
) -> None:
    scheduler = _Scheduler(OSError("job store briefly unavailable"))
    meta_description_hook.set_refresh_scheduler(scheduler)

    run_hook(_golden(tmp_path))

    assert scheduler.triggers == ["cidx-meta-global"]
    assert debouncer.signals == 1
    assert scheduler.holder is None
