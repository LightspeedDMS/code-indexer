"""Tests for Story #323: Wiki Metadata Fields Configuration via Web UI.

TDD red phase: all tests written BEFORE implementation.

Covers:
 AC1/AC2:  enable_header_block_parsing toggle
 AC3/AC4:  enable_article_number toggle
 AC5/AC6:  enable_publication_status toggle
 AC7/AC8:  enable_views_seeding toggle
 AC9:      golden regression - default WikiConfig == wiki_config=None
 AC10:     clean generic wiki - all toggles OFF
 AC12:     config persistence round-trip
 AC13:     default config migration (missing key -> all-defaults WikiConfig)
 AC14:     wiki_config=None backward compat
"""

import json
import tempfile
from dataclasses import asdict
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from cidx_wiki.server.wiki_service import WikiService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc():
    return WikiService()


def _make_cache(view_count: int = 0) -> MagicMock:
    cache = MagicMock()
    cache.get_view_count.return_value = view_count
    cache.get_all_view_counts.return_value = {}
    return cache


def _as_dict(pairs):
    return dict(pairs)


def _make_kb_metadata():
    """Return front matter metadata with all KB-specific fields present."""
    return {
        "article_number": "KA-00001",
        "publication_status": "Published",
        "views": 500,
        "created": "2024-01-01",
        "author": "Alice",
    }


# ---------------------------------------------------------------------------
# WikiConfig dataclass
# ---------------------------------------------------------------------------


class TestWikiConfigDataclass:
    """AC12/AC13: WikiConfig dataclass structure, persistence, and migration."""

    def test_wiki_config_can_be_imported(self):
        """WikiConfig must be importable from config_manager."""
        from cidx_wiki.config import WikiConfig

        assert WikiConfig is not None

    def test_wiki_config_defaults_all_true(self):
        """AC13: Default WikiConfig has all 4 toggles True."""
        from cidx_wiki.config import WikiConfig

        cfg = WikiConfig()
        assert cfg.enable_header_block_parsing is True
        assert cfg.enable_article_number is True
        assert cfg.enable_publication_status is True
        assert cfg.enable_views_seeding is True

    def test_wiki_config_can_set_all_false(self):
        """WikiConfig can be instantiated with all False."""
        from cidx_wiki.config import WikiConfig

        cfg = WikiConfig(
            enable_header_block_parsing=False,
            enable_article_number=False,
            enable_publication_status=False,
            enable_views_seeding=False,
        )
        assert cfg.enable_header_block_parsing is False
        assert cfg.enable_article_number is False
        assert cfg.enable_publication_status is False
        assert cfg.enable_views_seeding is False

    def test_wiki_config_round_trip_json(self):
        """AC12: WikiConfig survives JSON serialize/deserialize with custom toggle values."""
        from dataclasses import asdict
        from cidx_wiki.config import WikiConfig

        original = WikiConfig(
            enable_header_block_parsing=False,
            enable_article_number=True,
            enable_publication_status=False,
            enable_views_seeding=True,
        )
        serialized = json.dumps(asdict(original))
        deserialized_dict = json.loads(serialized)
        restored = WikiConfig(**deserialized_dict)

        assert restored.enable_header_block_parsing is False
        assert restored.enable_article_number is True
        assert restored.enable_publication_status is False
        assert restored.enable_views_seeding is True

    def test_server_config_no_longer_has_wiki_config(self):
        """The settings are the cidx-wiki feature's config section (modular
        phase 4e), not a ServerConfig field."""
        from code_indexer.server.utils.config_manager import ServerConfig

        fields = {f.name for f in ServerConfig.__dataclass_fields__.values()}
        assert "wiki_config" not in fields

    def test_feature_section_defaults_match_wiki_config(self):
        """AC13: the section's defaults are WikiConfig's all-True defaults."""
        from cidx_wiki.config import WIKI_CONFIG_SECTION, WikiConfig, default_values

        assert {key.name for key in WIKI_CONFIG_SECTION.keys} == set(
            asdict(WikiConfig())
        )
        assert default_values() == asdict(WikiConfig())

    def test_load_config_moves_wiki_config_into_the_feature_section(self):
        """AC13/upgrade: a stored wiki_config lands in feature_settings["wiki"]."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            raw_config = {
                "server_dir": tmpdir,
                "wiki_config": {
                    "enable_header_block_parsing": False,
                    "enable_article_number": True,
                    "enable_publication_status": False,
                    "enable_views_seeding": True,
                },
            }
            config_path.write_text(json.dumps(raw_config))
            manager = ServerConfigManager(tmpdir)
            loaded = manager.load_config()
            assert loaded is not None
            section = loaded.feature_settings["wiki"]
            assert section["enable_header_block_parsing"] is False
            assert section["enable_article_number"] is True
            assert section["enable_publication_status"] is False

    def test_load_config_missing_wiki_config_leaves_the_section_at_defaults(self):
        """AC13: Missing wiki_config -> nothing stored, the section's defaults."""
        from code_indexer.server.utils.config_manager import ServerConfigManager

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            raw_config = {"server_dir": tmpdir}
            config_path.write_text(json.dumps(raw_config))
            manager = ServerConfigManager(tmpdir)
            loaded = manager.load_config()
            assert loaded is not None
            assert "wiki" not in (loaded.feature_settings or {})


