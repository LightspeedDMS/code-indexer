"""
Shared fixtures for JobTracker unit tests.

Story #310: JobTracker Class, TrackedJob Dataclass, Schema Migration (Epic #261 Story 1A)
"""

import os
import sqlite3
import subprocess
from contextlib import ExitStack
from unittest.mock import patch

import pytest

from code_indexer.server.services.job_tracker import JobTracker


@pytest.fixture
def db_path(tmp_path):
    """Create a temporary database with the full background_jobs table schema."""
    db = tmp_path / "test.db"
    conn = sqlite3.connect(str(db))
    conn.execute(
        """CREATE TABLE IF NOT EXISTS background_jobs (
        job_id TEXT PRIMARY KEY NOT NULL,
        operation_type TEXT NOT NULL,
        status TEXT NOT NULL,
        created_at TEXT NOT NULL,
        started_at TEXT,
        completed_at TEXT,
        result TEXT,
        error TEXT,
        progress INTEGER NOT NULL DEFAULT 0,
        username TEXT NOT NULL,
        is_admin INTEGER NOT NULL DEFAULT 0,
        cancelled INTEGER NOT NULL DEFAULT 0,
        repo_alias TEXT,
        resolution_attempts INTEGER NOT NULL DEFAULT 0,
        claude_actions TEXT,
        failure_reason TEXT,
        extended_error TEXT,
        language_resolution_status TEXT,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )"""
    )
    conn.commit()
    conn.close()
    return str(db)


@pytest.fixture
def tracker(db_path):
    """Create a JobTracker connected to the temporary database."""
    return JobTracker(db_path)


@pytest.fixture(autouse=True)
def _disable_pace_maker_guard():
    """Disable pace-maker enforcement during tests.

    The research_assistant_service patch is conditional: the module requires
    optional dependencies (e.g. bleach) that may not be installed in all
    environments, and unittest.mock.patch only resolves a dotted path once the
    target module is already an attribute of its parent package (i.e. imported).
    If the module is unavailable or not yet imported, skip that patch — it is
    safe to do so because tests that actually exercise ResearchAssistantService
    will import the module themselves, which makes the attribute available.
    """
    import sys

    research_svc_key = "code_indexer.server.services.research_assistant_service"
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "code_indexer.server.services.claude_invoker.enforce_pace_maker_config"
            )
        )
        if research_svc_key in sys.modules:
            stack.enter_context(patch(f"{research_svc_key}.enforce_pace_maker_config"))
        yield


@pytest.fixture
def isolated_research_base_dir(tmp_path_factory):
    """
    Bug #1085: isolated root for Research Assistant session workspaces.

    Every research test (service, security-flags, cli-injection) is redirected
    here via the autouse ``_isolate_research_home`` fixture so that NO test ever
    writes into the developer's real ``~/.cidx-server/research``.

    The root is allocated via ``tmp_path_factory`` in its OWN pytest-managed temp
    directory (NOT the per-test ``tmp_path``), so the autouse redirect never
    pollutes the ``tmp_path`` that unrelated tests inspect. pytest removes the
    factory dirs automatically, so workspaces are torn down after the run.
    """
    return tmp_path_factory.mktemp("research_home")


@pytest.fixture(autouse=True)
def _isolate_research_home(isolated_research_base_dir):
    """
    Bug #1085 (autouse): redirect the Research Assistant default base dir to the
    isolated temp dir for the duration of every test in this package.

    This patches the single ``_default_research_base_dir`` seam so even services
    constructed the legacy way (``ResearchAssistantService(db_path=temp_db)``
    with no explicit ``research_base_dir``) land under the per-test tmp dir and
    are auto-removed by pytest -- the root cause of the 22k leaked dirs.

    Under ``PYTHONPATH=./src`` the service is importable under two distinct
    module objects (``code_indexer...`` and ``src.code_indexer...``); test files
    import via the ``src.`` namespace, so both aliases must be patched.
    """
    import contextlib
    import importlib

    module_names = [
        "code_indexer.server.services.research_assistant_service",
        "src.code_indexer.server.services.research_assistant_service",
    ]
    with contextlib.ExitStack() as stack:
        for module_name in module_names:
            try:
                importlib.import_module(module_name)
            except ImportError:
                continue
            stack.enter_context(
                patch(
                    f"{module_name}._default_research_base_dir",
                    return_value=isolated_research_base_dir,
                )
            )
        yield


