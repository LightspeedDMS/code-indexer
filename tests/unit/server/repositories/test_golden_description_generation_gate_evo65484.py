"""EVO-65484: golden-repo description-generation disable flag."""

from types import SimpleNamespace
from unittest.mock import patch

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.utils.config_manager import IndexingConfig


def _mgr():
    # The gate helper reads config via get_config_service and uses no instance
    # state, so bypass __init__ to test it in isolation.
    return GoldenRepoManager.__new__(GoldenRepoManager)


def test_indexing_config_default_is_enabled():
    assert IndexingConfig().golden_description_generation_enabled is True


def _patch_config(flag):
    cfg = SimpleNamespace(
        indexing_config=SimpleNamespace(golden_description_generation_enabled=flag)
    )
    svc = SimpleNamespace(get_config=lambda: cfg)
    return patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    )


def test_gate_true_when_enabled():
    with _patch_config(True):
        assert _mgr()._golden_description_generation_enabled() is True


def test_gate_false_when_disabled():
    with _patch_config(False):
        assert _mgr()._golden_description_generation_enabled() is False


def test_gate_fail_open_on_config_error():
    # A config read that raises must default to enabled (opt-OUT knob, never a
    # silent skip).
    with patch(
        "code_indexer.server.services.config_service.get_config_service",
        side_effect=RuntimeError("config unavailable"),
    ):
        assert _mgr()._golden_description_generation_enabled() is True


def test_gate_true_when_indexing_config_none():
    cfg = SimpleNamespace(indexing_config=None)
    svc = SimpleNamespace(get_config=lambda: cfg)
    with patch(
        "code_indexer.server.services.config_service.get_config_service",
        return_value=svc,
    ):
        assert _mgr()._golden_description_generation_enabled() is True
