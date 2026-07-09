"""
TDD regression tests for bug #1095: comma-separated exclude_path silently no-ops.

Reproduces the exact empirical failure:
  PathPatternMatcher().matches_pattern(
      'code/dms-financial-management/Foo.cs',
      'code/dms-financial-management/**,code/dms-rental-management/**'
  ) -> False  (wrong — treated as one pattern that matches nothing)

After fix:
  parse_exclude_patterns('code/dms-financial-management/**,code/dms-rental-management/**')
  -> ['code/dms-financial-management/**', 'code/dms-rental-management/**']
  and both legs apply match-ANY semantics.
"""

from code_indexer.services.path_pattern_matcher import (
    PathPatternMatcher,
    parse_exclude_patterns,
)


class TestParseExcludePatterns:
    """Unit tests for parse_exclude_patterns helper."""

    def test_splits_comma_separated_patterns(self):
        """Core case: two patterns joined with comma produce two independent patterns."""
        result = parse_exclude_patterns(
            "code/dms-financial-management/**,code/dms-rental-management/**"
        )
        assert result == [
            "code/dms-financial-management/**",
            "code/dms-rental-management/**",
        ]

    def test_trims_whitespace_around_each_pattern(self):
        """Whitespace around patterns is stripped."""
        result = parse_exclude_patterns("  *.min.js , **/vendor/**  ")
        assert result == ["*.min.js", "**/vendor/**"]

    def test_drops_empty_fragments_after_split(self):
        """Trailing/leading commas and double-commas produce no empty strings."""
        result = parse_exclude_patterns(",*/tests/*,,*/dist/*,")
        assert result == ["*/tests/*", "*/dist/*"]

    def test_single_pattern_returns_one_element_list(self):
        """Single pattern with no comma returns list with one element."""
        result = parse_exclude_patterns("**/node_modules/**")
        assert result == ["**/node_modules/**"]

    def test_none_returns_empty_list(self):
        """None exclude_path returns empty list (no exclusion)."""
        result = parse_exclude_patterns(None)
        assert result == []

    def test_empty_string_returns_empty_list(self):
        """Empty string returns empty list."""
        result = parse_exclude_patterns("")
        assert result == []

    def test_whitespace_only_returns_empty_list(self):
        """Whitespace-only string returns empty list."""
        result = parse_exclude_patterns("   ")
        assert result == []

    def test_three_patterns_comma_separated(self):
        """Three patterns are all returned."""
        result = parse_exclude_patterns("a/**,b/**,c/**")
        assert result == ["a/**", "b/**", "c/**"]


class TestBugReproduction1095:
    """Reproduce exact empirical failure from issue #1095."""

    def test_comma_joined_pattern_was_false_before_fix(self):
        """
        REGRESSION: the raw comma-joined string matched nothing.

        Before fix:
          PathPatternMatcher().matches_pattern(path, 'dir1/**,dir2/**') -> False
          (the comma is not a glob metachar; the pattern matches nothing)

        After fix: parse_exclude_patterns splits first, then matches each independently.
        The commit that fixes this converts the call site to use parse_exclude_patterns.
        This test verifies the helper produces patterns that EACH individually match.
        """
        matcher = PathPatternMatcher()
        patterns = parse_exclude_patterns(
            "code/dms-financial-management/**,code/dms-rental-management/**"
        )
        # Both patterns should be present
        assert len(patterns) == 2
        # Each pattern individually matches the right paths
        assert matcher.matches_pattern(
            "code/dms-financial-management/Poster.cs", patterns[0]
        )
        assert matcher.matches_pattern(
            "code/dms-rental-management/Rental.cs", patterns[1]
        )
        # matches_any_pattern with the split list covers both dirs
        assert matcher.matches_any_pattern(
            "code/dms-financial-management/Poster.cs", patterns
        )
        assert matcher.matches_any_pattern(
            "code/dms-rental-management/Rental.cs", patterns
        )
        # A path in neither excluded dir is NOT excluded
        assert not matcher.matches_any_pattern("code/dms-other/Other.cs", patterns)


