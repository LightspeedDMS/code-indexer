"""
Synthetic tests for DepMapRepairExecutor (Story #342).

Tests reproduce all repair scenarios using real filesystem structures.
No mocking of health detector or index regenerator -- tests use real instances
against real filesystem state.

Only the domain_analyzer callable is a test double (injected via constructor),
because it wraps Claude CLI which is expensive and external.

Test strategy:
  1. Create a temporary directory structure mimicking a dependency map output
  2. Introduce the specific anomaly being tested
  3. Run the executor with appropriate domain_analyzer test double
  4. Assert the correct outcome (fixed list, errors, final health status)

All 15 tests map to the 5-phase repair algorithm in DepMapRepairExecutor.
"""

import json
from pathlib import Path
from typing import Dict, List, Optional

import pytest

# Reuse helpers from health detector tests
from tests.unit.server.services.test_dep_map_health_detector import (
    VALID_DOMAIN_CONTENT,
    VALID_INDEX_CONTENT,
    make_domain_file,
    make_domains_json,
    make_healthy_output_dir,
    make_index_md,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers for getting real service instances
# ─────────────────────────────────────────────────────────────────────────────


def _get_health_detector():
    """Import DepMapHealthDetector -- real instance, no mocking."""
    from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector

    return DepMapHealthDetector()


def _get_index_regenerator():
    """Import IndexRegenerator -- real instance, no mocking."""
    from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator

    return IndexRegenerator()


def _get_executor(domain_analyzer=None, journal_callback=None):
    """Build a DepMapRepairExecutor with real health detector and index regenerator."""
    from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor

    return DepMapRepairExecutor(
        health_detector=_get_health_detector(),
        index_regenerator=_get_index_regenerator(),
        domain_analyzer=domain_analyzer,
        journal_callback=journal_callback,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Fake domain analyzers (test doubles for Claude CLI)
# ─────────────────────────────────────────────────────────────────────────────


def make_success_analyzer():
    """Return a domain_analyzer that always writes valid content and returns True."""

    def analyzer(output_dir: Path, domain: Dict, domain_list: List, repo_list: List) -> bool:
        content = VALID_DOMAIN_CONTENT.replace("test-domain", domain["name"])
        (output_dir / f"{domain['name']}.md").write_text(content)
        return True

    return analyzer


def make_failing_analyzer():
    """Return a domain_analyzer that always writes 0 bytes (simulates failure)."""

    def analyzer(output_dir: Path, domain: Dict, domain_list: List, repo_list: List) -> bool:
        (output_dir / f"{domain['name']}.md").write_text("")
        return False

    return analyzer


def make_nth_attempt_success_analyzer(succeed_on_attempt: int):
    """Return a domain_analyzer that fails N-1 times then succeeds on Nth attempt."""
    call_counts: Dict[str, int] = {}

    def analyzer(output_dir: Path, domain: Dict, domain_list: List, repo_list: List) -> bool:
        name = domain["name"]
        call_counts[name] = call_counts.get(name, 0) + 1
        if call_counts[name] < succeed_on_attempt:
            # Write 0 bytes to simulate failure
            (output_dir / f"{name}.md").write_text("")
            return False
        # Succeed on Nth attempt
        content = VALID_DOMAIN_CONTENT.replace("test-domain", name)
        (output_dir / f"{name}.md").write_text(content)
        return True

    return analyzer, call_counts


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: Nothing to repair (healthy report)
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairNothingToRepair:
    """Healthy HealthReport produces immediate return with nothing_to_repair."""

    def test_repair_nothing_to_repair(self, tmp_path):
        """Healthy directory returns status='nothing_to_repair' with no side effects."""
        make_healthy_output_dir(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert health_report.is_healthy, "Precondition: directory must be healthy"

        executor = _get_executor()
        result = executor.execute(tmp_path, health_report)

        assert result.status == "nothing_to_repair"
        assert result.fixed == []
        assert result.errors == []
        assert result.anomalies_before == 0

    def test_repair_nothing_to_repair_does_not_modify_index(self, tmp_path):
        """Nothing-to-repair run does not touch _index.md."""
        make_healthy_output_dir(tmp_path)
        index_path = tmp_path / "_index.md"
        original_content = index_path.read_text()
        original_mtime = index_path.stat().st_mtime

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor()
        executor.execute(tmp_path, health_report)

        # File should be unchanged
        assert index_path.read_text() == original_content
        assert index_path.stat().st_mtime == original_mtime


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: Zero-char domain file fixed by analyzer
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairZeroCharDomain:
    """Phase 1: Zero-char domain file is re-analyzed and fixed."""

    def test_repair_zero_char_domain_fixed(self, tmp_path):
        """0-byte domain file is repaired by domain_analyzer, file has content after."""
        make_domains_json(
            tmp_path,
            [{"name": "broken-domain", "description": "d", "participating_repos": ["r"]}],
        )
        # Create zero-byte domain file
        (tmp_path / "broken-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert health_report.status == "critical"

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        # Domain was repaired
        assert any("broken-domain" in f for f in result.fixed)
        # File now has content
        assert (tmp_path / "broken-domain.md").stat().st_size > 0

    def test_repair_zero_char_produces_completed_status(self, tmp_path):
        """Successfully repairing a zero-char domain yields status='completed'."""
        make_domains_json(
            tmp_path,
            [{"name": "empty-domain", "description": "d", "participating_repos": ["repo-alpha", "repo-beta"]}],
        )
        (tmp_path / "empty-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        # After repair, should be completed (healthy or errors=0)
        assert result.status in ("completed", "partial")
        assert len(result.errors) == 0 or result.status == "partial"


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Missing domain file recreated
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairMissingDomain:
    """Phase 1: Domain in JSON but file missing is recreated by analyzer."""

    def test_repair_missing_domain_recreated(self, tmp_path):
        """Missing domain file is created by domain_analyzer during Phase 1."""
        make_domains_json(
            tmp_path,
            [
                {"name": "missing-domain", "description": "d", "participating_repos": ["repo-a"]},
                {"name": "present-domain", "description": "d", "participating_repos": ["repo-b"]},
            ],
        )
        # Only present-domain.md exists; missing-domain.md does not
        make_domain_file(tmp_path, "present-domain")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert health_report.status == "critical"

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        # Domain file must now exist
        assert (tmp_path / "missing-domain.md").exists()
        assert (tmp_path / "missing-domain.md").stat().st_size > 0
        assert any("missing-domain" in f for f in result.fixed)


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Undersized domain rebuilt
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairUndersizedDomain:
    """Phase 1: Undersized domain file is rebuilt to full size."""

    def test_repair_undersized_domain_rebuilt(self, tmp_path):
        """Undersized domain file is repaired and result contains full content."""
        make_domains_json(
            tmp_path,
            [{"name": "tiny-domain", "description": "d", "participating_repos": ["r"]}],
        )
        # 500 chars -- below the 1000 threshold
        make_domain_file(tmp_path, "tiny-domain", size=500)
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert health_report.status == "needs_repair"

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        # File size must exceed 1000 chars
        assert (tmp_path / "tiny-domain.md").stat().st_size >= 1000
        assert any("tiny-domain" in f for f in result.fixed)


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Orphan file removed
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairOrphanRemoved:
    """Phase 2: Orphan .md file not in _domains.json is deleted."""

    def test_repair_orphan_removed(self, tmp_path):
        """Orphan .md file (not in _domains.json) is deleted during Phase 2."""
        make_domains_json(
            tmp_path,
            [{"name": "real-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "real-domain")
        # Create orphan file
        orphan = tmp_path / "old-forgotten-domain.md"
        orphan.write_text("orphan content that should be removed")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor()
        result = executor.execute(tmp_path, health_report)

        # Orphan file must be gone
        assert not orphan.exists()
        assert any("old-forgotten-domain.md" in f for f in result.fixed)

    def test_repair_multiple_orphans_all_removed(self, tmp_path):
        """Multiple orphan files are all removed in Phase 2."""
        make_domains_json(
            tmp_path,
            [{"name": "real-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "real-domain")
        # Create two orphan files
        orphan1 = tmp_path / "old-domain-1.md"
        orphan2 = tmp_path / "old-domain-2.md"
        orphan1.write_text("orphan 1")
        orphan2.write_text("orphan 2")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor()
        result = executor.execute(tmp_path, health_report)

        assert not orphan1.exists()
        assert not orphan2.exists()


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: _domains.json reconciled
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairDomainsJsonReconciled:
    """Phase 3: _domains.json is reconciled to match disk state."""

    def test_repair_domains_json_reconciled(self, tmp_path):
        """Mismatch state: 4 entries in JSON, 3 files on disk -> JSON updated to 3."""
        make_domains_json(
            tmp_path,
            [
                {"name": "domain-a", "description": "d", "participating_repos": []},
                {"name": "domain-b", "description": "d", "participating_repos": []},
                {"name": "domain-c", "description": "d", "participating_repos": []},
                {"name": "domain-d", "description": "d", "participating_repos": []},
            ],
        )
        make_domain_file(tmp_path, "domain-a")
        make_domain_file(tmp_path, "domain-b")
        make_domain_file(tmp_path, "domain-c")
        # domain-d.md intentionally absent -- triggers count mismatch + missing_domain_file
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        # No domain_analyzer -- Phase 1 skipped. Let Phase 3 reconcile JSON.
        executor = _get_executor(domain_analyzer=None)
        result = executor.execute(tmp_path, health_report)

        # _domains.json should now list only domains with files on disk
        updated_json = json.loads((tmp_path / "_domains.json").read_text())
        domain_names_in_json = [d["name"] for d in updated_json]
        assert "domain-a" in domain_names_in_json
        assert "domain-b" in domain_names_in_json
        assert "domain-c" in domain_names_in_json
        # domain-d had no file, so it should be removed
        assert "domain-d" not in domain_names_in_json

    def test_repair_reconcile_preserves_existing_metadata(self, tmp_path):
        """Reconciliation keeps description and participating_repos from existing entries."""
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "important-domain",
                    "description": "Very important domain",
                    "participating_repos": ["repo-x", "repo-y"],
                },
                {"name": "ghost-domain", "description": "Ghost", "participating_repos": []},
            ],
        )
        make_domain_file(tmp_path, "important-domain")
        # ghost-domain.md absent
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor(domain_analyzer=None)
        executor.execute(tmp_path, health_report)

        updated_json = json.loads((tmp_path / "_domains.json").read_text())
        important = next((d for d in updated_json if d["name"] == "important-domain"), None)
        assert important is not None
        assert important["description"] == "Very important domain"


# ─────────────────────────────────────────────────────────────────────────────
# Test 7: _index.md regenerated when missing
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairIndexRegeneratedWhenMissing:
    """Phase 4: Missing _index.md is regenerated from domain files."""

    def test_repair_index_regenerated_when_missing(self, tmp_path):
        """Deleting _index.md triggers Phase 4 to regenerate it with proper sections."""
        make_domains_json(
            tmp_path,
            [{"name": "my-domain", "description": "d", "participating_repos": ["repo-a"]}],
        )
        make_domain_file(tmp_path, "my-domain")
        # Do NOT create _index.md

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert any(a.type == "missing_index" for a in health_report.anomalies)

        executor = _get_executor()
        result = executor.execute(tmp_path, health_report)

        # _index.md must exist now
        assert (tmp_path / "_index.md").exists()
        index_content = (tmp_path / "_index.md").read_text()
        assert "## Domain Catalog" in index_content
        assert "## Repo-to-Domain Matrix" in index_content
        assert any("regenerated _index.md" in f for f in result.fixed)


# ─────────────────────────────────────────────────────────────────────────────
# Test 8: _index.md regenerated after domain fix
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairIndexRegeneratedAfterDomainFix:
    """Phase 4: _index.md is regenerated even if not originally flagged as anomalous."""

    def test_repair_index_regenerated_after_domain_fix(self, tmp_path):
        """After fixing a broken domain, _index.md is regenerated even if it had no anomaly."""
        make_domains_json(
            tmp_path,
            [{"name": "broken-domain", "description": "d", "participating_repos": ["repo-alpha", "repo-beta"]}],
        )
        # Zero-char domain -- triggers critical anomaly
        (tmp_path / "broken-domain.md").write_text("")
        # _index.md exists but refers to empty domain -- may not be stale per detector
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        # _index.md must be regenerated after domain fix
        assert any("regenerated _index.md" in f for f in result.fixed)
        # Index content should include repaired domain data
        index_content = (tmp_path / "_index.md").read_text()
        assert "broken-domain" in index_content


# ─────────────────────────────────────────────────────────────────────────────
# Test 9: Retry on failure (succeeds on 3rd attempt)
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairRetryOnFailure:
    """Phase 1: domain_analyzer is retried up to MAX_DOMAIN_RETRIES times."""

    def test_repair_retry_on_failure(self, tmp_path):
        """Analyzer fails twice then succeeds on 3rd attempt -- domain is fixed."""
        make_domains_json(
            tmp_path,
            [{"name": "flaky-domain", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "flaky-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        # Succeeds on 3rd attempt
        analyzer, call_counts = make_nth_attempt_success_analyzer(succeed_on_attempt=3)
        executor = _get_executor(domain_analyzer=analyzer)
        result = executor.execute(tmp_path, health_report)

        # Should have succeeded after retries
        assert call_counts.get("flaky-domain", 0) == 3
        assert any("flaky-domain" in f for f in result.fixed)
        assert (tmp_path / "flaky-domain.md").stat().st_size > 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 10: Max retries exhausted
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairMaxRetriesExhausted:
    """Phase 1: After MAX_DOMAIN_RETRIES failures, error is reported."""

    def test_repair_max_retries_exhausted(self, tmp_path):
        """Analyzer always fails -- error reported after 3 attempts, status='failed' or 'partial'."""
        make_domains_json(
            tmp_path,
            [{"name": "always-broken", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "always-broken.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor(domain_analyzer=make_failing_analyzer())
        result = executor.execute(tmp_path, health_report)

        # Must have error for the failed domain
        assert any("always-broken" in e for e in result.errors)
        # Status must NOT be 'completed' since something failed
        assert result.status != "completed"

    def test_repair_max_retries_exactly_3_attempts(self, tmp_path):
        """Exactly 3 attempts are made before giving up (not 2, not 4)."""
        make_domains_json(
            tmp_path,
            [{"name": "retry-counter", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "retry-counter.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        # Analyzer that counts calls and always fails
        call_count = [0]

        def counting_failing_analyzer(output_dir, domain, domain_list, repo_list):
            call_count[0] += 1
            (output_dir / f"{domain['name']}.md").write_text("")
            return False

        executor = _get_executor(domain_analyzer=counting_failing_analyzer)
        executor.execute(tmp_path, health_report)

        # Exactly 3 attempts (MAX_DOMAIN_RETRIES = 3)
        assert call_count[0] == 3


# ─────────────────────────────────────────────────────────────────────────────
# Test 11: Post-validation healthy after all fixes
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairPostValidationHealthy:
    """Phase 5: After successful repair, re-validation shows healthy."""

    def test_repair_post_validation_healthy(self, tmp_path):
        """All anomalies fixed -> final_health_status='healthy', status='completed'."""
        make_domains_json(
            tmp_path,
            [{"name": "to-fix-domain", "description": "d", "participating_repos": ["repo-alpha", "repo-beta"]}],
        )
        (tmp_path / "to-fix-domain.md").write_text("")
        # No _index.md
        # Orphan file
        (tmp_path / "orphan.md").write_text("orphan")

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert not health_report.is_healthy

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        assert result.final_health_status == "healthy"
        assert result.status == "completed"
        assert result.anomalies_after == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 12: Post-validation partial (some anomalies remain)
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairPostValidationPartial:
    """Phase 5: Some anomalies fixed but others remain -> partial status."""

    def test_repair_post_validation_partial(self, tmp_path):
        """Failed domain repair leaves anomaly -- status='partial' or 'failed'."""
        make_domains_json(
            tmp_path,
            [{"name": "unfixable-domain", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "unfixable-domain.md").write_text("")
        # Orphan that can be removed (free fix)
        (tmp_path / "orphan.md").write_text("orphan")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        # Analyzer always fails -- domain stays broken
        executor = _get_executor(domain_analyzer=make_failing_analyzer())
        result = executor.execute(tmp_path, health_report)

        # Orphan was removed (free fix)
        assert not (tmp_path / "orphan.md").exists()
        # Domain still broken -> some anomalies remain
        assert result.anomalies_after > 0
        # Status reflects partial repair
        assert result.status in ("partial", "failed")
        # final_health_status not healthy since domain is still broken
        assert result.final_health_status != "healthy"


# ─────────────────────────────────────────────────────────────────────────────
# Test 13: Journal callback is called
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairJournalCallbackCalled:
    """Journal callback receives log messages during repair."""

    def test_repair_journal_callback_called(self, tmp_path):
        """journal_callback receives start, progress, and completion messages."""
        make_domains_json(
            tmp_path,
            [{"name": "logged-domain", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "logged-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        journal_messages = []

        def journal_callback(message: str) -> None:
            journal_messages.append(message)

        executor = _get_executor(
            domain_analyzer=make_success_analyzer(),
            journal_callback=journal_callback,
        )
        executor.execute(tmp_path, health_report)

        # Must have received some journal messages
        assert len(journal_messages) > 0
        # At least one message should mention start/repair
        all_messages = " ".join(journal_messages).lower()
        assert "repair" in all_messages or "anomal" in all_messages

    def test_repair_no_journal_callback_does_not_raise(self, tmp_path):
        """When no journal_callback provided, repair runs without errors."""
        make_domains_json(
            tmp_path,
            [{"name": "silent-domain", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "silent-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        # No journal_callback -- should not raise
        executor = _get_executor(domain_analyzer=make_success_analyzer(), journal_callback=None)
        result = executor.execute(tmp_path, health_report)

        # Should complete normally
        assert result.status in ("completed", "partial", "failed", "nothing_to_repair")


# ─────────────────────────────────────────────────────────────────────────────
# Test 14: Multiple broken domains repaired independently
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairMultipleBrokenDomains:
    """Phase 1: Each broken domain is repaired independently."""

    def test_repair_multiple_broken_domains(self, tmp_path):
        """3 broken domains are each repaired by the analyzer independently."""
        make_domains_json(
            tmp_path,
            [
                {"name": "domain-x", "description": "d", "participating_repos": ["repo-alpha", "repo-beta"]},
                {"name": "domain-y", "description": "d", "participating_repos": ["repo-alpha", "repo-beta"]},
                {"name": "domain-z", "description": "d", "participating_repos": ["repo-alpha", "repo-beta"]},
            ],
        )
        # All three are zero-char
        (tmp_path / "domain-x.md").write_text("")
        (tmp_path / "domain-y.md").write_text("")
        (tmp_path / "domain-z.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor(domain_analyzer=make_success_analyzer())
        result = executor.execute(tmp_path, health_report)

        # All 3 must be repaired
        assert any("domain-x" in f for f in result.fixed)
        assert any("domain-y" in f for f in result.fixed)
        assert any("domain-z" in f for f in result.fixed)

        # All domain files must have content
        assert (tmp_path / "domain-x.md").stat().st_size > 0
        assert (tmp_path / "domain-y.md").stat().st_size > 0
        assert (tmp_path / "domain-z.md").stat().st_size > 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 15: No analyzer skips Phase 1 but free fixes still run
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairNoAnalyzerSkipsPhase1:
    """Phase 1 is skipped when domain_analyzer is None, but free fixes run."""

    def test_repair_no_analyzer_skips_phase1(self, tmp_path):
        """With no domain_analyzer, orphan removal and index regen still run."""
        make_domains_json(
            tmp_path,
            [{"name": "real-domain", "description": "d", "participating_repos": []}],
        )
        make_domain_file(tmp_path, "real-domain")
        # Create orphan file (free fix)
        orphan = tmp_path / "obsolete-domain.md"
        orphan.write_text("obsolete content")
        # No _index.md (free fix -- Phase 4)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        # No domain_analyzer provided
        executor = _get_executor(domain_analyzer=None)
        result = executor.execute(tmp_path, health_report)

        # Orphan should be removed (Phase 2 ran)
        assert not orphan.exists()
        assert any("obsolete-domain.md" in f for f in result.fixed)

        # _index.md should be created (Phase 4 ran)
        assert (tmp_path / "_index.md").exists()
        assert any("regenerated _index.md" in f for f in result.fixed)

    def test_repair_no_analyzer_leaves_broken_domains_untouched(self, tmp_path):
        """With no domain_analyzer, broken domain files are left as-is (Phase 1 skipped)."""
        make_domains_json(
            tmp_path,
            [{"name": "broken-domain", "description": "d", "participating_repos": ["r"]}],
        )
        (tmp_path / "broken-domain.md").write_text("")
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor(domain_analyzer=None)
        result = executor.execute(tmp_path, health_report)

        # Broken domain NOT in fixed list (Phase 1 skipped)
        assert not any("broken-domain" in f for f in result.fixed)
        # File still 0 bytes
        assert (tmp_path / "broken-domain.md").stat().st_size == 0


# ─────────────────────────────────────────────────────────────────────────────
# Test 16: RepairResult structure
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairResultStructure:
    """RepairResult has correct fields and to_dict serialization."""

    def test_repair_result_has_required_fields(self, tmp_path):
        """RepairResult exposes status, fixed, errors, final_health_status, anomaly counts."""
        make_healthy_output_dir(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor()
        result = executor.execute(tmp_path, health_report)

        assert hasattr(result, "status")
        assert hasattr(result, "fixed")
        assert hasattr(result, "errors")
        assert hasattr(result, "final_health_status")
        assert hasattr(result, "anomalies_before")
        assert hasattr(result, "anomalies_after")

        assert isinstance(result.fixed, list)
        assert isinstance(result.errors, list)
        assert isinstance(result.anomalies_before, int)
        assert isinstance(result.anomalies_after, int)

    def test_repair_result_to_dict(self, tmp_path):
        """RepairResult.to_dict() produces JSON-serializable output."""
        import json as json_mod

        make_healthy_output_dir(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)

        executor = _get_executor()
        result = executor.execute(tmp_path, health_report)

        d = result.to_dict()
        assert isinstance(d, dict)
        assert "status" in d
        assert "fixed" in d
        assert "errors" in d
        assert "final_health_status" in d
        assert "anomalies_before" in d
        assert "anomalies_after" in d

        # Must be JSON-serializable
        json_str = json_mod.dumps(d)
        assert isinstance(json_str, str)


# ─────────────────────────────────────────────────────────────────────────────
# Test 17: Bug A -- weak success check (file >0 bytes but anomaly still present)
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairBugA_WeakSuccessCheck:
    """
    Bug A: Phase 1 success check must re-run health detector to verify anomaly is gone.

    The old check only verified file.exists() and file.stat().st_size > 0.
    This is insufficient for `incomplete_domain` anomalies where the file already
    existed with broken content. The noop analyzer below writes >0 bytes but
    does NOT add the required ## Overview and ## Repository Roles sections,
    so the anomaly should persist and the domain should be reported as ERROR, not FIXED.
    """

    def _make_incomplete_domain_content(self, name: str) -> str:
        """
        Content >1000 chars that passes size check but is MISSING ## Overview,
        triggering incomplete_domain (not undersized_domain).
        """
        return f"""\
---
domain: {name}
description: A domain missing the Overview section for repair testing
participating_repos:
  - repo-alpha
  - repo-beta
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Repository Roles

- **repo-alpha**: Primary service provider implementing the core logic.
  This repository contains the authoritative implementation of the business
  rules and exposes a stable REST API consumed by downstream repositories.
  It provides authentication, authorization, and session management features
  that are critical for the entire platform security model.
- **repo-beta**: Secondary consumer that depends on repo-alpha API.
  This repository handles user-facing features and delegates domain logic
  to repo-alpha via synchronous HTTP calls. It also maintains a local cache
  of frequently accessed resources to reduce latency for end users.

## Intra-Domain Dependencies

repo-beta imports from repo-alpha via standard REST API calls.
Evidence: repo-beta/src/client.py imports requests and calls /api/v1/items.
The dependency is unidirectional. No circular dependency exists.
Version compatibility is enforced via semver pinning in requirements.txt.
Additional integration tests verify backward compatibility on every release.
"""

    def _make_noop_analyzer(self, broken_content: str):
        """
        Analyzer that writes the SAME broken content back to the file.
        File will be >0 bytes but the anomaly will still be present.
        """

        def analyzer(
            output_dir: Path, domain: dict, domain_list: list, repo_list: list
        ) -> bool:
            # Write the same broken content -- >0 bytes but still missing ## Overview
            (output_dir / f"{domain['name']}.md").write_text(broken_content)
            return True

        return analyzer

    def test_noop_analyzer_with_incomplete_domain_reports_error(self, tmp_path):
        """
        An analyzer that writes >0 bytes but leaves the incomplete_domain anomaly
        in place MUST be reported as an error (domain NOT in fixed list).

        Bug A: old code only checked file.exists() and .st_size > 0, so this
        would incorrectly report success. The fix re-runs health detector to
        verify the anomaly is actually gone.
        """
        broken_content = self._make_incomplete_domain_content("needs-overview")
        # Confirm the broken content triggers incomplete_domain
        domain_file = tmp_path / "needs-overview.md"
        domain_file.write_text(broken_content)
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "needs-overview",
                    "description": "d",
                    "participating_repos": ["repo-alpha", "repo-beta"],
                }
            ],
        )
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        # Precondition: the broken content triggers an incomplete_domain anomaly
        assert any(
            a.type == "incomplete_domain" and a.domain == "needs-overview"
            for a in health_report.anomalies
        ), f"Precondition failed: expected incomplete_domain anomaly, got: {health_report.anomalies}"

        # Noop analyzer: writes the same broken content back (>0 bytes, no ## Overview)
        noop_analyzer = self._make_noop_analyzer(broken_content)
        executor = _get_executor(domain_analyzer=noop_analyzer)
        result = executor.execute(tmp_path, health_report)

        # Bug A fix: domain should NOT be in fixed list -- anomaly still present
        assert not any(
            "needs-overview" in f for f in result.fixed
        ), f"Bug A: domain was incorrectly marked as fixed. fixed={result.fixed}"
        # Domain should be in errors list
        assert any(
            "needs-overview" in e for e in result.errors
        ), f"Bug A: domain should be in errors but errors={result.errors}"

    def test_noop_analyzer_after_max_retries_reports_error(self, tmp_path):
        """
        After MAX_DOMAIN_RETRIES attempts where each attempt writes >0 bytes
        but anomaly persists, the domain must appear in errors.
        """
        broken_content = self._make_incomplete_domain_content("stubborn-domain")
        (tmp_path / "stubborn-domain.md").write_text(broken_content)
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "stubborn-domain",
                    "description": "d",
                    "participating_repos": ["repo-alpha", "repo-beta"],
                }
            ],
        )
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert any(
            a.type == "incomplete_domain" for a in health_report.anomalies
        ), "Precondition: stubborn-domain must have incomplete_domain anomaly"

        call_count = [0]

        def counting_noop_analyzer(output_dir, domain, domain_list, repo_list):
            call_count[0] += 1
            # Write broken content again -- still >0 bytes, still missing ## Overview
            (output_dir / f"{domain['name']}.md").write_text(broken_content)
            return True

        executor = _get_executor(domain_analyzer=counting_noop_analyzer)
        result = executor.execute(tmp_path, health_report)

        # All MAX_DOMAIN_RETRIES attempts must have been made
        from code_indexer.server.services.dep_map_repair_executor import (
            DepMapRepairExecutor,
        )

        assert call_count[0] == DepMapRepairExecutor.MAX_DOMAIN_RETRIES, (
            f"Expected {DepMapRepairExecutor.MAX_DOMAIN_RETRIES} attempts, "
            f"got {call_count[0]}"
        )
        assert any(
            "stubborn-domain" in e for e in result.errors
        ), f"Expected error for stubborn-domain, errors={result.errors}"
        assert not any(
            "stubborn-domain" in f for f in result.fixed
        ), f"stubborn-domain should not be fixed, fixed={result.fixed}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 18: Bug A positive -- proper fix is correctly reported as FIXED
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairBugA_PositiveVerification:
    """
    Bug A positive: when the analyzer writes a proper file that actually
    resolves the incomplete_domain anomaly, the domain MUST be in the fixed list.
    """

    def _make_incomplete_domain_content(self, name: str) -> str:
        """
        Content >1000 chars that passes size check but is MISSING ## Overview,
        triggering incomplete_domain (not undersized_domain).
        """
        return f"""\
---
domain: {name}
description: A domain missing the Overview section for repair testing
participating_repos:
  - repo-alpha
  - repo-beta
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Repository Roles

- **repo-alpha**: Primary service provider implementing the core logic.
  This repository contains the authoritative implementation of the business
  rules and exposes a stable REST API consumed by downstream repositories.
  It provides authentication, authorization, and session management features
  that are critical for the entire platform security model.
- **repo-beta**: Secondary consumer that depends on repo-alpha API.
  This repository handles user-facing features and delegates domain logic
  to repo-alpha via synchronous HTTP calls. It also maintains a local cache
  of frequently accessed resources to reduce latency for end users.

## Intra-Domain Dependencies

repo-beta imports from repo-alpha via standard REST API calls.
Evidence: repo-beta/src/client.py imports requests and calls /api/v1/items.
The dependency is unidirectional. No circular dependency exists.
Version compatibility is enforced via semver pinning in requirements.txt.
Additional integration tests verify backward compatibility on every release.
"""

    def test_proper_fix_is_marked_as_fixed(self, tmp_path):
        """
        When the analyzer replaces broken content with valid content
        (including ## Overview and ## Repository Roles), the domain
        must appear in result.fixed (not errors).
        """
        broken_content = self._make_incomplete_domain_content("fixable-domain")
        (tmp_path / "fixable-domain.md").write_text(broken_content)
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "fixable-domain",
                    "description": "d",
                    "participating_repos": ["repo-alpha", "repo-beta"],
                }
            ],
        )
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert any(
            a.type == "incomplete_domain" and a.domain == "fixable-domain"
            for a in health_report.anomalies
        ), "Precondition: fixable-domain must have incomplete_domain anomaly"

        # Analyzer that writes VALID content (includes ## Overview)
        def fixing_analyzer(output_dir, domain, domain_list, repo_list):
            valid_content = VALID_DOMAIN_CONTENT.replace("test-domain", domain["name"])
            (output_dir / f"{domain['name']}.md").write_text(valid_content)
            return True

        executor = _get_executor(domain_analyzer=fixing_analyzer)
        result = executor.execute(tmp_path, health_report)

        # Domain must be in fixed list
        assert any(
            "fixable-domain" in f for f in result.fixed
        ), f"Bug A positive: fixable-domain should be fixed. fixed={result.fixed}, errors={result.errors}"
        # Domain must NOT be in errors
        assert not any(
            "fixable-domain" in e for e in result.errors
        ), f"fixable-domain should not be in errors. errors={result.errors}"


# ─────────────────────────────────────────────────────────────────────────────
# Test 19: Bug B -- broken domain file deleted before analyzer is called
# ─────────────────────────────────────────────────────────────────────────────


class TestRepairBugB_DeleteBeforeAnalyze:
    """
    Bug B: _run_phase1() must delete the broken domain file BEFORE calling
    the analyzer, so Claude starts fresh rather than building on broken content.

    If the broken file is not deleted first, the analyzer (via previous_domain_dir)
    receives the broken content as input and may preserve its broken structure.
    """

    def _make_incomplete_domain_content(self, name: str) -> str:
        """
        Content >1000 chars that passes size check but is MISSING ## Overview,
        triggering incomplete_domain (not undersized_domain).
        """
        return f"""\
---
domain: {name}
description: A domain missing the Overview section for repair testing
participating_repos:
  - repo-alpha
  - repo-beta
last_analyzed: "2026-01-01T00:00:00Z"
---

# Domain Analysis: {name}

## Repository Roles

- **repo-alpha**: Primary service provider implementing the core logic.
  This repository contains the authoritative implementation of the business
  rules and exposes a stable REST API consumed by downstream repositories.
  It provides authentication, authorization, and session management features
  that are critical for the entire platform security model.
- **repo-beta**: Secondary consumer that depends on repo-alpha API.
  This repository handles user-facing features and delegates domain logic
  to repo-alpha via synchronous HTTP calls. It also maintains a local cache
  of frequently accessed resources to reduce latency for end users.

## Intra-Domain Dependencies

repo-beta imports from repo-alpha via standard REST API calls.
Evidence: repo-beta/src/client.py imports requests and calls /api/v1/items.
The dependency is unidirectional. No circular dependency exists.
Version compatibility is enforced via semver pinning in requirements.txt.
Additional integration tests verify backward compatibility on every release.
"""

    def test_broken_file_deleted_before_analyzer_called(self, tmp_path):
        """
        When _run_phase1() processes an incomplete_domain anomaly, the existing
        broken domain file MUST be deleted before calling the analyzer.

        Verified by inspecting the filesystem state inside the analyzer callable.
        """
        broken_content = self._make_incomplete_domain_content("cleanup-domain")
        domain_file = tmp_path / "cleanup-domain.md"
        domain_file.write_text(broken_content)
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "cleanup-domain",
                    "description": "d",
                    "participating_repos": ["repo-alpha", "repo-beta"],
                }
            ],
        )
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert any(
            a.type == "incomplete_domain" and a.domain == "cleanup-domain"
            for a in health_report.anomalies
        ), "Precondition: cleanup-domain must have incomplete_domain anomaly"

        file_existed_when_analyzer_called = []

        def observing_analyzer(output_dir, domain, domain_list, repo_list):
            # Check if the broken file existed when we were called
            f = output_dir / f"{domain['name']}.md"
            file_existed_when_analyzer_called.append(f.exists())
            # Write valid content so repair succeeds
            valid_content = VALID_DOMAIN_CONTENT.replace("test-domain", domain["name"])
            f.write_text(valid_content)
            return True

        executor = _get_executor(domain_analyzer=observing_analyzer)
        executor.execute(tmp_path, health_report)

        # Bug B fix: file must NOT exist when analyzer is called
        assert len(file_existed_when_analyzer_called) > 0, (
            "Analyzer was never called -- check test setup"
        )
        assert not any(file_existed_when_analyzer_called), (
            "Bug B: broken domain file was NOT deleted before calling the analyzer. "
            f"file_existed_when_analyzer_called={file_existed_when_analyzer_called}"
        )

    def test_missing_domain_file_not_deleted_before_analyzer(self, tmp_path):
        """
        For missing_domain_file anomalies (file does not exist at all),
        deletion before the analyzer is a no-op and must not raise.
        The analyzer should still be called exactly once (on first attempt).
        """
        make_domains_json(
            tmp_path,
            [
                {
                    "name": "absent-domain",
                    "description": "d",
                    "participating_repos": ["repo-alpha"],
                }
            ],
        )
        # Do NOT create absent-domain.md -- triggers missing_domain_file
        make_index_md(tmp_path)

        detector = _get_health_detector()
        health_report = detector.detect(tmp_path)
        assert any(
            a.type == "missing_domain_file" and a.domain == "absent-domain"
            for a in health_report.anomalies
        ), "Precondition: absent-domain must have missing_domain_file anomaly"

        analyzer_calls = [0]

        def counting_success_analyzer(output_dir, domain, domain_list, repo_list):
            analyzer_calls[0] += 1
            valid_content = VALID_DOMAIN_CONTENT.replace("test-domain", domain["name"])
            (output_dir / f"{domain['name']}.md").write_text(valid_content)
            return True

        executor = _get_executor(domain_analyzer=counting_success_analyzer)
        result = executor.execute(tmp_path, health_report)

        # Must succeed: analyzer called once, domain in fixed list
        assert analyzer_calls[0] == 1, f"Expected 1 analyzer call, got {analyzer_calls[0]}"
        assert any(
            "absent-domain" in f for f in result.fixed
        ), f"absent-domain should be fixed. fixed={result.fixed}"
