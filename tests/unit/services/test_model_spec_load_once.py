"""Story #1082: static model-spec YAML must be parsed once per process.

Voyage/Cohere model-spec YAML is a static package asset (no drift). It must be
loaded once at module/process init, NOT re-opened and re-parsed on every
VoyageAIClient/CohereEmbeddingProvider construction (which happens per query on
the server hot path).

These tests count yaml parses by patching the module-level loader's underlying
file read, then construct many clients and assert exactly one parse occurred.
No mocks of the code under test -- only the file-read side effect is counted.
"""

import os

import pytest

from code_indexer.config import VoyageAIConfig, CohereConfig


def test_voyage_model_specs_parsed_once_across_many_clients(monkeypatch):
    from code_indexer.services import voyage_ai

    # Reset the process-level memo so the test is deterministic.
    voyage_ai._reset_model_specs_cache_for_tests()

    parse_count = {"n": 0}
    real_safe_load = voyage_ai.yaml.safe_load

    def counting_safe_load(stream):
        parse_count["n"] += 1
        return real_safe_load(stream)

    monkeypatch.setattr(voyage_ai.yaml, "safe_load", counting_safe_load)
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")

    clients = [voyage_ai.VoyageAIClient(VoyageAIConfig()) for _ in range(5)]

    # Exactly one parse despite five constructions.
    assert parse_count["n"] == 1
    # Every client received a usable model-spec mapping.
    for c in clients:
        assert "voyage_models" in c.model_specs
        assert "voyage-code-3" in c.model_specs["voyage_models"]


def test_voyage_specs_match_real_yaml(monkeypatch):
    from code_indexer.services import voyage_ai

    voyage_ai._reset_model_specs_cache_for_tests()
    monkeypatch.setenv("VOYAGE_API_KEY", "test-key")

    client = voyage_ai.VoyageAIClient(VoyageAIConfig())
    # The shared parsed dict is the same object across constructions (load-once).
    client2 = voyage_ai.VoyageAIClient(VoyageAIConfig())
    assert client.model_specs is client2.model_specs


def test_cohere_model_specs_parsed_once_across_many_clients(monkeypatch):
    from code_indexer.services import cohere_embedding

    cohere_embedding._reset_model_specs_cache_for_tests()

    parse_count = {"n": 0}
    real_safe_load = cohere_embedding.yaml.safe_load

    def counting_safe_load(stream):
        parse_count["n"] += 1
        return real_safe_load(stream)

    monkeypatch.setattr(cohere_embedding.yaml, "safe_load", counting_safe_load)

    cfg = CohereConfig(api_key="test-cohere-key")
    providers = [cohere_embedding.CohereEmbeddingProvider(cfg) for _ in range(5)]

    assert parse_count["n"] == 1
    for p in providers:
        assert "cohere_models" in p.model_specs


@pytest.fixture(autouse=True)
def _clean_env():
    # Ensure no ambient provider keys leak across tests.
    saved = {k: os.environ.get(k) for k in ("VOYAGE_API_KEY", "CO_API_KEY")}
    yield
    for k, v in saved.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
