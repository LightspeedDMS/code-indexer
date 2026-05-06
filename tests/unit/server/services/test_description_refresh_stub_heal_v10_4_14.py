"""
Unit tests for v10.4.14 stub-healing in DescriptionRefreshScheduler.

Existing on-disk stub descriptions (artifact of v10.4.9 wipe-and-replace bug
or pre-v10.4.13 README/static-regex fallback) were NOT healed by the periodic
refresh path: _get_refresh_prompt() bailed out with WARN on missing last_analyzed
or terse body, never attempting full regeneration.

v10.4.14 adds stub-detection logic inside _get_refresh_prompt: when a stub is
detected (missing last_analyzed OR body length < _STUB_BODY_CHAR_THRESHOLD), it
dispatches a FULL re-analysis via the same code path on_repo_added uses, then
returns None (signaling "no incremental refresh needed - full regen completed").

Stub-detection criteria (logical OR):
  (a) desc_data.get("last_analyzed") is None or empty string
  (b) body length (chars of content after YAML frontmatter) < _STUB_BODY_CHAR_THRESHOLD (800)

Anti-mock strategy:
  - Real scheduler is constructed with injectable backends (MagicMock).
  - Real _meta_dir with real .md files so _read_existing_description runs for real.
  - Only module-level boundary functions are patched:
      get_claude_cli_manager  (OS/singleton boundary)
      _generate_repo_description  (Claude CLI boundary - external process)
      atomic_write_description  (filesystem write boundary)
  - No internal scheduler methods are patched.
  - MagicMock(spec=ClaudeCliManager) satisfies isinstance() guards in production code.

Test inventory:
  TestStubThresholdConstant:
    test_threshold_default_is_800

  TestStubDetectionCriteria:
    test_missing_last_analyzed_triggers_full_regen
    test_short_body_triggers_full_regen
    test_both_absent_triggers_full_regen
    test_well_formed_description_no_stub_heal_invoked

  TestFullRegenInvocation:
    test_full_regen_calls_generate_repo_description_with_real_cli_manager
    test_full_regen_uses_alias_form_filename
    test_full_regen_writes_returned_md_content

  TestFullRegenFailureModes:
    test_cli_manager_none_logs_error_and_returns_none
    test_cli_unavailable_logs_error_and_returns_none
    test_generate_repo_description_runtime_error_logs_and_returns_none
    test_repo_url_lookup_failure_logs_and_returns_none
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path
from typing import Iterator, Optional
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Shared constants
# ---------------------------------------------------------------------------

_PAST_TIME = "2000-01-01T00:00:00+00:00"
_LONG_BODY = "x" * 900  # > 800 chars - well-formed
_SHORT_BODY = "x" * 100  # < 800 chars - stub
_DEFAULT_REPO_URL = "git@github.com:org/repo.git"
_SENTINEL_MD = "---\nname: x\nurl: y\nlast_analyzed: now\n---\nSENTINEL_BODY"

# Module paths for patching boundary functions.
# The heal function uses lazy imports, so names are looked up in their home modules.
_SCHEDULER_MODULE = "code_indexer.server.services.description_refresh_scheduler"
_CLI_MANAGER_MODULE = "code_indexer.server.services.claude_cli_manager"
_HOOK_MODULE = "code_indexer.global_repos.meta_description_hook"


# ---------------------------------------------------------------------------
# Shared builder helpers
# ---------------------------------------------------------------------------


def _make_full_content(
    alias: str,
    url: str,
    body: str,
    last_analyzed: Optional[str] = _PAST_TIME,
) -> str:
    """Return a synthetic .md file string with YAML frontmatter and *body*."""
    if last_analyzed:
        fm = f"---\nname: {alias}\nurl: {url}\nlast_analyzed: {last_analyzed}\n---\n"
    else:
        fm = f"---\nname: {alias}\nurl: {url}\n---\n"
    return fm + body


def _make_scheduler_with_meta(
    tmp_path: Path,
    alias: str,
    file_content: str,
    repo_url: str = _DEFAULT_REPO_URL,
):
    """
    Build a DescriptionRefreshScheduler with a real meta_dir containing one .md file.

    golden_backend.get_repo returns repo_url + clone_path so the stub healer
    can resolve the repo URL from the database without additional external calls.
    Uses a real _meta_dir so _read_existing_description works without mocking.

    Returns (scheduler, clone_dir).
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir(parents=True, exist_ok=True)
    (meta_dir / f"{alias}.md").write_text(file_content, encoding="utf-8")

    # _validate_refresh_inputs checks that clone_path resolves to an existing dir
    clone_dir = tmp_path / "repos" / alias
    clone_dir.mkdir(parents=True, exist_ok=True)

    tracking_backend = MagicMock(name="tracking_backend")
    golden_backend = MagicMock(name="golden_backend")
    golden_backend.get_repo.return_value = {
        "clone_path": str(clone_dir),
        "repo_url": repo_url,
    }

    config = ServerConfig(server_dir=str(tmp_path))
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = 24

    config_manager = MagicMock(name="config_manager")
    config_manager.load_config.return_value = config

    scheduler = DescriptionRefreshScheduler(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        config_manager=config_manager,
        claude_cli_manager=MagicMock(name="claude_cli_manager"),
        meta_dir=meta_dir,
    )
    return scheduler, clone_dir


