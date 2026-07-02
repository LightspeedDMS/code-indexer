"""
Unit tests for CliDispatcher wiring into DependencyMapAnalyzer Pass 2 (Story #848).

Tests that run_pass_2_per_domain routes its primary invocation through CliDispatcher
instead of calling _invoke_claude_cli directly.

Test inventory (6 tests across 3 classes):

  TestPass2DispatcherConstruction (2 tests)
    test_pass2_builds_dispatcher_with_both_invokers_when_codex_enabled
    test_pass2_builds_dispatcher_claude_only_when_codex_disabled

  TestPass2DispatcherInvocation (2 tests)
    test_pass2_dispatches_with_correct_flow_cwd_prompt_timeout
    test_pass2_failover_logging

  TestPass2DispatcherOutputAndSource (2 tests)
    test_pass2_consumes_result_output_identically_to_pre_848
    test_pass2_source_contains_github_16732_comment
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.dependency_map_analyzer import (
    DependencyMapAnalyzer,
    _strip_leading_yaml_frontmatter,
)
from code_indexer.server.services.intelligence_cli_invoker import InvocationResult


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_DOMAIN_NAME = "TestDomain"
_REPO_ALIAS = "repo-a"
_DEFAULT_PASS_TIMEOUT = 600
_DEFAULT_MAX_TURNS = 20
_DEFAULT_CODEX_WEIGHT = 0.5
_TEST_CODEX_WEIGHT = 0.7
_DISABLED_CODEX_WEIGHT = 0.0
_PROMPT_SNIPPET_LENGTH = 200
# Minimum body length that run_pass_2_per_domain accepts without retrying.
# Must stay in sync with the SUT threshold (currently 1000 chars).
_MIN_PASS2_OUTPUT_CHARS = 1000


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_success_result(
    cli_used: str = "codex",
    was_failover: bool = False,
    output: str = "",
) -> InvocationResult:
    # Default output must clear run_pass_2_per_domain's insufficient-output guard
    # (len >= _MIN_PASS2_OUTPUT_CHARS AND has markdown headings), otherwise the
    # retry path fires and spawns a real subprocess.run call.
    if not output:
        output = _make_long_domain_body()
    return InvocationResult(
        success=True,
        output=output,
        error="",
        cli_used=cli_used,
        was_failover=was_failover,
    )


def _make_mock_config(
    tmp_path: Path,
    codex_enabled: bool = False,
    codex_weight: float = _DEFAULT_CODEX_WEIGHT,
):
    """Build a minimal mock ServerConfig with tmp_path-derived values."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    codex_cfg = CodexIntegrationConfig(
        enabled=codex_enabled,
        codex_weight=codex_weight,
        credential_mode="api_key",
        api_key="placeholder",
    )
    cfg = MagicMock()
    cfg.codex_integration_config = codex_cfg
    return cfg


def _make_analyzer(tmp_path: Path, cli_dispatcher=None) -> DependencyMapAnalyzer:
    """Build a DependencyMapAnalyzer with injectable dispatcher."""
    return DependencyMapAnalyzer(
        golden_repos_root=tmp_path,
        cidx_meta_path=tmp_path / "cidx-meta",
        pass_timeout=_DEFAULT_PASS_TIMEOUT,
        cli_dispatcher=cli_dispatcher,
    )


def _make_minimal_domain(tmp_path: Path) -> dict:
    return {
        "name": _DOMAIN_NAME,
        "description": "A test domain",
        "participating_repos": [_REPO_ALIAS],
    }


def _make_minimal_repo_list(tmp_path: Path) -> list:
    return [{"alias": _REPO_ALIAS, "clone_path": str(tmp_path / _REPO_ALIAS)}]


def _run_pass2(analyzer: DependencyMapAnalyzer, tmp_path: Path) -> Path:
    """
    Call run_pass_2_per_domain with minimal fixtures derived from tmp_path.

    Returns the staging_dir so callers can inspect written files.
    """
    staging_dir = tmp_path / "staging"
    staging_dir.mkdir(exist_ok=True)
    domain = _make_minimal_domain(tmp_path)
    repo_list = _make_minimal_repo_list(tmp_path)
    analyzer.run_pass_2_per_domain(
        staging_dir=staging_dir,
        domain=domain,
        domain_list=[domain],
        repo_list=repo_list,
        max_turns=_DEFAULT_MAX_TURNS,
    )
    return staging_dir


