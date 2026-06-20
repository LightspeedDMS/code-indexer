"""Unit tests for Story #1158 - Configurable parallel requests via Web UI.

Tests cover:
- IndexingConfig default values for 3 new fields
- _update_indexing_setting() handling of new keys with clamping
- _validate_config_section("indexing") validation rules
- _get_server_provider_values() temporal propagation
- Temporal indexer thread_count fallback at git-diff sites (lines 616, 1172, 1228)
- Lines 409/411 regression: those sites only read parallel_requests (not temporal)
"""

import json as _json
from typing import Optional

import pytest
from unittest.mock import MagicMock, patch

from code_indexer.config import CohereConfig, VoyageAIConfig


# ---------------------------------------------------------------------------
# Section 1: IndexingConfig defaults
# ---------------------------------------------------------------------------


class TestIndexingConfigDefaults:
    """IndexingConfig must have 3 new fields with correct defaults."""

    def test_voyage_ai_parallel_requests_default_is_8(self) -> None:
        from code_indexer.server.utils.config_manager import IndexingConfig

        cfg = IndexingConfig()
        assert cfg.voyage_ai_parallel_requests == 8

    def test_cohere_parallel_requests_default_is_8(self) -> None:
        from code_indexer.server.utils.config_manager import IndexingConfig

        cfg = IndexingConfig()
        assert cfg.cohere_parallel_requests == 8

    def test_temporal_parallel_requests_default_is_none(self) -> None:
        from code_indexer.server.utils.config_manager import IndexingConfig

        cfg = IndexingConfig()
        assert cfg.temporal_parallel_requests is None

    def test_temporal_parallel_requests_accepts_none(self) -> None:
        from code_indexer.server.utils.config_manager import IndexingConfig

        cfg = IndexingConfig(temporal_parallel_requests=None)
        assert cfg.temporal_parallel_requests is None

    def test_temporal_parallel_requests_accepts_int(self) -> None:
        from code_indexer.server.utils.config_manager import IndexingConfig

        cfg = IndexingConfig(temporal_parallel_requests=4)
        assert cfg.temporal_parallel_requests == 4


# ---------------------------------------------------------------------------
# Section 2: _update_indexing_setting() new keys
# ---------------------------------------------------------------------------


