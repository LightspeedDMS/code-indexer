"""
Bug #1114 regression tests — two combined defects.

Defect 1 — logging flood: parse_domain_file_for_graph must emit exactly ONE
WARNING per malformed file path (keyed set de-spam), then demote subsequent
calls for that path to DEBUG.  The MALFORMED_YAML anomaly must still be
recorded on EVERY call regardless.

Defect 2 — writer root cause: _update_frontmatter_timestamp and
_build_refinement_frontmatter must produce frontmatter via yaml.safe_dump of
the complete parsed dict (or equivalent safe serialisation), not by line-by-line
string manipulation that can leave duplicated or un-indented YAML keys.

Test inventory (4 Defect-1 + 5 Defect-2 = 9 total):

Defect 1:
  1. test_warns_exactly_once_for_malformed_file       — 1 WARNING, N DEBUG, N anomalies
  2. test_despam_is_per_path_not_global               — two paths each warn once
  3. test_anomaly_recorded_every_call_independently_of_logging — anomaly still N at CRITICAL
  4. test_valid_file_does_not_produce_malformed_warning — no false positives

Defect 2:
  5. TestUpdateFrontmatterTimestamp.test_round_trip_with_list_field_produces_valid_yaml
  6. TestUpdateFrontmatterTimestamp.test_no_frontmatter_creates_minimal_valid_block
  7. TestBuildRefinementFrontmatter.test_round_trip_with_list_field_produces_valid_yaml
  8. TestBuildRefinementFrontmatter.test_output_frontmatter_is_valid_yaml
  9. TestRenderMdDeltaPath.test_render_md_and_write_atomic_round_trip_is_valid_yaml
"""

from __future__ import annotations

import logging
import re as _re
from pathlib import Path
from typing import Any, Dict, Set

import yaml


# ---------------------------------------------------------------------------
# Helpers — import under test
# ---------------------------------------------------------------------------


def _parse_domain(
    output_dir: Path,
    domain_name: str,
    edge_data: Dict,
    incoming_claims: Set,
    anomalies: list,
) -> None:
    from code_indexer.server.services.dep_map_parser_graph import (
        parse_domain_file_for_graph,
    )

    parse_domain_file_for_graph(
        output_dir=output_dir,
        base_dir=output_dir.resolve(),
        domain_name=domain_name,
        edge_data=edge_data,
        incoming_claims=incoming_claims,
        anomalies=anomalies,
    )


def _reset_warned_set(paths: list[str]) -> None:
    """Remove paths from the module-level malformed-domain seen set."""
    from code_indexer.server.services import dep_map_parser_graph

    with dep_map_parser_graph._warned_malformed_domains_lock:
        for p in paths:
            dep_map_parser_graph._warned_malformed_domains.discard(p)


# ---------------------------------------------------------------------------
# Fixtures
#
# Both fixtures use truly malformed YAML (unclosed bracket) that always raises
# yaml.scanner.ScannerError — this ensures the `except Exception` branch in
# parse_domain_file_for_graph is exercised, not the FileNotFoundError branch.
# ---------------------------------------------------------------------------

MALFORMED_YAML_CONTENT = """\
---
domain: bad-domain
participating_repos: [unclosed
---

# Bad Domain

Some body.
"""

MALFORMED_YAML_CONTENT_2 = """\
---
domain: other-domain
bad_field: [also unclosed
---

# Other Domain
"""

# A fully valid domain file (no outgoing/incoming tables — just frontmatter + body)
VALID_DOMAIN_CONTENT = """\
---
domain: good-domain
last_analyzed: 2024-01-01T00:00:00+00:00
---

# Good Domain

No cross-domain dependencies.
"""


# ---------------------------------------------------------------------------
# Helper — parse frontmatter from rendered .md string
# ---------------------------------------------------------------------------