def _make_cli_mock():
    """Return MagicMock(spec=ClaudeCliManager) with check_cli_available=True."""
    from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

    mock_cli = MagicMock(spec=ClaudeCliManager)
    mock_cli.check_cli_available.return_value = True
    return mock_cli


def _make_stub_scheduler(tmp_path: Path, alias: str):
    """
    Build a (scheduler, clone_dir) pair with a stub description (short body,
    last_analyzed present) ready for stub-heal tests.  Uses _DEFAULT_REPO_URL.
    """
    content = _make_full_content(alias, _DEFAULT_REPO_URL, _SHORT_BODY, _PAST_TIME)
    return _make_scheduler_with_meta(tmp_path, alias, content)


def _extract_error_messages(caplog) -> list:
    """Return the .message string for every ERROR-or-above record in caplog."""
    return [r.message for r in caplog.records if r.levelno >= logging.ERROR]


@contextlib.contextmanager
def _with_stub_heal_patches(
    cli_mock,
    gen_return_value: str = _SENTINEL_MD,
) -> Iterator[tuple]:
    """
    Context manager that patches the three module-level boundary functions used
    by the stub-healing path in _get_refresh_prompt.

    The heal function uses lazy imports so names are resolved in their home modules:
      - get_claude_cli_manager  in _CLI_MANAGER_MODULE  -> returns cli_mock
      - _generate_repo_description  in _HOOK_MODULE  -> returns gen_return_value
      - atomic_write_description  in _HOOK_MODULE  -> no-op, call-args inspectable

    Yields (mock_gen, mock_write) so callers can assert call details.
    """
    with (
        patch(f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager", return_value=cli_mock),
        patch(
            f"{_HOOK_MODULE}._generate_repo_description",
            return_value=gen_return_value,
        ) as mock_gen,
        patch(
            f"{_HOOK_MODULE}.atomic_write_description",
        ) as mock_write,
    ):
        yield mock_gen, mock_write


# ---------------------------------------------------------------------------
# TestStubThresholdConstant
# ---------------------------------------------------------------------------


class TestStubThresholdConstant:
    def test_threshold_default_is_800(self) -> None:
        """_STUB_BODY_CHAR_THRESHOLD must be 800 (per v10.4.14 spec)."""
        from code_indexer.server.services.description_refresh_scheduler import (
            _STUB_BODY_CHAR_THRESHOLD,
        )

        assert _STUB_BODY_CHAR_THRESHOLD == 800


# ---------------------------------------------------------------------------
# TestStubDetectionCriteria
# ---------------------------------------------------------------------------


class TestStubDetectionCriteria:
    @pytest.mark.parametrize(
        "body,last_analyzed,scenario",
        [
            (_LONG_BODY, None, "missing_last_analyzed"),
            (_SHORT_BODY, _PAST_TIME, "short_body_only"),
            (_SHORT_BODY, None, "both_missing"),
        ],
    )
    def test_stub_detection_triggers_full_regen(
        self,
        tmp_path: Path,
        body: str,
        last_analyzed: Optional[str],
        scenario: str,
    ) -> None:
        """
        Any stub-detection criterion triggers full regen:
          - missing_last_analyzed: body > 800 but no last_analyzed field
          - short_body_only: last_analyzed present but body < 800 chars
          - both_missing: both criteria fail simultaneously

        In all cases _get_refresh_prompt must return None (regen complete).
        """
        alias = f"repo-{scenario}"
        content = _make_full_content(alias, _DEFAULT_REPO_URL, body, last_analyzed)
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)

        with _with_stub_heal_patches(_make_cli_mock()) as (mock_gen, _):
            result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        assert result is None, f"[{scenario}] must return None after stub-heal regen"
        mock_gen.assert_called_once()

    def test_well_formed_description_no_stub_heal_invoked(self, tmp_path: Path) -> None:
        """
        Body length > 800 AND last_analyzed present -> NOT a stub -> stub-heal external
        boundary (_generate_repo_description) must NOT be called.

        _stage_and_build_prompt calls real RepoAnalyzer which may raise on a
        synthetic repo; we wrap in try/except so the assertion always executes.
        """
        alias = "repo-well-formed"
        content = _make_full_content(
            alias, _DEFAULT_REPO_URL, _LONG_BODY, last_analyzed=_PAST_TIME
        )
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)

        with patch(f"{_HOOK_MODULE}._generate_repo_description") as mock_gen:
            try:
                scheduler._get_refresh_prompt(alias, str(clone_dir))
            except Exception:  # noqa: BLE001  # intentional discard: see rationale below
                # EXPLICIT DISCARD: _stage_and_build_prompt calls RepoAnalyzer on disk.
                # A synthetic tmp_path has no git history so RepoAnalyzer may raise.
                # That downstream failure is irrelevant to this test: the assertion is
                # ONLY that the stub-heal path was NOT entered (mock_gen not called).
                # Any exception here means the code correctly bypassed stub-heal AND
                # failed at the downstream incremental-refresh step — which is expected.
                pass

        mock_gen.assert_not_called()


