"""Tests for Story #325: Configurable Metadata Panel Display Order.

TDD red phase: all tests written BEFORE implementation.

Covers:
 AC1:  Default order (empty string) preserves current behavior
 AC2:  Custom order reorders fields correctly
 AC3:  Unlisted fields appended alphabetically after listed ones
 AC4:  Disabled article_number excluded regardless of order string
 AC5:  Disabled publication_status excluded regardless of order string
 AC6:  Empty order string preserves current iteration order
 AC7:  Duplicate fields in order string: each field appears once (first occurrence)
 AC8:  Nonexistent fields in order string: silently ignored
 AC9:  Config round-trip: metadata_display_order survives JSON serialize/deserialize
 AC10: Config Web UI text input exists and saves to config.json
 AC11: wiki_config=None backward compatibility
 AC12: Golden regression: default config (empty order) matches wiki_config=None
 AC13: Interaction with real_views from database
"""

import json
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.server.wiki.wiki_service import WikiService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def svc():
    return WikiService()


def _make_cache(view_count: int = 0) -> MagicMock:
    cache = MagicMock()
    cache.get_view_count.return_value = view_count
    return cache


def _labels(pairs):
    """Extract just the label (first element) from each (label, value) tuple."""
    return [label for label, _ in pairs]


def _as_dict(pairs):
    return dict(pairs)


def _make_wiki_config(**kwargs):
    from code_indexer.server.utils.config_manager import WikiConfig
    return WikiConfig(**kwargs)


# ---------------------------------------------------------------------------
# AC1 & AC6: Default order (empty string) preserves current behavior
# ---------------------------------------------------------------------------


class TestDefaultOrder:
    """AC1/AC6: metadata_display_order='' preserves article_number first, then
    remaining in dict iteration order -- identical to wiki_config=None."""

    def test_ac1_empty_order_preserves_article_number_first(self, svc):
        """AC1: Empty metadata_display_order -> article_number is first item."""
        wiki_config = _make_wiki_config(metadata_display_order="")
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        assert len(result) > 0
        assert result[0][0] == "Salesforce Article"

    def test_ac6_empty_order_string_same_as_none(self, svc):
        """AC6: Empty order string produces same output as wiki_config=None."""
        cache = _make_cache(view_count=5)
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
            "author": "Alice",
            "visibility": "public",
        }
        result_none = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=None
        )
        wiki_config = _make_wiki_config(metadata_display_order="")
        result_empty = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=wiki_config
        )
        assert result_none == result_empty

    def test_ac1_whitespace_only_order_same_as_empty(self, svc):
        """AC1: Whitespace-only order string treated as empty (no custom ordering)."""
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
        }
        wiki_config_ws = _make_wiki_config(metadata_display_order="   ")
        wiki_config_empty = _make_wiki_config(metadata_display_order="")
        result_ws = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=wiki_config_ws
        )
        result_empty = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=wiki_config_empty
        )
        assert result_ws == result_empty


# ---------------------------------------------------------------------------
# AC2: Custom order reorders fields
# ---------------------------------------------------------------------------