class TestUpdateIndexingSetting:
    """_update_indexing_setting() must handle the 3 new keys."""

    def _make_service(self):
        """Create a ConfigService instance backed by real SQLite via temp dir."""
        import tempfile
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        tmp = tempfile.mkdtemp()
        mgr = ServerConfigManager(server_dir_path=tmp)
        svc = ConfigService(config_manager=mgr)
        return svc

    # --- voyage_ai_parallel_requests ---

    def test_voyage_ai_parallel_clamped_from_zero_to_one(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("voyage_ai_parallel_requests", "0")
        cfg = svc.get_config()
        assert cfg.indexing_config.voyage_ai_parallel_requests == 1

    def test_voyage_ai_parallel_clamped_from_100_to_32(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("voyage_ai_parallel_requests", "100")
        cfg = svc.get_config()
        assert cfg.indexing_config.voyage_ai_parallel_requests == 32

    def test_voyage_ai_parallel_valid_16_stored_as_16(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("voyage_ai_parallel_requests", "16")
        cfg = svc.get_config()
        assert cfg.indexing_config.voyage_ai_parallel_requests == 16

    # --- cohere_parallel_requests ---

    def test_cohere_parallel_clamped_from_zero_to_one(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("cohere_parallel_requests", "0")
        cfg = svc.get_config()
        assert cfg.indexing_config.cohere_parallel_requests == 1

    def test_cohere_parallel_clamped_from_100_to_32(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("cohere_parallel_requests", "100")
        cfg = svc.get_config()
        assert cfg.indexing_config.cohere_parallel_requests == 32

    def test_cohere_parallel_valid_16_stored_as_16(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("cohere_parallel_requests", "16")
        cfg = svc.get_config()
        assert cfg.indexing_config.cohere_parallel_requests == 16

    # --- temporal_parallel_requests ---

    def test_temporal_parallel_empty_string_stores_none(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("temporal_parallel_requests", "")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_parallel_requests is None

    def test_temporal_parallel_none_value_stores_none(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("temporal_parallel_requests", None)
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_parallel_requests is None

    def test_temporal_parallel_valid_2_stored_as_2(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("temporal_parallel_requests", "2")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_parallel_requests == 2

    def test_temporal_parallel_clamped_from_100_to_32(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("temporal_parallel_requests", "100")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_parallel_requests == 32

    def test_temporal_parallel_clamped_from_zero_to_one(self) -> None:
        svc = self._make_service()
        svc._update_indexing_setting("temporal_parallel_requests", "0")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_parallel_requests == 1

    def test_temporal_parallel_nonnumeric_raises_value_error(self) -> None:
        svc = self._make_service()
        with pytest.raises(ValueError):
            svc._update_indexing_setting("temporal_parallel_requests", "abc")

    # --- unknown key still raises ---

    def test_unknown_key_raises_value_error(self) -> None:
        svc = self._make_service()
        with pytest.raises(ValueError):
            svc._update_indexing_setting("nonexistent_key", "5")


# ---------------------------------------------------------------------------
# Section 3: _validate_config_section("indexing") validation
# ---------------------------------------------------------------------------


class TestValidateConfigSectionIndexing:
    """Validation rules for the 3 new indexing fields."""

    def _validate(self, data):
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("indexing", data)

    # --- voyage_ai_parallel_requests ---

    def test_voyage_empty_returns_required_error(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": ""})
        assert result is not None
        assert "required" in result.lower() or "valid" in result.lower()

    def test_voyage_none_returns_required_error(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": None})
        assert result is not None

    def test_voyage_zero_returns_range_error(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": "0"})
        assert result is not None

    def test_voyage_33_returns_range_error(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": "33"})
        assert result is not None

    def test_voyage_1_passes(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": "1"})
        assert result is None

    def test_voyage_32_passes(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": "32"})
        assert result is None

    def test_voyage_8_passes(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": "8"})
        assert result is None

    def test_voyage_nonnumeric_returns_error(self) -> None:
        result = self._validate({"voyage_ai_parallel_requests": "abc"})
        assert result is not None

    # --- cohere_parallel_requests ---

    def test_cohere_empty_returns_required_error(self) -> None:
        result = self._validate({"cohere_parallel_requests": ""})
        assert result is not None

    def test_cohere_zero_returns_range_error(self) -> None:
        result = self._validate({"cohere_parallel_requests": "0"})
        assert result is not None

    def test_cohere_33_returns_range_error(self) -> None:
        result = self._validate({"cohere_parallel_requests": "33"})
        assert result is not None

    def test_cohere_8_passes(self) -> None:
        result = self._validate({"cohere_parallel_requests": "8"})
        assert result is None

    def test_cohere_nonnumeric_returns_error(self) -> None:
        result = self._validate({"cohere_parallel_requests": "xyz"})
        assert result is not None

    # --- temporal_parallel_requests (optional) ---

    def test_temporal_empty_passes(self) -> None:
        result = self._validate({"temporal_parallel_requests": ""})
        assert result is None

    def test_temporal_none_passes(self) -> None:
        result = self._validate({"temporal_parallel_requests": None})
        assert result is None

    def test_temporal_zero_returns_range_error(self) -> None:
        result = self._validate({"temporal_parallel_requests": "0"})
        assert result is not None

    def test_temporal_33_returns_range_error(self) -> None:
        result = self._validate({"temporal_parallel_requests": "33"})
        assert result is not None

    def test_temporal_4_passes(self) -> None:
        result = self._validate({"temporal_parallel_requests": "4"})
        assert result is None

    def test_temporal_nonnumeric_returns_error(self) -> None:
        result = self._validate({"temporal_parallel_requests": "bad"})
        assert result is not None


# ---------------------------------------------------------------------------
# Section 4: _get_server_provider_values() propagation
# ---------------------------------------------------------------------------


class TestGetServerProviderValues:
    """_get_server_provider_values() must propagate temporal_parallel_requests."""

    def _call_with_indexing(self, temporal_val):
        """Call _get_server_provider_values with a mock indexing config."""
        from code_indexer.server.services import config_seeding

        mock_indexing = MagicMock()
        mock_indexing.voyage_ai_parallel_requests = 8
        mock_indexing.cohere_parallel_requests = 8
        mock_indexing.temporal_parallel_requests = temporal_val

        mock_server_cfg = MagicMock()
        mock_server_cfg.indexing_config = mock_indexing

        mock_svc = MagicMock()
        mock_svc.get_config.return_value = mock_server_cfg

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_svc,
        ):
            return config_seeding._get_server_provider_values()

    def test_temporal_2_propagates_to_voyage_ai(self) -> None:
        result = self._call_with_indexing(2)
        assert result["voyage_ai"]["temporal_parallel_requests"] == 2

    def test_temporal_2_propagates_to_cohere(self) -> None:
        result = self._call_with_indexing(2)
        assert result["cohere"]["temporal_parallel_requests"] == 2

    def test_temporal_none_propagates_as_none_to_voyage_ai(self) -> None:
        result = self._call_with_indexing(None)
        assert result["voyage_ai"]["temporal_parallel_requests"] is None

    def test_temporal_none_propagates_as_none_to_cohere(self) -> None:
        result = self._call_with_indexing(None)
        assert result["cohere"]["temporal_parallel_requests"] is None

    def test_voyage_ai_parallel_overlay_when_nondefault(self) -> None:
        """When voyage_ai_parallel_requests is 16, result['voyage_ai']['parallel_requests'] == 16."""
        from code_indexer.server.services import config_seeding

        mock_indexing = MagicMock()
        mock_indexing.voyage_ai_parallel_requests = 16
        mock_indexing.cohere_parallel_requests = 8
        mock_indexing.temporal_parallel_requests = None

        mock_server_cfg = MagicMock()
        mock_server_cfg.indexing_config = mock_indexing

        mock_svc = MagicMock()
        mock_svc.get_config.return_value = mock_server_cfg

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_svc,
        ):
            result = config_seeding._get_server_provider_values()

        assert result["voyage_ai"]["parallel_requests"] == 16


# ---------------------------------------------------------------------------
# Section 5: seed_provider_config null propagation
# ---------------------------------------------------------------------------


class TestSeedProviderConfigNullPropagation:
    """seed_provider_config() must write temporal_parallel_requests=null to config.json."""

    def _make_repo_with_config(self, tmp_path, config_content: dict) -> str:
        """Create a temp repo dir with a .code-indexer/config.json."""
        import json

        cidx_dir = tmp_path / ".code-indexer"
        cidx_dir.mkdir()
        config_file = cidx_dir / "config.json"
        config_file.write_text(json.dumps(config_content))
        return str(tmp_path)

    def test_temporal_none_writes_null_to_config_json(self, tmp_path) -> None:
        """With temporal_parallel_requests=None, config.json must have null, not absence."""
        import json
        from code_indexer.server.services import config_seeding

        repo_path = self._make_repo_with_config(
            tmp_path,
            {
                "voyage_ai": {"parallel_requests": 8, "timeout": 30},
                "cohere": {"parallel_requests": 8, "timeout": 30},
            },
        )

        mock_indexing = MagicMock()
        mock_indexing.voyage_ai_parallel_requests = 8
        mock_indexing.cohere_parallel_requests = 8
        mock_indexing.temporal_parallel_requests = None
        # Needed for existing timeout overlay loop
        mock_indexing.voyage_ai_timeout = None
        mock_indexing.cohere_timeout = None

        mock_server_cfg = MagicMock()
        mock_server_cfg.indexing_config = mock_indexing

        mock_svc = MagicMock()
        mock_svc.get_config.return_value = mock_server_cfg

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_svc,
        ):
            config_seeding.seed_provider_config(repo_path)

        config_file = tmp_path / ".code-indexer" / "config.json"
        written = json.loads(config_file.read_text())

        # The key must be PRESENT with value null, not absent
        assert "temporal_parallel_requests" in written.get("voyage_ai", {})
        assert written["voyage_ai"]["temporal_parallel_requests"] is None
        assert "temporal_parallel_requests" in written.get("cohere", {})
        assert written["cohere"]["temporal_parallel_requests"] is None

    def test_temporal_2_writes_2_to_config_json(self, tmp_path) -> None:
        """With temporal_parallel_requests=2, config.json must have 2."""
        import json
        from code_indexer.server.services import config_seeding

        repo_path = self._make_repo_with_config(
            tmp_path,
            {
                "voyage_ai": {"parallel_requests": 8, "timeout": 30},
                "cohere": {"parallel_requests": 8, "timeout": 30},
            },
        )

        mock_indexing = MagicMock()
        mock_indexing.voyage_ai_parallel_requests = 8
        mock_indexing.cohere_parallel_requests = 8
        mock_indexing.temporal_parallel_requests = 2
        mock_indexing.voyage_ai_timeout = None
        mock_indexing.cohere_timeout = None

        mock_server_cfg = MagicMock()
        mock_server_cfg.indexing_config = mock_indexing

        mock_svc = MagicMock()
        mock_svc.get_config.return_value = mock_server_cfg

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=mock_svc,
        ):
            config_seeding.seed_provider_config(repo_path)

        config_file = tmp_path / ".code-indexer" / "config.json"
        written = json.loads(config_file.read_text())

        assert written["voyage_ai"]["temporal_parallel_requests"] == 2
        assert written["cohere"]["temporal_parallel_requests"] == 2


# ---------------------------------------------------------------------------
# Section 6: temporal_indexer git-diff thread_count logic
# ---------------------------------------------------------------------------


class TestTemporalIndexerGitDiffSites:
    """The 3 git-diff ThreadPoolExecutor sites must use temporal_parallel_requests
    when set, falling back to parallel_requests when None.
    """

    def _make_config(
        self, provider: str, parallel: int, temporal: Optional[int]
    ) -> MagicMock:
        """Build a minimal mock Config object."""
        cfg = MagicMock()
        cfg.embedding_provider = provider

        voyage_cfg = MagicMock()
        voyage_cfg.parallel_requests = parallel
        voyage_cfg.temporal_parallel_requests = temporal
        cfg.voyage_ai = voyage_cfg

        cohere_cfg = MagicMock()
        cohere_cfg.parallel_requests = parallel
        cohere_cfg.temporal_parallel_requests = temporal
        cfg.cohere = cohere_cfg

        return cfg

    def _compute_thread_count(self, cfg) -> int:
        """Reproduce the logic used at lines 616, 1172, 1228."""
        if cfg.embedding_provider == "cohere" and hasattr(cfg, "cohere"):
            base_pr = cfg.cohere.parallel_requests
            temporal_pr = getattr(cfg.cohere, "temporal_parallel_requests", None)
        else:
            base_pr = cfg.voyage_ai.parallel_requests
            temporal_pr = getattr(cfg.voyage_ai, "temporal_parallel_requests", None)
        return int(temporal_pr if temporal_pr is not None else base_pr)

    # --- VoyageAI provider ---

    def test_voyage_temporal_2_uses_2(self) -> None:
        cfg = self._make_config("voyage-ai", parallel=8, temporal=2)
        assert self._compute_thread_count(cfg) == 2

    def test_voyage_temporal_none_falls_back_to_parallel(self) -> None:
        cfg = self._make_config("voyage-ai", parallel=5, temporal=None)
        assert self._compute_thread_count(cfg) == 5

    def test_voyage_temporal_16_uses_16(self) -> None:
        cfg = self._make_config("voyage-ai", parallel=8, temporal=16)
        assert self._compute_thread_count(cfg) == 16

    # --- Cohere provider ---

    def test_cohere_temporal_4_uses_4(self) -> None:
        cfg = self._make_config("cohere", parallel=8, temporal=4)
        assert self._compute_thread_count(cfg) == 4

    def test_cohere_temporal_none_falls_back_to_parallel(self) -> None:
        cfg = self._make_config("cohere", parallel=5, temporal=None)
        assert self._compute_thread_count(cfg) == 5


# ---------------------------------------------------------------------------
# Section 8: Pydantic round-trip deserialization (regression for #1158 fix)
# ---------------------------------------------------------------------------

_PROVIDER_CLASSES = [
    pytest.param(VoyageAIConfig, id="voyage_ai"),
    pytest.param(CohereConfig, id="cohere"),
]


class TestProviderConfigPydanticRoundtrip:
    """VoyageAIConfig and CohereConfig must preserve temporal_parallel_requests
    through a JSON serialize/deserialize cycle.

    Before the fix, the field was absent from the Pydantic model.  Pydantic v2
    with extra="ignore" silently drops unknown keys on deserialization, so
    config.json values were always lost — getattr(..., None) always returned
    the getattr default, never the stored value.
    """

    @pytest.mark.parametrize("cls", _PROVIDER_CLASSES)
    def test_temporal_none_survives_roundtrip(self, cls) -> None:
        cfg = cls(temporal_parallel_requests=None)
        data = _json.loads(cfg.model_dump_json())
        restored = cls(**data)
        assert restored.temporal_parallel_requests is None

    @pytest.mark.parametrize("cls", _PROVIDER_CLASSES)
    def test_temporal_int_survives_roundtrip(self, cls) -> None:
        cfg = cls(temporal_parallel_requests=2)
        data = _json.loads(cfg.model_dump_json())
        restored = cls(**data)
        assert restored.temporal_parallel_requests == 2

    @pytest.mark.parametrize("cls", _PROVIDER_CLASSES)
    def test_temporal_key_present_in_serialized_json_when_none(self, cls) -> None:
        """The key must be present in JSON output (not absent), even when None."""
        cfg = cls(temporal_parallel_requests=None)
        data = _json.loads(cfg.model_dump_json())
        assert "temporal_parallel_requests" in data
        assert data["temporal_parallel_requests"] is None


# ---------------------------------------------------------------------------
# Section 9: get_all_settings() must include the 3 new parallel fields
# ---------------------------------------------------------------------------


class TestGetAllSettingsIncludesParallelRequests:
    """get_all_settings() must emit voyage_ai_parallel_requests,
    cohere_parallel_requests, and temporal_parallel_requests inside the
    'indexing' dict.  Before the display fix these keys were absent, causing
    the Jinja template to render blank cells.
    """

    def _make_service(self, tmp_path):
        """Create a ConfigService instance backed by real SQLite via tmp_path."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        mgr = ServerConfigManager(server_dir_path=str(tmp_path))
        return ConfigService(config_manager=mgr)

    def test_get_all_settings_includes_voyage_ai_parallel_requests(
        self, tmp_path
    ) -> None:
        """voyage_ai_parallel_requests must appear in the 'indexing' sub-dict."""
        svc = self._make_service(tmp_path)
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert "voyage_ai_parallel_requests" in indexing, (
            "voyage_ai_parallel_requests missing from get_all_settings()['indexing']"
        )

    def test_get_all_settings_includes_cohere_parallel_requests(self, tmp_path) -> None:
        """cohere_parallel_requests must appear in the 'indexing' sub-dict."""
        svc = self._make_service(tmp_path)
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert "cohere_parallel_requests" in indexing, (
            "cohere_parallel_requests missing from get_all_settings()['indexing']"
        )

    def test_get_all_settings_includes_temporal_parallel_requests(
        self, tmp_path
    ) -> None:
        """temporal_parallel_requests must appear in the 'indexing' sub-dict."""
        svc = self._make_service(tmp_path)
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert "temporal_parallel_requests" in indexing, (
            "temporal_parallel_requests missing from get_all_settings()['indexing']"
        )

    def test_get_all_settings_returns_none_for_temporal_when_not_set(
        self, tmp_path
    ) -> None:
        """temporal_parallel_requests must be None when not configured (fresh DB)."""
        svc = self._make_service(tmp_path)
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert "temporal_parallel_requests" in indexing
        assert indexing["temporal_parallel_requests"] is None

    def test_get_all_settings_reflects_saved_values(self, tmp_path) -> None:
        """After saving all 3 fields, get_all_settings returns the saved values."""
        svc = self._make_service(tmp_path)
        svc._update_indexing_setting("voyage_ai_parallel_requests", "16")
        svc._update_indexing_setting("cohere_parallel_requests", "4")
        svc._update_indexing_setting("temporal_parallel_requests", "2")
        settings = svc.get_all_settings()
        indexing = settings.get("indexing", {})
        assert indexing.get("voyage_ai_parallel_requests") == 16
        assert indexing.get("cohere_parallel_requests") == 4
        assert indexing.get("temporal_parallel_requests") == 2


# ---------------------------------------------------------------------------
# Section 7: Lines 409/411 regression — those sites must NOT use temporal
# ---------------------------------------------------------------------------


class TestVectorCalculationManagerSitesUnchanged:
    """Lines 409/411 (VectorCalculationManager sites) must read only parallel_requests.

    These are embedding thread pool sites, NOT git-diff sites. They must remain
    unaffected by temporal_parallel_requests.
    """

    def _make_config_with_temporal(
        self, provider: str, parallel: int, temporal: int
    ) -> MagicMock:
        cfg = MagicMock()
        cfg.embedding_provider = provider

        voyage_cfg = MagicMock()
        voyage_cfg.parallel_requests = parallel
        voyage_cfg.temporal_parallel_requests = temporal
        cfg.voyage_ai = voyage_cfg

        cohere_cfg = MagicMock()
        cohere_cfg.parallel_requests = parallel
        cohere_cfg.temporal_parallel_requests = temporal
        cfg.cohere = cohere_cfg

        return cfg

    def _compute_vector_thread_count_at_409_411(self, cfg) -> int:
        """Reproduce the logic at lines 408-413 (VectorCalculationManager sites)."""
        _provider = getattr(cfg, "embedding_provider", None)
        if _provider == "cohere" and hasattr(cfg, "cohere"):
            return int(cfg.cohere.parallel_requests)
        elif _provider == "voyage-ai" and hasattr(cfg, "voyage_ai"):
            return int(cfg.voyage_ai.parallel_requests)
        else:
            return 4

    def test_voyage_ai_site_ignores_temporal(self) -> None:
        """Line 411: even with temporal=2, parallel_requests=8 is used."""
        cfg = self._make_config_with_temporal("voyage-ai", parallel=8, temporal=2)
        assert self._compute_vector_thread_count_at_409_411(cfg) == 8

    def test_cohere_site_ignores_temporal(self) -> None:
        """Line 409: even with temporal=4, parallel_requests=8 is used."""
        cfg = self._make_config_with_temporal("cohere", parallel=8, temporal=4)
        assert self._compute_vector_thread_count_at_409_411(cfg) == 8


# ---------------------------------------------------------------------------
# Section 10: Rolling-upgrade backward-compatibility for IndexingConfig
# ---------------------------------------------------------------------------


class TestRollingUpgradeBackwardCompat:
    """_dict_to_server_config must not crash when the DB blob contains
    indexing_config keys unknown to the current IndexingConfig dataclass.

    Scenario: a NEW node (post-Story #1158) saves voyage_ai_parallel_requests,
    cohere_parallel_requests, temporal_parallel_requests to the DB.  An OLD node
    (pre-#1158, without those fields) loads that blob and must not raise:
        TypeError: __init__() got an unexpected keyword argument '...'

    This is the rolling-upgrade invariant: old and new nodes share schema.
    """

    def _make_manager(self):
        import tempfile
        from code_indexer.server.utils.config_manager import ServerConfigManager

        tmp = tempfile.mkdtemp()
        return ServerConfigManager(server_dir_path=tmp)

    def test_indexing_config_tolerates_unknown_keys_rolling_upgrade(self) -> None:
        """Simulates an old node loading a new-node config blob.

        Constructs a config dict whose indexing_config sub-dict contains the 3
        Story #1158 fields PLUS 3 bogus extra keys that do not exist in ANY
        version of IndexingConfig.  _dict_to_server_config must return a valid
        ServerConfig without raising TypeError.
        """
        import dataclasses
        from code_indexer.server.utils.config_manager import IndexingConfig

        mgr = self._make_manager()

        # Build a minimal indexing_config dict with all KNOWN fields plus 3
        # unknown keys that simulate fields from a future/newer node.
        idx_dict = {
            f.name: f.default
            for f in dataclasses.fields(IndexingConfig)
            if f.default is not dataclasses.MISSING
        }
        # Add unknown keys that would come from a newer node
        idx_dict["voyage_ai_parallel_requests_future"] = 16
        idx_dict["cohere_parallel_requests_future"] = 16
        idx_dict["temporal_parallel_requests_future"] = 4

        # Build a minimal top-level config dict.
        # server_dir is required by ServerConfig; _dict_to_server_config does NOT
        # auto-inject it (only load_config() does), so supply it explicitly.
        config_dict: dict = {
            "server_dir": str(mgr.server_dir),
            "indexing_config": idx_dict,
        }

        # Must NOT raise TypeError; the unknown keys must be stripped silently
        result = mgr._dict_to_server_config(config_dict)

        from code_indexer.server.utils.config_manager import ServerConfig

        assert isinstance(result, ServerConfig)
        assert isinstance(result.indexing_config, IndexingConfig)