def _make_long_domain_body() -> str:
    """
    Build a domain analysis body that clears the SUT's insufficient-output guard.

    run_pass_2_per_domain retries when len(result) < _MIN_PASS2_OUTPUT_CHARS or
    there are no markdown headings.  This helper produces a body with headings
    whose length is guaranteed to be >= _MIN_PASS2_OUTPUT_CHARS.
    """
    header = f"# Domain Analysis: {_DOMAIN_NAME}\n\n## Overview\n\n"
    filler_sentence = (
        "This domain contains repository repo-a with verified dependencies. "
    )
    body = header
    while len(body) < _MIN_PASS2_OUTPUT_CHARS:
        body += filler_sentence
    return body


# ---------------------------------------------------------------------------
# Tests: dispatcher construction
# ---------------------------------------------------------------------------


class TestPass2DispatcherConstruction:
    """_build_pass2_dispatcher builds the right invoker composition."""

    def test_pass2_builds_dispatcher_with_both_invokers_when_codex_enabled(
        self, tmp_path
    ):
        """
        When Codex is enabled and CODEX_HOME is set, _build_pass2_dispatcher
        creates a CliDispatcher with both claude and codex invokers (both non-None).
        """
        config = _make_mock_config(
            tmp_path, codex_enabled=True, codex_weight=_TEST_CODEX_WEIGHT
        )
        codex_home = str(tmp_path / "codex-home")

        with (
            patch(
                "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
            ) as mock_get_cfg,
            patch.dict("os.environ", {"CODEX_HOME": codex_home}),
        ):
            mock_svc = MagicMock()
            mock_svc.get_config.return_value = config
            mock_get_cfg.return_value = mock_svc

            analyzer = _make_analyzer(tmp_path)
            dispatcher = analyzer._build_pass2_dispatcher()

        assert dispatcher.claude is not None, "claude invoker must always be present"
        assert dispatcher.codex is not None, (
            "codex invoker must be set when Codex enabled"
        )
        assert dispatcher.codex_weight == _TEST_CODEX_WEIGHT

    def test_pass2_builds_dispatcher_claude_only_when_codex_disabled(self, tmp_path):
        """
        When codex_integration_config.enabled=False the dispatcher uses Claude only
        (codex=None, effective codex_weight=_DISABLED_CODEX_WEIGHT inside CliDispatcher).
        """
        config = _make_mock_config(tmp_path, codex_enabled=False)

        with patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg:
            mock_svc = MagicMock()
            mock_svc.get_config.return_value = config
            mock_get_cfg.return_value = mock_svc

            analyzer = _make_analyzer(tmp_path)
            dispatcher = analyzer._build_pass2_dispatcher()

        assert dispatcher.claude is not None, "claude invoker must always be present"
        assert dispatcher.codex is None, "codex must be None when Codex disabled"
        # CliDispatcher collapses the weight to 0.0 when codex is None.
        assert dispatcher.codex_weight == _DISABLED_CODEX_WEIGHT


# ---------------------------------------------------------------------------
# Tests: dispatcher invocation behaviour
# ---------------------------------------------------------------------------