def _frontmatter_from_content(content: str) -> Dict[str, Any]:
    """Parse YAML frontmatter from a rendered domain .md string.

    Raises AssertionError if the block is absent, malformed, or not a dict.
    """
    assert content.startswith("---\n"), "Content must start with '---\\n'"
    rest = content[4:]
    close = rest.find("\n---\n")
    assert close != -1, "Frontmatter closing '---' not found"
    yaml_block = rest[:close]
    result = yaml.safe_load(yaml_block)
    assert isinstance(result, dict), (
        f"Frontmatter must parse as dict, got {type(result)}: {yaml_block!r}"
    )
    return result


# ---------------------------------------------------------------------------
# Defect 1: logging flood de-spam  (4 tests)
# ---------------------------------------------------------------------------


class TestParseGraphFloodDespam:
    """Defect 1: malformed-YAML branch must warn once per path, then DEBUG."""

    def test_warns_exactly_once_for_malformed_file(self, tmp_path, caplog):
        """Calling parse_domain_file_for_graph N times on a malformed file
        must produce exactly 1 WARNING (first call) carrying exc_info, then
        N-1 DEBUG-level entries for that path. MALFORMED_YAML anomaly is
        recorded every call."""
        domain_file = tmp_path / "bad-domain.md"
        domain_file.write_text(MALFORMED_YAML_CONTENT)
        _reset_warned_set([str(domain_file.resolve())])

        N = 5
        all_anomalies: list = []

        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            for _ in range(N):
                anomalies: list = []
                _parse_domain(tmp_path, "bad-domain", {}, set(), anomalies)
                all_anomalies.extend(anomalies)

        # Exactly 1 WARNING for this file path
        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "bad-domain" in r.getMessage()
        ]
        assert len(warning_records) == 1, (
            f"Expected exactly 1 WARNING for malformed path, got {len(warning_records)}: "
            f"{[r.getMessage() for r in warning_records]}"
        )

        # First WARNING must carry exc_info (traceback logged once)
        assert warning_records[0].exc_info is not None, (
            "First WARNING for malformed YAML must carry exc_info (traceback)"
        )

        # Subsequent calls should appear at DEBUG
        debug_records = [
            r
            for r in caplog.records
            if r.levelno == logging.DEBUG and "bad-domain" in r.getMessage()
        ]
        assert len(debug_records) >= N - 1, (
            f"Expected at least {N - 1} DEBUG records for follow-up calls, "
            f"got {len(debug_records)}"
        )

        # MALFORMED_YAML anomaly recorded every call
        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType

        malformed = [a for a in all_anomalies if a.type == AnomalyType.MALFORMED_YAML]
        assert len(malformed) == N, (
            f"Expected MALFORMED_YAML anomaly on every call ({N}), got {len(malformed)}"
        )

    def test_despam_is_per_path_not_global(self, tmp_path, caplog):
        """Two different malformed files each produce exactly one WARNING."""
        file_a = tmp_path / "domain-alpha.md"
        file_b = tmp_path / "domain-beta.md"
        file_a.write_text(MALFORMED_YAML_CONTENT)
        file_b.write_text(MALFORMED_YAML_CONTENT_2)
        _reset_warned_set([str(file_a.resolve()), str(file_b.resolve())])

        N = 3
        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            for _ in range(N):
                _parse_domain(tmp_path, "domain-alpha", {}, set(), [])
                _parse_domain(tmp_path, "domain-beta", {}, set(), [])

        alpha_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "domain-alpha" in r.getMessage()
        ]
        beta_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "domain-beta" in r.getMessage()
        ]
        assert len(alpha_warnings) == 1, (
            f"Expected 1 WARNING for domain-alpha, got {len(alpha_warnings)}"
        )
        assert len(beta_warnings) == 1, (
            f"Expected 1 WARNING for domain-beta, got {len(beta_warnings)}"
        )

    def test_anomaly_recorded_every_call_independently_of_logging(
        self, tmp_path, caplog
    ):
        """MALFORMED_YAML anomaly count must equal N even when log level is CRITICAL."""
        domain_file = tmp_path / "silent-domain.md"
        domain_file.write_text(MALFORMED_YAML_CONTENT)
        _reset_warned_set([str(domain_file.resolve())])

        N = 7
        all_anomalies: list = []
        with caplog.at_level(logging.CRITICAL, logger="code_indexer"):
            for _ in range(N):
                anomalies: list = []
                _parse_domain(tmp_path, "silent-domain", {}, set(), anomalies)
                all_anomalies.extend(anomalies)

        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType

        malformed = [a for a in all_anomalies if a.type == AnomalyType.MALFORMED_YAML]
        assert len(malformed) == N, (
            f"Anomaly count must equal N={N} regardless of log level, "
            f"got {len(malformed)}"
        )

    def test_valid_file_does_not_produce_malformed_warning(self, tmp_path, caplog):
        """A well-formed domain file must produce zero MALFORMED_YAML anomalies
        and zero WARNINGs from parse_domain_file_for_graph."""
        domain_file = tmp_path / "good-domain.md"
        domain_file.write_text(VALID_DOMAIN_CONTENT)
        _reset_warned_set([str(domain_file.resolve())])

        all_anomalies: list = []
        with caplog.at_level(logging.DEBUG, logger="code_indexer"):
            _parse_domain(tmp_path, "good-domain", {}, set(), all_anomalies)

        from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType

        malformed = [a for a in all_anomalies if a.type == AnomalyType.MALFORMED_YAML]
        assert len(malformed) == 0, (
            f"Valid domain file must produce no MALFORMED_YAML anomalies, "
            f"got: {malformed}"
        )

        parse_graph_warnings = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "good-domain" in r.getMessage()
        ]
        assert len(parse_graph_warnings) == 0, (
            f"Valid domain file must produce no WARNINGs, got: "
            f"{[r.getMessage() for r in parse_graph_warnings]}"
        )


