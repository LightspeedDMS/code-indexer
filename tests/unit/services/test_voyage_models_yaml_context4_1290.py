"""Unit test: voyage-context-4 model spec present in voyage_models.yaml (Story #1290).

The contextual temporal embedder (voyage-context-4) needs a token_limit entry
in the shared model-spec YAML for token-preflight / request-seal calculations
(AC23), and must be constrained to 1024 output dimensions (AC9).
"""

from src.code_indexer.services.voyage_ai import (
    _get_voyage_model_specs,
    _reset_model_specs_cache_for_tests,
)


class TestVoyageContext4ModelSpec:
    def setup_method(self):
        _reset_model_specs_cache_for_tests()

    def teardown_method(self):
        _reset_model_specs_cache_for_tests()

    def test_voyage_context_4_present_in_model_specs(self):
        specs = _get_voyage_model_specs()
        assert "voyage-context-4" in specs["voyage_models"]

    def test_voyage_context_4_has_token_limit(self):
        specs = _get_voyage_model_specs()
        entry = specs["voyage_models"]["voyage-context-4"]
        assert isinstance(entry["token_limit"], int)
        assert entry["token_limit"] > 0

    def test_voyage_context_4_default_dimension_is_1024(self):
        specs = _get_voyage_model_specs()
        entry = specs["voyage_models"]["voyage-context-4"]
        assert entry["default_dimension"] == 1024
        assert entry["dimensions"] == [1024]
