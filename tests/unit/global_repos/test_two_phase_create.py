"""
Tests for Phase B (two-phase merge in CREATE path) and Phase C (atomic write helper).

Covers:
1.  Output includes lifecycle: block and lifecycle_schema_version: 1 on Phase 2 success
2.  Phase 2 failure (None) writes lifecycle with confidence: unknown
3.  Phase 2 success merges lifecycle data into frontmatter correctly
4.  _generate_repo_description returns (content, "success") on valid lifecycle
5.  _generate_repo_description returns (content, "failed_degraded_to_unknown") when Phase 2 is None
6.  Output is a valid YAML frontmatter document: parseable dict + non-empty body (line-aware split)
7.  on_repo_added accepts mcp_registration_service as optional keyword arg
8.  ensure_registered() called (and called before Phase 2) when service is not None
9.  None service logs warning and continues without crash
10. atomic_write_description creates target file with correct content
11. atomic_write_description replaces existing file atomically
12. No .tmp files remain after a successful write (no partial state)
13. atomic_write_description acquires and releases the cidx-meta write lock
14. Lock is released even when the write itself fails
"""

import contextlib
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import yaml
import pytest

SRC_ROOT = Path(__file__).parent.parent.parent.parent / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_repo(tmp_path):
    """Minimal fake repo directory with a README.md."""
    repo_dir = tmp_path / "fake-repo"
    repo_dir.mkdir()
    (repo_dir / "README.md").write_text("# Fake Repo\n\nA repo for unit testing.\n")
    return repo_dir


@pytest.fixture
def golden_repos_dir(tmp_path):
    """tmp_path acting as golden repos dir; cidx-meta subdir created here."""
    (tmp_path / "cidx-meta").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Shared test data
# ---------------------------------------------------------------------------

_VALID_LIFECYCLE_DICT = {
    "lifecycle_schema_version": 1,
    "lifecycle": {
        "branches_to_env": {"main": "production"},
        "detected_sources": ["github_actions:deploy.yml"],
        "confidence": "high",
        "claude_notes": "Main deploys to production via CI.",
    },
}


def _make_mock_repo_info():
    """Return a MagicMock that quacks like RepoInfo."""
    info = MagicMock()
    info.technologies = ["Python"]
    info.purpose = "library"
    info.summary = "A test library"
    info.features = ["feature1"]
    info.use_cases = ["use case 1"]
    return info


# ---------------------------------------------------------------------------
# Frontmatter / body split — line-aware so '---' inside YAML values is safe
# ---------------------------------------------------------------------------


def _split_frontmatter_lines(content_str):
    """
    Split a ---\\n...\\n---\\n<body> document into (fm_dict, body) using
    line-by-line parsing so that '---' appearing inside a YAML value does
    not confuse the fence detection.

    Returns:
        (fm_dict, body) where fm_dict is the parsed YAML mapping and body
        is the text after the closing fence line.

    Raises:
        AssertionError if the document does not start with a standalone '---'
        line or has no closing '---' fence.
    """
    lines = content_str.splitlines(keepends=True)
    assert lines and lines[0].rstrip("\n") == "---", (
        "Document must open with a standalone --- line"
    )

    fm_lines = []
    closing_index = None
    for i, line in enumerate(lines[1:], start=1):
        if line.rstrip("\n") == "---":
            closing_index = i
            break
        fm_lines.append(line)

    assert closing_index is not None, "No closing standalone --- line found"

    fm_text = "".join(fm_lines)
    body = "".join(lines[closing_index + 1 :])
    fm_dict = yaml.safe_load(fm_text)
    return fm_dict, body


# ---------------------------------------------------------------------------
# Shared patch context-manager (eliminates duplication across Phase B tests)
# ---------------------------------------------------------------------------


@contextlib.contextmanager
def _patch_generate_stack(fake_repo, phase2_result, phase2_side_effect=None):
    """
    Context manager patching RepoAnalyzer, invoke_lifecycle_detection,
    atomic_write_description, and get_claude_cli_manager for Phase B tests.

    Yields a dict with keys:
        "analyzer_mock"  – the RepoAnalyzer class mock
        "phase2_mock"    – the invoke_lifecycle_detection mock
        "write_mock"     – the atomic_write_description mock
        "cli_mock"       – the ClaudeCliManager mock
    """
    mock_cli = MagicMock()
    mock_cli.check_cli_available.return_value = True

    with patch(
        "code_indexer.global_repos.meta_description_hook.get_claude_cli_manager",
        return_value=mock_cli,
    ):
        with patch(
            "code_indexer.global_repos.meta_description_hook.RepoAnalyzer"
        ) as MockAnalyzer:
            MockAnalyzer.return_value.extract_info.return_value = _make_mock_repo_info()

            phase2_kwargs = (
                {"side_effect": phase2_side_effect}
                if phase2_side_effect is not None
                else {"return_value": phase2_result}
            )

            with patch(
                "code_indexer.global_repos.meta_description_hook.invoke_lifecycle_detection",
                **phase2_kwargs,
            ) as mock_phase2:
                with patch(
                    "code_indexer.global_repos.meta_description_hook.atomic_write_description"
                ) as mock_write:
                    yield {
                        "analyzer_mock": MockAnalyzer,
                        "phase2_mock": mock_phase2,
                        "write_mock": mock_write,
                        "cli_mock": mock_cli,
                    }