# ---------------------------------------------------------------------------
# Defect 2: writer round-trip — frontmatter must be valid single-dict YAML
# (5 tests)
# ---------------------------------------------------------------------------


class TestUpdateFrontmatterTimestamp:
    """Defect 2: _update_frontmatter_timestamp must produce valid YAML frontmatter."""

    def _call(self, existing_content: str, new_body: str, domain_name: str) -> str:
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )
        from unittest.mock import MagicMock

        svc = MagicMock(spec=DependencyMapService)
        return str(
            DependencyMapService._update_frontmatter_timestamp(
                svc, existing_content, new_body, domain_name
            )
        )

    def test_round_trip_with_list_field_produces_valid_yaml(self):
        """existing_content with participating_repos list must round-trip cleanly
        — no duplicate or un-indented YAML keys in the output frontmatter."""
        existing = (
            "---\n"
            "domain: cloud-infra\n"
            "participating_repos:\n"
            "  - repo-a\n"
            "  - repo-b\n"
            "last_analyzed: 2024-01-01T00:00:00+00:00\n"
            "---\n\n"
            "# Cloud Infrastructure Platform\n\nSome body.\n"
        )
        result = self._call(existing, "New body content.\n", "cloud-infra")
        fm = _frontmatter_from_content(result)

        assert "domain" in fm
        assert "last_analyzed" in fm
        if "participating_repos" in fm:
            assert isinstance(fm["participating_repos"], list), (
                "participating_repos must survive round-trip as a proper list"
            )

    def test_no_frontmatter_creates_minimal_valid_block(self):
        """No existing frontmatter → creates minimal block parseable as a dict."""
        result = self._call("No frontmatter here.\n", "New body.\n", "my-domain")
        fm = _frontmatter_from_content(result)
        assert fm.get("domain") == "my-domain"
        assert "last_analyzed" in fm

    def test_corrupted_frontmatter_with_duplicated_participating_repos_is_self_healed(
        self,
    ):
        """If existing frontmatter already has a duplicated/un-indented
        participating_repos key (the exact corruption bug #1114 produces), the
        writer must re-serialise to ONE clean copy — not preserve the corruption.

        The line-based implementation would pass both copies through verbatim,
        producing invalid YAML that yaml.safe_load raises or ignores. The
        dict-based implementation parses once (last-key-wins or merge) and
        emits exactly one clean key.
        """
        # This is the corrupt artifact that the old line-based code emits:
        # participating_repos appears twice; the second copy is un-indented.
        corrupted = (
            "---\n"
            "domain: svc-mesh\n"
            "participating_repos:\n"
            "  - repo-a\n"
            "  - repo-b\n"
            "last_analyzed: 2024-01-01T00:00:00+00:00\n"
            "participating_repos:\n"
            "- repo-a\n"
            "- repo-b\n"
            "---\n\n"
            "Some body.\n"
        )
        result = self._call(corrupted, "New body.\n", "svc-mesh")

        # The output must contain `participating_repos:` exactly once
        occurrences = result.count("participating_repos:")
        assert occurrences == 1, (
            f"participating_repos: must appear exactly once in output, "
            f"got {occurrences} occurrences"
        )

        # And must be parseable as a valid dict
        fm = _frontmatter_from_content(result)
        assert isinstance(fm.get("participating_repos"), list), (
            "participating_repos must be a list after self-heal round-trip"
        )

    def test_invalid_yaml_frontmatter_logs_warning_and_produces_valid_output(
        self, caplog
    ):
        """When existing frontmatter is invalid YAML (e.g. unclosed bracket),
        the writer must: (a) log a WARNING (not raise), (b) produce a valid
        YAML frontmatter block in the output containing at minimum domain +
        last_analyzed.

        The line-based implementation either propagates the corrupt YAML or
        silently drops it without logging. The dict-based implementation must
        detect yaml.YAMLError, log a WARNING, and fall back to a minimal
        valid block.
        """
        invalid = (
            "---\n"
            "domain: broken-svc\n"
            "participating_repos: [unclosed\n"
            "---\n\n"
            "Body text.\n"
        )
        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            result = self._call(invalid, "New body.\n", "broken-svc")

        # Must not raise — result must be a non-empty string
        assert isinstance(result, str) and len(result) > 0

        # Must produce valid YAML frontmatter
        fm = _frontmatter_from_content(result)
        assert isinstance(fm, dict), "Output frontmatter must parse as a dict"
        assert "last_analyzed" in fm, "Fallback dict must include last_analyzed"

        # Must have logged a WARNING about the parse failure
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1, (
            "A WARNING must be logged when existing frontmatter is invalid YAML"
        )


