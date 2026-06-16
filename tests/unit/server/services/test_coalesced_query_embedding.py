"""Story #1079 Phase E — coalesced_query_embedding gating tests.

coalesced_query_embedding is the single entry point the 4 query sites call. Its
gating (server-gating via the registry + kill switch via runtime config) lives
ENTIRELY in this helper so the call sites are identical on CLI and server:

  1. No coalescer registry (CLI/solo — lifespan never built one)
        -> return governed_query_embedding(...) (direct governed single call).
  2. Registry present but coalesce_enabled is False (kill switch)
        -> return governed_query_embedding(...) (governor + AIMD still apply).
  3. Registry present, enabled, provider's :embed lane has a coalescer
        -> return coalescer.submit(text).
  4. Registry present, enabled, but that lane has NO coalescer (provider key
     absent) -> return governed_query_embedding(...) (explicit direct path).

CRITICAL acceptance criterion: in a CLI-like context (no registry), the helper
calls through to governed_query_embedding with NO coalescer constructed and no
batching wait. This guards "CLI paths untouched".
"""

from typing import List

import pytest

from code_indexer.server.services import governed_call
from code_indexer.server.services.coalescer_registry import (
    CoalescerRegistry,
    clear_coalescer_registry,
    set_coalescer_registry,
)

VOYAGE_EMBED = "voyage:embed"
COHERE_EMBED = "cohere:embed"
SENTINEL_VEC = [0.123, 0.456]
COALESCED_VEC = [9.0, 9.0]


class _FakeVoyageProvider:
    """Not a Cohere instance -> maps to voyage:embed."""


class _FakeCohereProvider:
    """Stand-in registered as the cohere isinstance target via patching."""


class _FakeCoalescer:
    def __init__(self) -> None:
        self.submitted: List[str] = []

    def submit(self, text: str, embedding_purpose: str = "query") -> List[float]:
        self.submitted.append(text)
        return COALESCED_VEC


class _FakeConfig:
    def __init__(self, coalesce_enabled: bool = True) -> None:
        self.coalesce_enabled = coalesce_enabled


class _FakeConfigService:
    def __init__(self, cfg: _FakeConfig) -> None:
        self._cfg = cfg

    def get_config(self) -> _FakeConfig:
        return self._cfg


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_coalescer_registry()
    yield
    clear_coalescer_registry()


def _patch_governed(monkeypatch):
    """Replace governed_query_embedding with a spy returning SENTINEL_VEC."""
    calls = {}

    def _spy(provider, text, *, embedding_purpose="query", acquire_timeout=30.0):
        calls["provider"] = provider
        calls["text"] = text
        calls["embedding_purpose"] = embedding_purpose
        return SENTINEL_VEC

    monkeypatch.setattr(governed_call, "governed_query_embedding", _spy)
    return calls


def _patch_config(monkeypatch, cfg: _FakeConfig):
    monkeypatch.setattr(
        governed_call,
        "get_config_service",
        lambda: _FakeConfigService(cfg),
        raising=False,
    )


class TestNoRegistryDelegates:
    def test_cli_context_delegates_to_governed_single_call(self, monkeypatch):
        """No registry (CLI) -> direct governed single call, NO coalescer used."""
        calls = _patch_governed(monkeypatch)
        # No registry set, and config service must NOT even be needed.
        prov = _FakeVoyageProvider()
        out = governed_call.coalesced_query_embedding(prov, "hello")
        assert out == SENTINEL_VEC
        assert calls["provider"] is prov
        assert calls["text"] == "hello"


class TestKillSwitchDelegates:
    def test_coalesce_disabled_delegates(self, monkeypatch):
        """Registry present but coalesce_enabled False -> delegate."""
        _patch_governed(monkeypatch)
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=False))
        coalescer = _FakeCoalescer()
        set_coalescer_registry(
            CoalescerRegistry.__new__(CoalescerRegistry)
        )  # placeholder; override get below
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(reg, "get", lambda lane: coalescer, raising=False)

        out = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), "hi")
        assert out == SENTINEL_VEC  # delegated, not coalesced
        assert coalescer.submitted == []  # coalescer never used