class TestSemanticLegMultiPatternExclusion:
    """Semantic leg: _build_filter_conditions splits comma-separated exclude_path."""

    def test_single_pattern_builds_one_must_not_condition(self):
        """Single exclude_path (no comma) builds exactly one must_not condition."""
        from code_indexer.server.services.search_service import SemanticSearchService

        svc = SemanticSearchService.__new__(SemanticSearchService)
        conditions = svc._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language=None,
            exclude_path="**/tests/**",
        )
        must_not = conditions.get("must_not", [])
        assert len(must_not) == 1
        assert must_not[0]["key"] == "path"
        assert must_not[0]["match"]["text"] == "**/tests/**"

    def test_comma_separated_builds_two_must_not_conditions(self):
        """Comma-separated exclude_path builds two independent must_not conditions."""
        from code_indexer.server.services.search_service import SemanticSearchService

        svc = SemanticSearchService.__new__(SemanticSearchService)
        conditions = svc._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language=None,
            exclude_path="code/dms-financial-management/**,code/dms-rental-management/**",
        )
        must_not = conditions.get("must_not", [])
        assert len(must_not) == 2
        texts = {c["match"]["text"] for c in must_not}
        assert "code/dms-financial-management/**" in texts
        assert "code/dms-rental-management/**" in texts

    def test_whitespace_around_comma_patterns_is_trimmed(self):
        """Whitespace around patterns in exclude_path is trimmed."""
        from code_indexer.server.services.search_service import SemanticSearchService

        svc = SemanticSearchService.__new__(SemanticSearchService)
        conditions = svc._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language=None,
            exclude_path="  */tests/*  ,  */dist/*  ",
        )
        must_not = conditions.get("must_not", [])
        assert len(must_not) == 2
        texts = {c["match"]["text"] for c in must_not}
        assert "*/tests/*" in texts
        assert "*/dist/*" in texts

    def test_none_exclude_path_produces_no_must_not(self):
        """None exclude_path produces empty conditions (no must_not)."""
        from code_indexer.server.services.search_service import SemanticSearchService

        svc = SemanticSearchService.__new__(SemanticSearchService)
        conditions = svc._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language=None,
            exclude_path=None,
        )
        assert "must_not" not in conditions or conditions.get("must_not") == []

    def test_must_not_filter_excludes_path_matching_any_comma_pattern(self):
        """
        Filesystem filter evaluation: a path matching ANY of the must_not patterns
        is excluded. Reproduces the original bug end-to-end at the filter level.
        """
        from code_indexer.server.services.search_service import SemanticSearchService

        svc = SemanticSearchService.__new__(SemanticSearchService)
        conditions = svc._build_filter_conditions(
            path_filter=None,
            language=None,
            exclude_language=None,
            exclude_path="code/dms-financial-management/**,code/dms-rental-management/**",
        )
        # The filter conditions are consumed by FilesystemVectorStore's evaluate_filter
        # We verify the must_not list contains two conditions (one per pattern).
        must_not = conditions.get("must_not", [])
        assert len(must_not) == 2

        # Verify each condition uses the pattern text that DOES match individual paths
        matcher = PathPatternMatcher()
        financial_path = "code/dms-financial-management/Poster.cs"
        rental_path = "code/dms-rental-management/Rental.cs"
        other_path = "code/other-module/Other.cs"

        financial_patterns = [c["match"]["text"] for c in must_not]
        assert matcher.matches_any_pattern(financial_path, financial_patterns)
        assert matcher.matches_any_pattern(rental_path, financial_patterns)
        assert not matcher.matches_any_pattern(other_path, financial_patterns)