class TestBuildRefinementFrontmatter:
    """Defect 2: _build_refinement_frontmatter must produce valid YAML frontmatter."""

    def _call(self, existing_content: str, new_body: str, domain_name: str) -> str:
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )
        from unittest.mock import MagicMock

        svc = MagicMock(spec=DependencyMapService)
        return str(
            DependencyMapService._build_refinement_frontmatter(
                svc, existing_content, new_body, domain_name
            )
        )

    def test_round_trip_with_list_field_produces_valid_yaml(self):
        """participating_repos list in existing frontmatter must survive round-trip."""
        existing = (
            "---\n"
            "domain: cloud-infra\n"
            "participating_repos:\n"
            "  - repo-x\n"
            "  - repo-y\n"
            "last_analyzed: 2024-06-01T00:00:00+00:00\n"
            "---\n\n"
            "Existing body.\n"
        )
        result = self._call(existing, "Refined body.\n", "cloud-infra")
        fm = _frontmatter_from_content(result)
        assert "domain" in fm
        if "participating_repos" in fm:
            assert isinstance(fm["participating_repos"], list)

    def test_output_frontmatter_is_valid_yaml(self):
        """Output frontmatter must be parseable YAML with no scanner errors."""
        existing = (
            "---\ndomain: svc-a\nparticipating_repos:\n  - r1\n  - r2\n---\n\nBody.\n"
        )
        result = self._call(existing, "New body.\n", "svc-a")
        fm = _frontmatter_from_content(result)
        assert isinstance(fm, dict)

    def test_corrupted_frontmatter_with_duplicated_participating_repos_is_self_healed(
        self,
    ):
        """If existing frontmatter has a duplicated participating_repos key,
        _build_refinement_frontmatter must emit exactly one clean copy.

        The line-based implementation preserves whatever lines are present,
        so a duplicated key passes through verbatim — invalid YAML. The
        dict-based implementation parse → update → yaml.safe_dump ensures
        exactly one copy.
        """
        corrupted = (
            "---\n"
            "domain: svc-mesh\n"
            "participating_repos:\n"
            "  - repo-x\n"
            "  - repo-y\n"
            "last_analyzed: 2024-02-01T00:00:00+00:00\n"
            "participating_repos:\n"
            "- repo-x\n"
            "- repo-y\n"
            "---\n\n"
            "Old body.\n"
        )
        result = self._call(corrupted, "Refined body.\n", "svc-mesh")

        occurrences = result.count("participating_repos:")
        assert occurrences == 1, (
            f"participating_repos: must appear exactly once, got {occurrences}"
        )

        fm = _frontmatter_from_content(result)
        assert isinstance(fm.get("participating_repos"), list)

    def test_invalid_yaml_frontmatter_logs_warning_and_produces_valid_output(
        self, caplog
    ):
        """When existing frontmatter is invalid YAML, _build_refinement_frontmatter
        must log a WARNING and fall back to a valid minimal block (not raise, not
        silently emit corrupt YAML).
        """
        invalid = (
            "---\n"
            "domain: broken-svc\n"
            "participating_repos: [unclosed\n"
            "---\n\n"
            "Body text.\n"
        )
        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            result = self._call(invalid, "Refined body.\n", "broken-svc")

        assert isinstance(result, str) and len(result) > 0
        fm = _frontmatter_from_content(result)
        assert isinstance(fm, dict)
        assert "last_refined" in fm

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) >= 1, (
            "A WARNING must be logged when existing frontmatter is invalid YAML"
        )


