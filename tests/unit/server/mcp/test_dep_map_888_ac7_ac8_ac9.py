"""
Story #888 — AC7, AC8, AC9: Per-tool resolution state mappings.

AC7: find_consumers supports 4 reachable resolution states:
     ok | invalid_input | repo_not_indexed | repo_has_no_consumers
     (domain_not_indexed not reachable — tool scans ALL domains exhaustively)

AC8: per-tool resolution mappings:
     - depmap_get_repo_domains: ok | invalid_input | repo_not_indexed
     - depmap_get_domain_summary: ok | invalid_input | domain_not_indexed
     - depmap_get_stale_domains: ok | invalid_input

AC9: depmap_get_cross_domain_graph: ok only (scan-based, no identifier input)
     depmap_get_hub_domains: reserved for Story #889, NOT tested here.
"""

import pytest
from pathlib import Path
from typing import get_args

from code_indexer.server.mcp.handlers._depmap_aliases import ResolutionLiteral
from tests.unit.server.mcp.test_dep_map_888_fixtures import (
    _assert_resolution,
    _call_handler,
    make_dep_map_indexed_no_consumers,
    make_dep_map_with_consumer,
    make_dep_map_with_domain,
    make_empty_dep_map,
    make_two_domain_graph,
)


# ---------------------------------------------------------------------------
# AC7: depmap_find_consumers — resolution contract (5 states in type, 4 reachable)
# ---------------------------------------------------------------------------


class TestAC7ResolutionContractHasFiveStates:
    """Assert the ResolutionLiteral TYPE CONTRACT declares all 5 states.

    This test verifies the shared type definition is complete even though
    find_consumers can only reach 4 of the 5 states at runtime.
    """

    def test_resolution_literal_contains_all_five_states(self) -> None:
        states = set(get_args(ResolutionLiteral))
        assert "ok" in states
        assert "invalid_input" in states
        assert "repo_not_indexed" in states
        assert "domain_not_indexed" in states
        assert "repo_has_no_consumers" in states
        assert len(states) == 5, (
            f"Expected 5 resolution states, got {len(states)}: {states}"
        )


class TestAC7DomainNotIndexedGap:
    """Document the architectural gap: domain_not_indexed is unreachable via find_consumers.

    find_consumers scans ALL domains exhaustively looking for entries that reference
    repo_name as a dependency. There is no per-domain lookup step where a single domain
    could be "not indexed" — the tool operates at repo level, not domain level.

    Resolution: xfailed pending product decision. See Codex review round 2.
    """

    @pytest.mark.xfail(
        reason=(
            "AC7 spec requires 5 resolution states but depmap_find_consumers has no "
            "domain-lookup path — the tool scans ALL domains exhaustively so "
            "domain_not_indexed cannot be triggered. "
            "Awaiting product decision. See Codex review round 2."
        ),
        strict=True,
    )
    def test_domain_not_indexed_reachable_via_find_consumers(
        self, tmp_path: Path
    ) -> None:
        """This test documents that domain_not_indexed is NOT reachable.

        It is expected to fail (xfail strict=True) because there is no fixture
        or handler path that produces resolution='domain_not_indexed' from
        depmap_find_consumers_handler. The xfail documents the gap explicitly.
        """
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_find_consumers_handler,
        )

        # No fixture exists that can produce domain_not_indexed from find_consumers.
        # Calling with any repo returns repo_not_indexed, not domain_not_indexed.
        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_find_consumers_handler, {"repo_name": "any-repo"}, root
        )
        # This assertion will never pass — confirming domain_not_indexed is unreachable.
        assert data.get("resolution") == "domain_not_indexed", (
            f"Expected domain_not_indexed but got {data.get('resolution')!r} — "
            "confirming domain_not_indexed is unreachable via find_consumers."
        )


# ---------------------------------------------------------------------------
# AC7: depmap_find_consumers — 4 reachable resolution states
# ---------------------------------------------------------------------------