def _run_git_fixture_command(args, cwd, env):
    """Run a git command for fixture setup, raising loudly on failure.

    Bug #1572: fixture-construction helper only -- not used by any test
    assertion. A failure here means the fixture itself is broken, so it must
    fail fast and loud rather than let a broken fixture masquerade as a
    passing (or flaky) test.
    """
    result = subprocess.run(
        args,
        cwd=cwd,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        raise RuntimeError(
            f"git fixture setup command failed: {args}\n"
            f"stdout={result.stdout}\nstderr={result.stderr}"
        )
    return result


@pytest.fixture(scope="session")
def remote_git_repo_dir(tmp_path_factory):
    """
    Bug #1572: real local bare git repository for RemoteBranchService tests.

    `git ls-remote` behaves identically against a local path/`file://` URL as
    it does against a real HTTPS remote. This fixture builds ONE genuine bare
    repository (session-scoped -- paid once for the whole test session) via a
    real working clone that is pushed and then discarded, replacing the
    previous live `github.com` calls with a real, deterministic, network-free
    git repository.

    Branch set is deliberately chosen to exercise the behaviours the tests
    assert on end-to-end through the real service:
    - "main": the default branch (bare repo HEAD symref points here, exactly
      what `_detect_default_branch`'s `git ls-remote --symref` reads).
    - "develop", "feature/login": ordinary branches, including a
      slash-containing name.
    - "SCM-1234", "feature/SCM-1234-hotfix": issue-tracker-pattern branches
      that `filter_issue_tracker_branches` must exclude from the result, so
      the issue-tracker-filtering test exercises real filtering rather than
      trivially passing on an empty case.
    """
    base = tmp_path_factory.mktemp("remote_branch_service_repo")
    bare_repo = base / "remote.git"
    work_clone = base / "work"

    git_env = dict(os.environ)
    git_env.update(
        {
            "GIT_AUTHOR_NAME": "Bug1572 Fixture",
            "GIT_AUTHOR_EMAIL": "bug1572@example.invalid",
            "GIT_COMMITTER_NAME": "Bug1572 Fixture",
            "GIT_COMMITTER_EMAIL": "bug1572@example.invalid",
            "GIT_TERMINAL_PROMPT": "0",
        }
    )

    # Bare "remote" repo whose HEAD symref points at "main" -- this is what
    # RemoteBranchService._detect_default_branch reads via
    # `git ls-remote --symref <url> HEAD`.
    _run_git_fixture_command(
        ["git", "init", "--bare", "-b", "main", str(bare_repo)],
        cwd=str(base),
        env=git_env,
    )

    # Scratch working clone used only to populate the bare repo with a known
    # branch set, then pushed once and discarded (never read by tests).
    _run_git_fixture_command(
        ["git", "init", "-b", "main", str(work_clone)], cwd=str(base), env=git_env
    )
    (work_clone / "README.md").write_text("Bug #1572 fixture repo\n")
    _run_git_fixture_command(
        ["git", "add", "README.md"], cwd=str(work_clone), env=git_env
    )
    _run_git_fixture_command(
        ["git", "commit", "-m", "initial commit"], cwd=str(work_clone), env=git_env
    )

    other_branches = ["develop", "feature/login", "SCM-1234", "feature/SCM-1234-hotfix"]
    for branch_name in other_branches:
        _run_git_fixture_command(
            ["git", "checkout", "-b", branch_name, "main"],
            cwd=str(work_clone),
            env=git_env,
        )
    _run_git_fixture_command(
        ["git", "checkout", "main"], cwd=str(work_clone), env=git_env
    )

    _run_git_fixture_command(
        ["git", "push", str(bare_repo), "main", *other_branches],
        cwd=str(work_clone),
        env=git_env,
    )

    return str(bare_repo)


@pytest.fixture(scope="session")
def non_git_repo_dir(tmp_path_factory):
    """
    Bug #1572: a real local directory that is definitively NOT a git repository.

    Used for RemoteBranchService failure-path tests: `git ls-remote` against
    this path fails deterministically (non-zero exit, git's own "does not
    appear to be a git repository" on stderr) with zero network involvement
    -- replacing a previous dependency on how DNS/GitHub answer for a
    nonexistent repository name.
    """
    return str(tmp_path_factory.mktemp("not_a_git_repo"))
