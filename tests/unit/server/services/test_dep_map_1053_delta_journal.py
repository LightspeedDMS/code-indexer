"""
Unit tests for Story #1053: Resumable Delta Dep-Map Analysis via Per-Domain Frontmatter Journal.

Covers Scenarios 1-15 (unit and integration).
Scenario 16 (E2E) is exercised via helper scripts in tests/e2e/manual/.

Test mapping:
  Scenario 1  -> test_skip_domain_when_fingerprint_matches, test_integration_resume_skips_marked_domains
  Scenario 2  -> test_changed_delta_set_invalidates_fingerprint
  Scenario 3  -> test_all_new_repos_covered_skips_discovery
  Scenario 4  -> test_partial_coverage_reruns_discovery
  Scenario 5  -> test_cancellation_leaves_frontmatter_intact
  Scenario 6  -> test_domain_without_frontmatter_is_processed
  Scenario 7  -> test_write_atomic_failure_leaves_target_unchanged
  Scenario 8  -> test_operator_frontmatter_fields_preserved
  Scenario 9  -> test_double_frontmatter_prevention
  Scenario 10 -> test_corrupted_domains_json_triggers_rediscovery
  Scenario 11 -> test_concurrent_lock_rejection
  Scenario 12 -> test_malformed_yaml_emits_warning_and_treats_as_no_frontmatter
  Scenario 13 -> test_wrong_shape_domains_json_triggers_rediscovery
  Scenario 14 -> test_empty_claude_response_no_write_no_frontmatter_update
  Scenario 15 -> test_missing_domain_file_triggers_synthetic_creation
"""