# ---------------------------------------------------------------------------
# AC1/AC2: enable_header_block_parsing
# ---------------------------------------------------------------------------


class TestHeaderBlockParsingToggle:
    """AC1: Toggle ON -> header stripped. AC2: Toggle OFF -> header preserved as markdown."""

    def test_ac1_header_block_stripped_when_toggle_on(self, svc):
        """AC1: enable_header_block_parsing=True -> _strip_header_block runs normally."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_header_block_parsing=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "article.md"
            f.write_text(
                "Article Number: KA-00001\n"
                "Title: My Article\n"
                "Publication Status: Draft\n"
                "Summary: Brief\n"
                "---\n"
                "# Real Content\n"
                "Body text."
            )
            result = svc.render_article(f, "test-repo", wiki_config=wiki_config)
            assert "Article Number" not in result["html"]
            assert "Real Content" in result["html"]

    def test_ac2_header_block_preserved_when_toggle_off(self, svc):
        """AC2: enable_header_block_parsing=False -> header fields NOT stripped, render as-is."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_header_block_parsing=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "article.md"
            f.write_text(
                "Article Number: KA-00001\n"
                "Title: My Article\n"
                "Publication Status: Draft\n"
                "---\n"
                "# Real Content\n"
                "Body text."
            )
            result = svc.render_article(f, "test-repo", wiki_config=wiki_config)
            assert "Article Number" in result["html"]


# ---------------------------------------------------------------------------
# AC3/AC4: enable_article_number
# ---------------------------------------------------------------------------


