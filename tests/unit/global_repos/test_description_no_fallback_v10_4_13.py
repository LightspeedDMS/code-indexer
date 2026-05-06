"""v10.4.13 anti-fallback contract for description generation.

Production user reported descriptions look terse vs richer historical state.
Root cause: two silent fallback paths in the description-generation pipeline
(README copy when Claude unavailable; static regex extraction when Claude
returned None). Both violated Messi Rule #2 (anti-fallback): graceful
failure over forced success.

v10.4.13 fix:
- on_repo_added: raise RuntimeError when cli_manager None or CLI unavailable.
- _generate_repo_description: REQUIRES cli_manager; raises TypeError on
  None / wrong type; passes cli_manager into RepoAnalyzer.
- repo_analyzer.extract_info(): raises RuntimeError when Claude returns None.
- _create_readme_fallback function DELETED — anti-orphan-code.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.claude_cli_manager import ClaudeCliManager


# ---------------------------------------------------------------------------
# AC1 — _generate_repo_description requires a real ClaudeCliManager
# ---------------------------------------------------------------------------


class TestGenerateRepoDescriptionRequiresCliManager:
    """_generate_repo_description() must reject None / wrong-type cli_manager
    AND forward valid manager to RepoAnalyzer (observable via Claude boundary
    invocation)."""

    def test_raises_type_error_when_cli_manager_is_none(self, tmp_path):
        """Passing None must raise TypeError, NOT silently fall through.

        type: ignore is intentional — the test exists precisely BECAUSE
        Python's type annotation alone doesn't enforce non-None at runtime;
        we're verifying the function's runtime guard catches it.
        """
        from code_indexer.global_repos.meta_description_hook import (
            _generate_repo_description,
        )

        with pytest.raises(TypeError, match="ClaudeCliManager"):
            _generate_repo_description(
                repo_name="test-repo",
                repo_url="https://github.com/example/test",
                clone_path=str(tmp_path),
                cli_manager=None,  # type: ignore[arg-type]  # negative test of runtime guard
            )

    def test_raises_type_error_when_cli_manager_is_wrong_type(self, tmp_path):
        """Passing a non-ClaudeCliManager object must raise TypeError."""
        from code_indexer.global_repos.meta_description_hook import (
            _generate_repo_description,
        )

        bogus = MagicMock(spec=str)  # MagicMock spec'd to a different type
        with pytest.raises(TypeError, match="ClaudeCliManager"):
            _generate_repo_description(
                repo_name="test-repo",
                repo_url="https://github.com/example/test",
                clone_path=str(tmp_path),
                cli_manager=bogus,
            )

    def test_forwards_cli_manager_to_claude_boundary(self, tmp_path):
        """When a valid cli_manager is passed, RepoAnalyzer must reach the
        Claude boundary using THAT manager's check_cli_available() —
        observable proof of forwarding without mocking SUT internals.
        """
        from code_indexer.global_repos.meta_description_hook import (
            _generate_repo_description,
        )

        # Set up a minimal real repo for RepoAnalyzer to inspect
        (tmp_path / "README.md").write_text("# Test\nA test repo.")

        # ClaudeCliManager is the EXTERNAL boundary (the SUT calls into it)
        cli_manager = MagicMock(spec=ClaudeCliManager)
        # Make CLI report unavailable so the Claude path returns None and
        # extract_info raises RuntimeError (anti-fallback). What we're
        # observing is that cli_manager.check_cli_available() WAS called —
        # proof the manager was forwarded into RepoAnalyzer.
        cli_manager.check_cli_available.return_value = False

        with pytest.raises(RuntimeError):
            _generate_repo_description(
                repo_name="test-repo",
                repo_url="https://github.com/example/test",
                clone_path=str(tmp_path),
                cli_manager=cli_manager,
            )

        # Forwarding is observable: cli_manager.check_cli_available() must
        # have been called by RepoAnalyzer (proves manager was forwarded in).
        cli_manager.check_cli_available.assert_called()


# ---------------------------------------------------------------------------
# AC2 — RepoAnalyzer.extract_info raises when Claude returns None
# ---------------------------------------------------------------------------


class TestRepoAnalyzerExtractInfoNoStaticFallback:
    """extract_info() must raise RuntimeError when Claude unavailable, NOT fall
    back to static regex extraction. Tests drive through the EXTERNAL
    subprocess boundary (the actual Claude integration point)."""

    def test_raises_runtime_error_when_claude_subprocess_unavailable(self, tmp_path):
        """When Claude CLI subprocess returns non-zero and no manager is
        provided, extract_info() must raise — not return static-extraction
        result. External boundary mocked: `subprocess.run` (the actual
        Claude CLI invocation point). The SUT (extract_info) runs unmocked.
        """
        from code_indexer.global_repos.repo_analyzer import RepoAnalyzer

        (tmp_path / "README.md").write_text("# Test\n\nA test repo.")

        # No claude_cli_manager → falls into the direct-subprocess path
        analyzer = RepoAnalyzer(str(tmp_path))

        # Mock the subprocess.run boundary (external dependency).
        # Returncode 1 means `which claude` failed → CLI not on PATH.
        mock_result = MagicMock()
        mock_result.returncode = 1
        with patch("subprocess.run", return_value=mock_result):
            with pytest.raises(
                RuntimeError, match="Claude CLI extraction returned no result"
            ):
                analyzer.extract_info()


# ---------------------------------------------------------------------------
# AC3 — _create_readme_fallback function is deleted (anti-orphan-code)
# ---------------------------------------------------------------------------


class TestCreateReadmeFallbackDeleted:
    """v10.4.13 deleted _create_readme_fallback — anti-orphan-code per
    Messi Rule #12. Verify it can't be re-introduced accidentally."""

    def test_create_readme_fallback_does_not_exist_via_import(self):
        """Importing _create_readme_fallback must raise ImportError."""
        with pytest.raises(ImportError, match="_create_readme_fallback"):
            from code_indexer.global_repos.meta_description_hook import (  # noqa: F401
                _create_readme_fallback,
            )

    def test_create_readme_fallback_not_in_module_namespace(self):
        """Module-namespace check: _create_readme_fallback must not be present
        as ANY top-level symbol (FunctionDef, Assign, AnnAssign, imported
        alias, etc.). Catches regressions where the symbol is reintroduced
        via a non-def form."""
        import code_indexer.global_repos.meta_description_hook as module

        assert not hasattr(module, "_create_readme_fallback"), (
            "v10.4.13 deleted _create_readme_fallback from the module "
            "namespace. Any top-level symbol with this name (function, "
            "variable, alias) suggests the anti-fallback contract has "
            "regressed."
        )