class TestRenderMdDeltaPath:
    """Defect 2: render_md + write_atomic (used in the delta Story #1053 path)
    must produce a single valid YAML frontmatter block that round-trips cleanly
    through the filesystem."""

    def test_render_md_and_write_atomic_round_trip_is_valid_yaml(self, tmp_path):
        """render_md serializes the complete frontmatter dict via yaml.safe_dump,
        then write_atomic persists it; the file read back must parse to a valid
        dict with participating_repos as a proper list (not duplicated/un-indented)."""
        from code_indexer.server.services.dep_map_delta_journal import (
            render_md,
            write_atomic,
        )

        frontmatter = {
            "domain": "cloud-infra",
            "participating_repos": ["repo-a", "repo-b", "repo-c"],
            "last_delta_applied": "abc123",
            "last_applied_at": "2024-06-01T00:00:00+00:00",
        }
        body = "# Cloud Infra\n\nSome content.\n"
        rendered = render_md(frontmatter, body)

        dest = tmp_path / "cloud-infra.md"
        write_atomic(dest, rendered)

        on_disk = dest.read_text()
        fm = _frontmatter_from_content(on_disk)

        assert fm["domain"] == "cloud-infra"
        assert isinstance(fm["participating_repos"], list), (
            "participating_repos must be a list after render_md+write_atomic round-trip"
        )
        assert fm["participating_repos"] == ["repo-a", "repo-b", "repo-c"]
        assert fm["last_delta_applied"] == "abc123"

        # Body is preserved after the closing ---
        assert "# Cloud Infra" in on_disk
        assert "Some content." in on_disk


# ---------------------------------------------------------------------------
# Bug #1114 Defect 2 — timestamp corruption fix (byte-exact regression tests)
# ---------------------------------------------------------------------------


