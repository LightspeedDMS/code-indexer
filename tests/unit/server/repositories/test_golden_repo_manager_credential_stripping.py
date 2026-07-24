"""EVO-64616: golden-repo URLs are credential-stripped before persistence when the
git backend owns credentials (delegated → git-mcp), so the PAT lives ONLY in
git-mcp — and left intact in local mode, where in-process git needs the token.

These assert on the REAL security artifacts, not a proxy:
  * the background-job ``metadata["repo_url"]`` that pod-pull re-reads to
    reconstruct the clone (the submit path), and
  * the URL handed to the clone at the persistence boundary
    (``execute_add_golden_repo_work``, reachable directly from that metadata).
A regression that stripped only the pre-flight probe would leave both dirty and
fail here.
"""

from __future__ import annotations

from unittest.mock import Mock

import pytest

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

PAT_URL = "https://oauth2:glpat-SECRET123@gitlab.com/org/repo.git"
CLEAN_URL = "https://gitlab.com/org/repo.git"


def _manager(tmp_path, *, owns_credentials: bool) -> GoldenRepoManager:
    m = GoldenRepoManager(data_dir=str(tmp_path))
    m._git_backend = Mock()
    m._git_backend.owns_credentials = owns_credentials
    m.background_job_manager = Mock()
    m.background_job_manager.submit_job = Mock(return_value="job-1")
    m._validate_git_repository = Mock(return_value=True)  # type: ignore[method-assign]
    return m


def _submitted_metadata_url(m: GoldenRepoManager) -> str:
    return m.background_job_manager.submit_job.call_args.kwargs["metadata"]["repo_url"]


class TestAddGoldenRepoSubmitPath:
    def test_job_metadata_and_probe_are_stripped_when_owning(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=True)

        m.add_golden_repo(repo_url=PAT_URL, alias="pat-strip")

        # The artifact pod-pull re-reads must be credential-free...
        assert _submitted_metadata_url(m) == CLEAN_URL
        # ...and so must the pre-flight probe (both fed by the one repo_url).
        m._validate_git_repository.assert_called_once_with(CLEAN_URL)

    def test_job_metadata_keeps_pat_in_local_mode(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=False)

        m.add_golden_repo(repo_url=PAT_URL, alias="pat-keep")

        assert _submitted_metadata_url(m) == PAT_URL
        m._validate_git_repository.assert_called_once_with(PAT_URL)

    def test_uppercase_scheme_is_still_stripped(self, tmp_path):
        # RFC 3986 §3.1: schemes are case-insensitive; the guard must not be fooled
        # by HTTPS:// into persisting the credential.
        m = _manager(tmp_path, owns_credentials=True)

        m.add_golden_repo(
            repo_url="HTTPS://oauth2:glpat-SECRET123@gitlab.com/org/repo.git",
            alias="pat-upper",
        )

        assert "glpat" not in _submitted_metadata_url(m)
        assert _submitted_metadata_url(m) == "HTTPS://gitlab.com/org/repo.git"

    def test_credential_free_url_unchanged_when_owning(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=True)

        m.add_golden_repo(repo_url=CLEAN_URL, alias="already-clean")

        assert _submitted_metadata_url(m) == CLEAN_URL


class TestExecutePersistenceBoundary:
    """Defense-in-depth: ``execute_add_golden_repo_work`` is reachable directly
    from a pod-pull job's metadata, so it strips again at the clone/persist point.
    Driving it to the clone call proves the stored record can't get a credential."""

    def _execute_capturing_clone_url(self, m: GoldenRepoManager) -> str:
        # Short-circuit at the first post-strip use of the URL and capture its arg.
        m._clone_repository = Mock(  # type: ignore[method-assign]
            side_effect=RuntimeError("stop-after-strip")
        )
        with pytest.raises(Exception):
            m.execute_add_golden_repo_work(repo_url=PAT_URL, alias="x")
        return m._clone_repository.call_args[0][0]

    def test_clone_receives_clean_url_when_owning(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=True)
        assert self._execute_capturing_clone_url(m) == CLEAN_URL

    def test_clone_receives_pat_in_local_mode(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=False)
        assert self._execute_capturing_clone_url(m) == PAT_URL