class TestEnabledUsesCoalescer:
    def test_enabled_with_lane_uses_submit(self, monkeypatch):
        """Registry + enabled + lane present -> coalescer.submit().

        Intent preserved: when coalesce_enabled=True and the registry holds a
        coalescer for the lane, coalesced_query_embedding must call
        coalescer.submit() (not governed_query_embedding).

        Adaptation: _compute_live now calls registry.get_or_create(lane, digest,
        provider) instead of registry.get(lane), so we stub get_or_create to
        return the fake coalescer and capture the lane argument.
        """
        _patch_governed(monkeypatch)  # spy present but must NOT be called
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=True))
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        captured: dict = {}

        def _get_or_create(lane, digest, provider):
            captured["lane"] = lane
            return coalescer

        monkeypatch.setattr(reg, "get_or_create", _get_or_create, raising=False)

        out = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), "abc")
        assert out == COALESCED_VEC
        assert coalescer.submitted == ["abc"]
        assert captured["lane"] == VOYAGE_EMBED

    def test_cohere_provider_maps_to_cohere_embed_lane(self, monkeypatch):
        """A Cohere provider routes to the cohere:embed lane.

        Intent preserved: when the provider is a CohereEmbeddingProvider instance,
        _get_embedding_budget must return "cohere:embed" and the coalescer for
        that lane must be invoked.

        Adaptation: stub get_or_create (not get) to match the new dispatch path.
        """
        _patch_governed(monkeypatch)
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=True))
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        captured: dict = {}

        def _get_or_create(lane, digest, provider):
            captured["lane"] = lane
            return coalescer

        monkeypatch.setattr(reg, "get_or_create", _get_or_create, raising=False)

        # Patch the cohere isinstance check used by _get_embedding_budget.
        from code_indexer.services import cohere_embedding

        monkeypatch.setattr(
            cohere_embedding, "CohereEmbeddingProvider", _FakeCohereProvider
        )
        out = governed_call.coalesced_query_embedding(_FakeCohereProvider(), "z")
        assert out == COALESCED_VEC
        assert captured["lane"] == COHERE_EMBED


class TestHotReload:
    def test_toggling_coalesce_enabled_takes_effect_without_restart(self, monkeypatch):
        """Flipping coalesce_enabled live switches behavior between calls.

        Intent preserved: when coalesce_enabled is False, coalesced_query_embedding
        delegates to governed_query_embedding (no coalescer used). After flipping
        coalesce_enabled=True on the live config object (no restart, no re-registration),
        the next call must use the coalescer.

        Adaptation: stub get_or_create (not get) to match the new _compute_live
        dispatch path. The behavioral contract is identical.
        """
        calls = _patch_governed(monkeypatch)
        cfg = _FakeConfig(coalesce_enabled=False)
        _patch_config(monkeypatch, cfg)
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(
            reg,
            "get_or_create",
            lambda lane, digest, provider: coalescer,
            raising=False,
        )

        prov = _FakeVoyageProvider()
        # First call: disabled -> delegates.
        out1 = governed_call.coalesced_query_embedding(prov, "first")
        assert out1 == SENTINEL_VEC
        assert coalescer.submitted == []
        assert calls["text"] == "first"

        # Flip the LIVE config; no restart, no re-registration.
        cfg.coalesce_enabled = True
        out2 = governed_call.coalesced_query_embedding(prov, "second")
        assert out2 == COALESCED_VEC
        assert coalescer.submitted == ["second"]


class TestEnabledButLaneAbsentDelegates:
    def test_absent_lane_delegates(self, monkeypatch):
        """Registry + enabled but no coalescer for the lane -> delegate."""
        calls = _patch_governed(monkeypatch)
        _patch_config(monkeypatch, _FakeConfig(coalesce_enabled=True))
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(reg, "get", lambda lane: None, raising=False)

        out = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), "q")
        assert out == SENTINEL_VEC
        assert calls["text"] == "q"


class TestConfigReadFailureDelegates:
    def test_unreadable_config_delegates(self, monkeypatch):
        """Registry present but config read raises -> defensive direct call."""
        calls = _patch_governed(monkeypatch)
        coalescer = _FakeCoalescer()
        set_coalescer_registry(CoalescerRegistry.__new__(CoalescerRegistry))
        reg = governed_call.get_coalescer_registry()
        monkeypatch.setattr(reg, "get", lambda lane: coalescer, raising=False)

        def _boom():
            raise RuntimeError("config service not initialized")

        monkeypatch.setattr(governed_call, "get_config_service", _boom, raising=False)

        out = governed_call.coalesced_query_embedding(_FakeVoyageProvider(), "x")
        assert out == SENTINEL_VEC  # delegated (fail toward direct call)
        assert coalescer.submitted == []  # coalescer never used
        assert calls["text"] == "x"