# ---------------------------------------------------------------------------
# TestFullRegenInvocation
# ---------------------------------------------------------------------------


class TestFullRegenInvocation:
    """Verify _heal_stub_description dispatches correctly to external boundaries."""

    def test_full_regen_calls_generate_repo_description_with_real_cli_manager(
        self, tmp_path: Path
    ) -> None:
        """
        _generate_repo_description must be called once, and its 4th positional arg
        (cli_manager) must be the SAME object returned by get_claude_cli_manager.
        """
        alias = "repo-regen-cli"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        cli_mock = _make_cli_mock()
        with _with_stub_heal_patches(cli_mock) as (mock_gen, _):
            scheduler._get_refresh_prompt(alias, str(clone_dir))

        mock_gen.assert_called_once()
        assert mock_gen.call_args[0][3] is cli_mock, (
            "4th positional arg to _generate_repo_description must be the "
            "ClaudeCliManager returned by get_claude_cli_manager"
        )

    def test_full_regen_uses_alias_form_filename(self, tmp_path: Path) -> None:
        """
        atomic_write_description must be called with target ending in
        '{alias}-global.md' (v10.4.9 alias-form convention).
        """
        alias = "my-repo"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with _with_stub_heal_patches(_make_cli_mock()) as (_, mock_write):
            scheduler._get_refresh_prompt(alias, str(clone_dir))

        mock_write.assert_called_once()
        target_path = mock_write.call_args[0][0]
        assert str(target_path).endswith(f"{alias}-global.md"), (
            f"atomic_write_description target must end with '{alias}-global.md', "
            f"got: {target_path}"
        )

    def test_full_regen_writes_returned_md_content(self, tmp_path: Path) -> None:
        """
        atomic_write_description must receive the exact string returned by
        _generate_repo_description (no transformation).
        """
        alias = "repo-content-check"
        sentinel_content = "SENTINEL_MD_CONTENT_12345"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with _with_stub_heal_patches(
            _make_cli_mock(), gen_return_value=sentinel_content
        ) as (_, mock_write):
            scheduler._get_refresh_prompt(alias, str(clone_dir))

        mock_write.assert_called_once()
        assert mock_write.call_args[0][1] == sentinel_content, (
            f"atomic_write_description must receive exact content from "
            f"_generate_repo_description; got: {mock_write.call_args[0][1]!r}"
        )