class TestArticleNumberToggle:
    """AC3: Toggle ON -> article_number in panel. AC4: Toggle OFF -> excluded."""

    def test_ac3_article_number_included_when_toggle_on(self, svc):
        """AC3: enable_article_number=True -> article_number appears in metadata panel."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_article_number=True)
        cache = _make_cache()
        metadata = {"article_number": "KA-00001", "created": "2024-01-01"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Article" in d
        assert d["Salesforce Article"] == "KA-00001"

    def test_ac4_article_number_excluded_when_toggle_off(self, svc):
        """AC4: enable_article_number=False -> article_number not in metadata panel."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_article_number=False)
        cache = _make_cache()
        metadata = {"article_number": "KA-00001", "created": "2024-01-01"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Article" not in d

    def test_ac4_original_article_also_excluded_when_toggle_off(self, svc):
        """AC4: original_article (normalized to article_number) also excluded when toggle OFF."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_article_number=False)
        cache = _make_cache()
        metadata = {"original_article": "KA-00002"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Article" not in d

    def test_ac3_article_number_is_first_item_when_toggle_on(self, svc):
        """AC3: article_number is the first item in the metadata panel list."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_article_number=True)
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        assert result[0][0] == "Salesforce Article"


# ---------------------------------------------------------------------------
# AC5/AC6: enable_publication_status
# ---------------------------------------------------------------------------


class TestPublicationStatusToggle:
    """AC5: Toggle ON -> publication_status in panel. AC6: Toggle OFF -> excluded."""

    def test_ac5_publication_status_included_when_toggle_on(self, svc):
        """AC5: enable_publication_status=True -> publication_status in panel."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_publication_status=True)
        cache = _make_cache()
        metadata = {"publication_status": "Published"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Status" in d
        assert d["Status"] == "Published"

    def test_ac6_publication_status_excluded_when_toggle_off(self, svc):
        """AC6: enable_publication_status=False -> publication_status NOT in panel."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_publication_status=False)
        cache = _make_cache()
        metadata = {"publication_status": "Published"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Status" not in d


# ---------------------------------------------------------------------------
# AC7/AC8: enable_views_seeding
# ---------------------------------------------------------------------------


class TestViewsSeedingToggle:
    """AC7: Toggle ON -> populate_views_from_front_matter runs. AC8: Toggle OFF -> no-op."""

    def test_ac7_populate_views_runs_when_toggle_on(self, svc):
        """AC7: enable_views_seeding=True -> populate_views_from_front_matter seeds views."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_views_seeding=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            md = repo_path / "article.md"
            md.write_text("---\nviews: 42\n---\n# Article\nContent.")
            cache = _make_cache()
            svc.populate_views_from_front_matter(
                "test-repo", repo_path, cache, wiki_config=wiki_config
            )
            cache.insert_initial_views.assert_called_once_with(
                "test-repo", "article", 42
            )

    def test_ac8_populate_views_is_noop_when_toggle_off(self, svc):
        """AC8: enable_views_seeding=False -> populate_views_from_front_matter returns immediately."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_views_seeding=False)
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            md = repo_path / "article.md"
            md.write_text("---\nviews: 42\n---\n# Article\nContent.")
            cache = _make_cache()
            svc.populate_views_from_front_matter(
                "test-repo", repo_path, cache, wiki_config=wiki_config
            )
            cache.insert_initial_views.assert_not_called()

    def test_ac8_views_key_excluded_from_metadata_panel_when_views_seeding_off(
        self, svc
    ):
        """AC8/AC10: enable_views_seeding=False -> 'views' front-matter key excluded from panel."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_views_seeding=False)
        cache = _make_cache()
        metadata = {"views": 500, "author": "Alice"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Views" not in d

    def test_ac7_views_key_included_in_metadata_panel_when_views_seeding_on(self, svc):
        """AC7: enable_views_seeding=True -> 'views' front-matter key included as 'Salesforce Views'."""
        from cidx_wiki.config import WikiConfig

        wiki_config = WikiConfig(enable_views_seeding=True)
        cache = _make_cache()
        metadata = {"views": 500}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Views" in d
        assert d["Salesforce Views"] == "500"


# ---------------------------------------------------------------------------
# AC9: Golden regression - default WikiConfig == wiki_config=None
# ---------------------------------------------------------------------------


class TestGoldenRegression:
    """AC9: Default WikiConfig output must be identical to wiki_config=None output."""

    def test_prepare_metadata_context_default_config_equals_none(self, svc):
        """AC9: prepare_metadata_context with default WikiConfig == with wiki_config=None."""
        from cidx_wiki.config import WikiConfig

        cache = _make_cache(view_count=5)
        metadata = _make_kb_metadata()

        result_none = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=None
        )
        result_default = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=WikiConfig()
        )
        assert result_none == result_default

    def test_render_article_default_config_equals_none(self, svc):
        """AC9: render_article with default WikiConfig == with wiki_config=None."""
        from cidx_wiki.config import WikiConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "article.md"
            f.write_text(
                "Article Number: KA-00001\nTitle: My Article\n---\n# Body\nContent."
            )
            result_none = svc.render_article(f, "test-repo", wiki_config=None)
            result_default = svc.render_article(
                f, "test-repo", wiki_config=WikiConfig()
            )
            assert result_none["html"] == result_default["html"]

    def test_populate_views_default_config_equals_none(self, svc):
        """AC9: populate_views_from_front_matter with default WikiConfig == with wiki_config=None."""
        from cidx_wiki.config import WikiConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            (repo_path / "article.md").write_text(
                "---\nviews: 10\n---\n# Art\nContent."
            )

            cache_none = _make_cache()
            cache_default = _make_cache()

            svc.populate_views_from_front_matter(
                "r", repo_path, cache_none, wiki_config=None
            )
            svc.populate_views_from_front_matter(
                "r", repo_path, cache_default, wiki_config=WikiConfig()
            )

            assert (
                cache_none.insert_initial_views.call_args_list
                == cache_default.insert_initial_views.call_args_list
            )


# ---------------------------------------------------------------------------
# AC10: Clean generic wiki - all toggles OFF
# ---------------------------------------------------------------------------


class TestCleanGenericWiki:
    """AC10: All 4 toggles OFF -> no KB-specific artifacts."""

    def _all_off(self):
        from cidx_wiki.config import WikiConfig

        return WikiConfig(
            enable_header_block_parsing=False,
            enable_article_number=False,
            enable_publication_status=False,
            enable_views_seeding=False,
        )

    def test_ac10_header_fields_render_as_markdown_when_all_off(self, svc):
        """AC10: Header fields render as raw markdown when header parsing is OFF."""
        wiki_config = self._all_off()
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "article.md"
            f.write_text(
                "Article Number: KA-00001\n"
                "Publication Status: Published\n"
                "---\n"
                "# Body\n"
                "Content."
            )
            result = svc.render_article(f, "test-repo", wiki_config=wiki_config)
            assert "Article Number" in result["html"]

    def test_ac10_no_article_number_in_panel_when_all_off(self, svc):
        """AC10: No article_number in panel when enable_article_number=False."""
        wiki_config = self._all_off()
        cache = _make_cache()
        metadata = _make_kb_metadata()
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Article" not in d

    def test_ac10_no_publication_status_in_panel_when_all_off(self, svc):
        """AC10: No publication_status in panel when enable_publication_status=False."""
        wiki_config = self._all_off()
        cache = _make_cache()
        metadata = _make_kb_metadata()
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Status" not in d

    def test_ac10_no_salesforce_views_in_panel_when_all_off(self, svc):
        """AC10: 'Salesforce Views' not in panel when enable_views_seeding=False."""
        wiki_config = self._all_off()
        cache = _make_cache()
        metadata = _make_kb_metadata()
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Views" not in d

    def test_ac10_generic_fields_still_present_when_all_off(self, svc):
        """AC10: Generic fields (author, created) still appear when all KB toggles OFF."""
        wiki_config = self._all_off()
        cache = _make_cache()
        metadata = {"created": "2024-01-01", "author": "Alice"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Created" in d
        assert "Author" in d

    def test_ac10_views_seeding_noop_when_all_off(self, svc):
        """AC10: populate_views_from_front_matter does nothing when views_seeding=False."""
        wiki_config = self._all_off()
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            (repo_path / "article.md").write_text(
                "---\nviews: 100\n---\n# Art\nContent."
            )
            cache = _make_cache()
            svc.populate_views_from_front_matter(
                "r", repo_path, cache, wiki_config=wiki_config
            )
            cache.insert_initial_views.assert_not_called()


# ---------------------------------------------------------------------------
# AC14: Backward compat - wiki_config=None
# ---------------------------------------------------------------------------


class TestBackwardCompatibility:
    """AC14: All methods work identically to current behavior when wiki_config=None."""

    def test_ac14_render_article_works_without_wiki_config(self, svc):
        """AC14: render_article() called without wiki_config parameter works as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            f = Path(tmpdir) / "article.md"
            f.write_text("# Title\nContent here.")
            result = svc.render_article(f, "test-repo")
            assert "html" in result
            assert "Title" in result["html"] or "Content here" in result["html"]

    def test_ac14_prepare_metadata_context_works_without_wiki_config(self, svc):
        """AC14: prepare_metadata_context() called without wiki_config parameter works as before."""
        cache = _make_cache(view_count=3)
        metadata = {"created": "2024-01-01", "visibility": "public"}
        result = svc.prepare_metadata_context(metadata, "repo", "path", cache)
        d = _as_dict(result)
        assert "Created" in d
        assert "Views" in d

    def test_ac14_populate_views_works_without_wiki_config(self, svc):
        """AC14: populate_views_from_front_matter() called without wiki_config works as before."""
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_path = Path(tmpdir)
            (repo_path / "article.md").write_text("---\nviews: 7\n---\n# Art\nContent.")
            cache = _make_cache()
            svc.populate_views_from_front_matter("repo", repo_path, cache)
            cache.insert_initial_views.assert_called_once_with("repo", "article", 7)


# ---------------------------------------------------------------------------
# ConfigService: _update_wiki_setting
# ---------------------------------------------------------------------------


def _wiki_section(config: Any) -> Dict[str, Any]:
    from cidx_wiki.config import default_values

    values = default_values()
    values.update((config.feature_settings or {}).get("wiki", {}))
    return values


def _set_wiki(config: Any, **updates: Any) -> None:
    """Apply *updates* to *config* as the feature config door does (the
    validated mutation it commits)."""
    from cidx_wiki.config import WIKI_CONFIG_SECTION

    from code_indexer.server.features import config as feature_config

    feature_config.runtime_update_mutator("wiki", WIKI_CONFIG_SECTION, updates)(config)


class TestWikiSettingsThroughTheFeatureDoor:
    """The keys are the cidx-wiki feature's section (feature_settings["wiki"]),
    changed only through the feature config door."""

    @pytest.mark.parametrize(
        "key",
        [
            "enable_header_block_parsing",
            "enable_article_number",
            "enable_publication_status",
            "enable_views_seeding",
        ],
    )
    def test_toggles_off(self, key):
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/srv/example")
        _set_wiki(config, **{key: False})
        assert _wiki_section(config)[key] is False

    def test_toggles_back_on(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/srv/example")
        _set_wiki(config, enable_article_number=False)
        _set_wiki(config, enable_article_number=True)
        assert _wiki_section(config)["enable_article_number"] is True

    def test_display_order_is_a_string(self):
        from code_indexer.server.utils.config_manager import ServerConfig

        config = ServerConfig(server_dir="/srv/example")
        _set_wiki(config, metadata_display_order="author,created")
        assert _wiki_section(config)["metadata_display_order"] == "author,created"

    def test_unknown_key_refused(self):
        from code_indexer.server.features.config import FeatureConfigError
        from code_indexer.server.utils.config_manager import ServerConfig

        with pytest.raises(FeatureConfigError, match="no setting"):
            _set_wiki(ServerConfig(server_dir="/srv/example"), nonexistent_key=True)

    def test_the_core_has_no_wiki_category(self):
        """Only the feature config door changes a feature's settings."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            with pytest.raises(ValueError, match="Unknown category"):
                ConfigService(tmpdir)._apply_setting(
                    ServerConfig(server_dir=tmpdir),
                    "wiki",
                    "enable_article_number",
                    "false",
                )