class TestAC7FindConsumersInvalidInput:
    def test_empty_repo_name_returns_invalid_input(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_find_consumers_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(depmap_find_consumers_handler, {"repo_name": ""}, root)
        _assert_resolution(
            data, "invalid_input", False, "find_consumers empty repo_name"
        )


class TestAC7FindConsumersRepoNotIndexed:
    def test_unknown_repo_returns_repo_not_indexed(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_find_consumers_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_find_consumers_handler, {"repo_name": "ghost"}, root
        )
        _assert_resolution(
            data, "repo_not_indexed", False, "find_consumers unknown repo"
        )


class TestAC7FindConsumersHasNoConsumers:
    def test_indexed_isolated_repo_returns_repo_has_no_consumers(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_find_consumers_handler,
        )

        root = make_dep_map_indexed_no_consumers(tmp_path, "isolated-repo")
        data = _call_handler(
            depmap_find_consumers_handler, {"repo_name": "isolated-repo"}, root
        )
        _assert_resolution(
            data, "repo_has_no_consumers", False, "find_consumers isolated"
        )


class TestAC7FindConsumersOk:
    def test_repo_with_consumers_returns_ok(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_find_consumers_handler,
        )

        root = make_dep_map_with_consumer(tmp_path, "hub-repo")
        data = _call_handler(
            depmap_find_consumers_handler, {"repo_name": "hub-repo"}, root
        )
        _assert_resolution(data, "ok", True, "find_consumers hub")
        assert len(data["consumers"]) >= 1


# ---------------------------------------------------------------------------
# AC8: depmap_get_repo_domains
# ---------------------------------------------------------------------------


class TestAC8RepoDomains:
    def test_empty_repo_name_returns_invalid_input(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_repo_domains_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(depmap_get_repo_domains_handler, {"repo_name": ""}, root)
        _assert_resolution(data, "invalid_input", False, "repo_domains empty")

    def test_unknown_repo_returns_repo_not_indexed(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_repo_domains_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_get_repo_domains_handler, {"repo_name": "ghost"}, root
        )
        _assert_resolution(data, "repo_not_indexed", False, "repo_domains unknown")

    def test_known_repo_returns_ok(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_repo_domains_handler,
        )

        root = make_dep_map_with_domain(tmp_path, "beta-domain", "known-repo")
        data = _call_handler(
            depmap_get_repo_domains_handler, {"repo_name": "known-repo"}, root
        )
        _assert_resolution(data, "ok", True, "repo_domains known")


# ---------------------------------------------------------------------------
# AC8: depmap_get_domain_summary
# ---------------------------------------------------------------------------


class TestAC8DomainSummary:
    def test_empty_domain_name_returns_invalid_input(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_domain_summary_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_get_domain_summary_handler, {"domain_name": ""}, root
        )
        _assert_resolution(data, "invalid_input", False, "domain_summary empty")

    def test_unknown_domain_returns_domain_not_indexed(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_domain_summary_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_get_domain_summary_handler, {"domain_name": "ghost"}, root
        )
        _assert_resolution(data, "domain_not_indexed", False, "domain_summary unknown")

    def test_known_domain_returns_ok(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_domain_summary_handler,
        )

        root = make_dep_map_with_domain(tmp_path, "known-domain", "r")
        data = _call_handler(
            depmap_get_domain_summary_handler, {"domain_name": "known-domain"}, root
        )
        _assert_resolution(data, "ok", True, "domain_summary known")


# ---------------------------------------------------------------------------
# AC8: depmap_get_stale_domains
# ---------------------------------------------------------------------------


class TestAC8StaleDomains:
    def test_negative_threshold_returns_invalid_input(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_stale_domains_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_get_stale_domains_handler, {"days_threshold": -1}, root
        )
        _assert_resolution(data, "invalid_input", False, "stale_domains negative")

    def test_valid_threshold_returns_ok(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_stale_domains_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_get_stale_domains_handler, {"days_threshold": 30}, root
        )
        _assert_resolution(data, "ok", True, "stale_domains valid")

    def test_never_returns_not_indexed_states(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_stale_domains_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(
            depmap_get_stale_domains_handler, {"days_threshold": 0}, root
        )
        assert data.get("resolution") not in {"repo_not_indexed", "domain_not_indexed"}


# ---------------------------------------------------------------------------
# AC9: depmap_get_cross_domain_graph — ok only
# ---------------------------------------------------------------------------


class TestAC9CrossDomainGraph:
    def test_valid_data_returns_ok(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_cross_domain_graph_handler,
        )

        root = make_two_domain_graph(tmp_path)
        data = _call_handler(depmap_get_cross_domain_graph_handler, {}, root)
        _assert_resolution(data, "ok", True, "graph valid")

    def test_empty_dep_map_returns_ok(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_cross_domain_graph_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(depmap_get_cross_domain_graph_handler, {}, root)
        _assert_resolution(data, "ok", True, "graph empty")

    def test_never_returns_identifier_level_states(self, tmp_path: Path) -> None:
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_cross_domain_graph_handler,
        )

        root = make_empty_dep_map(tmp_path)
        data = _call_handler(depmap_get_cross_domain_graph_handler, {}, root)
        assert data.get("resolution") not in {
            "repo_not_indexed",
            "domain_not_indexed",
            "repo_has_no_consumers",
        }

    def test_missing_dep_map_path_returns_invalid_input(self, tmp_path: Path) -> None:
        """AC9 + Rule 15: missing dep_map_path must return invalid_input (not ok).

        success=False with resolution=ok violates the success/resolution invariant.
        """
        from code_indexer.server.mcp.handlers.depmap import (
            depmap_get_cross_domain_graph_handler,
        )

        missing_root = tmp_path / "does-not-exist"
        data = _call_handler(depmap_get_cross_domain_graph_handler, {}, missing_root)
        _assert_resolution(data, "invalid_input", False, "graph missing path")