# ---------------------------------------------------------------------------
# TestFullRegenFailureModes
# ---------------------------------------------------------------------------


class TestFullRegenFailureModes:
    """
    Messi Rule #13 anti-silent-failure: error paths reachable via _get_refresh_prompt
    when stub detection triggers the heal branch.

    v10.4.14+ contract update: when heal preconditions fail (cli_manager absent or
    CLI unavailable) but last_analyzed IS present, _get_refresh_prompt falls through
    to incremental refresh (_stage_and_build_prompt) and returns a prompt string —
    not None.  Runtime failures (HEAL-005, HEAL-010) still return None.
    """

    @pytest.mark.parametrize(
        "cli_available,scenario",
        [
            (None, "cli_manager_is_none"),
            (False, "check_cli_available_false"),
        ],
    )
    def test_cli_not_available_logs_warning_and_falls_through_to_incremental(
        self,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
        cli_available: Optional[bool],
        scenario: str,
    ) -> None:
        """
        When the CLI manager is absent (None) or reports CLI unavailable (False),
        _heal_stub_description returns False (preconditions unmet).  Because
        _make_stub_scheduler provides last_analyzed=_PAST_TIME, _get_refresh_prompt
        falls through to incremental refresh and returns a prompt string (not None).

        _generate_repo_description and atomic_write_description must NOT be called
        (heal was abandoned before dispatch).  WARNING DESC-REFRESH-STUB-HEAL-004
        must still be emitted.

        v10.4.14+ optimistic-heal contract: precondition failure + last_analyzed
        present => incremental refresh, not silent skip.
        """
        alias = f"repo-{scenario}"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)

        from code_indexer.server.services.claude_cli_manager import ClaudeCliManager

        if cli_available is None:
            cli_return = None
        else:
            cli_return = MagicMock(spec=ClaudeCliManager)
            cli_return.check_cli_available.return_value = cli_available

        with (
            patch(
                f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager",
                return_value=cli_return,
            ),
            patch(f"{_HOOK_MODULE}._generate_repo_description") as mock_gen,
            patch(f"{_HOOK_MODULE}.atomic_write_description") as mock_write,
            caplog.at_level(logging.WARNING),
        ):
            result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        # Preconditions unmet + last_analyzed present => falls through to incremental.
        assert isinstance(result, str), (
            f"[{scenario}] expected a prompt string (incremental refresh fallback), "
            f"got: {result!r}"
        )
        mock_gen.assert_not_called()
        mock_write.assert_not_called()
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("DESC-REFRESH-STUB-HEAL-004" in msg for msg in warning_messages), (
            f"[{scenario}] expected DESC-REFRESH-STUB-HEAL-004 at WARNING level; "
            f"got warnings: {warning_messages}"
        )

    def test_generate_repo_description_runtime_error_logs_and_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        _generate_repo_description raises RuntimeError -> _get_refresh_prompt returns
        None. The generate function IS invoked (and raises); atomic_write_description
        must NOT be called (no half-baked write). ERROR DESC-REFRESH-STUB-HEAL-005 emitted.
        """
        alias = "repo-gen-raises"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with (
            patch(
                f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager",
                return_value=_make_cli_mock(),
            ),
            patch(
                f"{_HOOK_MODULE}._generate_repo_description",
                side_effect=RuntimeError("simulated v10.4.13 anti-fallback"),
            ) as mock_gen,
            patch(f"{_HOOK_MODULE}.atomic_write_description") as mock_write,
            caplog.at_level(logging.ERROR),
        ):
            result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        assert result is None
        mock_gen.assert_called_once()  # was invoked, raised
        mock_write.assert_not_called()  # write suppressed after exception
        assert any(
            "DESC-REFRESH-STUB-HEAL-005" in msg
            for msg in _extract_error_messages(caplog)
        ), (
            f"Expected DESC-REFRESH-STUB-HEAL-005; got: {_extract_error_messages(caplog)}"
        )

    def test_repo_url_lookup_failure_logs_and_returns_none(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        golden_backend.get_repo raises (repo_url lookup failure) ->
        _get_refresh_prompt returns None, both _generate_repo_description and
        atomic_write_description suppressed, ERROR DESC-REFRESH-STUB-HEAL-006 emitted.

        Setup uses last_analyzed=None so the v10.4.14 fall-through-to-incremental-
        refresh path cannot fire (no last_analyzed = nothing to incrementally
        refresh from). This isolates the test to the repo_url lookup failure path.
        """
        alias = "repo-url-fails"
        content = _make_full_content(
            alias, _DEFAULT_REPO_URL, _SHORT_BODY, last_analyzed=None
        )
        scheduler, clone_dir = _make_scheduler_with_meta(tmp_path, alias, content)
        scheduler._golden_backend.get_repo.side_effect = RuntimeError(
            "DB connection lost"
        )
        with (
            patch(
                f"{_CLI_MANAGER_MODULE}.get_claude_cli_manager",
                return_value=_make_cli_mock(),
            ),
            patch(f"{_HOOK_MODULE}._generate_repo_description") as mock_gen,
            patch(f"{_HOOK_MODULE}.atomic_write_description") as mock_write,
            caplog.at_level(logging.ERROR),
        ):
            result = scheduler._get_refresh_prompt(alias, str(clone_dir))

        assert result is None
        mock_gen.assert_not_called()
        mock_write.assert_not_called()
        assert any(
            "DESC-REFRESH-STUB-HEAL-006" in msg
            for msg in _extract_error_messages(caplog)
        ), (
            f"Expected DESC-REFRESH-STUB-HEAL-006; got: {_extract_error_messages(caplog)}"
        )


# ---------------------------------------------------------------------------
# TestStubHealLogging
# ---------------------------------------------------------------------------


class TestStubHealLogging:
    """Verify informational logging emitted during a successful stub-heal dispatch."""

    def test_dispatch_emits_info_log_with_alias(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """
        Successful stub-heal dispatch (all collaborators mocked to succeed) must
        emit an INFO log containing DESC-REFRESH-STUB-HEAL-001 and the repo alias.
        """
        alias = "repo-log-check"
        scheduler, clone_dir = _make_stub_scheduler(tmp_path, alias)
        with (
            _with_stub_heal_patches(_make_cli_mock()),
            caplog.at_level(logging.INFO),
        ):
            scheduler._get_refresh_prompt(alias, str(clone_dir))

        info_messages = [r.message for r in caplog.records if r.levelno == logging.INFO]
        assert any(
            "DESC-REFRESH-STUB-HEAL-001" in msg and alias in msg
            for msg in info_messages
        ), (
            f"Expected INFO log with DESC-REFRESH-STUB-HEAL-001 and alias '{alias}'; "
            f"got INFO messages: {info_messages}"
        )
