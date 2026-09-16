"""
Unit/integration tests for Bug #1870: dependency-map delta-apply writer
corruption (duplicate, unindented participating_repos list) and the
fail-open parse_frontmatter self-perpetuation defect.

This file adds FOUR new tests (bug-report scope items 1-4). Scope item 5
(REGRESSION) is deliberately NOT a new test method here -- the mission's own
wording for it is "the existing dep_map_delta_journal tests still pass", i.e.
it is satisfied by running the pre-existing, unmodified
tests/unit/server/services/test_dep_map_1053_delta_journal.py suite, not by
writing a new test that duplicates it. That run's exact command and result
are reported in the turn-7 handoff, per the mission's test-result-attribution
requirement.

Test mapping:
  1. ROUND-TRIP         -> TestRoundTrip::test_round_trip_analyzer_style_file_produces_single_clean_block
  2. IDEMPOTENCE        -> TestIdempotence::test_idempotent_reapplication_produces_byte_identical_output
  3. ALREADY-CORRUPTED  -> TestAlreadyCorruptedInput::test_already_corrupted_existing_file_is_not_silently_rewritten
  4. FAIL-OPEN BRANCH   -> TestFailOpenBranch::test_malformed_yaml_blocks_delta_write_no_duplication

Reproduction finding (settled turn 1, re-verified turn 7): the literal
"parse_frontmatter under-consumes on 2-space style" hypothesis (a) is FALSE --
a genuine _build_domain_frontmatter-style file round-trips cleanly through
parse_frontmatter -> render_md with zero duplication. The real defect is (b)/
other: parse_frontmatter's fail-open branch converts an already-malformed
file to {} + full-text-as-body, and the writer had no validation gate to stop
processing (or re-writing) when that happens -- which is self-perpetuating.
The literal mission-provided corrupted shape below was verified this turn to
raise the exact same yaml.parser.ParserError, via the real downstream
consumer's own strict parser (dep_map_parser_tables.parse_frontmatter_strict),
that the bug report quotes -- confirming this fixture is faithful, not just
plausible.
"""

from pathlib import Path
from unittest.mock import Mock

from code_indexer.server.services.dep_map_delta_journal import (
    parse_frontmatter,
    render_md,
    validate_rendered_frontmatter,
    write_atomic,
    compute_delta_fingerprint,
)
from code_indexer.server.services.dep_map_parser_tables import parse_frontmatter_strict
from code_indexer.server.services.dependency_map_service import _DomainUpdateResult


def _make_repo(alias: str) -> dict:
    return {"alias": alias, "clone_path": f"/repos/{alias}"}


def _build_analyzer_style_file(
    domain,
    repos,
    description="Test automation domain",
    last_analyzed="2026-07-28T00:18:45.011486+00:00",
    body="Some domain description body.\n",
):
    """Mirrors dependency_map_analyzer.py::_build_domain_frontmatter's actual
    hand-rolled string output byte-for-byte (2-space indented list)."""
    fm = "---\n"
    fm += f"domain: {domain}\n"
    fm += f"description: {description}\n"
    fm += f"last_analyzed: {last_analyzed}\n"
    fm += "participating_repos:\n"
    for r in repos:
        fm += f"  - {r}\n"
    fm += "---\n\n"
    return fm + body


REPOS = ["rpc-api-automation", "rpc-capture-tool", "pyramid-mcp"]

# Literal reconstruction of the mission's reported production corruption shape
# (test-automation.md), used as a TEST FIXTURE ONLY per the hard constraints --
# no production/data files are touched anywhere in this suite.
CORRUPTED_PRODUCTION_SHAPE = (
    "---\n"
    "domain: test-automation\n"
    "participating_repos:\n"
    "  - rpc-api-automation\n"
    "  - rpc-capture-tool\n"
    "  - pyramid-mcp\n"
    "- rpc-api-automation\n"
    "- rpc-capture-tool\n"
    "- remoteswinglibrary\n"
    "last_refined: 2026-07-28 00:18:45.011486+00:00\n"
    "last_delta_applied: 2a97692b8884b5563099b2aed7cb4cf3bb49454b18fd6c21fe302a8ce4341be5\n"
    "last_applied_at: 2026-09-15T03:40:42.183911+00:00\n"
    "---\n\n"
    "Domain body content here.\n"
)

