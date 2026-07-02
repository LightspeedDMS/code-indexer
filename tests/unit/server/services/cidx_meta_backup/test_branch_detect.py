"""Unit tests for Story #926 branch detection."""

from subprocess import CompletedProcess, TimeoutExpired
from unittest.mock import patch


def test_detect_default_branch_parses_master():
    """# Story #926 AC1: detect_default_branch parses 'master' from git remote show output."""
    from code_indexer.server.services.cidx_meta_backup.branch_detect import (
        detect_default_branch,
    )

    completed = CompletedProcess(
        args=["git", "remote", "show", "origin"],
        returncode=0,
        stdout="  HEAD branch: master\n",
        stderr="",
    )
    with patch(
        "code_indexer.server.services.cidx_meta_backup.branch_detect.subprocess.run",
        return_value=completed,
    ):
        assert detect_default_branch("/tmp/repo") == "master"


def test_detect_default_branch_parses_main():
    """# Story #926 AC1: detect_default_branch parses 'main' from git remote show output."""
    from code_indexer.server.services.cidx_meta_backup.branch_detect import (
        detect_default_branch,
    )

    completed = CompletedProcess(
        args=["git", "remote", "show", "origin"],
        returncode=0,
        stdout="Fetch URL: file:///tmp/origin.git\n  HEAD branch: main\n",
        stderr="",
    )
    with patch(
        "code_indexer.server.services.cidx_meta_backup.branch_detect.subprocess.run",
        return_value=completed,
    ):
        assert detect_default_branch("/tmp/repo") == "main"


def test_detect_default_branch_returns_none_on_timeout():
    """# Story #926 AC1: detect_default_branch returns None on subprocess timeout."""
    from code_indexer.server.services.cidx_meta_backup.branch_detect import (
        detect_default_branch,
    )

    with patch(
        "code_indexer.server.services.cidx_meta_backup.branch_detect.subprocess.run",
        side_effect=TimeoutExpired(cmd=["git"], timeout=30),
    ):
        assert detect_default_branch("/tmp/repo") is None


def test_detect_default_branch_returns_none_on_parse_failure():
    """# Story #926 AC1: detect_default_branch returns None when HEAD branch line is absent."""
    from code_indexer.server.services.cidx_meta_backup.branch_detect import (
        detect_default_branch,
    )

    completed = CompletedProcess(
        args=["git", "remote", "show", "origin"],
        returncode=0,
        stdout="Fetch URL: file:///tmp/origin.git\n",
        stderr="",
    )
    with patch(
        "code_indexer.server.services.cidx_meta_backup.branch_detect.subprocess.run",
        return_value=completed,
    ):
        assert detect_default_branch("/tmp/repo") is None
