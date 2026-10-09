"""Epic #2103 item 15: every former copy of git URL parsing now uses the
single parser (``code_indexer.utils.git_remote_url``).

Sites that select a credential (a PAT's host, a platform token's
destination, the host SSH keys are tried against) use
``credential_scope_host``, whose never-wider proof against the previous
helpers is in tests/unit/utils/test_git_remote_url_credential_scope.py.
Tests named ``..._previously_...`` pin cases where the copies diverged.
Hosts and tokens are neutral placeholders.
"""

from typing import Tuple

import pytest

from code_indexer.server.clients.forge_client import extract_owner_repo
from code_indexer.server.jobs.exceptions import DuplicateRepositorySyncError
from code_indexer.server.jobs.manager import SyncJobManager
from code_indexer.server.jobs.models import JobType
from code_indexer.server.services.git_credential_helper import GitCredentialHelper
from code_indexer.server.services.remote_branch_service import (
    _build_effective_url,
    _detect_platform_from_url,
)
from code_indexer.server.services.remote_discovery_service import (
    RemoteDiscoveryService,
)
from code_indexer.utils.git_remote_url import (
    credential_scope_host,
    parse_git_remote_url,
)
from tests.unit.utils.test_git_remote_url import PARSE_TABLE, UNPARSEABLE

ALL_INPUTS = sorted(PARSE_TABLE) + UNPARSEABLE
NON_GIT_SSH_USERS = [
    "ssh://deploy@git.example.com:2222/owner/repo.git",
    "deploy@git.example.com:owner/repo.git",
]


class TestCredentialHelperScope:
    @pytest.mark.parametrize("url", ALL_INPUTS)
    def test_host_is_the_shared_credential_scope(self, url: str) -> None:
        assert GitCredentialHelper.extract_host_from_remote_url(
            url
        ) == credential_scope_host(url)

    @pytest.mark.parametrize(
        "url,host",
        [
            ("git@github.com:owner/repo.git", "github.com"),
            ("https://github.com/owner/repo.git", "github.com"),
            ("http://git.example.com/owner/repo.git", "git.example.com"),
            ("https://u:tok@git.example.com:8443/o/r.git", "git.example.com:8443"),
            ("https://Git.Example.com/o/r.git", "Git.Example.com"),
            ("https://[2001:db8::1]:8443/o/r.git", "[2001:db8::1]:8443"),
            ("ssh://git@github.com/owner/repo.git", "github.com"),
            ("ssh://git@github.com:22/owner/repo.git", "github.com"),
        ],
    )
    def test_scope_unchanged_for_forms_the_old_helper_handled(
        self, url: str, host: str
    ) -> None:
        assert GitCredentialHelper.extract_host_from_remote_url(url) == host

    @pytest.mark.parametrize("url", NON_GIT_SSH_USERS)
    def test_non_git_ssh_users_scope_no_credential_and_stay_ssh(self, url: str) -> None:
        assert GitCredentialHelper.extract_host_from_remote_url(url) is None
        assert GitCredentialHelper.convert_ssh_to_https(url) == url

    @pytest.mark.parametrize(
        "url,expected",
        [
            ("git@github.com:owner/repo.git", "https://github.com/owner/repo.git"),
            (
                "ssh://git@github.com:22/owner/repo.git",
                "https://github.com/owner/repo.git",
            ),
        ],
    )
    def test_git_user_ssh_urls_convert(self, url: str, expected: str) -> None:
        assert GitCredentialHelper.convert_ssh_to_https(url) == expected

    @pytest.mark.parametrize(
        "url",
        [
            "https://github.com/owner/repo.git",
            "https://user:tok@github.com/owner/repo.git",
            "/srv/repos/repo",
        ],
    )
    def test_non_ssh_urls_pass_through(self, url: str) -> None:
        assert GitCredentialHelper.convert_ssh_to_https(f"  {url} ") == url


