"""
Tests for Bug #960 — suppress repeated missing-domain-file warnings.

parse_domain_file_for_graph() must log WARNING once per missing file path,
then switch to DEBUG for subsequent occurrences. No exc_info=True on the warning.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

import pytest

import code_indexer.server.services.dep_map_parser_graph as dep_map_parser_graph
from code_indexer.server.services.dep_map_parser_graph import parse_domain_file_for_graph
from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry


@pytest.fixture(autouse=True)
def clear_warned_set():
    """Clear the module-level dedup set before each test."""
    dep_map_parser_graph._warned_missing_domains.clear()
    yield
    dep_map_parser_graph._warned_missing_domains.clear()


def _call_parse_for_missing(
    output_dir: Path, domain_name: str, times: int
) -> Tuple[List[AnomalyEntry], List[Tuple[str, bool]]]:
    """
    Call parse_domain_file_for_graph() *times* times for a domain whose .md file
    does not exist.  Returns (anomalies, log_records) where log_records is a list
    of (levelname, exc_info_present) tuples captured from the logger.
    """
    base_dir = output_dir.resolve()
    edge_data: Dict[Tuple[str, str], Dict[str, Any]] = {}
    incoming_claims: Set[frozenset] = set()
    anomalies: List[AnomalyEntry] = []

    captured: List[Tuple[str, bool]] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            captured.append((record.levelname, record.exc_info is not None))

    logger = logging.getLogger("code_indexer.server.services.dep_map_parser_graph")
    handler = _Handler()
    logger.addHandler(handler)
    old_level = logger.level
    logger.setLevel(logging.DEBUG)

    try:
        for _ in range(times):
            parse_domain_file_for_graph(
                output_dir,
                base_dir,
                domain_name,
                edge_data,
                incoming_claims,
                anomalies,
            )
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)

    return anomalies, captured


class TestMissingFileWarningOnce:
    """WARNING must be logged exactly once for a missing domain file."""

    def test_missing_file_logs_warning_once(self, tmp_path: Path) -> None:
        """Calling parse_domain_file_for_graph 5 times with same missing path
        must produce exactly 1 WARNING and 4 DEBUG records."""
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()

        anomalies, records = _call_parse_for_missing(output_dir, "missing-domain", 5)

        warnings = [r for r in records if r[0] == "WARNING"]
        debugs = [r for r in records if r[0] == "DEBUG"]

        assert len(warnings) == 1, (
            f"Expected exactly 1 WARNING for repeated missing file, got {len(warnings)}"
        )
        assert len(debugs) == 4, (
            f"Expected exactly 4 DEBUG records for subsequent calls, got {len(debugs)}"
        )

    def test_missing_file_no_stack_trace_in_warning(self, tmp_path: Path) -> None:
        """The WARNING for a missing domain file must not carry exc_info (no stack trace)."""
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()

        _anomalies, records = _call_parse_for_missing(output_dir, "missing-domain", 1)

        warnings = [r for r in records if r[0] == "WARNING"]
        assert len(warnings) == 1, "Expected 1 WARNING"
        _levelname, exc_info_present = warnings[0]
        assert not exc_info_present, (
            "WARNING for missing domain file must NOT carry exc_info "
            "(no stack trace needed for a simple FileNotFoundError)"
        )

    def test_second_different_missing_file_also_warns_once(
        self, tmp_path: Path
    ) -> None:
        """Two different missing file paths must each get exactly 1 WARNING."""
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()

        base_dir = output_dir.resolve()
        captured: List[Tuple[str, bool]] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured.append((record.levelname, record.message if hasattr(record, "message") else record.getMessage()))

        logger = logging.getLogger("code_indexer.server.services.dep_map_parser_graph")
        handler = _Handler()
        logger.addHandler(handler)
        old_level = logger.level
        logger.setLevel(logging.DEBUG)

        try:
            for _ in range(3):
                parse_domain_file_for_graph(
                    output_dir, base_dir, "domain-alpha",
                    {}, set(), [],
                )
            for _ in range(3):
                parse_domain_file_for_graph(
                    output_dir, base_dir, "domain-beta",
                    {}, set(), [],
                )
        finally:
            logger.removeHandler(handler)
            logger.setLevel(old_level)

        warnings = [r for r in captured if r[0] == "WARNING"]
        assert len(warnings) == 2, (
            f"Expected exactly 2 WARNINGs (one per unique missing path), "
            f"got {len(warnings)}: {warnings}"
        )


class TestExistingFileStillParsesNormally:
    """A valid domain file must still parse successfully after Bug #960 fix."""

    def test_existing_file_still_parses_normally(self, tmp_path: Path) -> None:
        """A valid domain .md file must be read and populate edge_data without warning."""
        output_dir = tmp_path / "dep-map"
        output_dir.mkdir()

        # Write minimal _domains.json (not strictly needed for this call, but realistic)
        (output_dir / "_domains.json").write_text(
            json.dumps([{"name": "my-domain", "participating_repos": []}]),
            encoding="utf-8",
        )

        # Write a valid domain file with outgoing + incoming sections
        content = (
            "---\nname: my-domain\n---\n"
            "## Cross-Domain Connections\n\n"
            "### Outgoing Dependencies\n\n"
            "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
            "| repo-a | lib-b | other-domain | Code-level | for X | ev |\n\n"
            "### Incoming Dependencies\n\n"
            "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
            "|---|---|---|---|---|---|\n"
        )
        (output_dir / "my-domain.md").write_text(content, encoding="utf-8")

        base_dir = output_dir.resolve()
        edge_data: Dict[Tuple[str, str], Dict[str, Any]] = {}
        incoming_claims: Set[frozenset] = set()
        anomalies: List[AnomalyEntry] = []

        warned_before = set(dep_map_parser_graph._warned_missing_domains)

        captured_levels: List[str] = []

        class _Handler(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                captured_levels.append(record.levelname)

        logger = logging.getLogger("code_indexer.server.services.dep_map_parser_graph")
        handler = _Handler()
        logger.addHandler(handler)
        logger.setLevel(logging.DEBUG)

        try:
            parse_domain_file_for_graph(
                output_dir,
                base_dir,
                "my-domain",
                edge_data,
                incoming_claims,
                anomalies,
            )
        finally:
            logger.removeHandler(handler)

        # The edge ("my-domain", "other-domain") must be populated
        assert ("my-domain", "other-domain") in edge_data, (
            "Existing valid file must populate edge_data; bug #960 fix must not break normal parsing"
        )
        # No WARNING emitted for an existing, readable file
        warnings = [lvl for lvl in captured_levels if lvl == "WARNING"]
        assert len(warnings) == 0, (
            f"No WARNING expected for a valid, readable domain file; got {warnings}"
        )
        # Module-level set must not have grown (no missing path was logged)
        assert dep_map_parser_graph._warned_missing_domains == warned_before