class TestTemporalLegMultiPatternExclusion:
    """Temporal leg: fusion dispatch must pass split patterns to query_temporal.

    Bug #1095 review finding: _query_single_provider and _query_multi_provider_fusion
    both do `[exclude_path]` (one-element list of raw comma string) instead of calling
    parse_exclude_patterns first.  This causes comma-separated exclusions to be a
    silent no-op in the temporal path, identical to the original bug in semantic/FTS.
    """

    def _make_mock_service(self, captured: list):
        """Return a TemporalSearchService-alike whose query_temporal captures args."""
        from unittest.mock import MagicMock
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        mock_svc = MagicMock()
        mock_svc.query_temporal.return_value = TemporalSearchResults(
            results=[],
            query="test",
            filter_type="none",
            filter_value=None,
        )

        # Capture the exclude_path kwarg each time query_temporal is called
        def capture(*args, **kwargs):
            captured.append(kwargs.get("exclude_path"))
            return mock_svc.query_temporal.return_value

        mock_svc.query_temporal.side_effect = capture
        return mock_svc

    def test_single_provider_passes_split_list_not_raw_string(self, monkeypatch):
        """_query_single_provider: comma-separated exclude_path yields split list."""
        from unittest.mock import MagicMock, patch
        from code_indexer.services.temporal import temporal_fusion_dispatch as tfd

        captured: list = []
        mock_svc = self._make_mock_service(captured)

        fake_config = MagicMock()
        fake_vs = MagicMock()
        fake_vs.project_root = "/tmp"

        # TemporalSearchService is imported locally inside _query_single_provider;
        # patch it at its source module so the local import gets the mock class.
        with (
            patch.object(
                tfd,
                "_create_embedding_provider_for_collection",
                return_value=MagicMock(),
            ),
            patch.object(tfd, "_make_config_manager", return_value=MagicMock()),
            patch(
                "code_indexer.services.temporal.temporal_search_service"
                ".TemporalSearchService",
                return_value=mock_svc,
            ),
        ):
            tfd._query_single_provider(
                config=fake_config,
                vector_store=fake_vs,
                coll_name="temporal_voyage_code_3",
                query_text="auth logic",
                limit=10,
                time_range=("2024-01-01", "2024-12-31"),
                file_path_filter=None,
                exclude_path="dirA/**,dirB/**",
            )

        assert len(captured) == 1, "query_temporal must be called exactly once"
        received = captured[0]
        assert received == ["dirA/**", "dirB/**"], (
            f"Expected split list ['dirA/**', 'dirB/**'], got {received!r}"
        )

    def test_single_provider_none_exclude_path_passes_none(self, monkeypatch):
        """_query_single_provider: None exclude_path passes None (no exclusion)."""
        from unittest.mock import MagicMock, patch
        from code_indexer.services.temporal import temporal_fusion_dispatch as tfd

        captured: list = []
        mock_svc = self._make_mock_service(captured)

        fake_config = MagicMock()
        fake_vs = MagicMock()
        fake_vs.project_root = "/tmp"

        with (
            patch.object(
                tfd,
                "_create_embedding_provider_for_collection",
                return_value=MagicMock(),
            ),
            patch.object(tfd, "_make_config_manager", return_value=MagicMock()),
            patch(
                "code_indexer.services.temporal.temporal_search_service"
                ".TemporalSearchService",
                return_value=mock_svc,
            ),
        ):
            tfd._query_single_provider(
                config=fake_config,
                vector_store=fake_vs,
                coll_name="temporal_voyage_code_3",
                query_text="auth logic",
                limit=10,
                time_range=("2024-01-01", "2024-12-31"),
                file_path_filter=None,
                exclude_path=None,
            )

        assert len(captured) == 1
        assert captured[0] is None, (
            f"None exclude_path should pass None to query_temporal, got {captured[0]!r}"
        )

    def test_single_provider_single_pattern_unchanged(self, monkeypatch):
        """_query_single_provider: single pattern (no comma) still works."""
        from unittest.mock import MagicMock, patch
        from code_indexer.services.temporal import temporal_fusion_dispatch as tfd

        captured: list = []
        mock_svc = self._make_mock_service(captured)

        fake_config = MagicMock()
        fake_vs = MagicMock()
        fake_vs.project_root = "/tmp"

        with (
            patch.object(
                tfd,
                "_create_embedding_provider_for_collection",
                return_value=MagicMock(),
            ),
            patch.object(tfd, "_make_config_manager", return_value=MagicMock()),
            patch(
                "code_indexer.services.temporal.temporal_search_service"
                ".TemporalSearchService",
                return_value=mock_svc,
            ),
        ):
            tfd._query_single_provider(
                config=fake_config,
                vector_store=fake_vs,
                coll_name="temporal_voyage_code_3",
                query_text="auth logic",
                limit=10,
                time_range=("2024-01-01", "2024-12-31"),
                file_path_filter=None,
                exclude_path="**/tests/**",
            )

        assert len(captured) == 1
        assert captured[0] == ["**/tests/**"], (
            f"Single pattern should pass ['**/tests/**'], got {captured[0]!r}"
        )

    def test_multi_provider_passes_split_list_not_raw_string(
        self, monkeypatch, tmp_path
    ):
        """execute_temporal_query_with_fusion: comma-separated exclude_path yields split list.

        Migrated from _query_multi_provider_fusion (deleted, Story #1171 C3).
        Uses the single-provider path (one provider group) of
        execute_temporal_query_with_fusion to verify that exclude_path is split
        before reaching query_temporal.
        """
        from unittest.mock import MagicMock, patch
        from code_indexer.services.temporal import temporal_fusion_dispatch as tfd
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        captured: list = []

        fake_config = MagicMock()
        fake_vs = MagicMock()
        fake_vs.project_root = "/tmp"

        def fake_query_temporal(*args, **kwargs):
            captured.append(kwargs.get("exclude_path"))
            return TemporalSearchResults(
                results=[],
                query="test",
                filter_type="none",
                filter_value=None,
            )

        mock_svc = MagicMock()
        mock_svc.query_temporal.side_effect = fake_query_temporal

        # One provider group to keep the single-provider path (simpler to reason about)
        one_provider = [("temporal_voyage_code_3", ["temporal_voyage_code_3"])]

        with (
            patch.object(
                tfd,
                "_create_embedding_provider_for_collection",
                return_value=MagicMock(),
            ),
            patch.object(tfd, "_make_config_manager", return_value=MagicMock()),
            patch.object(
                tfd,
                "_discover_provider_shards_with_pruning",
                return_value=one_provider,
            ),
            patch.object(
                tfd,
                "filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration"
                ".migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service"
                ".TemporalSearchService",
                return_value=mock_svc,
            ),
        ):
            tfd.execute_temporal_query_with_fusion(
                config=fake_config,
                index_path=tmp_path,
                vector_store=fake_vs,
                query_text="auth logic",
                limit=10,
                time_range=("2024-01-01", "2024-12-31"),
                exclude_path="dirA/**,dirB/**",
            )

        assert len(captured) >= 1, "query_temporal must be called at least once"
        for ep in captured:
            assert ep == ["dirA/**", "dirB/**"], (
                f"Expected split list ['dirA/**', 'dirB/**'], got {ep!r}"
            )

    def test_build_exclusion_filter_receives_split_patterns(self):
        """
        Integration: temporal_search_service.query_temporal with a list of two
        patterns produces two must_not conditions (not one useless raw-string condition).

        This tests the full data flow at the temporal_search_service level —
        the service receives a proper split list and build_exclusion_filter produces
        one must_not entry per pattern (match-ANY semantics).
        """
        from code_indexer.services.path_pattern_matcher import parse_exclude_patterns
        from code_indexer.services.path_filter_builder import PathFilterBuilder

        # Simulate what the fixed dispatch sites will pass:
        # parse_exclude_patterns("dirA/**,dirB/**") -> ["dirA/**", "dirB/**"]
        split_patterns = parse_exclude_patterns("dirA/**,dirB/**")
        assert split_patterns == ["dirA/**", "dirB/**"]

        # PathFilterBuilder.build_exclusion_filter with split list produces 2 conditions
        builder = PathFilterBuilder()
        result = builder.build_exclusion_filter(split_patterns)
        must_not = result.get("must_not", [])
        assert len(must_not) == 2, (
            f"Expected 2 must_not conditions from split list, got {len(must_not)}: {must_not}"
        )
        texts = {c["match"]["text"] for c in must_not}
        assert "dirA/**" in texts
        assert "dirB/**" in texts

        # By contrast, the BUGGY path: build_exclusion_filter with raw string in a list
        buggy_patterns = ["dirA/**,dirB/**"]
        buggy_result = builder.build_exclusion_filter(buggy_patterns)
        buggy_must_not = buggy_result.get("must_not", [])
        # The raw-string pattern produces 1 condition with the comma-joined text
        # (which matches nothing), proving the bug was real:
        assert len(buggy_must_not) == 1, (
            "Buggy path should produce exactly 1 (useless) must_not condition"
        )
        assert buggy_must_not[0]["match"]["text"] == "dirA/**,dirB/**"


