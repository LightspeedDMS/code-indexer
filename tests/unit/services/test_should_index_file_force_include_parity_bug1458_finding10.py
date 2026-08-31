"""Codex Finding #10 (MEDIUM, Story #1458 review round): SmartIndexer's
git-diff-based incremental discovery (`_should_index_file`) lacked
force-include parity with the full-walk discovery
(`FileFinder._should_include_file` -> `OverrideFilterService.
should_include_file`, file_finder.py). A file matching
`.code-indexer-override.yaml`'s `force_include_patterns` is correctly
indexed on the FIRST (full-walk) `cidx index` run, but was silently
REJECTED by the incremental path once committed -- the opposite-direction
sibling gap to Bug #1467 (which was about a file being wrongly INCLUDED
incrementally that the full walk correctly excludes; this is about a file
being wrongly EXCLUDED incrementally that the full walk correctly
force-includes).

Real git repo, real Config/OverrideConfig, real FileFinder.
override_filter_service -- no mocking of the filtering logic under test.
"""

from pathlib import Path

from code_indexer.config import OverrideConfig

from .incremental_filter_helpers import (
    build_smart_indexer,
    create_git_repo,
)


class TestForceIncludeParityBetweenFullAndIncrementalDiscovery:
    def test_force_included_extension_rejected_by_default_config_is_still_indexed(
        self, tmp_path: Path
    ) -> None:
        """A file with a non-standard extension (not in the default
        file_extensions allow-list) that is explicitly force-included via
        .code-indexer-override.yaml's force_include_patterns must be
        indexed by the incremental (git-diff-based) discovery path too --
        matching what the full-walk find_files() already does."""
        create_git_repo(tmp_path)
        metadata = tmp_path / ".code-indexer" / "metadata.json"
        metadata.parent.mkdir(exist_ok=True)
        metadata.write_text("{}")

        override_config = OverrideConfig(force_include_patterns=["special/*.forceme"])
        indexer = build_smart_indexer(tmp_path, metadata, override_config)

        assert indexer._should_index_file("special/data.forceme") is True, (
            "Bug: a file matching force_include_patterns was rejected by "
            "the incremental discovery path's _should_index_file, even "
            "though the full-walk find_files() (via "
            "OverrideFilterService.should_include_file) would correctly "
            "index it -- a real scope-of-discovery parity gap between "
            "the first and incremental index runs."
        )

    def test_non_force_included_unknown_extension_still_rejected(
        self, tmp_path: Path
    ) -> None:
        """Sanity: force-include parity must not become a blanket
        allow-everything -- a file NOT matching any force_include pattern,
        with an extension outside the allow-list, is still rejected."""
        create_git_repo(tmp_path)
        metadata = tmp_path / ".code-indexer" / "metadata.json"
        metadata.parent.mkdir(exist_ok=True)
        metadata.write_text("{}")

        override_config = OverrideConfig(force_include_patterns=["special/*.forceme"])
        indexer = build_smart_indexer(tmp_path, metadata, override_config)

        assert indexer._should_index_file("other/data.unknownext") is False
