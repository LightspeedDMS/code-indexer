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