# A DIFFERENT malformed-YAML shape (not participating_repos-related), so this
# test proves the fix generalizes beyond the one reported symptom.
MALFORMED_UNRELATED_SHAPE = (
    "---\n"
    "domain: test-automation\n"
    "owner: platform-team\n"
    "  bad_indent_here: true\n"  # invalid: unexpected indentation on a mapping key
    "last_refined: 2026-07-28 00:18:45.011486+00:00\n"
    "---\n\n"
    "Domain body content here.\n"
)


def _make_service_for_delta(tmp_path: Path, invoke_result: str = "Updated body\n"):
    """Same harness as test_dep_map_1053_delta_journal.py::_make_service_for_delta."""
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


def _run_delta_against_malformed_fixture(tmp_path: Path, malformed_text: str):
    """
    Shared harness for tests 3 and 4: pre-seed a domain file with the given
    malformed/corrupted text, run the real _update_affected_domains against
    it, and return (errors, domain_file, svc) for the caller's own
    fixture-specific assertions.
    """
    svc, config = _make_service_for_delta(tmp_path, invoke_result="Fresh claude body\n")
    dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
    dep_map_dir.mkdir(parents=True)

    domain_file = dep_map_dir / "test-automation.md"
    domain_file.write_text(malformed_text)

    fingerprint = compute_delta_fingerprint([_make_repo("r")], [], [])

    errors = svc._update_affected_domains(
        affected_domains={"test-automation"},
        dependency_map_dir=dep_map_dir,
        changed_repos=[_make_repo("r")],
        new_repos=[],
        removed_repos=[],
        config=config,
        fingerprint=fingerprint,
    )
    return errors, domain_file, svc


# ---------------------------------------------------------------------------
# Test 1: ROUND-TRIP (the primary RED per the mission's own literal recipe)
# ---------------------------------------------------------------------------
class TestRoundTrip:
    def test_round_trip_analyzer_style_file_produces_single_clean_block(self):
        """
        Genuine _build_domain_frontmatter-style file (2-space indented list)
        through parse_frontmatter -> render_md, exactly mirroring the write
        site's own logic (dependency_map_service.py's Story #1053 rewrap).
        Result must parse as valid YAML via the REAL downstream consumer's
        own strict parser, and contain exactly ONE participating_repos key.

        Also asserts a second, previously-missed round-trip requirement:
        render_md's own docstring promises "parse_frontmatter(render_md(fm,
        body)) round-trips cleanly". The participating_repos duplication
        hypothesis (a) is disproven (this is the settled negative result,
        see the module docstring) -- but a REAL round-trip fidelity defect
        exists: parse_frontmatter's plain yaml.safe_load silently converts
        an ISO-8601 "T"-separated timestamp string into a datetime object,
        which render_md then re-emits SPACE-separated. This is directly
        visible in the mission's own reported corrupted file
        (`last_refined: 2026-07-28 00:18:45...`, space not T) and is the
        genuine, mechanically-confirmed "MUST FAIL on current code" RED for
        this scope item.
        """
        original = _build_analyzer_style_file("test-automation", REPOS)
        fm, body = parse_frontmatter(original, domain_hint="test-automation")
        new_fm = {
            k: v
            for k, v in fm.items()
            if k not in ("last_delta_applied", "last_applied_at")
        }
        new_fm["domain"] = "test-automation"
        new_fm["last_delta_applied"] = "deadbeef"
        new_fm["last_applied_at"] = "2026-09-15T03:40:42.183911+00:00"
        rendered = render_md(new_fm, body)

        parsed = parse_frontmatter_strict(rendered)
        assert isinstance(parsed, dict)
        assert rendered.count("participating_repos:") == 1
        assert parsed["participating_repos"] == REPOS

        # The genuine RED: last_analyzed must survive byte-for-byte, T intact.
        assert "last_analyzed: 2026-07-28T00:18:45.011486+00:00" in rendered, (
            "last_analyzed lost its ISO T-separator across the round trip "
            f"(round-trip is not clean); rendered block was:\n{rendered}"
        )


# ---------------------------------------------------------------------------
# Test 2: IDEMPOTENCE
# ---------------------------------------------------------------------------
class TestIdempotence:
    def test_idempotent_reapplication_produces_byte_identical_output(
        self, tmp_path: Path
    ):
        """Applying the round trip twice on a clean input, through the real
        write_atomic persistence boundary, must produce byte-identical output."""
        domain_file = tmp_path / "test-automation.md"
        domain_file.write_text(_build_analyzer_style_file("test-automation", REPOS))

        def apply_once(fingerprint):
            existing_text = domain_file.read_text()
            fm2, body2 = parse_frontmatter(existing_text, domain_hint="test-automation")
            new_fm = {
                k: v
                for k, v in fm2.items()
                if k not in ("last_delta_applied", "last_applied_at")
            }
            new_fm["domain"] = "test-automation"
            new_fm["last_delta_applied"] = fingerprint
            new_fm["last_applied_at"] = "2026-09-15T03:40:42.183911+00:00"
            rendered = render_md(new_fm, body2)
            write_atomic(domain_file, rendered)
            return rendered

        once = apply_once("deadbeef")
        twice = apply_once("deadbeef")
        assert once == twice
        assert domain_file.read_text() == twice