class TestRemoteDiscoveryHostname:
    """The discovered host is the host SSH keys are tried against."""

    @pytest.mark.parametrize("url", ALL_INPUTS)
    def test_hostname_is_the_shared_credential_scope(self, url: str) -> None:
        assert RemoteDiscoveryService().extract_hostname(url) == credential_scope_host(
            url
        )

    @pytest.mark.parametrize("url", NON_GIT_SSH_USERS)
    def test_non_git_ssh_users_select_no_ssh_key_host(self, url: str) -> None:
        assert RemoteDiscoveryService().extract_hostname(url) is None


class TestRemoteBranchServiceUrls:
    """A platform token is attached only where the push credential would be
    (``credential_scope_host``), and never over plain http."""

    @pytest.mark.parametrize(
        "url,platform,expected",
        [
            (
                "git@gitlab.com:group/repo.git",
                "gitlab",
                "https://oauth2:tok@gitlab.com/group/repo.git",
            ),
            (
                "https://old:cred@github.com/owner/repo.git",
                "github",
                "https://tok@github.com/owner/repo.git",
            ),
            (
                "https://git.example.com:8443/owner/repo.git",
                None,
                "https://tok@git.example.com:8443/owner/repo.git",
            ),
            (
                "ssh://git@git.example.com/owner/repo.git",
                "github",
                "https://tok@git.example.com/owner/repo.git",
            ),
            # No token for plain http or a non-git SSH user; other values
            # are returned unchanged.
            (
                "http://git.example.com/owner/repo.git",
                "github",
                "http://git.example.com/owner/repo.git",
            ),
            (
                "ssh://deploy@git.example.com:2222/owner/repo.git",
                "github",
                "ssh://deploy@git.example.com:2222/owner/repo.git",
            ),
            (
                "deploy@git.example.com:owner/repo.git",
                "github",
                "deploy@git.example.com:owner/repo.git",
            ),
            ("/srv/repos/repo", "github", "/srv/repos/repo"),
        ],
    )
    def test_effective_url(self, url: str, platform, expected: str) -> None:
        assert _build_effective_url(url, platform, "tok") == expected

    def test_no_credentials_returns_url_unchanged(self) -> None:
        url = "git@git.example.com:owner/repo.git"
        assert _build_effective_url(url, "github", None) == url

    @pytest.mark.parametrize(
        "url,platform",
        [
            ("git@github.com:owner/repo.git", "github"),
            ("https://api.github.com/owner/repo.git", "github"),
            ("ssh://git@gitlab.example.com:2222/g/r.git", "gitlab"),
            ("deploy@GitLab.example.com:g/r.git", "gitlab"),
            ("https://git.example.com/github/repo.git", None),
            ("not-a-url", None),
        ],
    )
    def test_platform_detection_reads_the_canonical_host(
        self, url: str, platform
    ) -> None:
        assert _detect_platform_from_url(url) == platform


class TestSyncJobManagerRepositoryLocks:
    @pytest.fixture
    def manager(self, tmp_path) -> SyncJobManager:
        return SyncJobManager(
            storage_path=str(tmp_path / "jobs.json"),
            max_total_concurrent_jobs=10,
            max_concurrent_jobs_per_user=10,
            max_cpu_percent=100.0,
            max_memory_percent=100.0,
        )

    def _create(self, manager: SyncJobManager, url: str) -> str:
        return manager.create_job(
            username="user1",
            user_alias="User 1",
            job_type=JobType.REPOSITORY_SYNC,
            repository_url=url,
        )

    @pytest.mark.parametrize(
        "second_url",
        [
            "ssh://deploy@git.example.com:22/owner/repo.git",
            "https://tok@git.example.com/owner/repo",
            "http://git.example.com/Owner/Repo.git",
            "deploy@git.example.com:owner/repo",
        ],
    )
    def test_ssh_non_git_user_and_other_forms_previously_bypassed_the_lock(
        self, manager: SyncJobManager, second_url: str
    ) -> None:
        self._create(manager, "git@git.example.com:owner/repo.git")

        with pytest.raises(DuplicateRepositorySyncError):
            self._create(manager, second_url)

    def test_distinct_ssh_ports_do_not_share_a_lock(
        self, manager: SyncJobManager
    ) -> None:
        self._create(manager, "ssh://git@git.example.com:2222/owner/repo.git")
        self._create(manager, "ssh://git@git.example.com:2223/owner/repo.git")

    def test_non_default_port_does_not_share_the_default_port_lock(
        self, manager: SyncJobManager
    ) -> None:
        self._create(manager, "git@git.example.com:owner/repo.git")
        self._create(manager, "ssh://git@git.example.com:2222/owner/repo.git")

    def test_different_repositories_do_not_conflict(
        self, manager: SyncJobManager
    ) -> None:
        self._create(manager, "git@git.example.com:owner/repo.git")
        self._create(manager, "git@git.example.com:owner/other.git")

    def test_lock_key_never_holds_credentials(self, manager: SyncJobManager) -> None:
        self._create(manager, "https://user:s3cr3t-value@git.example.com/o/r.git")

        assert all("s3cr3t-value" not in key for key in manager._repository_locks)