class TestFTSLegMultiPatternExclusion:
    """FTS leg: semantic_query_manager passes split list to tantivy exclude_paths."""

    def test_fts_exclude_path_list_excludes_matching_paths(self):
        """
        The FTS leg in _execute_fts_search must pass parse_exclude_patterns result
        as the exclude_paths list. We verify this by checking that the split patterns
        individually match paths that should be excluded.

        We test the helper integration directly (tantivy manager does path-per-pattern
        matching already via matches_any_pattern logic in tantivy_index_manager.py).
        """
        patterns = parse_exclude_patterns(
            "code/dms-financial-management/**,code/dms-rental-management/**"
        )
        assert len(patterns) == 2

        matcher = PathPatternMatcher()
        # FTS leg uses matches_any_pattern over the exclude_paths list
        financial = "code/dms-financial-management/SomeFile.java"
        rental = "code/dms-rental-management/OtherFile.java"
        unaffected = "code/dms-core/Core.java"

        assert matcher.matches_any_pattern(financial, patterns)
        assert matcher.matches_any_pattern(rental, patterns)
        assert not matcher.matches_any_pattern(unaffected, patterns)

    def test_single_pattern_fts_still_works(self):
        """Single exclude_path (no comma) still works — backward compatible."""
        patterns = parse_exclude_patterns("**/node_modules/**")
        assert len(patterns) == 1
        matcher = PathPatternMatcher()
        assert matcher.matches_any_pattern(
            "proj/node_modules/lodash/index.js", patterns
        )
        assert not matcher.matches_any_pattern("proj/src/main.js", patterns)