class TestPass2DispatcherInvocation:
    """run_pass_2_per_domain calls dispatcher.dispatch with the right arguments."""

    def test_pass2_dispatches_with_correct_flow_cwd_prompt_timeout(self, tmp_path):
        """
        dispatcher.dispatch is called with flow='dependency_map_pass_2',
        cwd=str(golden_repos_root), prompt contains the domain name,
        and timeout=_DEFAULT_PASS_TIMEOUT.

        The injected dispatcher mock short-circuits the primary call so no
        subprocess is spawned. Retry paths must not fire when dispatch succeeds.
        """
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = _make_success_result()
        analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

        _run_pass2(analyzer, tmp_path)

        mock_dispatcher.dispatch.assert_called_once()
        call_kwargs = mock_dispatcher.dispatch.call_args.kwargs
        assert call_kwargs["flow"] == "dependency_map_pass_2", (
            f"flow must be 'dependency_map_pass_2', got {call_kwargs['flow']!r}"
        )
        assert call_kwargs["cwd"] == str(tmp_path)
        assert call_kwargs["timeout"] == _DEFAULT_PASS_TIMEOUT
        assert _DOMAIN_NAME in call_kwargs["prompt"], (
            f"prompt must contain domain name {_DOMAIN_NAME!r}; "
            f"got first {_PROMPT_SNIPPET_LENGTH} chars: "
            f"{call_kwargs['prompt'][:_PROMPT_SNIPPET_LENGTH]}"
        )

    def test_pass2_failover_logging(self, tmp_path, caplog):
        """
        When result.was_failover=True, exactly one INFO log record in the
        dependency_map_analyzer logger contains both "cli_used" and "was_failover"
        as explicit field names in the same message.
        """
        # Output must clear the insufficient-output guard (>= _MIN_PASS2_OUTPUT_CHARS
        # with markdown headings) so the retry path does not spawn a real subprocess.
        failover_result = _make_success_result(
            cli_used="claude",
            was_failover=True,
            output=_make_long_domain_body(),
        )
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = failover_result
        analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.global_repos.dependency_map_analyzer",
        ):
            _run_pass2(analyzer, tmp_path)

        # Exactly one INFO log record must contain both field names in the same message.
        failover_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "cli_used" in r.message
            and "was_failover" in r.message
        ]
        assert len(failover_records) == 1, (
            "Expected exactly one INFO log record containing both 'cli_used' and "
            f"'was_failover'; got {len(failover_records)} records: "
            f"{[r.message for r in caplog.records]}"
        )


# ---------------------------------------------------------------------------
# Tests: output consumption and source guard
# ---------------------------------------------------------------------------


class TestPass2DispatcherCaching:
    """_build_pass2_dispatcher result is cached per instance (HIGH #3)."""

    def test_dispatcher_built_only_once_across_multiple_pass2_calls(self, tmp_path):
        """
        When _cli_dispatcher is None, repeated calls to _invoke_pass2_dispatcher
        must construct CliDispatcher exactly once; subsequent calls reuse the cached
        dispatcher without re-invoking the constructor.

        Strategy: wrap CliDispatcher.__init__ (preserving real behaviour) to count
        constructor invocations, patch CliDispatcher.dispatch to return a success
        result, then call _invoke_pass2_dispatcher twice on the same analyzer
        instance and assert the constructor fired exactly once.
        """
        import code_indexer.server.services.cli_dispatcher as cli_mod

        config = _make_mock_config(tmp_path, codex_enabled=False)

        with patch(
            "code_indexer.global_repos.dependency_map_analyzer.get_config_service"
        ) as mock_get_cfg:
            mock_svc = MagicMock()
            mock_svc.get_config.return_value = config
            mock_get_cfg.return_value = mock_svc

            analyzer = _make_analyzer(tmp_path, cli_dispatcher=None)

            real_init = cli_mod.CliDispatcher.__init__
            constructor_call_count = [0]

            def _counting_init(self_inner, **kwargs):
                constructor_call_count[0] += 1
                real_init(self_inner, **kwargs)

            prompt = "Minimal prompt for cache test"
            timeout = _DEFAULT_PASS_TIMEOUT

            with (
                patch.object(cli_mod.CliDispatcher, "__init__", _counting_init),
                patch.object(
                    cli_mod.CliDispatcher,
                    "dispatch",
                    return_value=_make_success_result(),
                ),
            ):
                analyzer._invoke_pass2_dispatcher(prompt, timeout)
                analyzer._invoke_pass2_dispatcher(prompt, timeout)

        assert constructor_call_count[0] == 1, (
            f"CliDispatcher.__init__ must be called exactly once per analyzer instance "
            f"when the dispatcher is cached; was called {constructor_call_count[0]} times"
        )