class TestCustomOrder:
    """AC2: Non-empty metadata_display_order reorders fields accordingly."""

    def test_ac2_custom_order_reorders_fields(self, svc):
        """AC2: metadata_display_order='author,modified,created,visibility' -> that order."""
        wiki_config = _make_wiki_config(
            metadata_display_order="author,modified,created,visibility"
        )
        cache = _make_cache()
        metadata = {
            "author": "Alice",
            "modified": "2024-06-01",
            "created": "2024-01-01",
            "visibility": "public",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        # Find the positions of each label
        author_pos = labels.index("Author")
        modified_pos = labels.index("Modified")
        created_pos = labels.index("Created")
        visibility_pos = labels.index("Visibility")
        # Author must come before Modified, which must come before Created, etc.
        assert author_pos < modified_pos
        assert modified_pos < created_pos
        assert created_pos < visibility_pos

    def test_ac2_order_first_four_match_configured_sequence(self, svc):
        """AC2: With only the listed fields, result must match configured sequence exactly."""
        wiki_config = _make_wiki_config(
            metadata_display_order="author,modified,created,visibility"
        )
        cache = _make_cache()
        metadata = {
            "author": "Alice",
            "modified": "2024-06-01",
            "created": "2024-01-01",
            "visibility": "public",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        assert labels == ["Author", "Modified", "Created", "Visibility"]

    def test_ac2_article_number_unlisted_appended_after_custom_order(self, svc):
        """AC2: article_number not in order string -> appended alphabetically after listed."""
        wiki_config = _make_wiki_config(
            metadata_display_order="author,modified,created,visibility"
        )
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "author": "Alice",
            "modified": "2024-06-01",
            "created": "2024-01-01",
            "visibility": "public",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        # article_number must appear after all 4 listed fields
        art_pos = labels.index("Salesforce Article")
        assert art_pos > labels.index("Visibility")


# ---------------------------------------------------------------------------
# AC3: Unlisted fields appended alphabetically
# ---------------------------------------------------------------------------


class TestUnlistedFieldsAlphabetical:
    """AC3: Fields not in order string appear after listed ones, sorted alphabetically by key."""

    def test_ac3_unlisted_fields_appended_alphabetically(self, svc):
        """AC3: created,modified listed -> article_number,author,visibility appended alpha."""
        wiki_config = _make_wiki_config(
            metadata_display_order="created,modified"
        )
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "author": "Alice",
            "created": "2024-01-01",
            "modified": "2024-06-01",
            "visibility": "public",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        # Listed fields come first
        assert labels[0] == "Created"
        assert labels[1] == "Modified"
        # Remaining: article_number (key), author (key), visibility (key)
        # alphabetical by KEY: article_number < author < visibility
        remaining = labels[2:]
        assert "Salesforce Article" in remaining
        assert "Author" in remaining
        assert "Visibility" in remaining
        art_pos = remaining.index("Salesforce Article")
        author_pos = remaining.index("Author")
        vis_pos = remaining.index("Visibility")
        assert art_pos < author_pos
        assert author_pos < vis_pos

    def test_ac3_no_unlisted_fields_when_all_listed(self, svc):
        """AC3: When all fields are listed, nothing is appended."""
        wiki_config = _make_wiki_config(
            metadata_display_order="author,created"
        )
        cache = _make_cache()
        metadata = {
            "author": "Alice",
            "created": "2024-01-01",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        assert labels == ["Author", "Created"]


# ---------------------------------------------------------------------------
# AC4: Disabled article_number excluded regardless of order
# ---------------------------------------------------------------------------


class TestDisabledArticleNumberIgnoredInOrder:
    """AC4: enable_article_number=False excludes article_number even if in order string."""

    def test_ac4_article_number_excluded_when_disabled_even_if_in_order(self, svc):
        """AC4: enable_article_number=False AND order includes 'article_number' -> excluded."""
        wiki_config = _make_wiki_config(
            enable_article_number=False,
            metadata_display_order="article_number,created,author",
        )
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Salesforce Article" not in d

    def test_ac4_other_listed_fields_still_appear_when_article_number_disabled(self, svc):
        """AC4: Other fields in order string still appear even when article_number disabled."""
        wiki_config = _make_wiki_config(
            enable_article_number=False,
            metadata_display_order="article_number,created,author",
        )
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Created" in d
        assert "Author" in d


# ---------------------------------------------------------------------------
# AC5: Disabled publication_status excluded regardless of order
# ---------------------------------------------------------------------------


class TestDisabledPublicationStatusIgnoredInOrder:
    """AC5: enable_publication_status=False excludes publication_status even if in order."""

    def test_ac5_publication_status_excluded_when_disabled_even_if_in_order(self, svc):
        """AC5: enable_publication_status=False AND order includes 'publication_status' -> excluded."""
        wiki_config = _make_wiki_config(
            enable_publication_status=False,
            metadata_display_order="publication_status,created,author",
        )
        cache = _make_cache()
        metadata = {
            "publication_status": "Published",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Status" not in d

    def test_ac5_other_fields_still_appear_when_publication_status_disabled(self, svc):
        """AC5: Other fields still appear even when publication_status disabled."""
        wiki_config = _make_wiki_config(
            enable_publication_status=False,
            metadata_display_order="publication_status,created,author",
        )
        cache = _make_cache()
        metadata = {
            "publication_status": "Published",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Created" in d
        assert "Author" in d


# ---------------------------------------------------------------------------
# AC7: Duplicate fields in order string
# ---------------------------------------------------------------------------


class TestDuplicateFieldsInOrder:
    """AC7: Duplicate fields in order string -> each field appears once at first occurrence."""

    def test_ac7_duplicate_fields_appear_once(self, svc):
        """AC7: 'created,author,created,modified' -> created, author, modified (no dupes)."""
        wiki_config = _make_wiki_config(
            metadata_display_order="created,author,created,modified"
        )
        cache = _make_cache()
        metadata = {
            "created": "2024-01-01",
            "author": "Alice",
            "modified": "2024-06-01",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        assert labels.count("Created") == 1
        assert labels.count("Author") == 1
        assert labels.count("Modified") == 1

    def test_ac7_duplicate_fields_use_first_occurrence_position(self, svc):
        """AC7: First occurrence position wins for duplicates."""
        wiki_config = _make_wiki_config(
            metadata_display_order="created,author,created,modified"
        )
        cache = _make_cache()
        metadata = {
            "created": "2024-01-01",
            "author": "Alice",
            "modified": "2024-06-01",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        # created (pos 0), author (pos 1), modified (pos 2)
        assert labels == ["Created", "Author", "Modified"]


# ---------------------------------------------------------------------------
# AC8: Nonexistent fields in order string silently ignored
# ---------------------------------------------------------------------------


class TestNonexistentFieldsInOrder:
    """AC8: Fields in order string that don't exist in metadata are silently ignored."""

    def test_ac8_nonexistent_fields_ignored(self, svc):
        """AC8: 'nonexistent_field,created,also_fake,modified' -> created, modified only."""
        wiki_config = _make_wiki_config(
            metadata_display_order="nonexistent_field,created,also_fake,modified"
        )
        cache = _make_cache()
        metadata = {
            "created": "2024-01-01",
            "modified": "2024-06-01",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        assert "Created" in labels
        assert "Modified" in labels
        # No phantom entries from nonexistent fields
        assert len(labels) == 2

    def test_ac8_nonexistent_fields_do_not_raise(self, svc):
        """AC8: Nonexistent fields in order string cause no exception."""
        wiki_config = _make_wiki_config(
            metadata_display_order="fake_key,created"
        )
        cache = _make_cache()
        metadata = {"created": "2024-01-01"}
        # Must not raise
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        assert result is not None


# ---------------------------------------------------------------------------
# AC9: Config round-trip persistence
# ---------------------------------------------------------------------------


class TestConfigRoundTrip:
    """AC9: metadata_display_order survives JSON serialize/deserialize exactly."""

    def test_ac9_metadata_display_order_round_trips_json(self):
        """AC9: WikiConfig with metadata_display_order survives JSON round-trip."""
        from dataclasses import asdict
        from code_indexer.server.utils.config_manager import WikiConfig

        original = WikiConfig(
            metadata_display_order="author,visibility,created"
        )
        serialized = json.dumps(asdict(original))
        restored = WikiConfig(**json.loads(serialized))
        assert restored.metadata_display_order == "author,visibility,created"

    def test_ac9_empty_order_round_trips(self):
        """AC9: Empty metadata_display_order round-trips as empty string."""
        from dataclasses import asdict
        from code_indexer.server.utils.config_manager import WikiConfig

        original = WikiConfig(metadata_display_order="")
        serialized = json.dumps(asdict(original))
        restored = WikiConfig(**json.loads(serialized))
        assert restored.metadata_display_order == ""

    def test_ac9_wiki_config_dataclass_has_metadata_display_order_field(self):
        """AC9: WikiConfig dataclass must have metadata_display_order field."""
        from code_indexer.server.utils.config_manager import WikiConfig

        cfg = WikiConfig()
        assert hasattr(cfg, "metadata_display_order")
        assert cfg.metadata_display_order == ""

    def test_ac9_load_config_preserves_metadata_display_order(self):
        """AC9: load_config() restores metadata_display_order from JSON file."""
        from code_indexer.server.utils.config_manager import (
            ServerConfigManager,
            WikiConfig,
        )

        with tempfile.TemporaryDirectory() as tmpdir:
            config_path = Path(tmpdir) / "config.json"
            raw_config = {
                "server_dir": tmpdir,
                "wiki_config": {
                    "enable_header_block_parsing": True,
                    "enable_article_number": True,
                    "enable_publication_status": True,
                    "enable_views_seeding": True,
                    "metadata_display_order": "author,visibility,created",
                },
            }
            config_path.write_text(json.dumps(raw_config))
            manager = ServerConfigManager(tmpdir)
            loaded = manager.load_config()
            assert isinstance(loaded.wiki_config, WikiConfig)
            assert loaded.wiki_config.metadata_display_order == "author,visibility,created"


# ---------------------------------------------------------------------------
# AC11: wiki_config=None backward compatibility
# ---------------------------------------------------------------------------


class TestBackwardCompatibilityNone:
    """AC11: wiki_config=None behaves identically to current codebase field ordering."""

    def test_ac11_none_wiki_config_article_number_first(self, svc):
        """AC11: wiki_config=None -> article_number is first (current behavior preserved)."""
        cache = _make_cache()
        metadata = {
            "article_number": "KA-00001",
            "created": "2024-01-01",
            "author": "Alice",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=None
        )
        assert len(result) > 0
        assert result[0][0] == "Salesforce Article"

    def test_ac11_none_wiki_config_no_ordering_applied(self, svc):
        """AC11: wiki_config=None -> no ordering logic applied, dict iteration order."""
        cache = _make_cache()
        metadata = {
            "author": "Alice",
            "created": "2024-01-01",
        }
        result_none = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=None
        )
        # Should work without error and return the fields
        d = _as_dict(result_none)
        assert "Author" in d
        assert "Created" in d


# ---------------------------------------------------------------------------
# AC12: Golden regression - default config matches wiki_config=None
# ---------------------------------------------------------------------------


class TestGoldenRegression:
    """AC12: Default WikiConfig (empty order) produces identical output to wiki_config=None."""

    def test_ac12_default_wiki_config_equals_none(self, svc):
        """AC12: Default WikiConfig() output identical to wiki_config=None."""
        from code_indexer.server.utils.config_manager import WikiConfig

        cache = _make_cache(view_count=5)
        metadata = {
            "article_number": "KA-00001",
            "publication_status": "Published",
            "views": 500,
            "created": "2024-01-01",
            "author": "Alice",
        }
        result_none = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=None
        )
        result_default = svc.prepare_metadata_context(
            metadata.copy(), "repo", "path", cache, wiki_config=WikiConfig()
        )
        assert result_none == result_default

    def test_ac12_default_order_is_empty_string(self):
        """AC12: Default WikiConfig has metadata_display_order='' (empty)."""
        from code_indexer.server.utils.config_manager import WikiConfig

        cfg = WikiConfig()
        assert cfg.metadata_display_order == ""


# ---------------------------------------------------------------------------
# AC13: Interaction with real_views from database
# ---------------------------------------------------------------------------


class TestRealViewsOrdering:
    """AC13: metadata_display_order works correctly with real_views injected from DB."""

    def test_ac13_real_views_first_when_listed_first(self, svc):
        """AC13: 'real_views,created,modified' -> real_views (Views) appears first."""
        wiki_config = _make_wiki_config(
            metadata_display_order="real_views,created,modified"
        )
        cache = _make_cache(view_count=42)
        metadata = {
            "created": "2024-01-01",
            "modified": "2024-06-01",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        assert labels[0] == "Views"
        assert "Created" in labels
        assert "Modified" in labels

    def test_ac13_real_views_in_correct_position_with_other_fields(self, svc):
        """AC13: real_views placed at configured position relative to other fields."""
        wiki_config = _make_wiki_config(
            metadata_display_order="created,real_views,modified"
        )
        cache = _make_cache(view_count=10)
        metadata = {
            "created": "2024-01-01",
            "modified": "2024-06-01",
        }
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        labels = _labels(result)
        created_pos = labels.index("Created")
        views_pos = labels.index("Views")
        modified_pos = labels.index("Modified")
        assert created_pos < views_pos < modified_pos

    def test_ac13_real_views_zero_not_injected(self, svc):
        """AC13: real_views=0 -> 'Views' not in result (unchanged from current behavior)."""
        wiki_config = _make_wiki_config(
            metadata_display_order="real_views,created"
        )
        cache = _make_cache(view_count=0)
        metadata = {"created": "2024-01-01"}
        result = svc.prepare_metadata_context(
            metadata, "repo", "path", cache, wiki_config=wiki_config
        )
        d = _as_dict(result)
        assert "Views" not in d
        assert "Created" in d


# ---------------------------------------------------------------------------
# ConfigService: _update_wiki_setting for metadata_display_order
# ---------------------------------------------------------------------------


class TestConfigServiceDisplayOrderSetting:
    """AC10: ConfigService can save metadata_display_order as string setting."""

    def test_config_service_saves_metadata_display_order(self):
        """AC10: _update_wiki_setting handles 'metadata_display_order' key as string."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            svc = ConfigService(tmpdir)
            config = ServerConfig(server_dir=tmpdir)
            svc._update_wiki_setting(
                config, "metadata_display_order", "author,visibility,created"
            )
            assert config.wiki_config.metadata_display_order == "author,visibility,created"

    def test_config_service_saves_empty_metadata_display_order(self):
        """AC10: _update_wiki_setting handles empty string for metadata_display_order."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            svc = ConfigService(tmpdir)
            config = ServerConfig(server_dir=tmpdir)
            svc._update_wiki_setting(config, "metadata_display_order", "")
            assert config.wiki_config.metadata_display_order == ""

    def test_config_service_metadata_display_order_unknown_key_still_raises_for_others(self):
        """AC10: Other unknown keys still raise ValueError."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfig

        with tempfile.TemporaryDirectory() as tmpdir:
            svc = ConfigService(tmpdir)
            config = ServerConfig(server_dir=tmpdir)
            with pytest.raises(ValueError, match="Unknown wiki setting"):
                svc._update_wiki_setting(config, "totally_unknown_key", "value")