class TestTimestampPreservation:
    """ISO-8601 timestamps with T separator must survive YAML round-trip
    in all three writer paths: _update_frontmatter_timestamp,
    _build_refinement_frontmatter, and render_md (delta path).

    Guards against yaml.safe_load/safe_dump converting timestamps to datetime
    objects and re-emitting them without the T separator or with single-quotes.
    """

    _LINE_READER_RE = _re.compile(r"last_analyzed:\s*(\S+)")
    _REFINED_RE = _re.compile(r"last_refined:\s*(\S+)")

    def _call_update(
        self, existing_content: str, new_body: str, domain_name: str
    ) -> str:
        from unittest.mock import MagicMock

        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        svc = MagicMock(spec=DependencyMapService)
        return str(
            DependencyMapService._update_frontmatter_timestamp(
                svc, existing_content, new_body, domain_name
            )
        )

    def _call_refinement(
        self, existing_content: str, new_body: str, domain_name: str
    ) -> str:
        from unittest.mock import MagicMock

        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        svc = MagicMock(spec=DependencyMapService)
        return str(
            DependencyMapService._build_refinement_frontmatter(
                svc, existing_content, new_body, domain_name
            )
        )

    def test_update_frontmatter_new_last_analyzed_has_T(self):
        """The freshly-written last_analyzed must contain T and have no surrounding
        quotes so the line-based reader regex captures it cleanly."""
        existing = (
            "---\n"
            "domain: svc-b\n"
            "last_analyzed: 2023-05-10T12:00:00+00:00\n"
            "---\n\n"
            "Body.\n"
        )
        result = self._call_update(existing, "Updated body.\n", "svc-b")

        m = self._LINE_READER_RE.search(result)
        assert m is not None, "line-reader regex did not match last_analyzed in output"
        raw_value = m.group(1)
        assert raw_value[0] not in ("'", '"'), (
            f"last_analyzed is quoted — breaks line-reader: {raw_value!r}"
        )
        assert "T" in raw_value, (
            f"freshly-written last_analyzed missing T separator: {raw_value!r}"
        )

    def test_refinement_preserves_last_analyzed_with_T_exactly(self):
        """_build_refinement_frontmatter must keep last_analyzed byte-identical
        to the input value (with T) — it only adds/updates last_refined."""
        original_ts = "2024-01-01T00:00:00+00:00"
        existing = (
            "---\n"
            "domain: auth-domain\n"
            "participating_repos:\n"
            "  - repo-a\n"
            f"last_analyzed: {original_ts}\n"
            "---\n\n"
            "Body.\n"
        )
        result = self._call_refinement(existing, "Refined body.\n", "auth-domain")

        m = self._LINE_READER_RE.search(result)
        assert m is not None, "last_analyzed not found in refinement output"
        raw_value = m.group(1)
        assert raw_value == original_ts, (
            f"last_analyzed was mutated by refinement: expected {original_ts!r}, "
            f"got {raw_value!r}"
        )

    def test_refinement_new_last_refined_has_T_no_quotes(self):
        """The freshly-written last_refined must have T and no surrounding quotes."""
        existing = (
            "---\n"
            "domain: auth-domain\n"
            "last_analyzed: 2024-01-01T00:00:00+00:00\n"
            "---\n\n"
            "Body.\n"
        )
        result = self._call_refinement(existing, "Refined body.\n", "auth-domain")

        m = self._REFINED_RE.search(result)
        assert m is not None, "last_refined not found in output frontmatter"
        raw_value = m.group(1)
        assert raw_value[0] not in ("'", '"'), (
            f"last_refined is single/double-quoted — breaks line-reader: {raw_value!r}"
        )
        assert "T" in raw_value, f"last_refined missing T separator: {raw_value!r}"

    def test_render_md_last_applied_at_has_T_no_quotes(self):
        """render_md (delta path) must emit last_applied_at with T and no quotes."""
        from code_indexer.server.services.dep_map_delta_journal import render_md

        frontmatter = {
            "domain": "data-domain",
            "last_delta_applied": "deadbeef01",
            "last_applied_at": "2024-06-01T00:00:00+00:00",
        }
        rendered = render_md(frontmatter, "Body text.\n")

        last_applied_re = _re.compile(r"last_applied_at:\s*(\S+)")
        m = last_applied_re.search(rendered)
        assert m is not None, "last_applied_at not found in render_md output"
        raw_value = m.group(1)
        assert raw_value[0] not in ("'", '"'), (
            f"last_applied_at is quoted — breaks line-reader: {raw_value!r}"
        )
        assert "T" in raw_value, f"last_applied_at missing T separator: {raw_value!r}"
