"""EVO-64616: golden-repo URLs are credential-stripped before persistence when the
git backend owns credentials (delegated → git-mcp), so the PAT lives ONLY in
git-mcp — and left intact in local mode, where in-process git needs the token.

The strip happens up front in ``add_golden_repo``, so the pre-flight probe, the
background-job metadata, the clone, and the persisted record all carry the clean
URL. Asserting on the pre-flight validation argument proves the strip ran before
anything downstream sees the URL.
"""

from __future__ import annotations

from unittest.mock import Mock

from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

PAT_URL = "https://oauth2:glpat-SECRET123@gitlab.com/org/repo.git"
CLEAN_URL = "https://gitlab.com/org/repo.git"


def _manager(tmp_path, *, owns_credentials: bool) -> GoldenRepoManager:
    m = GoldenRepoManager(data_dir=str(tmp_path))
    m._git_backend = Mock()
    m._git_backend.owns_credentials = owns_credentials
    m.background_job_manager = Mock()
    m.background_job_manager.submit_job = Mock(return_value="job-1")
    # Records the URL that reaches pre-flight — i.e. the post-strip value.
    m._validate_git_repository = Mock(return_value=True)  # type: ignore[method-assign]
    return m


class TestGoldenRepoCredentialStripping:
    def test_strips_pat_when_backend_owns_credentials(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=True)

        m.add_golden_repo(repo_url=PAT_URL, alias="pat-strip")

        # Everything downstream sees the credential-free URL; the token stays in git-mcp.
        m._validate_git_repository.assert_called_once_with(CLEAN_URL)

    def test_keeps_pat_in_local_mode(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=False)

        m.add_golden_repo(repo_url=PAT_URL, alias="pat-keep")

        # Local git needs the embedded token — the URL is untouched.
        m._validate_git_repository.assert_called_once_with(PAT_URL)

    def test_credential_free_url_unchanged_when_owning(self, tmp_path):
        m = _manager(tmp_path, owns_credentials=True)

        m.add_golden_repo(repo_url=CLEAN_URL, alias="already-clean")

        m._validate_git_repository.assert_called_once_with(CLEAN_URL)
