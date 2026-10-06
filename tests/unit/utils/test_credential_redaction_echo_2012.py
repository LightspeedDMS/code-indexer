"""Bug #2012 follow-up: credentials echoed in a subprocess's output are
masked even when the output repeats only the bare value -- an
Authorization-header token, or a URL password in decoded form -- and a
SUCCESSFUL git command's logged stdout is redacted too.
"""

import logging
import os
import shutil
import subprocess
from pathlib import Path

import pytest

from code_indexer.utils.credential_redaction import redact_command_output

SECRET = "FAKEsecret42"
CRED_URL = f"https://user:{SECRET}@127.0.0.1:1/x.git"


def test_header_credential_echo_is_redacted() -> None:
    header = "http.extraHeader=Authorization: Bearer FAKEtoken123"
    assert redact_command_output(
        "token FAKEtoken123 was rejected", ["git", "-c", header, "fetch"]
    ) == ("token *** was rejected")
    curl_style = ["curl", "-H", "Authorization: Bearer FAKEtoken123", "x"]
    assert "FAKEtoken123" not in redact_command_output(
        "server said FAKEtoken123 is expired", curl_style
    )


def test_percent_encoded_password_echo_is_redacted() -> None:
    url = "https://user:p%40ss-word9@example.com/r.git"
    out = redact_command_output("tried p@ss-word9 and p%40ss-word9", [url])
    assert "p@ss-word9" not in out
    assert "p%40ss-word9" not in out


def test_successful_git_output_logs_are_redacted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from code_indexer.global_repos.git_pull_updater import GitPullUpdater

    real_git = shutil.which("git")
    assert real_git is not None
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run([real_git, "init"], cwd=repo, check=True, capture_output=True)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake = bin_dir / "git"
    fake.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "pull" ]; then echo "From {CRED_URL}"; exit 0; fi\n'
        f'exec "{real_git}" "$@"\n'
    )
    fake.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")

    caplog.set_level(logging.INFO)
    GitPullUpdater(str(repo)).update()
    pulled = [
        r.getMessage() for r in caplog.records if "pull successful" in r.getMessage()
    ]
    assert pulled, "the successful pull must still be logged"
    for message in pulled:
        assert SECRET not in message, message
        assert "user:" not in message, message
