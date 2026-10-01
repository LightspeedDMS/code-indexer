"""
Unit tests for the new `file_listing_paths` module (Bug #1886, round 4):
- escape_gitwildmatch_literal(segment): escapes gitwildmatch metacharacters
  (`\\`, `[`, `]`, `!`, `*`, `?`, `#`) in a LITERAL repo-relative path
  segment so it is never mis-parsed as a glob when spliced into a
  gitignore-style pattern string.
- is_absolute_path_pattern(user_path_pattern): the shared "does this
  caller-supplied pattern carry its own directory component" predicate
  used by both build_non_composite_path_pattern and
  mcp/handlers/files.py's _build_browse_path_pattern.
- build_non_composite_path_pattern(path, user_path_pattern): the REST
  GET /api/repositories/{repo_id}/files non-composite pattern builder,
  moved here from routers/inline_repos_v2.py and now escaping the
  literal `path` component.
- compute_direct_children_of(..., pattern_overrides_path): the depth-base
  now must NOT be re-derived from the (possibly escaped) final pattern
  when that pattern was built by prefixing the normalized path -- only
  when an absolute user pattern genuinely overrides `path`.

No mocks -- these are pure string functions tested directly.
"""

import pathspec

from code_indexer.server.services.file_listing_paths import (
    escape_gitwildmatch_literal,
    is_absolute_path_pattern,
    build_non_composite_path_pattern,
    compute_direct_children_of,
)


class TestEscapeGitwildmatchLiteral:
    def test_plain_segment_unchanged(self):
        assert escape_gitwildmatch_literal("src") == "src"
        assert escape_gitwildmatch_literal("code/src") == "code/src"

    def test_empty_segment_unchanged(self):
        assert escape_gitwildmatch_literal("") == ""

    def test_brackets_escaped(self):
        assert escape_gitwildmatch_literal("[slug]") == "\\[slug\\]"

    def test_star_escaped(self):
        assert escape_gitwildmatch_literal("a*b") == "a\\*b"

    def test_question_mark_escaped(self):
        assert escape_gitwildmatch_literal("a?b") == "a\\?b"

    def test_bang_escaped(self):
        assert escape_gitwildmatch_literal("!important") == "\\!important"

    def test_hash_escaped(self):
        assert escape_gitwildmatch_literal("#comment") == "\\#comment"

    def test_backslash_escaped(self):
        assert escape_gitwildmatch_literal("weird\\name") == "weird\\\\name"

    def test_slash_never_escaped(self):
        # "/" is the directory separator, never a gitwildmatch metachar --
        # multi-segment literal paths must round-trip unchanged apart from
        # their metacharacter segments.
        assert escape_gitwildmatch_literal("app/[slug]") == "app/\\[slug\\]"

    def test_escaped_bracket_matches_literal_directory_only(self):
        """The whole point: an escaped bracket directory name matches ONLY
        that literal directory, never acting as a character-class glob."""
        escaped = escape_gitwildmatch_literal("app/[slug]")
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [f"{escaped}/**/*"])
        assert spec.match_file("app/[slug]/page.tsx") is True
        assert spec.match_file("app/[slug]/sub/x.tsx") is True
        # A sibling directory that would accidentally satisfy the
        # character-class glob [slug] (any single char s/l/u/g) must NOT
        # match once escaped.
        assert spec.match_file("app/s/other.tsx") is False

    def test_escaped_leading_bang_does_not_negate(self):
        escaped = escape_gitwildmatch_literal("!important")
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [f"{escaped}/**/*"])
        assert spec.match_file("!important/x.py") is True


class TestIsAbsolutePathPattern:
    def test_none_is_not_absolute(self):
        assert is_absolute_path_pattern(None) is False

    def test_empty_is_not_absolute(self):
        assert is_absolute_path_pattern("") is False

    def test_bare_glob_is_not_absolute(self):
        assert is_absolute_path_pattern("*.py") is False

    def test_pattern_with_slash_is_absolute(self):
        assert is_absolute_path_pattern("code/src/*.java") is True

    def test_pattern_starting_with_double_star_is_absolute(self):
        assert is_absolute_path_pattern("**/*.py") is True