import json
import logging
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.services.dep_map_delta_journal import (
    all_new_repos_have_domain_assignments,
    compute_delta_fingerprint,
    parse_frontmatter,
    render_md,
    write_atomic,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_repo(alias: str) -> dict:
    return {"alias": alias, "clone_path": f"/repos/{alias}"}


def _make_domains_json(entries: list, path: Path) -> None:
    path.write_text(json.dumps(entries))


# ---------------------------------------------------------------------------
# compute_delta_fingerprint (Scenarios 1, 2)
# ---------------------------------------------------------------------------


class TestComputeDeltaFingerprint:
    """Unit tests for compute_delta_fingerprint."""

    def test_same_inputs_produce_same_fingerprint(self):
        """Same changed/new/removed inputs always produce the same hash."""
        changed = [_make_repo("repo-a"), _make_repo("repo-b")]
        new = [_make_repo("repo-c")]
        removed = ["repo-d"]

        fp1 = compute_delta_fingerprint(changed, new, removed)
        fp2 = compute_delta_fingerprint(changed, new, removed)
        assert fp1 == fp2

    def test_different_inputs_produce_different_fingerprint(self):
        """Adding one more changed repo changes the fingerprint."""
        changed = [_make_repo("repo-a")]
        new: list = []
        removed: list = []

        fp1 = compute_delta_fingerprint(changed, new, removed)
        fp2 = compute_delta_fingerprint(changed + [_make_repo("repo-b")], new, removed)
        assert fp1 != fp2

    def test_order_independent_within_each_list(self):
        """Fingerprint is the same regardless of list order within each category."""
        changed_a = [_make_repo("alpha"), _make_repo("beta")]
        changed_b = [_make_repo("beta"), _make_repo("alpha")]
        new: list = []
        removed: list = []

        fp1 = compute_delta_fingerprint(changed_a, new, removed)
        fp2 = compute_delta_fingerprint(changed_b, new, removed)
        assert fp1 == fp2

    def test_empty_inputs_produce_stable_fingerprint(self):
        """Empty inputs produce a deterministic fingerprint."""
        fp = compute_delta_fingerprint([], [], [])
        assert isinstance(fp, str)
        assert len(fp) == 64  # sha256 hex digest

    def test_fingerprint_changes_when_new_repos_differ(self):
        """New repo set change changes the fingerprint."""
        fp1 = compute_delta_fingerprint([], [_make_repo("x")], [])
        fp2 = compute_delta_fingerprint([], [_make_repo("y")], [])
        assert fp1 != fp2

    def test_fingerprint_changes_when_removed_repos_differ(self):
        """Removed repo list change changes the fingerprint."""
        fp1 = compute_delta_fingerprint([], [], ["old-a"])
        fp2 = compute_delta_fingerprint([], [], ["old-b"])
        assert fp1 != fp2

    def test_returns_64_char_hex_string(self):
        """Return value is a 64-character hex string (sha256)."""
        fp = compute_delta_fingerprint([_make_repo("a")], [_make_repo("b")], ["c"])
        assert len(fp) == 64
        assert all(c in "0123456789abcdef" for c in fp)


# ---------------------------------------------------------------------------
# parse_frontmatter (Scenarios 6, 12)
# ---------------------------------------------------------------------------


class TestParseFrontmatter:
    """Unit tests for parse_frontmatter."""

    def test_no_frontmatter_returns_empty_dict_and_full_body(self):
        """Text without frontmatter returns ({}, full_text)."""
        text = "# Domain Analysis\n\nSome content here.\n"
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_with_valid_frontmatter_returns_parsed_dict_and_body(self):
        """Text with valid YAML frontmatter returns parsed dict and remaining body."""
        text = (
            "---\n"
            "domain: services\n"
            "last_delta_applied: abc123\n"
            "---\n\n"
            "# Services Domain\n"
        )
        fm, body = parse_frontmatter(text)
        assert fm == {"domain": "services", "last_delta_applied": "abc123"}
        assert body == "# Services Domain\n"

    def test_malformed_yaml_returns_empty_dict_full_body(self):
        """Malformed YAML frontmatter falls back to ({}, full_md_text)."""
        text = "---\ndomain: services\nbad_key: [unclosed\n---\n\n# Body\n"
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_malformed_yaml_emits_warning_log(self, caplog):
        """Malformed YAML frontmatter emits a WARNING log (Scenario 12)."""
        text = "---\ndomain: services\nbad_key: [unclosed\n---\n\n# Body\n"
        with caplog.at_level(logging.WARNING):
            parse_frontmatter(text, domain_hint="services")

        assert any(
            "services" in r.message or "WARNING" in r.levelname or "YAML" in r.message
            for r in caplog.records
            if r.levelno >= logging.WARNING
        )

    def test_only_opening_delimiter_treated_as_no_frontmatter(self):
        """A lone '---' at start without closing delimiter is treated as no frontmatter."""
        text = "---\nsome: yaml\nno closing\n"
        fm, body = parse_frontmatter(text)
        assert fm == {}
        assert body == text

    def test_empty_string_returns_empty_dict_and_empty_body(self):
        """Empty string returns ({}, '')."""
        fm, body = parse_frontmatter("")
        assert fm == {}
        assert body == ""

    def test_frontmatter_with_operator_fields(self):
        """Operator fields like 'owner' are preserved in the parsed dict."""
        text = (
            "---\n"
            "domain: services\n"
            "owner: platform-team\n"
            "review_required: true\n"
            "---\n\n"
            "Body here.\n"
        )
        fm, body = parse_frontmatter(text)
        assert fm["owner"] == "platform-team"
        assert fm["review_required"] is True
        assert body == "Body here.\n"


# ---------------------------------------------------------------------------
# render_md (round-trip with parse_frontmatter)
# ---------------------------------------------------------------------------


class TestRenderMd:
    """Unit tests for render_md."""

    def test_render_produces_valid_frontmatter_block(self):
        """render_md produces text that parse_frontmatter can re-parse correctly."""
        fm = {"domain": "api", "last_delta_applied": "fp123"}
        body = "# API Domain\n"
        rendered = render_md(fm, body)
        reparsed_fm, reparsed_body = parse_frontmatter(rendered)
        assert reparsed_fm["domain"] == "api"
        assert reparsed_fm["last_delta_applied"] == "fp123"
        assert reparsed_body == body

    def test_render_exactly_two_delimiter_lines(self):
        """render_md result has exactly two '---' delimiter lines (one YAML block)."""
        fm = {"domain": "x"}
        rendered = render_md(fm, "body\n")
        delimiter_count = rendered.split("\n").count("---")
        assert delimiter_count == 2

    def test_render_empty_frontmatter(self):
        """render_md with empty frontmatter dict still produces valid output."""
        rendered = render_md({}, "body\n")
        fm, body = parse_frontmatter(rendered)
        assert fm == {}
        assert body == "body\n"


# ---------------------------------------------------------------------------
# write_atomic (Scenario 7)
# ---------------------------------------------------------------------------


class TestWriteAtomic:
    """Unit tests for write_atomic — atomicity invariant."""

    def test_successful_write_creates_target(self, tmp_path):
        """write_atomic creates the target file with correct content."""
        target = tmp_path / "services.md"
        write_atomic(target, "new content\n")
        assert target.read_text() == "new content\n"

    def test_os_replace_failure_leaves_target_unchanged(self, tmp_path):
        """If os.replace raises, the original target is byte-equal to its pre-write state."""
        target = tmp_path / "services.md"
        pre_write_content = "pre-delta content\n"
        target.write_text(pre_write_content)

        with patch("os.replace", side_effect=OSError("simulated failure")):
            with pytest.raises(OSError):
                write_atomic(target, "new delta content\n")

        assert target.read_text() == pre_write_content

    def test_os_replace_failure_no_orphaned_tmp_at_target_path(self, tmp_path):
        """If os.replace raises, no orphaned file lands at the exact target path."""
        target = tmp_path / "services.md"
        target.write_text("original\n")

        with patch("os.replace", side_effect=OSError("simulated failure")):
            with pytest.raises(OSError):
                write_atomic(target, "new\n")

        # The target path must remain unchanged
        assert target.read_text() == "original\n"

    def test_overwrite_existing_file(self, tmp_path):
        """write_atomic overwrites an existing file atomically."""
        target = tmp_path / "domain.md"
        target.write_text("old\n")
        write_atomic(target, "new\n")
        assert target.read_text() == "new\n"


# ---------------------------------------------------------------------------
# all_new_repos_have_domain_assignments (Scenarios 3, 4, 10, 13)
# ---------------------------------------------------------------------------


class TestAllNewReposHaveDomainAssignments:
    """Unit tests for all_new_repos_have_domain_assignments."""

    def test_returns_false_when_domains_json_missing(self, tmp_path):
        """Returns False if _domains.json does not exist."""
        new_repos = [_make_repo("new-a")]
        assert (
            all_new_repos_have_domain_assignments(new_repos, tmp_path / "_domains.json")
            is False
        )

    def test_returns_true_when_all_new_repos_assigned(self, tmp_path):
        """Returns True when all new repo aliases appear in _domains.json members."""
        domains = [
            {"name": "web", "participating_repos": ["new-a", "new-b"]},
        ]
        path = tmp_path / "_domains.json"
        _make_domains_json(domains, path)
        new_repos = [_make_repo("new-a"), _make_repo("new-b")]
        assert all_new_repos_have_domain_assignments(new_repos, path) is True

    def test_returns_false_when_partial_coverage(self, tmp_path):
        """Returns False when only some new repos have domain assignments."""
        domains = [
            {"name": "web", "participating_repos": ["new-a"]},
        ]
        path = tmp_path / "_domains.json"
        _make_domains_json(domains, path)
        new_repos = [_make_repo("new-a"), _make_repo("new-b")]
        assert all_new_repos_have_domain_assignments(new_repos, path) is False

    def test_returns_false_for_corrupted_json(self, tmp_path):
        """Returns False (not raises) when _domains.json contains invalid JSON (Scenario 10)."""
        path = tmp_path / "_domains.json"
        path.write_text('{"key": [truncated')
        new_repos = [_make_repo("new-a")]
        assert all_new_repos_have_domain_assignments(new_repos, path) is False

    def test_returns_false_for_null_json(self, tmp_path):
        """Returns False when _domains.json contains null (wrong shape) (Scenario 13)."""
        path = tmp_path / "_domains.json"
        path.write_text("null")
        new_repos = [_make_repo("new-a")]
        assert all_new_repos_have_domain_assignments(new_repos, path) is False

    def test_returns_false_for_integer_json(self, tmp_path):
        """Returns False when _domains.json is an integer (wrong shape) (Scenario 13)."""
        path = tmp_path / "_domains.json"
        path.write_text("42")
        new_repos = [_make_repo("new-a")]
        assert all_new_repos_have_domain_assignments(new_repos, path) is False

    def test_returns_false_for_string_json(self, tmp_path):
        """Returns False when _domains.json is a bare string (wrong shape) (Scenario 13)."""
        path = tmp_path / "_domains.json"
        path.write_text('"just a string"')
        new_repos = [_make_repo("new-a")]
        assert all_new_repos_have_domain_assignments(new_repos, path) is False

    def test_returns_true_for_empty_new_repos(self, tmp_path):
        """Returns True when new_repos is empty (vacuously all covered)."""
        path = tmp_path / "_domains.json"
        _make_domains_json([], path)
        assert all_new_repos_have_domain_assignments([], path) is True

    def test_handles_dict_shaped_domains_json(self, tmp_path):
        """Handles the dict-shaped _domains.json (alternate shape with values as entries)."""
        # dict: "domain_name" -> {"participating_repos": [...]}
        domains_dict = {
            "web": {"participating_repos": ["new-a"]},
        }
        path = tmp_path / "_domains.json"
        path.write_text(json.dumps(domains_dict))
        new_repos = [_make_repo("new-a")]
        # dict is a valid shape — function must not crash (returns True if all covered)
        result = all_new_repos_have_domain_assignments(new_repos, path)
        assert isinstance(result, bool)


# ---------------------------------------------------------------------------
# Integration tests: per-domain skip logic (Scenarios 1, 5, 6, 8, 9, 14, 15)
# These import DependencyMapService and exercise _update_affected_domains
# with mocked Claude responses and real filesystem.
# ---------------------------------------------------------------------------


def _make_service_for_delta(tmp_path: Path, invoke_result: str = "Updated body\n"):
    """
    Build a DependencyMapService configured for delta journal tests.

    All external I/O is mocked except the real filesystem (tmp_path).
    invoke_delta_merge_file returns invoke_result by default.
    """
    from code_indexer.server.services.dependency_map_service import DependencyMapService
    from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

    config = ClaudeIntegrationConfig(
        dependency_map_enabled=True,
        dependency_map_interval_hours=24,
        dependency_map_pass_timeout_seconds=300,
        dependency_map_delta_max_turns=30,
    )
    config_manager = Mock()
    config_manager.get_claude_integration_config.return_value = config

    golden_repos_dir = str(tmp_path / "golden-repos")
    Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

    golden_repos_manager = Mock()
    golden_repos_manager.golden_repos_dir = golden_repos_dir

    tracking_backend = Mock()
    tracking_backend.get_tracking.return_value = {
        "id": 1,
        "last_run": None,
        "next_run": None,
        "status": "pending",
        "commit_hashes": "{}",
        "error_message": None,
        "refinement_cursor": 0,
        "refinement_next_run": None,
    }
    tracking_backend.update_tracking = Mock()

    analyzer = Mock()
    analyzer.build_delta_merge_prompt.return_value = "mock prompt"
    analyzer.invoke_delta_merge_file.return_value = invoke_result
    analyzer.generate_orientation_files.return_value = None

    svc = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
    )
    return svc, config