# ---------------------------------------------------------------------------
# AC4 — on_repo_added raises RuntimeError when cli_manager unavailable
# ---------------------------------------------------------------------------


class TestOnRepoAddedRaisesWhenCliUnavailable:
    """on_repo_added must raise RuntimeError when cli_manager is None or CLI
    is not on PATH — NOT write a stub or fallback description."""

    def test_raises_runtime_error_when_cli_manager_is_none(self, tmp_path):
        """When get_claude_cli_manager() returns None, raise RuntimeError."""
        from code_indexer.global_repos import meta_description_hook

        # Create golden_repos_dir/cidx-meta so the early-return at line 333
        # doesn't fire (we want the cli_manager check to fire instead)
        cidx_meta = tmp_path / "cidx-meta"
        cidx_meta.mkdir()

        with patch.object(
            meta_description_hook, "get_claude_cli_manager", return_value=None
        ):
            with pytest.raises(RuntimeError, match="ClaudeCliManager not initialized"):
                meta_description_hook.on_repo_added(
                    repo_name="test-repo",
                    repo_url="https://github.com/example/test",
                    clone_path=str(tmp_path),
                    golden_repos_dir=str(tmp_path),
                )

    def test_raises_runtime_error_when_cli_not_available(self, tmp_path):
        """When ClaudeCliManager.check_cli_available() returns False, raise."""
        from code_indexer.global_repos import meta_description_hook

        cidx_meta = tmp_path / "cidx-meta"
        cidx_meta.mkdir()

        cli_manager = MagicMock(spec=ClaudeCliManager)
        cli_manager.check_cli_available.return_value = False

        with patch.object(
            meta_description_hook, "get_claude_cli_manager", return_value=cli_manager
        ):
            with pytest.raises(RuntimeError, match="Claude CLI not available on PATH"):
                meta_description_hook.on_repo_added(
                    repo_name="test-repo",
                    repo_url="https://github.com/example/test",
                    clone_path=str(tmp_path),
                    golden_repos_dir=str(tmp_path),
                )
