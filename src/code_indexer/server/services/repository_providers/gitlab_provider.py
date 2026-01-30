"""
GitLab Repository Provider for CIDX Server.

Implements repository discovery from GitLab API, supporting both gitlab.com
and self-hosted GitLab instances.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Set

import httpx

from .base import RepositoryProviderBase
from ...models.auto_discovery import (
    DiscoveredRepository,
    RepositoryDiscoveryResult,
)
from ..git_url_normalizer import GitUrlNormalizer, GitUrlNormalizationError
from code_indexer.server.logging_utils import format_error_log

if TYPE_CHECKING:
    from ..ci_token_manager import CITokenManager
    from ...repositories.golden_repo_manager import GoldenRepoManager

logger = logging.getLogger(__name__)


class GitLabProviderError(Exception):
    """Exception raised for GitLab provider errors."""

    pass


class GitLabProvider(RepositoryProviderBase):
    """
    GitLab repository discovery provider.

    Discovers repositories from GitLab API, excludes already-indexed repos,
    and handles pagination.
    """

    DEFAULT_BASE_URL = "https://gitlab.com"
    API_VERSION = "v4"
    SHORT_SHA_LENGTH = 7  # Length for displaying short commit hashes

    def __init__(
        self,
        token_manager: "CITokenManager",
        golden_repo_manager: "GoldenRepoManager",
    ):
        """
        Initialize the GitLab provider.

        Args:
            token_manager: CI token manager for retrieving GitLab API token
            golden_repo_manager: Manager for listing already-indexed golden repos
        """
        self._token_manager = token_manager
        self._golden_repo_manager = golden_repo_manager
        self._url_normalizer = GitUrlNormalizer()

        # Bug #83 Phase 1: Load timeout from config
        from code_indexer.server.utils.config_manager import ServerConfigManager
        config = ServerConfigManager().load_config()
        self._api_timeout = config.git_timeouts_config.gitlab_api_timeout

    @property
    def platform(self) -> str:
        """Return the platform name."""
        return "gitlab"

    def is_configured(self) -> bool:
        """Check if GitLab token is configured."""
        token_data = self._token_manager.get_token("gitlab")
        return token_data is not None

    def _get_base_url(self) -> str:
        """Get the GitLab API base URL."""
        token_data = self._token_manager.get_token("gitlab")
        if token_data and token_data.base_url:
            return token_data.base_url
        return self.DEFAULT_BASE_URL

    def _get_api_url(self, endpoint: str) -> str:
        """Construct full API URL for an endpoint."""
        base_url = self._get_base_url()
        return f"{base_url}/api/{self.API_VERSION}/{endpoint}"

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

    def _make_api_request(
        self,
        endpoint: str,
        params: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Make a synchronous API request to GitLab.

        Args:
            endpoint: API endpoint (e.g., "projects")
            params: Query parameters

        Returns:
            HTTP response

        Raises:
            GitLabProviderError: If request fails
        """
        token_data = self._token_manager.get_token("gitlab")
        if not token_data:
            raise GitLabProviderError("GitLab token not configured")

        url = self._get_api_url(endpoint)
        headers = {"PRIVATE-TOKEN": token_data.token}

        response = httpx.get(
            url,
            headers=headers,
            params=params,
            timeout=self._api_timeout,
        )
        return response

    def _make_graphql_request(
        self,
        query: str,
        variables: Optional[dict] = None,
    ) -> httpx.Response:
        """
        Make a synchronous GraphQL request to GitLab.

        Args:
            query: GraphQL query string
            variables: Query variables (optional)

        Returns:
            HTTP response

        Raises:
            GitLabProviderError: If request fails
        """
        token_data = self._token_manager.get_token("gitlab")
        if not token_data:
            raise GitLabProviderError("GitLab token not configured")

        base_url = self._get_base_url()
        url = f"{base_url}/api/graphql"
        headers = {
            "PRIVATE-TOKEN": token_data.token,
            "Content-Type": "application/json",
        }

        payload = {"query": query}
        if variables:
            payload["variables"] = variables

        response = httpx.post(
            url,
            headers=headers,
            json=payload,
            timeout=self._api_timeout,
        )
        return response

    def _build_multiplex_query(self, full_paths: List[str]) -> str:
        """
        Build GraphQL multiplex query for fetching commit info from multiple projects.

        Uses aliased queries to batch-fetch commit information for up to 50 projects
        in a single GraphQL request.

        Args:
            full_paths: List of project full paths (e.g., ["group/repo1", "group/repo2"])

        Returns:
            GraphQL query string with aliased project queries
        """
        query_parts = ["query {"]

        for idx, full_path in enumerate(full_paths):
            alias = f"project{idx}"
            query_parts.append(
                f"""
  {alias}: project(fullPath: "{full_path}") {{
    repository {{
      tree {{
        lastCommit {{
          sha
          author {{ name }}
          committedDate
        }}
      }}
    }}
  }}"""
            )

        query_parts.append("}")
        return "\n".join(query_parts)

    def _parse_multiplex_response(
        self, graphql_response: dict, full_paths: List[str]
    ) -> dict:
        """
        Parse GraphQL multiplex response and extract commit info per project.

        Args:
            graphql_response: GraphQL response JSON
            full_paths: List of project full paths (same order as query)

        Returns:
            Dict mapping full_path to commit info dict with keys:
            - commit_hash: 7-character SHA (or None)
            - commit_author: Author name (or None)
            - commit_date: datetime object (or None)
        """
        result = {}
        data = graphql_response.get("data", {})

        for idx, full_path in enumerate(full_paths):
            alias = f"project{idx}"
            project_data = data.get(alias)

            commit_info = {
                "commit_hash": None,
                "commit_author": None,
                "commit_date": None,
            }

            if project_data:
                repository = project_data.get("repository", {})
                tree = repository.get("tree")

                if tree:
                    last_commit = tree.get("lastCommit")

                    if last_commit:
                        # Extract commit hash (7 chars)
                        sha = last_commit.get("sha")
                        if sha:
                            commit_info["commit_hash"] = sha[: self.SHORT_SHA_LENGTH]

                        # Extract author name
                        author = last_commit.get("author")
                        if author:
                            commit_info["commit_author"] = author.get("name")

                        # Extract commit date
                        committed_date = last_commit.get("committedDate")
                        if committed_date:
                            try:
                                commit_info["commit_date"] = datetime.fromisoformat(
                                    committed_date.replace("Z", "+00:00")
                                )
                            except (ValueError, TypeError) as e:
                                logger.debug(
                                    f"Failed to parse commit date '{committed_date}' "
                                    f"for project '{full_path}': {e}"
                                )

            result[full_path] = commit_info

        return result

    def _enrich_repositories_with_commits(
        self, repositories: List[DiscoveredRepository]
    ) -> List[DiscoveredRepository]:
        """
        Enrich repositories with commit info via GraphQL batch query.

        Batches repositories into groups of 50 (GitLab's GraphQL limit) and
        fetches commit information for each batch. Updates repository objects
        with last_commit_hash, last_commit_author, and last_commit_date.

        Args:
            repositories: List of repositories to enrich

        Returns:
            Same list of repositories with commit info populated (mutated in place)
        """
        if not repositories:
            return repositories

        # GitLab GraphQL has query complexity limits - use smaller batches
        # to avoid "Query too large" errors (50 projects exceeds the limit)
        BATCH_SIZE = 10

        try:
            # Process repositories in batches
            for batch_start in range(0, len(repositories), BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, len(repositories))
                batch_repos = repositories[batch_start:batch_end]

                # Extract full paths for this batch
                full_paths = [repo.name for repo in batch_repos]

                # Build and execute GraphQL query
                query = self._build_multiplex_query(full_paths)
                logger.debug(f"GitLab GraphQL query: {query[:500]}...")
                response = self._make_graphql_request(query)

                # Log error response body for debugging before raising
                if response.status_code >= 400:
                    logger.error(format_error_log(
                        "GIT-GENERAL-067",
                        f"GitLab GraphQL error {response.status_code}: {response.text}"
                    ))
                response.raise_for_status()

                # Parse response and update repositories
                graphql_data = response.json()
                commit_map = self._parse_multiplex_response(graphql_data, full_paths)

                for repo in batch_repos:
                    commit_info = commit_map.get(repo.name, {})
                    repo.last_commit_hash = commit_info.get("commit_hash")
                    repo.last_commit_author = commit_info.get("commit_author")
                    repo.last_commit_date = commit_info.get("commit_date")

        except Exception as e:
            # Graceful degradation: Log warning but don't fail discovery
            logger.warning(format_error_log(
                "GIT-GENERAL-068",
                f"Failed to enrich repositories with commit info: {e}"
            ))

        return repositories

    def _parse_project(self, project: dict) -> DiscoveredRepository:
        """
        Parse a GitLab project into a DiscoveredRepository.

        Args:
            project: GitLab project data from API

        Returns:
            DiscoveredRepository model
        """
        # Parse last_activity_at timestamp
        last_activity = None
        last_activity_at = project.get("last_activity_at")
        if last_activity_at:
            try:
                last_activity = datetime.fromisoformat(
                    last_activity_at.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        # Extract commit info from API response (if available)
        # GitLab may include this in detailed project responses or we use test data
        last_commit_hash = project.get("_last_commit_hash")
        last_commit_author = project.get("_last_commit_author")

        return DiscoveredRepository(
            platform="gitlab",
            name=project.get("path_with_namespace", ""),
            description=project.get("description"),
            clone_url_https=project.get("http_url_to_repo", ""),
            clone_url_ssh=project.get("ssh_url_to_repo", ""),
            default_branch=project.get("default_branch", "main"),
            last_commit_hash=last_commit_hash,
            last_commit_author=last_commit_author,
            last_activity=last_activity,
            is_private=project.get("visibility") == "private",
        )

    def discover_repositories(
        self, page: int = 1, page_size: int = 50, search: Optional[str] = None
    ) -> RepositoryDiscoveryResult:
        """
        Discover repositories from GitLab API.

        Args:
            page: Page number (1-indexed)
            page_size: Number of repositories per page

        Returns:
            RepositoryDiscoveryResult with discovered repositories

        Raises:
            GitLabProviderError: If API call fails or token not configured
        """
        if not self.is_configured():
            raise GitLabProviderError(
                "GitLab token not configured. "
                "Please configure a GitLab token in the CI Tokens settings."
            )

        # Get indexed repos for filtering
        indexed_urls = self._get_indexed_canonical_urls()

        # Build params for API request
        params = {
            "membership": "true",
            "page": page,
            "per_page": page_size,
            "order_by": "last_activity_at",
            "sort": "desc",
        }
        # Add search parameter for server-side filtering (Story #16)
        if search:
            params["search"] = search

        try:
            response = self._make_api_request("projects", params=params)
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise GitLabProviderError(f"GitLab API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            raise GitLabProviderError(
                f"GitLab API error: {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise GitLabProviderError(f"GitLab API request failed: {e}") from e

        # Parse response
        projects = response.json()
        total_count = int(response.headers.get("x-total", "0"))
        total_pages = int(response.headers.get("x-total-pages", "0"))

        # Filter out already-indexed repositories
        repositories: List[DiscoveredRepository] = []
        for project in projects:
            https_url = project.get("http_url_to_repo", "")
            ssh_url = project.get("ssh_url_to_repo", "")

            if not self._is_repo_indexed(https_url, ssh_url, indexed_urls):
                repositories.append(self._parse_project(project))

        # Note: Client-side search filtering removed (Story #16)
        # Server-side filtering via API `search` parameter is used instead

        # Story #81: Enrich with commit info via GraphQL
        repositories = self._enrich_repositories_with_commits(repositories)

        return RepositoryDiscoveryResult(
            repositories=repositories,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            platform="gitlab",
        )
