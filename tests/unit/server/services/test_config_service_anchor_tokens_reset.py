"""Regression tests for per-provider anchor_tokens reset to None (inherit-global).

Bug: `_update_query_embedding_cache_setting` did `int(value)` unconditionally for
     query_embedding_cache_voyage_anchor_tokens and query_embedding_cache_cohere_anchor_tokens.
     An empty form value ("") triggers ValueError: invalid literal for int().
     More importantly, there is no way to reset the per-provider override to None
     (inherit the global anchor_tokens value) via the Web UI.

Fix: treat empty string or None as None (inherit-global) for these two fields only.
     A numeric string is still parsed as int.

Also: web validation must accept "" / None for these two fields (not return an error).

Guards:
  A1  empty string sets voyage_anchor_tokens to None
  A2  None value sets voyage_anchor_tokens to None
  A3  numeric string sets voyage_anchor_tokens to int
  A4  empty string sets cohere_anchor_tokens to None
  A5  None value sets cohere_anchor_tokens to None
  A6  numeric string sets cohere_anchor_tokens to int
  A7  global anchor_tokens is NOT reset on empty (still requires int)
  A8  validation (routes.py) accepts "" for voyage_anchor_tokens
  A9  validation (routes.py) accepts "" for cohere_anchor_tokens
  A10 validation (routes.py) accepts None for voyage_anchor_tokens
  A11 validation (routes.py) accepts None for cohere_anchor_tokens
"""

from __future__ import annotations


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_service(tmp_path):
    from code_indexer.server.services.config_service import ConfigService

    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    return svc


def _qec(service):
    """Return the QueryEmbeddingCacheConfig from service, creating if absent."""
    cfg = service.get_config()
    return cfg.query_embedding_cache_config


# ---------------------------------------------------------------------------
# A1 / A2 — voyage anchor_tokens reset to None
# ---------------------------------------------------------------------------


class TestVoyageAnchorTokensReset:
    def test_empty_string_resets_voyage_anchor_tokens_to_none(self, tmp_path):
        """An empty string value must set voyage_anchor_tokens to None (inherit-global)."""
        svc = _make_service(tmp_path)
        # First set it to a value
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_voyage_anchor_tokens", "512"
        )
        assert _qec(svc).query_embedding_cache_voyage_anchor_tokens == 512

        # Now reset via empty string
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_voyage_anchor_tokens", ""
        )
        result = _qec(svc).query_embedding_cache_voyage_anchor_tokens
        assert result is None, (
            f"Expected None (inherit-global) when empty string passed, got {result!r}. "
            "Bug: int('') raises ValueError."
        )

    def test_none_value_resets_voyage_anchor_tokens_to_none(self, tmp_path):
        """Passing None must set voyage_anchor_tokens to None (inherit-global)."""
        svc = _make_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_voyage_anchor_tokens", "256"
        )
        assert _qec(svc).query_embedding_cache_voyage_anchor_tokens == 256

        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_voyage_anchor_tokens", None
        )
        result = _qec(svc).query_embedding_cache_voyage_anchor_tokens
        assert result is None, f"Expected None when None passed, got {result!r}"

    def test_numeric_string_sets_voyage_anchor_tokens(self, tmp_path):
        """A numeric string must still be parsed as int."""
        svc = _make_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache",
            "query_embedding_cache_voyage_anchor_tokens",
            "1024",
        )
        assert _qec(svc).query_embedding_cache_voyage_anchor_tokens == 1024


# ---------------------------------------------------------------------------
# A4 / A5 / A6 — cohere anchor_tokens reset to None
# ---------------------------------------------------------------------------


class TestCohereAnchorTokensReset:
    def test_empty_string_resets_cohere_anchor_tokens_to_none(self, tmp_path):
        """An empty string value must set cohere_anchor_tokens to None (inherit-global)."""
        svc = _make_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_cohere_anchor_tokens", "300"
        )
        assert _qec(svc).query_embedding_cache_cohere_anchor_tokens == 300

        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_cohere_anchor_tokens", ""
        )
        result = _qec(svc).query_embedding_cache_cohere_anchor_tokens
        assert result is None, (
            f"Expected None (inherit-global) when empty string passed, got {result!r}."
        )

    def test_none_value_resets_cohere_anchor_tokens_to_none(self, tmp_path):
        """Passing None must set cohere_anchor_tokens to None."""
        svc = _make_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_cohere_anchor_tokens", "128"
        )
        assert _qec(svc).query_embedding_cache_cohere_anchor_tokens == 128

        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_cohere_anchor_tokens", None
        )
        result = _qec(svc).query_embedding_cache_cohere_anchor_tokens
        assert result is None, f"Expected None when None passed, got {result!r}"

    def test_numeric_string_sets_cohere_anchor_tokens(self, tmp_path):
        """A numeric string must still be parsed as int."""
        svc = _make_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_cohere_anchor_tokens", "512"
        )
        assert _qec(svc).query_embedding_cache_cohere_anchor_tokens == 512


# ---------------------------------------------------------------------------
# A8–A11 — validation (routes._validate_config_section) accepts empty/None
# ---------------------------------------------------------------------------


class TestValidationAcceptsEmptyAnchorTokens:
    """_validate_config_section must return None (no error) for empty/None per-provider fields."""

    def _validate(self, data: dict):
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("query_embedding_cache", data)

    def test_validation_accepts_empty_string_for_voyage(self):
        err = self._validate({"query_embedding_cache_voyage_anchor_tokens": ""})
        assert err is None, (
            f"Expected no validation error for empty string, got: {err!r}"
        )

    def test_validation_accepts_empty_string_for_cohere(self):
        err = self._validate({"query_embedding_cache_cohere_anchor_tokens": ""})
        assert err is None, (
            f"Expected no validation error for empty string, got: {err!r}"
        )

    def test_validation_accepts_none_for_voyage(self):
        err = self._validate({"query_embedding_cache_voyage_anchor_tokens": None})
        assert err is None, f"Expected no validation error for None, got: {err!r}"

    def test_validation_accepts_none_for_cohere(self):
        err = self._validate({"query_embedding_cache_cohere_anchor_tokens": None})
        assert err is None, f"Expected no validation error for None, got: {err!r}"

    def test_validation_accepts_numeric_string_voyage(self):
        err = self._validate({"query_embedding_cache_voyage_anchor_tokens": "512"})
        assert err is None

    def test_validation_accepts_numeric_string_cohere(self):
        err = self._validate({"query_embedding_cache_cohere_anchor_tokens": "256"})
        assert err is None

    def test_validation_rejects_negative_for_voyage(self):
        err = self._validate({"query_embedding_cache_voyage_anchor_tokens": "-1"})
        assert err is not None, "Negative value should be rejected"

    def test_validation_rejects_non_numeric_for_voyage(self):
        err = self._validate({"query_embedding_cache_voyage_anchor_tokens": "abc"})
        assert err is not None, "Non-numeric string should be rejected"