class TestPerDomainSkipLogic:
    """Integration tests for the frontmatter-based per-domain skip logic."""

    def test_domain_with_matching_fingerprint_is_skipped(self, tmp_path):
        """Scenario 1 core: domain file with matching last_delta_applied is skipped."""
        svc, config = _make_service_for_delta(tmp_path)
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("repo-a")], [], [])

        # Pre-write domain file with matching frontmatter
        domain_file = dep_map_dir / "services.md"
        domain_file.write_text(
            f"---\ndomain: services\nlast_delta_applied: {fingerprint}\n---\n\n# Services\n"
        )

        errors = svc._update_affected_domains(
            affected_domains={"services"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("repo-a")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fingerprint,
        )

        assert errors == []
        # Claude was NOT invoked for this domain
        svc._analyzer.invoke_delta_merge_file.assert_not_called()

    def test_domain_without_fingerprint_is_processed(self, tmp_path):
        """Scenario 6: domain file without last_delta_applied frontmatter is processed."""
        svc, config = _make_service_for_delta(tmp_path, invoke_result="New body\n")
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("repo-a")], [], [])

        # Domain file without frontmatter
        domain_file = dep_map_dir / "services.md"
        domain_file.write_text("# Services\n\nExisting body.\n")

        errors = svc._update_affected_domains(
            affected_domains={"services"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("repo-a")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fingerprint,
        )

        assert errors == []
        svc._analyzer.invoke_delta_merge_file.assert_called_once()

        # File must now have frontmatter with current fingerprint
        updated = domain_file.read_text()
        fm, _ = parse_frontmatter(updated)
        assert fm.get("last_delta_applied") == fingerprint

    def test_operator_fields_preserved_across_delta(self, tmp_path):
        """Scenario 8: operator-added frontmatter fields survive a delta update."""
        svc, config = _make_service_for_delta(tmp_path, invoke_result="New body\n")
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("r")], [], [])

        domain_file = dep_map_dir / "services.md"
        domain_file.write_text(
            "---\n"
            "domain: services\n"
            "owner: platform-team\n"
            "review_required: true\n"
            "---\n\n"
            "Pre-delta body.\n"
        )

        svc._update_affected_domains(
            affected_domains={"services"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("r")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fingerprint,
        )

        fm, _ = parse_frontmatter(domain_file.read_text())
        assert fm.get("last_delta_applied") == fingerprint
        assert fm.get("owner") == "platform-team"
        assert fm.get("review_required") is True

    def test_double_frontmatter_prevention(self, tmp_path):
        """Scenario 9: Claude echoing frontmatter in raw output results in exactly one block."""
        fp_old = "old_fingerprint_abc"
        fp_new = compute_delta_fingerprint([_make_repo("r")], [], [])

        # Claude returns output that INCLUDES a frontmatter block (simulating echo-back)
        claude_echo_output = (
            "---\n"
            f"domain: services\n"
            f"last_delta_applied: {fp_old}\n"
            "last_applied_at: 2026-06-01T00:00:00+00:00\n"
            "owner: platform-team\n"
            "---\n\n"
            "Claude's updated body content.\n"
        )

        svc, config = _make_service_for_delta(
            tmp_path, invoke_result=claude_echo_output
        )
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        domain_file = dep_map_dir / "services.md"
        domain_file.write_text(
            "---\n"
            f"domain: services\n"
            f"last_delta_applied: {fp_old}\n"
            "owner: platform-team\n"
            "---\n\n"
            "Pre-delta body.\n"
        )

        svc._update_affected_domains(
            affected_domains={"services"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("r")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fp_new,
        )

        result = domain_file.read_text()
        # Exactly two '---' delimiter lines bracketing exactly one YAML block
        lines = result.split("\n")
        delimiter_count = lines.count("---")
        assert delimiter_count == 2, f"Expected 2 delimiters, got {delimiter_count}"

        fm, body = parse_frontmatter(result)
        assert fm.get("last_delta_applied") == fp_new
        assert fm.get("owner") == "platform-team"
        assert "Claude's updated body content." in body

    def test_empty_claude_response_no_write_no_frontmatter_update(
        self, tmp_path, caplog
    ):
        """Scenario 14: empty/whitespace Claude response does not update the file."""
        svc, config = _make_service_for_delta(tmp_path, invoke_result="   \n\n   ")
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("r")], [], [])

        domain_file = dep_map_dir / "services.md"
        original_content = "---\ndomain: services\n---\n\nPre-delta body.\n"
        domain_file.write_text(original_content)

        with caplog.at_level(logging.ERROR):
            svc._update_affected_domains(
                affected_domains={"services"},
                dependency_map_dir=dep_map_dir,
                changed_repos=[_make_repo("r")],
                new_repos=[],
                removed_repos=[],
                config=config,
                fingerprint=fingerprint,
            )

        # File must remain unchanged (no frontmatter update, no content update)
        assert domain_file.read_text() == original_content

        # On a subsequent run, the domain should still be processed (not skipped)
        svc._analyzer.invoke_delta_merge_file.reset_mock()
        svc._analyzer.invoke_delta_merge_file.return_value = "Real body content\n"
        svc._update_affected_domains(
            affected_domains={"services"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("r")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fingerprint,
        )
        svc._analyzer.invoke_delta_merge_file.assert_called()

    def test_missing_domain_file_triggers_synthetic_creation(self, tmp_path, caplog):
        """Scenario 15: missing domain file is created with frontmatter and Claude's body."""
        svc, config = _make_service_for_delta(
            tmp_path, invoke_result="Synthetic body.\n"
        )
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("r")], [], [])
        domain_file = dep_map_dir / "ghosts.md"
        assert not domain_file.exists()

        with caplog.at_level(logging.INFO):
            svc._update_affected_domains(
                affected_domains={"ghosts"},
                dependency_map_dir=dep_map_dir,
                changed_repos=[_make_repo("r")],
                new_repos=[],
                removed_repos=[],
                config=config,
                fingerprint=fingerprint,
            )

        assert domain_file.exists()
        fm, body = parse_frontmatter(domain_file.read_text())
        assert fm.get("last_delta_applied") == fingerprint
        assert "Synthetic body." in body

        # Log must indicate synthetic creation
        assert any(
            "ghosts" in r.message
            and ("synthetic" in r.message.lower() or "missing" in r.message.lower())
            for r in caplog.records
        )


