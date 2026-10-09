"""Bug #2012 follow-up: credentials carried by a subprocess argv must never
leave the process -- not in diagnostics, logs, or raised exceptions.

Real subprocesses: `git clone`/`git fetch` against an unreachable URL
with embedded credentials (127.0.0.1:1 refuses at once), and real python
children. The secret is >= 6 chars so the output rule applies to it.
"""

import logging
import subprocess
import sys
from pathlib import Path

import pytest

SECRET = "FAKEsecret42"
CRED_URL = f"https://user:{SECRET}@127.0.0.1:1/x.git"


def _clean(text: object) -> None:
    rendered = str(text)
    assert SECRET not in rendered, f"secret leaked: {rendered!r}"
    assert "user:" not in rendered, f"URL userinfo leaked: {rendered!r}"


def test_module_is_stdlib_only_and_reexported_by_server() -> None:
    probe = (
        "import sys; import code_indexer.utils.credential_redaction; "
        "print(any(m.startswith('code_indexer.server') for m in sys.modules))"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        timeout=60,
        env={"PYTHONPATH": ":".join(sys.path)},
    )
    assert result.stdout.strip() == "False", result.stderr

    from code_indexer.server import logging_utils
    from code_indexer.utils import credential_redaction

    for name in ("mask_url_credentials", "redact_command", "redact_command_output"):
        assert getattr(logging_utils, name) is getattr(credential_redaction, name)


def test_failed_clone_diagnostic_is_redacted(tmp_path: Path) -> None:
    from code_indexer.utils.subprocess_diagnostics import (
        format_completed_process_diagnostic,
    )

    result = subprocess.run(
        ["git", "clone", CRED_URL, str(tmp_path / "x")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode != 0
    diagnostic = format_completed_process_diagnostic(result)
    _clean(diagnostic)
    assert "https://***@127.0.0.1:1/x.git" in diagnostic


def test_failed_reclone_critical_log_is_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.config import ConfigManager
    from code_indexer.global_repos.cleanup_manager import CleanupManager
    from code_indexer.global_repos.query_tracker import QueryTracker
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    golden = tmp_path / "golden_repos"
    (golden / "example-repo").mkdir(parents=True)
    tracker = QueryTracker()
    scheduler = RefreshScheduler(
        golden_repos_dir=str(golden),
        config_source=ConfigManager(tmp_path / ".code-indexer" / "config.json"),
        query_tracker=tracker,
        cleanup_manager=CleanupManager(tracker),
    )
    caplog.set_level(logging.CRITICAL)
    assert not scheduler._attempt_reclone(
        "example-repo-global", CRED_URL, str(golden / "example-repo")
    )
    critical = [r.getMessage() for r in caplog.records if r.levelno == logging.CRITICAL]
    assert critical, "the failed re-clone must still be reported"
    for message in critical:
        _clean(message)


def test_git_fetch_failure_error_and_log_are_redacted(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.global_repos.git_error_classifier import GitFetchError
    from code_indexer.global_repos.git_pull_updater import GitPullUpdater

    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (["init"], ["remote", "add", "origin", CRED_URL]):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    caplog.set_level(logging.WARNING)
    with pytest.raises(GitFetchError) as raised:
        GitPullUpdater(str(repo)).has_changes()
    _clean(raised.value)
    _clean(raised.value.stderr)
    _clean(raised.value.stdout or "")
    for record in caplog.records:
        _clean(record.getMessage())


def test_run_with_cancel_without_check_raises_redacted_errors() -> None:
    from code_indexer.server.utils.cancellable_subprocess import run_with_cancel

    echo = "import sys; print(' '.join(sys.argv[1:])); sys.exit(3)"
    with pytest.raises(subprocess.CalledProcessError) as failed:
        run_with_cancel(
            [sys.executable, "-c", echo, CRED_URL, "--token", SECRET],
            None,
            capture_output=True,
            text=True,
            check=True,
        )
    _clean(failed.value)
    _clean(failed.value.output)
    assert failed.value.__cause__ is None and failed.value.__suppress_context__

    sleep = (
        "import sys, time; print(' '.join(sys.argv[1:]), flush=True); time.sleep(60)"
    )
    with pytest.raises(subprocess.TimeoutExpired) as timed_out:
        run_with_cancel(
            [sys.executable, "-c", sleep, CRED_URL],
            None,
            capture_output=True,
            text=True,
            timeout=1,
        )
    _clean(timed_out.value)
    _clean(timed_out.value.output)

    with pytest.raises(TypeError) as bad:
        run_with_cancel(["git", "clone", CRED_URL], lambda: False, input="x")
    _clean(bad.value)


def test_output_rules() -> None:
    from code_indexer.utils.credential_redaction import (
        redact_command,
        redact_command_output,
    )

    # password echoed alone, and a token-only userinfo
    assert redact_command_output(f"auth failed for {SECRET}", [CRED_URL]) == (
        "auth failed for ***"
    )
    token_url = "https://glpatTOKEN123@example.com/r.git"
    assert "glpatTOKEN123" not in redact_command_output(
        "using glpatTOKEN123 now", [token_url]
    )
    # short values are not hunted in output; matching is word-bounded
    assert redact_command_output("abc abcdef", ["--token", "abc"]) == "abc abcdef"
    assert redact_command_output("xsecret99x secret99", ["--token", "secret99"]) == (
        "xsecret99x ***"
    )
    # credential.helper is configuration, not a secret; --no-auth is a flag
    assert redact_command(["git", "-c", "credential.helper=store", "fetch"]) == [
        "git",
        "-c",
        "credential.helper=store",
        "fetch",
    ]
    assert redact_command(["tool", "--no-auth", "origin"]) == [
        "tool",
        "--no-auth",
        "origin",
    ]
    # Authorization header values, in argv and in free text
    header = "http.extraHeader=Authorization: Bearer abcdef123456"
    assert "abcdef123456" not in " ".join(redact_command(["git", "-c", header]))
    assert redact_command_output("sent Authorization: Basic dXNlcjpwYXNz ok", []) == (
        "sent Authorization: *** ok"
    )
