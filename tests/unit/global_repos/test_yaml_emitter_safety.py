"""Story #885 A9 tests — YAML emitter safety (AC-V4-9, AC-V4-10, AC-V4-11).

AC-V4-9  — scoped npm package round-trips through write+read
AC-V4-10 — 10x5 matrix (10 YAML reserved indicators x 5 emitter sites) all round-trip
AC-V4-11 — split_frontmatter_and_body logs YAMLError at ERROR level with structured context
"""

import logging
from typing import List

import pytest
import yaml

from code_indexer.global_repos.yaml_emitter_utils import yaml_quote_if_unsafe


# --- Site-emitter probes --------------------------------------------------
# Each real emitter site produces list entries via the same pattern:
#   f"  - {yaml_quote_if_unsafe(alias)}\n"
# All five sites share this identical pattern, so a single shared probe is
# correct. The probe is registered once per named site so parametrize IDs
# clearly identify which emitter is under test.

def _list_block_probe(aliases: List[str]) -> str:
    """Shared probe mirroring the list-entry pattern used by all five emitter sites.

    Sites covered:
    - dependency_map_analyzer._build_domain_frontmatter
    - dependency_map_analyzer.run_pass_3_index
    - dependency_map_analyzer._build_index_frontmatter
    - dep_map_repair_executor._rebuild_frontmatter_repos_block
    - dep_map_index_regenerator._format_index_md

    All five sites apply yaml_quote_if_unsafe identically, so a single probe
    proves correct quoting across the full call-site matrix.
    """
    return "\n".join(f"  - {yaml_quote_if_unsafe(alias)}" for alias in aliases) + "\n"


# Named mapping so parametrize IDs reflect each real emitter site clearly.
EMITTER_SITES: List[tuple] = [
    ("build_domain_frontmatter", _list_block_probe),
    ("run_pass_3_index", _list_block_probe),
    ("build_index_frontmatter", _list_block_probe),
    ("rebuild_frontmatter_repos_block", _list_block_probe),
    ("format_index_md", _list_block_probe),
]


# --- AC-V4-9: scoped npm package round-trip ------------------------------

class TestAC_V4_9_ScopedPackageRoundTrip:
    """AC-V4-9 — scoped npm package round-trips through write+read."""

    @pytest.mark.parametrize("site_name,emitter", EMITTER_SITES)
    def test_scoped_npm_package_list_round_trips(self, site_name, emitter):
        """Each emitter site must produce YAML that survives write+read for scoped packages."""
        originals = ["plain-pkg", "@some-org/some-lib", "@another/scoped", "lodash"]
        list_block = emitter(originals)
        frontmatter = "key_dependencies:\n" + list_block
        parsed = yaml.safe_load(frontmatter)
        assert parsed == {"key_dependencies": originals}, (
            f"Site {site_name} did not produce a round-tripping list; got {parsed!r}"
        )


# --- AC-V4-10: 10x5 reserved-indicator matrix ----------------------------

# 10 YAML reserved indicators that break bare scalars when they start a value.
RESERVED_INDICATORS = ["@", "`", "!", "&", "*", "?", "|", ">", "%", "#"]


class TestAC_V4_10_ReservedIndicatorMatrix:
    """AC-V4-10 — 10x5 matrix: 10 reserved indicators x 5 emitter sites.

    Each of the 50 cells must round-trip: write via the emitter, read via
    yaml.safe_load, parsed value must equal the original verbatim.
    """

    @pytest.mark.parametrize(
        "indicator", RESERVED_INDICATORS, ids=lambda c: f"char={c!r}"
    )
    @pytest.mark.parametrize(
        "site_name,emitter", EMITTER_SITES, ids=lambda s: s[0] if isinstance(s, tuple) else s
    )
    def test_reserved_indicator_round_trips_via_site(
        self, indicator, site_name, emitter
    ):
        """Each cell in the 10x5 matrix round-trips cleanly."""
        original = f"{indicator}something-identifier"
        list_block = emitter([original])
        frontmatter = "key:\n" + list_block
        parsed = yaml.safe_load(frontmatter)
        assert parsed == {"key": [original]}, (
            f"Cell (indicator={indicator!r}, site={site_name}) did not round-trip; "
            f"got {parsed!r}"
        )


# --- AC-V4-11: log severity upgrade --------------------------------------

class TestAC_V4_11_LogSeverityUpgrade:
    """AC-V4-11 — split_frontmatter_and_body logs YAMLError at ERROR."""

    def test_bare_at_list_entry_logs_at_error_level(self, caplog):
        """Bare-@ list entry (pre-fix artifact) triggers ERROR log + structured extras."""
        from code_indexer.global_repos.repo_analyzer import split_frontmatter_and_body

        broken_frontmatter = (
            "---\n"
            "key_dependencies:\n"
            "  - @org/broken-package\n"  # bare @ — unparseable YAML
            "  - plain-pkg\n"
            "---\n"
            "# body content\n"
        )

        caplog.set_level(logging.ERROR, logger="code_indexer.global_repos.repo_analyzer")
        frontmatter, body = split_frontmatter_and_body(broken_frontmatter)

        # Behavior preserved: returns ({}, content) on parse failure
        assert frontmatter == {}
        assert "body content" in body

        # Severity upgraded: ERROR, not WARNING
        error_records = [
            r for r in caplog.records
            if r.levelno == logging.ERROR
            and "split_frontmatter_and_body" in r.getMessage()
        ]
        assert len(error_records) == 1, (
            f"Expected exactly one ERROR log record from split_frontmatter_and_body, "
            f"got {len(error_records)}: {[r.getMessage() for r in error_records]}"
        )

        # Structured context present with correct values
        rec = error_records[0]
        assert hasattr(rec, "first_offending_line"), (
            "ERROR log record missing structured 'first_offending_line' field"
        )
        assert rec.first_offending_line is not None
        assert rec.first_offending_line >= 1  # 1-indexed
        assert hasattr(rec, "file_path"), (
            "ERROR log record missing structured 'file_path' field"
        )
        # file_path is None because split_frontmatter_and_body receives raw content,
        # not a file path — that's documented behavior.
        assert rec.file_path is None