class TestPass2FailoverOnRetryableOther:
    """CliDispatcher handles RETRYABLE_ON_OTHER by failing over to Claude (MEDIUM #5)."""

    def test_pass2_failover_when_codex_returns_retryable_on_other(
        self, tmp_path, caplog
    ):
        """
        When the Codex invoker returns RETRYABLE_ON_OTHER, the real CliDispatcher
        fails over to Claude and returns a successful result with was_failover=True.

        Strategy: build a real CliDispatcher wired with:
          - a mock codex invoker that returns InvocationResult(success=False,
            failure_class=RETRYABLE_ON_OTHER, cli_used='codex')
          - a mock claude invoker that returns InvocationResult(success=True,
            output=<long_body>, cli_used='claude')
        Inject this real dispatcher into the analyzer so _invoke_pass2_dispatcher
        uses it. Drive one call and assert:
          - output equals the claude long body
          - exactly one INFO log record contains both 'cli_used' and 'was_failover'
        """
        from code_indexer.server.services.intelligence_cli_invoker import FailureClass
        from code_indexer.server.services.cli_dispatcher import CliDispatcher

        long_body = _make_long_domain_body()

        codex_fail = InvocationResult(
            success=False,
            output="",
            error="codex error",
            cli_used="codex",
            was_failover=False,
            failure_class=FailureClass.RETRYABLE_ON_OTHER,
        )
        claude_success = InvocationResult(
            success=True,
            output=long_body,
            error="",
            cli_used="claude",
            was_failover=False,
        )

        mock_codex_invoker = MagicMock()
        mock_codex_invoker.invoke.return_value = codex_fail

        mock_claude_invoker = MagicMock()
        mock_claude_invoker.invoke.return_value = claude_success

        # Use a real CliDispatcher with codex_weight=1.0 to force codex as primary.
        real_dispatcher = CliDispatcher(
            claude=mock_claude_invoker,
            codex=mock_codex_invoker,
            codex_weight=1.0,
        )
        analyzer = _make_analyzer(tmp_path, cli_dispatcher=real_dispatcher)

        with caplog.at_level(
            logging.INFO,
            logger="code_indexer.global_repos.dependency_map_analyzer",
        ):
            output = analyzer._invoke_pass2_dispatcher(
                "Test prompt", _DEFAULT_PASS_TIMEOUT
            )

        assert output == long_body, (
            f"Output must match Claude's body after failover; got: {output[:200]!r}"
        )
        failover_records = [
            r
            for r in caplog.records
            if r.levelno == logging.INFO
            and "cli_used" in r.message
            and "was_failover" in r.message
        ]
        assert len(failover_records) == 1, (
            f"Expected exactly one INFO log with 'cli_used' and 'was_failover'; "
            f"got {len(failover_records)}: {[r.message for r in caplog.records]}"
        )


class TestPass2DispatcherOutputAndSource:
    """Output is consumed correctly and the #16732 comment is present in source."""

    def test_pass2_consumes_result_output_identically_to_pre_848(self, tmp_path):
        """
        result.output from the dispatcher is written verbatim as the domain file
        body. After stripping the SUT-generated frontmatter, the body must match
        expected_body exactly.

        expected_body is >= _MIN_PASS2_OUTPUT_CHARS so it clears the SUT's
        insufficient-output retry guard (run_pass_2_per_domain retries when
        len(result) < _MIN_PASS2_OUTPUT_CHARS or no markdown headings found),
        preventing the retry path from spawning a real subprocess.
        """
        expected_body = _make_long_domain_body()
        mock_dispatcher = MagicMock()
        mock_dispatcher.dispatch.return_value = _make_success_result(
            cli_used="codex",
            was_failover=False,
            output=expected_body,
        )
        analyzer = _make_analyzer(tmp_path, cli_dispatcher=mock_dispatcher)

        staging_dir = _run_pass2(analyzer, tmp_path)

        domain_file = staging_dir / f"{_DOMAIN_NAME}.md"
        assert domain_file.exists(), (
            "Domain file must be written from dispatcher output"
        )
        written = domain_file.read_text()
        # Strip the YAML frontmatter the SUT prepends — the body must equal expected_body.
        body = _strip_leading_yaml_frontmatter(written)
        assert body == expected_body, (
            f"Domain file body must equal dispatcher output verbatim.\n"
            f"Expected:\n{expected_body!r}\n"
            f"Got:\n{body!r}"
        )

    def test_pass2_source_contains_github_16732_comment(self):
        """
        The dependency_map_analyzer.py source must contain the GitHub issue #16732
        URL comment documenting the accepted degradation for Codex PostToolUse hooks.
        This guards against accidentally removing the warning.
        """
        import inspect
        import code_indexer.global_repos.dependency_map_analyzer as mod

        source = inspect.getsource(mod)
        assert "https://github.com/openai/codex/issues/16732" in source, (
            "GitHub issue #16732 reference comment is missing from dependency_map_analyzer.py"
        )
