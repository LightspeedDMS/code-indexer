"""Story #2118 slice 1b: the refresh volume alarm is wired, not only built.

- ``build_index_spend_alarm(backend_registry)`` uses the registry's shared
  ``embedding_call_stats`` and ``golden_repo_metadata`` backends (SQLite solo,
  PostgreSQL cluster) and reads ``embedding_stats_config.enabled`` LIVE;
- ``GlobalReposLifecycleManager`` forwards it to its ``RefreshScheduler``;
- lifespan builds it, publishes it on ``app.state.index_spend_alarm`` (read
  by the health collector) and hands it to the lifecycle manager.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator

import pytest

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)
from code_indexer.server.services.config_service import (
    reset_config_service,
    set_config_service,
)
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.index_spend_alarm import build_index_spend_alarm
from tests.utils.golden_repo_metadata_stores import golden_repo_metadata_store


class _FakeConfigService:
    def __init__(self) -> None:
        self.config = SimpleNamespace(
            embedding_stats_config=SimpleNamespace(enabled=True),
            snapshot_min_retention_age_seconds=0,
            alias_lock_config=SimpleNamespace(db_backed_enabled=False),
        )

    def get_config(self) -> Any:
        return self.config


@pytest.fixture
def config_service() -> Iterator[_FakeConfigService]:
    reset_config_service()
    fake = _FakeConfigService()
    set_config_service(fake)  # type: ignore[arg-type]
    yield fake
    reset_config_service()


@pytest.fixture
def registry(tmp_path: Path) -> Iterator[SimpleNamespace]:
    with golden_repo_metadata_store("sqlite", tmp_path) as metadata:
        yield SimpleNamespace(
            embedding_call_stats=EmbeddingCallStatsSqliteBackend(
                str(tmp_path / "stats.db")
            ),
            golden_repo_metadata=metadata,
        )


def test_built_alarm_reads_the_stats_switch_live(config_service, registry):
    alarm = build_index_spend_alarm(registry)

    assert alarm.evaluate().blind_reasons == ()

    config_service.config.embedding_stats_config.enabled = False

    assert any("disabled" in r for r in alarm.evaluate().blind_reasons)


def test_registry_without_a_backend_cannot_build(config_service, registry):
    with pytest.raises(RuntimeError):
        build_index_spend_alarm(
            SimpleNamespace(
                embedding_call_stats=None,
                golden_repo_metadata=registry.golden_repo_metadata,
            )
        )
    with pytest.raises(RuntimeError):
        build_index_spend_alarm(
            SimpleNamespace(
                embedding_call_stats=registry.embedding_call_stats,
                golden_repo_metadata=None,
            )
        )


def test_lifecycle_manager_hands_the_alarm_to_its_scheduler(
    config_service, registry, tmp_path
):
    alarm = build_index_spend_alarm(registry)

    lifecycle = GlobalReposLifecycleManager(
        golden_repos_dir=str(tmp_path / "golden-repos"),
        index_spend_alarm=alarm,
    )

    assert lifecycle.refresh_scheduler._index_spend_alarm is alarm


def test_lifespan_builds_publishes_and_forwards_the_alarm():
    from code_indexer.server.startup import lifespan

    tree = ast.parse(inspect.getsource(lifespan))
    calls = [n for n in ast.walk(tree) if isinstance(n, ast.Call)]
    called = {getattr(c.func, "id", getattr(c.func, "attr", None)): c for c in calls}

    assert "build_index_spend_alarm" in called
    lifecycle_call = called["GlobalReposLifecycleManager"]
    assert "index_spend_alarm" in {kw.arg for kw in lifecycle_call.keywords}
    assigned = {
        t.attr
        for n in ast.walk(tree)
        if isinstance(n, ast.Assign)
        for t in n.targets
        if isinstance(t, ast.Attribute)
    }
    assert "index_spend_alarm" in assigned  # app.state.index_spend_alarm = ...