class TestCancellationLeavesIntact:
    """Scenario 5: cancellation leaves already-written frontmatter intact."""

    def test_cancel_mid_loop_preserves_written_frontmatter(self, tmp_path):
        """After cancel, domains already processed retain their frontmatter."""
        svc, config = _make_service_for_delta(tmp_path, invoke_result="New body\n")
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("r")], [], [])

        # Create two domains
        domain_a = dep_map_dir / "alpha.md"
        domain_b = dep_map_dir / "beta.md"
        domain_a.write_text("# Alpha\n")
        domain_b.write_text("# Beta\n")

        # Set cancel event AFTER first domain call to simulate mid-loop kill
        invoke_call_count = 0

        def invoke_side_effect(*args, **kwargs):
            nonlocal invoke_call_count
            invoke_call_count += 1
            if invoke_call_count >= 1:
                svc._cancel_event.set()
            return "New body\n"

        svc._analyzer.invoke_delta_merge_file.side_effect = invoke_side_effect

        svc._update_affected_domains(
            affected_domains={"alpha", "beta"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("r")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fingerprint,
        )

        # At least one domain must have received frontmatter
        alpha_text = domain_a.read_text()
        beta_text = domain_b.read_text()
        processed_texts = [alpha_text, beta_text]
        has_frontmatter = [
            parse_frontmatter(t)[0].get("last_delta_applied") == fingerprint
            for t in processed_texts
        ]
        # At least one was written (the one processed before cancel)
        assert any(has_frontmatter), (
            "Expected at least one domain to have received frontmatter"
        )


class TestResumeSkipsMarkedDomains:
    """Scenario 1 integration: re-run skips domains that already have matching frontmatter."""

    def test_rerun_skips_frontmatter_marked_domains(self, tmp_path):
        """Second run skips all domains marked with current fingerprint."""
        svc, config = _make_service_for_delta(tmp_path, invoke_result="New body\n")
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fingerprint = compute_delta_fingerprint([_make_repo("repo-a")], [], [])

        # Create 3 domains; 2 already marked with current fingerprint
        for name in ["dom-a", "dom-b"]:
            (dep_map_dir / f"{name}.md").write_text(
                f"---\ndomain: {name}\nlast_delta_applied: {fingerprint}\n---\n\n# {name}\n"
            )
        (dep_map_dir / "dom-c.md").write_text("# dom-c\nNo frontmatter.\n")

        svc._update_affected_domains(
            affected_domains={"dom-a", "dom-b", "dom-c"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("repo-a")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fingerprint,
        )

        # Only dom-c should have been processed by Claude
        assert svc._analyzer.invoke_delta_merge_file.call_count == 1

        # All three must end with correct fingerprint
        for name in ["dom-a", "dom-b", "dom-c"]:
            fm, _ = parse_frontmatter((dep_map_dir / f"{name}.md").read_text())
            assert fm.get("last_delta_applied") == fingerprint, (
                f"{name} missing fingerprint"
            )


class TestFingerprintInvalidation:
    """Scenario 2: changed delta set invalidates old fingerprint."""

    def test_new_fingerprint_does_not_skip_old_marked_domains(self, tmp_path):
        """Domains marked with old fingerprint are NOT skipped when delta set changes."""
        svc, config = _make_service_for_delta(tmp_path, invoke_result="New body\n")
        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)

        fp_old = compute_delta_fingerprint([_make_repo("repo-a")], [], [])
        fp_new = compute_delta_fingerprint(
            [_make_repo("repo-a"), _make_repo("repo-foo")], [], []
        )
        assert fp_old != fp_new

        # Domain marked with old fingerprint
        (dep_map_dir / "services.md").write_text(
            f"---\ndomain: services\nlast_delta_applied: {fp_old}\n---\n\n# Services\n"
        )

        svc._update_affected_domains(
            affected_domains={"services"},
            dependency_map_dir=dep_map_dir,
            changed_repos=[_make_repo("repo-a"), _make_repo("repo-foo")],
            new_repos=[],
            removed_repos=[],
            config=config,
            fingerprint=fp_new,
        )

        # Must have been processed (old fingerprint does not match)
        svc._analyzer.invoke_delta_merge_file.assert_called_once()

        fm, _ = parse_frontmatter((dep_map_dir / "services.md").read_text())
        assert fm.get("last_delta_applied") == fp_new


class TestConcurrentLockRejection:
    """Scenario 11: concurrent-run rejection via the in-process threading lock."""

    def test_second_run_noop_when_threading_lock_held(self, tmp_path, caplog):
        """
        When self._lock is already held (simulating a concurrent in-process run),
        run_delta_analysis returns None immediately without modifying domain files.

        This tests the non-blocking lock.acquire(blocking=False) guard at the
        top of run_delta_analysis, which is the primary same-process concurrency gate.
        """
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )
        from code_indexer.server.utils.config_manager import ClaudeIntegrationConfig

        config = ClaudeIntegrationConfig(
            dependency_map_enabled=True,
            dependency_map_interval_hours=24,
            dependency_map_pass_timeout_seconds=300,
            dependency_map_delta_max_turns=30,
        )
        config_manager = Mock()
        config_manager.get_claude_integration_config.return_value = config

        golden_repos_dir = str(tmp_path / "golden-repos")
        Path(golden_repos_dir).mkdir(parents=True, exist_ok=True)

        golden_repos_manager = Mock()
        golden_repos_manager.golden_repos_dir = golden_repos_dir
        golden_repos_manager.list_golden_repos.return_value = []

        tracking_backend = Mock()
        tracking_backend.get_tracking.return_value = {
            "id": 1,
            "last_run": None,
            "next_run": None,
            "status": "pending",
            "commit_hashes": "{}",
            "error_message": None,
            "refinement_cursor": 0,
            "refinement_next_run": None,
        }
        tracking_backend.update_tracking = Mock()

        analyzer = Mock()
        analyzer.invoke_delta_merge_file.return_value = "body\n"
        analyzer.build_delta_merge_prompt.return_value = "prompt"
        analyzer.generate_orientation_files.return_value = None

        svc = DependencyMapService(
            golden_repos_manager=golden_repos_manager,
            config_manager=config_manager,
            tracking_backend=tracking_backend,
            analyzer=analyzer,
        )

        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        domain_file = dep_map_dir / "services.md"
        domain_file.write_text("# Services\n")

        # Simulate concurrent in-progress run by holding the lock before calling run_delta_analysis.
        # Non-blocking acquire will fail and the method must return None immediately.
        acquired = svc._lock.acquire(blocking=False)
        assert acquired, "Expected to acquire the lock in test setup"

        try:
            with caplog.at_level(logging.INFO):
                result = svc.run_delta_analysis()

            # Should return None (skipped — lock held)
            assert result is None

            # Domain file must NOT have been modified by the second caller
            assert domain_file.read_text() == "# Services\n"

            # Claude must NOT have been invoked
            assert svc._analyzer.invoke_delta_merge_file.call_count == 0

            # Log must indicate it was skipped
            assert any(
                "skipped" in r.message.lower() or "in progress" in r.message.lower()
                for r in caplog.records
                if r.levelno >= logging.INFO
            )
        finally:
            svc._lock.release()
