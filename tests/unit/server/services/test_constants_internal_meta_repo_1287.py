"""
Unit tests for the shared `is_internal_meta_repo` predicate (Bug #1287 Defect B,
code-reviewer finding 1).

The predicate is the SINGLE source of truth for identifying the internal,
auto-bootstrapped cidx-meta bookkeeping repo across its two known aliases
("cidx-meta" and "cidx-meta-global"). It MUST be an anchored/exact match --
never a prefix or substring check -- so legitimate user repos whose real
name happens to start with the same characters (e.g. "cidx-metadata-global",
"cidx-meta-analytics-global") are never mistaken for the internal repo.

TDD: written before the constants.py implementation.
"""


class TestCidxMetaRepoGlobalConstant:
    def test_cidx_meta_repo_global_constant_exists(self):
        from code_indexer.server.services.constants import CIDX_META_REPO_GLOBAL

        assert CIDX_META_REPO_GLOBAL == "cidx-meta-global"


class TestIsInternalMetaRepoPredicate:
    """Exact/anchored match only -- no prefix/substring over-matching."""

    def test_exact_cidx_meta_is_internal(self):
        from code_indexer.server.services.constants import is_internal_meta_repo

        assert is_internal_meta_repo("cidx-meta") is True

    def test_exact_cidx_meta_global_is_internal(self):
        from code_indexer.server.services.constants import is_internal_meta_repo

        assert is_internal_meta_repo("cidx-meta-global") is True

    def test_cidx_metadata_global_is_not_internal(self):
        """A real user repo named 'cidx-metadata-global' must NOT be treated
        as the internal meta repo -- this is the exact over-match the review
        flagged against a loose str.startswith('cidx-meta') check."""
        from code_indexer.server.services.constants import is_internal_meta_repo

        assert is_internal_meta_repo("cidx-metadata-global") is False

    def test_cidx_meta_analytics_global_is_not_internal(self):
        from code_indexer.server.services.constants import is_internal_meta_repo

        assert is_internal_meta_repo("cidx-meta-analytics-global") is False

    def test_unrelated_repo_is_not_internal(self):
        from code_indexer.server.services.constants import is_internal_meta_repo

        assert is_internal_meta_repo("real-repo-alpha") is False

    def test_empty_string_is_not_internal(self):
        from code_indexer.server.services.constants import is_internal_meta_repo

        assert is_internal_meta_repo("") is False