class TestGitLabProjectFullPath:
    """GitLabProvider's GraphQL project path previously split ssh:// and
    userinfo URLs at their first ':'."""

    @staticmethod
    def _full_path(url: str) -> str:
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProvider,
        )

        provider = GitLabProvider.__new__(GitLabProvider)
        return provider._resolve_full_path_from_url(url)

    @pytest.mark.parametrize(
        "url",
        [
            "https://gitlab.example.com/group/sub/project.git",
            "git@gitlab.example.com:group/sub/project.git",
            "http://gitlab.example.com/group/sub/project/",
        ],
    )
    def test_full_path_is_the_canonical_repo_path(self, url: str) -> None:
        assert self._full_path(url) == "group/sub/project"

    def test_ssh_non_git_user_and_port_previously_kept_the_host(self) -> None:
        assert (
            self._full_path("ssh://deploy@gitlab.example.com:2222/group/project.git")
            == "group/project"
        )

    def test_userinfo_previously_leaked_into_the_project_path(self) -> None:
        assert (
            self._full_path("https://user:s3cr3t-value@gitlab.example.com/g/p.git")
            == "g/p"
        )

    @pytest.mark.parametrize(
        "url", ["", "/srv/repos/project", "https://gitlab.example.com"]
    )
    def test_values_without_a_project_path_raise(self, url: str) -> None:
        from code_indexer.server.services.repository_providers.gitlab_provider import (
            GitLabProviderError,
        )

        with pytest.raises(GitLabProviderError):
            self._full_path(url)


class TestForgeOwnerRepo:
    @pytest.mark.parametrize(
        "url,expected",
        [
            ("git@github.com:owner/repo.git", ("owner", "repo")),
            ("https://gitlab.com/group/sub/repo.git", ("group/sub", "repo")),
            ("ssh://deploy@git.example.com:2222/team/repo.git", ("team", "repo")),
            ("deploy@git.example.com:team/repo", ("team", "repo")),
        ],
    )
    def test_owner_repo_is_canonical(self, url: str, expected: Tuple[str, str]) -> None:
        parsed = parse_git_remote_url(url)
        assert parsed is not None
        assert extract_owner_repo(url) == parsed.owner_repo() == expected

    def test_trailing_slash_previously_gave_an_empty_repo(self) -> None:
        assert extract_owner_repo("https://gitlab.com/group/sub/repo/") == (
            "group/sub",
            "repo",
        )

    @pytest.mark.parametrize(
        "url",
        [
            "file:///srv/repos/owner/repo",
            "/srv/repos/repo",
            "https://github.com/owner",
            "not-a-url",
        ],
    )
    def test_non_forge_urls_raise(self, url: str) -> None:
        with pytest.raises(ValueError):
            extract_owner_repo(url)

    def test_error_message_never_holds_credentials(self) -> None:
        with pytest.raises(ValueError) as raised:
            extract_owner_repo("https://user:s3cr3t-value@github.com/owner")

        assert "s3cr3t-value" not in str(raised.value)
