"""
Unit tests for the shared path-normalization / depth-restriction helper
functions added to FileListingService's module (Bug #1886, round 2).

These are pure functions extracted so browse_directory, list_files (MCP)
and GET /api/repositories/{repo_id}/files (REST) can all agree on:
  - normalize_listing_path(path): strip leading/trailing "/", collapse a
    leading "./" repeat-safely, and map "." to the repository root ("").
  - literal_dir_prefix_of_pattern(pattern): the glob-free directory prefix
    of a path_pattern, or None when the directory portion itself carries a
    glob char (the pattern already expresses its own depth).
  - compute_direct_children_of(normalized_path, recursive, final_path_pattern,
    pattern_overrides_path): the FileListQueryParams.direct_children_of
    value to apply. Bug #1886 (R4): the base is `normalized_path` by
    default; only when the caller explicitly passes
    pattern_overrides_path=True (a genuine caller-supplied absolute
    pattern that overrode `path`, e.g. browse_directory's R1 case) is the
    base derived from final_path_pattern's own literal directory prefix
    instead -- see test_file_listing_paths_escape_1886_r4.py for the full
    rationale (a pattern built by escaping+prefixing normalized_path must
    never be re-parsed for a depth base).

Moved to code_indexer.server.services.file_listing_paths (Bug #1886, R4)
from file_service.py.

No mocks -- these are pure string functions tested directly.
"""

import pytest

from code_indexer.server.services.file_listing_paths import (
    normalize_listing_path,
    literal_dir_prefix_of_pattern,
    compute_direct_children_of,
)


class TestNormalizeListingPath:
    def test_empty_and_none_map_to_root(self):
        assert normalize_listing_path("") == ""
        assert normalize_listing_path(None) == ""

    def test_plain_path_unchanged(self):
        assert normalize_listing_path("src") == "src"
        assert normalize_listing_path("code/src") == "code/src"

    def test_trailing_slash_stripped(self):
        assert normalize_listing_path("src/") == "src"

    def test_leading_slash_stripped(self):
        assert normalize_listing_path("/src") == "src"

    def test_leading_and_trailing_slash_stripped(self):
        assert normalize_listing_path("/src/") == "src"

    def test_leading_dot_slash_stripped(self):
        assert normalize_listing_path("./src") == "src"

    def test_leading_dot_slash_repeat_safe(self):
        # Repeat-safe: multiple "./" prefixes, and a trailing slash too.
        assert normalize_listing_path("././src/") == "src"
        assert normalize_listing_path("./src/") == "src"

    def test_bare_dot_maps_to_root(self):
        assert normalize_listing_path(".") == ""
        assert normalize_listing_path("./") == ""


class TestLiteralDirPrefixOfPattern:
    def test_simple_suffix_glob_yields_directory_prefix(self):
        assert literal_dir_prefix_of_pattern("code/src/*.java") == "code/src"

    def test_single_directory_prefix(self):
        assert literal_dir_prefix_of_pattern("src/*.py") == "src"

    def test_trailing_bare_star_yields_directory_prefix(self):
        # The bug-report's exact leaky shape: still has a clean dir prefix.
        assert literal_dir_prefix_of_pattern("code/src/*") == "code/src"

    def test_no_slash_pattern_yields_empty_prefix(self):
        assert literal_dir_prefix_of_pattern("*.java") == ""

    def test_leading_double_star_yields_none(self):
        # "**/*.py": the directory portion ("**") itself is a glob -- the
        # pattern already expresses its own depth, so no restriction.
        assert literal_dir_prefix_of_pattern("**/*.py") is None

    def test_glob_in_middle_directory_segment_yields_none(self):
        # "src/*/x.py": the directory portion ("src", "*") contains a glob
        # segment -- applying "src" alone would wrongly exclude src/foo/x.py.
        assert literal_dir_prefix_of_pattern("src/*/x.py") is None

    def test_multi_level_literal_prefix(self):
        assert literal_dir_prefix_of_pattern("a/b/c/*.txt") == "a/b/c"


class TestLiteralDirPrefixOfPatternLeadingSlash:
    """Bug #1886 (R3, item 1): an absolute-LOOKING pattern that itself
    starts with "/" (or "./") must have that prefix stripped before the
    literal directory prefix is derived -- otherwise the derived base
    ("/src") never equals a real file's dirname ("src", no leading slash,
    per FileInfo.path's Path.relative_to() construction), silently zeroing
    out every result."""

    @pytest.mark.parametrize(
        "pattern,expected",
        [
            ("/src/*.py", "src"),
            ("./src/*.py", "src"),
            ("/code/src/*.java", "code/src"),
            ("/code/src/*", "code/src"),
            ("/*.java", ""),
            ("/**/*.py", None),
        ],
    )
    def test_leading_slash_or_dot_slash_stripped(self, pattern, expected):
        assert literal_dir_prefix_of_pattern(pattern) == expected


class TestComputeDirectChildrenOf:
    def test_recursive_true_always_none(self):
        assert compute_direct_children_of("src", True, "src/**/*") is None
        assert compute_direct_children_of("", True, None) is None

    def test_non_recursive_no_pattern_uses_normalized_path(self):
        assert compute_direct_children_of("src", False, None) == "src"
        assert compute_direct_children_of("", False, None) == ""

    def test_non_recursive_with_pattern_derives_from_pattern(self):
        # R1: pattern overrides path (browse_directory's absolute-pattern
        # case) -- the base must come from the pattern, not normalized_path.
        # R4: this derivation now requires the caller to explicitly say the
        # pattern overrode path (pattern_overrides_path=True) -- it is no
        # longer implied merely by final_path_pattern being truthy.
        assert (
            compute_direct_children_of(
                "wrong/path", False, "code/src/*.java", pattern_overrides_path=True
            )
            == "code/src"
        )

    def test_non_recursive_pattern_with_ungoverned_depth_yields_none(self):
        assert (
            compute_direct_children_of(
                "src", False, "**/*.py", pattern_overrides_path=True
            )
            is None
        )
