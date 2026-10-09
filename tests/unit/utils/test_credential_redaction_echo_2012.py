"""Bug #2012 follow-up: credentials echoed in a subprocess's output are
masked even when the output repeats only the bare value -- an
Authorization-header token, or a URL password in decoded form -- and a
SUCCESSFUL git command's logged stdout is redacted too.
"""

import base64
import logging
import os
import shutil
import subprocess
from pathlib import Path
from urllib.parse import quote

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


@pytest.mark.parametrize(
    "echoed",
    [
        "abc/def:ghi",  # plain
        "abc%2Fdef%3Aghi",  # uppercase escapes
        "abc%2fdef%3aghi",  # lowercase escapes
        "abc%2fdef%3Aghi",  # mixed case escapes
        "abc/def%3aghi",  # partly encoded (safe="/" form), lowercase
    ],
)
def test_supplied_secret_masked_in_every_percent_encoding_case(echoed: str) -> None:
    out = redact_command_output(
        f"hook said [{echoed}] here", ["git", "push"], ["abc/def:ghi"]
    )
    assert out == "hook said [***] here"


_ODD_SECRET = "s3cr/t:v@l ue+x"
_ODD_USER = "example-user"
_ODD_URL = f"https://{_ODD_USER}:{quote(_ODD_SECRET, safe='')}@git.example.com/r.git"


def _b64(text: str) -> str:
    return base64.b64encode(text.encode()).decode()


@pytest.mark.parametrize(
    "echoed",
    [
        "s3cr%2Ft%3Av%40l%20ue%2Bx",  # fully encoded
        "s3cr/t%3av@l%20ue+x",  # partially encoded, lowercase escape
        "s3cr%2ft:v%40l+ue%2Bx",  # partial, '+' as space, mixed case
        "s3cr%252Ft%253Av%2540l%2520ue%252Bx",  # double encoded
        "s3cr%252ft:v%2540l+ue%252bx",  # double, partial, '+' as space
        _b64(_ODD_SECRET),  # standalone base64 of the secret
        _b64(_ODD_SECRET).rstrip("="),  # unpadded
        _b64(f"{_ODD_USER}:{_ODD_SECRET}"),  # Basic credential
    ],
)
def test_supplied_secret_masked_in_every_encoding(echoed: str) -> None:
    out = redact_command_output(
        f"remote said [{echoed}] then 'done'", [_ODD_URL], [_ODD_SECRET]
    )
    assert out == "remote said [***] then 'done'"


def test_authorization_basic_header_for_supplied_secret_is_masked() -> None:
    basic = _b64(f"{_ODD_USER}:{_ODD_SECRET}")
    out = redact_command_output(
        f"> Authorization: Basic {basic}\n", [_ODD_URL], [_ODD_SECRET]
    )
    assert basic not in out and _ODD_SECRET not in out


def test_unrelated_output_stays_readable_with_a_supplied_secret() -> None:
    text = (
        "From https://git.example.com/r.git\n"
        "   1a2b3c4..5d6e7f8  main       -> origin/main\n"
        "Updating a%20b+c files: 100% (3/3), done. dGVzdA==\n"
    )
    assert redact_command_output(text, [_ODD_URL], [_ODD_SECRET]) == text


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