# ---------------------------------------------------------------------------
# Test 3: ALREADY-CORRUPTED INPUT (integration-level, via the real service)
# ---------------------------------------------------------------------------
class TestAlreadyCorruptedInput:
    def test_already_corrupted_existing_file_is_not_silently_rewritten(
        self, tmp_path: Path
    ):
        """
        Pre-seed a domain file with the literal reported production
        corruption shape, then run the real _update_affected_domains against
        it. The writer must NEVER silently discard the malformed content and
        replace it with a fresh minimal frontmatter (today's behavior) --
        it must either repair or fail loudly, recording an error and never
        invoking Claude for an unrecoverable input.

        CURRENT (RED) behavior: parse_frontmatter's fail-open branch makes
        orig_fm == {} for this file, so the skip-check does not skip it,
        Claude IS invoked, and the file gets silently overwritten with a
        fresh minimal frontmatter (only domain/last_delta_applied/
        last_applied_at) built from an EMPTY base -- losing the original
        participating_repos/last_refined data with zero error reported.
        That is exactly the "self-perpetuating silent degrade" the mission
        describes as defect #2.
        """
        errors, domain_file, svc = _run_delta_against_malformed_fixture(
            tmp_path, CORRUPTED_PRODUCTION_SHAPE
        )

        # Behavioral assertion (not an import/symbol check): the malformed
        # input must be surfaced as an error, not silently accepted.
        assert errors, (
            "Expected an error for the already-corrupted domain, got none -- "
            "the malformed file was silently accepted and processed."
        )
        assert any("test-automation" in e for e in errors)

        # No Claude call for an input that cannot be safely merged.
        svc._analyzer.invoke_delta_merge_file.assert_not_called()

        # The file on disk must be untouched -- never silently rewritten,
        # never re-written with a (further) duplicated block.
        assert domain_file.read_text() == CORRUPTED_PRODUCTION_SHAPE


# ---------------------------------------------------------------------------
# Test 4: FAIL-OPEN BRANCH (a different malformed-YAML shape, generalizing
# beyond the one reported symptom)
# ---------------------------------------------------------------------------
class TestFailOpenBranch:
    def test_malformed_yaml_blocks_delta_write_no_duplication(self, tmp_path: Path):
        """
        A malformed-YAML file unrelated to participating_repos duplication
        must ALSO be rejected before any write -- proving the fix is a
        general fail-open guard, not a special case for one shape.
        """
        errors, domain_file, svc = _run_delta_against_malformed_fixture(
            tmp_path, MALFORMED_UNRELATED_SHAPE
        )

        assert errors, (
            "Malformed YAML must be surfaced as an error, not silently swallowed."
        )
        svc._analyzer.invoke_delta_merge_file.assert_not_called()
        assert domain_file.read_text() == MALFORMED_UNRELATED_SHAPE
        # No duplicated participating_repos block could have been written,
        # because nothing was written at all.
        assert "participating_repos:" not in domain_file.read_text()