# ---------------------------------------------------------------------------
# Unified helpers that operate on _generate_repo_description directly
# ---------------------------------------------------------------------------


def _call_generate_raw(fake_repo, phase2_result):
    """
    Call _generate_repo_description with Phase 1 and Phase 2 mocked.
    Returns the full (content, phase2_outcome) tuple.
    """
    from code_indexer.global_repos.meta_description_hook import (
        _generate_repo_description,
    )

    with patch(
        "code_indexer.global_repos.meta_description_hook.RepoAnalyzer"
    ) as MockAnalyzer:
        MockAnalyzer.return_value.extract_info.return_value = _make_mock_repo_info()
        with patch(
            "code_indexer.global_repos.meta_description_hook.invoke_lifecycle_detection",
            return_value=phase2_result,
        ):
            return _generate_repo_description(
                "fake-repo",
                "https://github.com/test/fake-repo",
                str(fake_repo),
            )


def _content(raw):
    """Extract the content string from a (content, phase2_outcome) raw result."""
    assert isinstance(raw, tuple), (
        f"_generate_repo_description must return a tuple, got {type(raw).__name__}"
    )
    return raw[0]


def _outcome(raw):
    """Extract the phase2_outcome string from a raw result tuple."""
    assert isinstance(raw, tuple)
    return raw[1]


# ---------------------------------------------------------------------------
# Phase B — _generate_repo_description two-phase merge tests
# ---------------------------------------------------------------------------


class TestGenerateRepoDescriptionPhaseB:
    """Two-phase merge behavior in _generate_repo_description."""

    def test_create_path_includes_lifecycle_block(self, fake_repo):
        """Output contains 'lifecycle:' and 'lifecycle_schema_version: 1' on Phase 2 success."""
        content = _content(_call_generate_raw(fake_repo, _VALID_LIFECYCLE_DICT))
        assert "lifecycle:" in content
        assert "lifecycle_schema_version: 1" in content

    def test_create_path_phase2_success_writes_lifecycle_data(self, fake_repo):
        """Phase 2 success: frontmatter contains correct lifecycle data."""
        content = _content(_call_generate_raw(fake_repo, _VALID_LIFECYCLE_DICT))
        fm, _ = _split_frontmatter_lines(content)

        assert isinstance(fm, dict)
        assert "lifecycle" in fm
        assert fm["lifecycle"]["confidence"] == "high"
        assert fm["lifecycle"]["branches_to_env"]["main"] == "production"
        assert fm["lifecycle_schema_version"] == 1

    def test_create_path_phase2_failure_writes_unknown_confidence(self, fake_repo):
        """Phase 2 failure (None): frontmatter has lifecycle with confidence: unknown."""
        content = _content(_call_generate_raw(fake_repo, None))
        fm, _ = _split_frontmatter_lines(content)

        assert isinstance(fm, dict)
        assert "lifecycle" in fm
        assert fm["lifecycle"]["confidence"] == "unknown"
        assert fm["lifecycle_schema_version"] == 1
        assert "branches_to_env" in fm["lifecycle"]
        assert "detected_sources" in fm["lifecycle"]

    def test_phase2_outcome_success_on_valid_lifecycle(self, fake_repo):
        """Returns tuple with phase2_outcome == 'success' on Phase 2 success."""
        raw = _call_generate_raw(fake_repo, _VALID_LIFECYCLE_DICT)
        assert _outcome(raw) == "success"

    def test_phase2_outcome_failed_degraded_on_failure(self, fake_repo):
        """Returns tuple with phase2_outcome == 'failed_degraded_to_unknown' when None."""
        raw = _call_generate_raw(fake_repo, None)
        assert _outcome(raw) == "failed_degraded_to_unknown"

    def test_output_is_valid_yaml_frontmatter_plus_body(self, fake_repo):
        """
        Output has a parseable YAML frontmatter dict and a non-empty body.

        Uses line-aware splitting so '---' inside YAML values cannot
        accidentally be treated as the closing fence.
        """
        content = _content(_call_generate_raw(fake_repo, None))

        fm_dict, body = _split_frontmatter_lines(content)

        assert isinstance(fm_dict, dict), "Frontmatter must parse as a YAML dict"
        assert len(fm_dict) > 0, "Frontmatter dict must not be empty"
        assert body.strip(), "Body after closing --- must be non-empty"


# ---------------------------------------------------------------------------
# Phase B — on_repo_added MCP service wiring tests
# ---------------------------------------------------------------------------


