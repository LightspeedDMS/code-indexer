"""
GitHub Repository Provider for CIDX Server.

Implements exhaustive repository discovery from GitHub API using GraphQL,
supporting user and organization repositories accessible via personal access token.
Server-side cursor pagination (Story #724) has been replaced by client-side
pagination (Story #754): all repos are fetched in a single exhaustive pass.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional, Set, Tuple

import httpx

from .base import RepositoryProviderBase
from ...models.auto_discovery import (
    DiscoveredRepository,
    RepositoryDiscoveryResult,
)
from ..git_url_normalizer import GitUrlNormalizer, GitUrlNormalizationError

if TYPE_CHECKING:
    from ..ci_token_manager import CITokenManager
    from ...repositories.golden_repo_manager import GoldenRepoManager

logger = logging.getLogger(__name__)

_DISCOVERY_GRAPHQL_PAGE_SIZE = 100  # repos per GraphQL page for exhaustive fetch
_DISCOVERY_MAX_PAGES = 10000  # hard upper bound; GitHubProviderError if exceeded


class GitHubProviderError(Exception):
    """Exception raised for GitHub provider errors."""

    pass


class GitHubProvider(RepositoryProviderBase):
    """
    GitHub repository discovery provider.

    Fetches all repositories exhaustively via GraphQL, returning a flat list
    for client-side pagination (Story #754).
    """

    DEFAULT_BASE_URL = "https://api.github.com"

    def __init__(
        self,
        token_manager: "CITokenManager",
        golden_repo_manager: "GoldenRepoManager",
    ):
        """
        Initialize the GitHub provider.

        Args:
            token_manager: CI token manager for retrieving GitHub API token
            golden_repo_manager: Manager for listing already-indexed golden repos
        """
        self._token_manager = token_manager
        self._golden_repo_manager = golden_repo_manager
        self._url_normalizer = GitUrlNormalizer()

        # Bug #83 Phase 1: Load timeout from config
        from code_indexer.server.services.config_service import get_config_service

        config = get_config_service().get_config()
        self._api_timeout = config.git_timeouts_config.github_api_timeout

    @property
    def platform(self) -> str:
        """Return the platform name."""
        return "github"

    def is_configured(self) -> bool:
        """Check if GitHub token is configured."""
        token_data = self._token_manager.get_token("github")
        return token_data is not None

    def _get_base_url(self) -> str:
        """Get the GitHub API base URL."""
        # GitHub Enterprise support could be added here in future
        return self.DEFAULT_BASE_URL

    def _get_indexed_canonical_urls(self) -> Set[str]:
        """
        Get canonical forms of all already-indexed repository URLs.

        Returns:
            Set of canonical URL forms for indexed repositories
        """
        indexed_urls: Set[str] = set()
        golden_repos = self._golden_repo_manager.list_golden_repos()

        for repo in golden_repos:
            repo_url = repo.get("repo_url", "")
            if repo_url:
                try:
                    canonical = self._url_normalizer.get_canonical_form(repo_url)
                    indexed_urls.add(canonical)
                except GitUrlNormalizationError:
                    # Skip URLs that cannot be normalized (e.g., local paths)
                    pass

        return indexed_urls

    def _is_repo_indexed(
        self, https_url: str, ssh_url: str, indexed_urls: Set[str]
    ) -> bool:
        """
        Check if a repository is already indexed.

        Args:
            https_url: HTTPS clone URL
            ssh_url: SSH clone URL
            indexed_urls: Set of canonical URLs for indexed repos

        Returns:
            True if the repository is already indexed
        """
        for url in [https_url, ssh_url]:
            try:
                canonical = self._url_normalizer.get_canonical_form(url)
                if canonical in indexed_urls:
                    return True
            except GitUrlNormalizationError:
                pass
        return False

    def _make_graphql_request(
        self,
        query: str,
        variables: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Make a synchronous GraphQL request to GitHub.

        Args:
            query: GraphQL query string
            variables: Query variables

        Returns:
            HTTP response

        Raises:
            GitHubProviderError: If token not configured
        """
        token_data = self._token_manager.get_token("github")
        if not token_data:
            raise GitHubProviderError("GitHub token not configured")

        url = f"{self._get_base_url()}/graphql"
        headers = {
            "Authorization": f"Bearer {token_data.token}",
            "Content-Type": "application/json",
        }

        payload: dict[str, Any] = {"query": query}
        if variables:
            payload["variables"] = variables

        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=self._api_timeout,
        )
        return response

    def _build_graphql_query(self, first: int, after: Optional[str]) -> str:
        """
        Build GraphQL query for repository discovery with commit info.

        Args:
            first: Number of repositories to fetch per page
            after: Cursor for pagination

        Returns:
            GraphQL query string
        """
        after_clause = f', after: "{after}"' if after else ""

        query = f"""
        query {{
          viewer {{
            repositories(
              first: {first}{after_clause},
              affiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER],
              ownerAffiliations: [OWNER, ORGANIZATION_MEMBER],
              orderBy: {{field: PUSHED_AT, direction: DESC}}
            ) {{
              pageInfo {{
                hasNextPage
                endCursor
              }}
              totalCount
              nodes {{
                name
                nameWithOwner
                description
                isPrivate
                url
                sshUrl
                pushedAt
                defaultBranchRef {{
                  name
                  target {{
                    ... on Commit {{
                      history(first: 1) {{
                        nodes {{
                          oid
                          author {{
                            name
                          }}
                          committedDate
                        }}
                      }}
                    }}
                  }}
                }}
              }}
            }}
          }}
        }}
        """
        return query

    def _extract_commit_info_from_graphql_node(
        self, default_branch_ref: Optional[dict]
    ) -> tuple:
        """
        Extract commit information from GraphQL defaultBranchRef node.

        Args:
            default_branch_ref: GraphQL defaultBranchRef object

        Returns:
            Tuple of (commit_hash, author_name, commit_date)
        """
        if not default_branch_ref:
            return None, None, None

        target = default_branch_ref.get("target", {})
        history = target.get("history", {})
        nodes = history.get("nodes", [])

        if not nodes:
            return None, None, None

        commit = nodes[0]

        commit_hash = None
        oid = commit.get("oid")
        if oid:
            commit_hash = oid[:7]

        author_name = None
        author = commit.get("author")
        if author:
            author_name = author.get("name")

        commit_date = None
        committed_date = commit.get("committedDate")
        if committed_date:
            try:
                commit_date = datetime.fromisoformat(
                    committed_date.replace("Z", "+00:00")
                )
            except (ValueError, TypeError) as e:
                logger.debug(f"Failed to parse committedDate '{committed_date}': {e}")

        return commit_hash, author_name, commit_date

    def _parse_graphql_response(self, node: dict) -> DiscoveredRepository:
        """
        Parse a GraphQL repository node into DiscoveredRepository.

        Args:
            node: GraphQL repository node

        Returns:
            DiscoveredRepository model
        """
        name = node.get("nameWithOwner", "")
        description = node.get("description")
        is_private = node.get("isPrivate", False)

        https_url = node.get("url", "")
        if https_url:
            https_url = f"{https_url}.git"
        ssh_url = node.get("sshUrl", "")

        last_activity = None
        pushed_at = node.get("pushedAt")
        if pushed_at:
            try:
                last_activity = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            except (ValueError, TypeError) as e:
                logger.debug(f"Failed to parse pushed_at date '{pushed_at}': {e}")

        default_branch = "main"
        default_branch_ref = node.get("defaultBranchRef")
        if default_branch_ref:
            default_branch = default_branch_ref.get("name", "main")

        commit_hash, commit_author, commit_date = (
            self._extract_commit_info_from_graphql_node(default_branch_ref)
        )

        return DiscoveredRepository(
            platform="github",
            name=name,
            description=description,
            clone_url_https=https_url,
            clone_url_ssh=ssh_url,
            default_branch=default_branch,
            last_commit_hash=commit_hash,
            last_commit_author=commit_author,
            last_commit_date=commit_date,
            last_activity=last_activity,
            is_private=is_private,
        )

    def _fetch_all_pages_graphql(
        self,
    ) -> Tuple[List[DiscoveredRepository], int]:
        """
        Fetch every page from GitHub GraphQL until hasNextPage=False.

        Returns:
            Tuple of (all_repos, total_count_from_api)

        Raises:
            GitHubProviderError: on HTTP error, null batch, or hasNextPage=True with no cursor
        """
        if not (isinstance(_DISCOVERY_MAX_PAGES, int) and _DISCOVERY_MAX_PAGES > 0):
            raise GitHubProviderError("_DISCOVERY_MAX_PAGES must be a positive integer")
        if not (
            isinstance(_DISCOVERY_GRAPHQL_PAGE_SIZE, int)
            and _DISCOVERY_GRAPHQL_PAGE_SIZE > 0
        ):
            raise GitHubProviderError(
                "_DISCOVERY_GRAPHQL_PAGE_SIZE must be a positive integer"
            )

        all_repos: List[DiscoveredRepository] = []
        cursor: Optional[str] = None
        total_count = 0

        for _page in range(1, _DISCOVERY_MAX_PAGES + 1):
            query = self._build_graphql_query(
                first=_DISCOVERY_GRAPHQL_PAGE_SIZE,
                after=cursor,
            )
            try:
                response = self._make_graphql_request(query)
                response.raise_for_status()
            except Exception as exc:
                raise GitHubProviderError(
                    f"GitHub GraphQL request failed: {exc}"
                ) from exc

            data = response.json()
            viewer = data.get("data", {}).get("viewer", {})
            repos_data = viewer.get("repositories", {})

            nodes = repos_data.get("nodes")
            if nodes is None:
                raise GitHubProviderError("GraphQL returned null batch list")

            total_count = repos_data.get("totalCount", total_count)

            for node in nodes:
                all_repos.append(self._parse_graphql_response(node))

            page_info = repos_data.get("pageInfo", {})
            has_next = page_info.get("hasNextPage", False)
            if not has_next:
                break
            next_cursor = page_info.get("endCursor")
            if next_cursor is None:
                raise GitHubProviderError(
                    "GraphQL returned hasNextPage=True but endCursor is None"
                )
            cursor = next_cursor
        else:
            raise GitHubProviderError(
                f"Exhausted _DISCOVERY_MAX_PAGES={_DISCOVERY_MAX_PAGES} without reaching last page"
            )

        return all_repos, total_count

    def _map_repos_to_dicts_github(
        self,
        repos: List[DiscoveredRepository],
        indexed_urls: Set[str],
        hidden_identifiers: Set[str],
    ) -> List[dict]:
        """
        Convert DiscoveredRepository list to JSON-serialisable dicts.

        Excludes already-indexed repos. Sets is_hidden from hidden_identifiers.

        Returns:
            List of repo dicts with clone_url_https, clone_url_ssh, name,
            is_hidden, platform fields.
        """
        if repos is None:
            raise GitHubProviderError("repos must not be None")
        if indexed_urls is None:
            raise GitHubProviderError("indexed_urls must not be None")
        if hidden_identifiers is None:
            raise GitHubProviderError("hidden_identifiers must not be None")

        result = []
        for repo in repos:
            if self._is_repo_indexed(
                repo.clone_url_https, repo.clone_url_ssh, indexed_urls
            ):
                continue
            ssh_id = f"github:{repo.clone_url_ssh}"
            https_id = f"github:{repo.clone_url_https}"
            is_hidden = ssh_id in hidden_identifiers or https_id in hidden_identifiers

            last_commit_date_iso = None
            if repo.last_commit_date:
                last_commit_date_iso = repo.last_commit_date.isoformat()

            last_activity_iso = None
            if repo.last_activity:
                last_activity_iso = repo.last_activity.isoformat()

            result.append(
                {
                    "platform": "github",
                    "name": repo.name,
                    "description": repo.description,
                    "clone_url_https": repo.clone_url_https,
                    "clone_url_ssh": repo.clone_url_ssh,
                    "default_branch": repo.default_branch,
                    "is_hidden": is_hidden,
                    "is_private": repo.is_private,
                    # GitHub-specific: commit info embedded since /enrich is a no-op
                    "last_commit_hash": repo.last_commit_hash,
                    "last_commit_author": repo.last_commit_author,
                    "last_commit_date": last_commit_date_iso,
                    "last_activity": last_activity_iso,
                }
            )
        return result

    def discover_all_repositories(
        self,
        indexed_urls: Set[str],
        hidden_identifiers: Set[str],
    ) -> dict:
        """
        Exhaustively fetch all GitHub repositories and return as JSON dict.

        Args:
            indexed_urls: Canonical URLs of already-indexed repositories to exclude.
            hidden_identifiers: Set of 'platform:url' identifiers for hidden repos.

        Returns:
            Dict with keys: repositories (list), total_source (int),
            total_unregistered (int).

        Raises:
            GitHubProviderError: On upstream API failure or misconfiguration.
        """
        if indexed_urls is None:
            raise GitHubProviderError("indexed_urls must not be None")
        if hidden_identifiers is None:
            raise GitHubProviderError("hidden_identifiers must not be None")

        if not self.is_configured():
            return {"repositories": [], "total_source": 0, "total_unregistered": 0}

        all_repos, total_source = self._fetch_all_pages_graphql()
        repo_dicts = self._map_repos_to_dicts_github(
            all_repos, indexed_urls, hidden_identifiers
        )
        total_unregistered = sum(1 for r in repo_dicts if not r["is_hidden"])
        return {
            "repositories": repo_dicts,
            "total_source": total_source,
            "total_unregistered": total_unregistered,
        }

    def enrich_repositories(self, clone_urls: List[str]) -> dict:
        """
        GitHub enrichment no-op.

        GitHub commit data is already embedded in each GraphQL repository node
        fetched during discover_all_repositories, so no additional HTTP calls
        are required at enrich time.

        Args:
            clone_urls: List of HTTPS clone URLs (unused).

        Returns:
            Empty dict always.
        """
        return {}

    def discover_repositories(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        search: Optional[str] = None,
        hidden_identifiers: Optional[Set[str]] = None,
    ) -> "RepositoryDiscoveryResult":
        """
        Server-side cursor pagination removed in Story #754.

        Use discover_all_repositories for exhaustive client-side pagination.

        Raises:
            NotImplementedError: Always. Use discover_all_repositories instead.
        """
        raise NotImplementedError(
            "Server-side cursor pagination removed in Story #754. "
            "Use discover_all_repositories() instead."
        )