class TestBuildNonCompositePathPatternBackwardCompat:
    """Byte-identical to the pre-move `_build_non_composite_path_pattern`
    for every scenario that doesn't involve glob metacharacters in `path`."""

    def test_no_path_passes_pattern_through(self):
        assert build_non_composite_path_pattern(None, "*.py") == "*.py"
        assert build_non_composite_path_pattern("", None) is None

    def test_path_only_builds_subtree_glob(self):
        assert build_non_composite_path_pattern("src", None) == "src/**/*"

    def test_path_with_relative_pattern_combines(self):
        assert build_non_composite_path_pattern("src", "*.py") == "src/**/*.py"

    def test_path_with_absolute_pattern_overrides(self):
        assert (
            build_non_composite_path_pattern("wrong", "code/src/*.java")
            == "code/src/*.java"
        )


class TestBuildNonCompositePathPatternEscaping:
    """Bug #1886 (R4): glob metacharacters in the literal `path` component
    must be escaped so they never act as an unintended glob."""

    def test_bracket_directory_path_escaped_in_subtree_glob(self):
        pattern = build_non_composite_path_pattern("app/[slug]", None)
        assert pattern == "app/\\[slug\\]/**/*"

    def test_bracket_directory_path_matches_only_its_own_subtree(self):
        pattern = build_non_composite_path_pattern("app/[slug]", None)
        assert pattern is not None
        spec = pathspec.PathSpec.from_lines("gitwildmatch", [pattern])
        assert spec.match_file("app/[slug]/page.tsx") is True
        assert spec.match_file("app/[slug]/sub/x.tsx") is True
        assert spec.match_file("app/s/other.tsx") is False

    def test_bracket_directory_path_with_relative_pattern_escaped(self):
        pattern = build_non_composite_path_pattern("app/[slug]", "*.tsx")
        assert pattern == "app/\\[slug\\]/**/*.tsx"

    def test_absolute_user_pattern_not_escaped(self):
        # The user's own absolute pattern is never auto-escaped -- only the
        # literal `path` component is.
        pattern = build_non_composite_path_pattern("app/[slug]", "code/[x]/*.py")
        assert pattern == "code/[x]/*.py"


class TestComputeDirectChildrenOfPatternOverridesPath:
    def test_recursive_true_always_none(self):
        assert compute_direct_children_of("src", True, "src/**/*", True) is None
        assert compute_direct_children_of("", True, None, False) is None

    def test_default_does_not_override(self):
        # pattern_overrides_path defaults to False.
        assert compute_direct_children_of("src", False, "src/**/*") == "src"

    def test_non_recursive_pattern_not_overriding_uses_normalized_path(self):
        # Bug #1886 (R4): when the pattern was built BY PREFIXING the
        # normalized path (pattern_overrides_path=False), the depth base
        # must be the (unescaped) normalized_path itself -- NOT re-derived
        # from final_path_pattern, which may now carry escape backslashes
        # that never equal a real file's dirname.
        assert (
            compute_direct_children_of("app/[slug]", False, "app/\\[slug\\]/*", False)
            == "app/[slug]"
        )

    def test_non_recursive_pattern_overriding_derives_from_pattern(self):
        assert (
            compute_direct_children_of("wrong/path", False, "code/src/*.java", True)
            == "code/src"
        )

    def test_non_recursive_overriding_pattern_with_ungoverned_depth_yields_none(self):
        assert compute_direct_children_of("src", False, "**/*.py", True) is None

    def test_non_recursive_no_pattern_uses_normalized_path(self):
        assert compute_direct_children_of("src", False, None) == "src"
        assert compute_direct_children_of("", False, None) == ""