class TestOnRepoAddedMCPService:
    """mcp_registration_service parameter wiring on on_repo_added."""

    def _call_on_repo_added(
        self,
        fake_repo,
        golden_repos_dir,
        mcp_service=None,
        phase2_side_effect=None,
    ):
        """
        Call on_repo_added via the shared _patch_generate_stack.
        Returns the atomic_write_description mock for call inspection.
        """
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        with _patch_generate_stack(
            fake_repo, _VALID_LIFECYCLE_DICT, phase2_side_effect=phase2_side_effect
        ) as mocks:
            on_repo_added(
                repo_name="fake-repo",
                repo_url="https://github.com/test/fake-repo",
                clone_path=str(fake_repo),
                golden_repos_dir=str(golden_repos_dir),
                mcp_registration_service=mcp_service,
            )
            return mocks["write_mock"]

    def test_on_repo_added_accepts_mcp_registration_service_param(
        self, fake_repo, golden_repos_dir
    ):
        """on_repo_added must accept mcp_registration_service as optional keyword arg without raising."""
        mock_service = MagicMock()
        self._call_on_repo_added(fake_repo, golden_repos_dir, mcp_service=mock_service)

    def test_mcp_ensure_registered_called_before_phase2(
        self, fake_repo, golden_repos_dir
    ):
        """ensure_registered() is called once and occurs before invoke_lifecycle_detection."""
        from code_indexer.global_repos.meta_description_hook import on_repo_added

        mock_service = MagicMock()
        call_order = []

        mock_service.ensure_registered.side_effect = lambda: call_order.append(
            "ensure_registered"
        )

        def phase2_side_effect(*args, **kwargs):
            call_order.append("phase2")
            return _VALID_LIFECYCLE_DICT

        with _patch_generate_stack(
            fake_repo, _VALID_LIFECYCLE_DICT, phase2_side_effect=phase2_side_effect
        ):
            on_repo_added(
                repo_name="fake-repo",
                repo_url="https://github.com/test/fake-repo",
                clone_path=str(fake_repo),
                golden_repos_dir=str(golden_repos_dir),
                mcp_registration_service=mock_service,
            )

        mock_service.ensure_registered.assert_called_once()
        assert call_order.index("ensure_registered") < call_order.index("phase2"), (
            "ensure_registered must be called before Phase 2 lifecycle detection"
        )

    def test_mcp_none_logs_warning_and_continues(
        self, fake_repo, golden_repos_dir, caplog
    ):
        """When mcp_registration_service is None, a warning is logged and execution continues."""
        with caplog.at_level(
            logging.WARNING,
            logger="code_indexer.global_repos.meta_description_hook",
        ):
            self._call_on_repo_added(fake_repo, golden_repos_dir, mcp_service=None)

        warning_texts = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any(
            "MCPSelfRegistrationService" in t or "mcp" in t.lower()
            for t in warning_texts
        ), f"Expected MCP-related warning, got: {warning_texts}"


# ---------------------------------------------------------------------------
# Phase C — atomic_write_description helper tests
# ---------------------------------------------------------------------------


class TestAtomicWriteDescription:
    """Tests for the atomic_write_description helper."""

    def test_atomic_write_creates_file(self, tmp_path):
        """atomic_write_description creates the target file with the given content."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "test.md"
        content = "# Hello\n\nAtomic write test.\n"

        atomic_write_description(target, content)

        assert target.exists()
        assert target.read_text(encoding="utf-8") == content

    def test_atomic_write_replaces_existing(self, tmp_path):
        """atomic_write_description overwrites an existing file atomically."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "existing.md"
        target.write_text("old content", encoding="utf-8")

        atomic_write_description(target, "new content")

        assert target.read_text(encoding="utf-8") == "new content"

    def test_atomic_write_no_partial_state(self, tmp_path):
        """No .tmp files remain in the directory after a successful write."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "partial_test.md"
        content = "X" * 10_000

        atomic_write_description(target, content)

        remaining_tmp = list(tmp_path.glob("*.tmp"))
        assert remaining_tmp == [], f"Leftover .tmp files: {remaining_tmp}"
        assert target.read_text(encoding="utf-8") == content

    def test_atomic_write_acquires_and_releases_lock(self, tmp_path):
        """atomic_write_description acquires and releases the cidx-meta write lock."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "locked.md"
        mock_scheduler = MagicMock()

        atomic_write_description(target, "content", refresh_scheduler=mock_scheduler)

        mock_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )
        mock_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="lifecycle_writer"
        )

    def test_atomic_write_releases_lock_even_on_write_failure(self, tmp_path):
        """Lock is released even when os.replace raises."""
        from code_indexer.global_repos.meta_description_hook import (
            atomic_write_description,
        )

        target = tmp_path / "fail.md"
        mock_scheduler = MagicMock()

        with patch("os.replace", side_effect=OSError("disk full")):
            with pytest.raises(OSError):
                atomic_write_description(
                    target, "content", refresh_scheduler=mock_scheduler
                )

        mock_scheduler.release_write_lock.assert_called_once()