# ---------------------------------------------------------------------------
# Direct unit coverage for validate_rendered_frontmatter (turn 11 correction).
#
# Turn 10 review (codex) found that no test forces validate_rendered_frontmatter
# itself to reject an invalid candidate. Verified true by mutation this turn:
# with the skip-check early-rejection active, both service-level fixtures
# above (TestAlreadyCorruptedInput, TestFailOpenBranch) are caught BEFORE the
# write site is ever reached -- inverting the skip-check's condition made
# exactly those two tests (and no others) go RED, confirming the write-site
# gate is currently unreachable via the integration path. These direct unit
# tests close that gap.
# ---------------------------------------------------------------------------
class TestValidateRenderedFrontmatter:
    def test_rejects_malformed_yaml_block(self):
        rendered = (
            "---\n"
            "domain: test-automation\n"
            "participating_repos:\n"
            "  - a\n"
            "- b\n"  # invalid: bare '-' at column 0 mid-block-mapping
            "---\n\nBody.\n"
        )
        error = validate_rendered_frontmatter(rendered, {"domain": "test-automation"})
        assert error is not None
        assert "failed to parse" in error

    def test_rejects_non_mapping_parse_result(self):
        # The delimited block parses as a YAML LIST, not a mapping.
        rendered = "---\n- a\n- b\n---\n\nBody.\n"
        error = validate_rendered_frontmatter(rendered, {"domain": "test-automation"})
        assert error is not None
        assert "is not a mapping" in error

    def test_rejects_participating_repos_mismatch(self):
        expected_fm = {"domain": "test-automation", "participating_repos": ["a", "b"]}
        rendered = render_md(
            {"domain": "test-automation", "participating_repos": ["a"]}, "Body.\n"
        )
        error = validate_rendered_frontmatter(rendered, expected_fm)
        assert error == "rendered participating_repos does not match the intended value"

    def test_accepts_clean_render(self):
        expected_fm = {"domain": "test-automation", "participating_repos": ["a", "b"]}
        rendered = render_md(expected_fm, "Body.\n")
        assert validate_rendered_frontmatter(rendered, expected_fm) is None


# ---------------------------------------------------------------------------
# Bug #1870, second writer: _update_domain_file's fact-check branch reads
# back whatever the verification pass wrote to its temp file with ZERO
# validation whenever the pass reports success, and persists it straight to
# the live domain file. Different from the scenarios above (those exercise
# _update_affected_domains' frontmatter-journal skip logic on an
# already-corrupted starting file); this exercises _update_domain_file
# itself, on a CLEAN starting file, where the corruption is introduced by
# the verification pass mid-flight.
# ---------------------------------------------------------------------------

# Dedented list item breaks the participating_repos YAML block -- same
# corruption family as CORRUPTED_PRODUCTION_SHAPE above.
VERIFICATION_CORRUPTED_OUTPUT = (
    "---\n"
    "domain: test-automation\n"
    "participating_repos:\n"
    "  - rpc-api-automation\n"
    "  - rpc-capture-tool\n"
    "- pyramid-mcp\n"
    "last_analyzed: 2026-09-15T03:40:42.183911+00:00\n"
    "---\n\n"
    "Verified body content.\n"
)


def _fake_invoke_verification_pass_writes_corruption(
    *, document_path, repo_list, config
):
    """Simulates Claude's verification pass reporting success (True) while
    having corrupted the temp file it edited in place -- the exact shape of
    Bug #1870's second writer defect."""
    document_path.write_text(VERIFICATION_CORRUPTED_OUTPUT, encoding="utf-8")
    return True


class TestVerificationPassCorruptsFrontmatter:
    def test_verification_success_with_corrupted_output_falls_back_to_pre_verification_content(
        self, tmp_path: Path
    ):
        """
        Verification pass reports success but corrupted the frontmatter it
        edited in place -- the live domain file must not end up corrupted.
        CURRENT (RED): _update_domain_file writes the corrupted temp-file
        content straight to domain_file with no validation.
        """
        svc, config = _make_service_for_delta(
            tmp_path, invoke_result="Fresh claude body\n"
        )
        config.dep_map_fact_check_enabled = True

        dep_map_dir = tmp_path / "golden-repos" / "cidx-meta" / "dependency-map"
        dep_map_dir.mkdir(parents=True)
        domain_file = dep_map_dir / "test-automation.md"
        domain_file.write_text(_build_analyzer_style_file("test-automation", REPOS))

        svc._analyzer.invoke_verification_pass = Mock(
            side_effect=_fake_invoke_verification_pass_writes_corruption
        )

        result = svc._update_domain_file(
            domain_name="test-automation",
            domain_file=domain_file,
            changed_repos=["rpc-api-automation"],
            new_repos=[],
            removed_repos=[],
            domain_list=["test-automation"],
            config=config,
        )

        assert result == _DomainUpdateResult.WRITTEN

        live_text = domain_file.read_text()
        assert live_text != VERIFICATION_CORRUPTED_OUTPUT, (
            "Corrupted verification-pass output written verbatim to the "
            "live domain file -- Bug #1870's second writer is unguarded."
        )

        fm, _body = parse_frontmatter(live_text, domain_hint="test-automation")
        assert fm, "Live domain file frontmatter failed to parse after update"
        assert fm.get("participating_repos") == REPOS, (
            f"participating_repos corrupted by verification pass; got: {fm}"
        )
