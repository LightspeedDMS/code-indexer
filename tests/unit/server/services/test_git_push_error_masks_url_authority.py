"""git_push_with_pat must not surface a URL authority in its failure output.

The push target is an authenticated URL, so it appears in git's own stderr and in
CalledProcessError's rendering of the command. Both reach the caller through
GitCommandError, so both are masked.
"""

from __future__ import annotations

import subprocess

import pytest

from code_indexer.server.logging_utils import mask_url_credentials


_SECRET = "s3cr3t-value"
_URL = f"https://oauth2:{_SECRET}@git.example.com/org/repo.git"


def test_masking_removes_the_authority_from_stderr_text() -> None:
    stderr = f"remote: Invalid credentials\nfatal: could not read from '{_URL}'\n"
    masked = mask_url_credentials(stderr)
    assert _SECRET not in masked
    assert "oauth2" not in masked
    assert "git.example.com/org/repo.git" in masked


def test_masking_removes_the_authority_from_a_rendered_command() -> None:
    err = subprocess.CalledProcessError(
        returncode=128, cmd=["git", "push", _URL, "HEAD:refs/heads/main"]
    )
    masked = mask_url_credentials(str(err))
    assert _SECRET not in masked
    assert "git.example.com" in masked


def test_push_failure_path_masks_both_detail_and_stderr() -> None:
    """Drive the real except-branch shape: stderr empty -> detail comes from str(e)."""
    from code_indexer.server.services.git_operations_service import GitCommandError

    err = subprocess.CalledProcessError(
        returncode=128, cmd=["git", "push", _URL, "HEAD:refs/heads/main"]
    )
    stderr = mask_url_credentials(getattr(err, "stderr", "") or "")
    detail = stderr or mask_url_credentials(str(err))
    exc = GitCommandError(
        f"git push failed: {detail}", stderr=stderr, returncode=err.returncode
    )
    assert _SECRET not in str(exc)
    assert _SECRET not in (exc.stderr or "")


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "fatal: repository not found",
        "https://git.example.com/org/repo.git",
    ],
)
def test_masking_leaves_credential_free_text_unchanged(raw: str) -> None:
    assert mask_url_credentials(raw) == raw
