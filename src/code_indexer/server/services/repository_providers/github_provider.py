"""
GitHub Repository Provider for CIDX Server.

Implements repository discovery from GitHub API, supporting user repositories
and organization repositories accessible via personal access token.
"""

import logging
import re
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional, Set

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


class GitHubProviderError(Exception):
    """Exception raised for GitHub provider errors."""

    pass


class GitHubProvider(RepositoryProviderBase):
    """
    GitHub repository discovery provider.

    Discovers repositories from GitHub API, excludes already-indexed repos,
    and handles pagination via Link header parsing.
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
        from code_indexer.server.utils.config_manager import ServerConfigManager
        config = ServerConfigManager().load_config()
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

    def _get_api_url(self, endpoint: str) -> str:
        """Construct full API URL for an endpoint."""
        base_url = self._get_base_url()
        return f"{base_url}/{endpoint}"

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
        Make a synchronous API request to GitHub.

        Args:
            endpoint: API endpoint (e.g., "user/repos")
            params: Query parameters

        Returns:
            HTTP response

        Raises:
            GitHubProviderError: If request fails
        """
        token_data = self._token_manager.get_token("github")
        if not token_data:
            raise GitHubProviderError("GitHub token not configured")

        url = self._get_api_url(endpoint)
        # GitHub uses Bearer token authentication
        headers = {
            "Authorization": f"Bearer {token_data.token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28",
        }

        response = httpx.get(
            url,
            headers=headers,
            params=params,
            timeout=self._api_timeout,
        )
        return response

    def _extract_commit_info_from_graphql_node(
        self, default_branch_ref: Optional[dict]
    ) -> tuple[Optional[str], Optional[str], Optional[datetime]]:
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

        if not nodes or len(nodes) == 0:
            return None, None, None

        commit = nodes[0]

        # Extract commit hash (7 chars)
        commit_hash = None
        oid = commit.get("oid")
        if oid:
            commit_hash = oid[:7]

        # Extract author name
        author_name = None
        author = commit.get("author")
        if author:
            author_name = author.get("name")

        # Extract commit date
        commit_date = None
        committed_date = commit.get("committedDate")
        if committed_date:
            try:
                commit_date = datetime.fromisoformat(
                    committed_date.replace("Z", "+00:00")
                )
            except (ValueError, TypeError):
                pass

        return commit_hash, author_name, commit_date

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
            GitHubProviderError: If request fails
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
            first: Number of repositories to fetch
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

    def _parse_graphql_response(self, node: dict) -> DiscoveredRepository:
        """
        Parse a GraphQL repository node into DiscoveredRepository.

        Args:
            node: GraphQL repository node

        Returns:
            DiscoveredRepository model
        """
        # Extract basic repository info
        name = node.get("nameWithOwner", "")
        description = node.get("description")
        is_private = node.get("isPrivate", False)

        # Construct clone URLs
        https_url = node.get("url", "")
        if https_url:
            https_url = f"{https_url}.git"
        ssh_url = node.get("sshUrl", "")

        # Parse pushed_at for last_activity
        last_activity = None
        pushed_at = node.get("pushedAt")
        if pushed_at:
            try:
                last_activity = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            except (ValueError, TypeError) as e:
                logger.debug(f"Failed to parse pushed_at date '{pushed_at}': {e}")

        # Extract default branch and commit info using helper
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

    def _enrich_with_commits_graphql(
        self, repositories: List[DiscoveredRepository]
    ) -> List[DiscoveredRepository]:
        """
        Enrich repositories with commit info via GraphQL batch query.

        Args:
            repositories: List of repositories to enrich

        Returns:
            Enriched repositories with commit info
        """
        if not repositories:
            return repositories

        # Build batch GraphQL query
        query_parts = ["query {"]
        repo_map = {}

        for idx, repo in enumerate(repositories):
            parts = repo.name.split("/", 1)
            if len(parts) != 2:
                continue

            owner, name = parts
            alias = f"r{idx}"
            repo_map[alias] = idx

            query_parts.append(
                f"""
              {alias}: repository(owner: "{owner}", name: "{name}") {{
                defaultBranchRef {{
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
            """
            )

        query_parts.append("}")
        query = "\n".join(query_parts)

        try:
            response = self._make_graphql_request(query)
            response.raise_for_status()
            data = response.json()

            if "data" in data:
                for alias, repo_idx in repo_map.items():
                    repo_data = data["data"].get(alias)
                    if not repo_data:
                        continue

                    default_branch_ref = repo_data.get("defaultBranchRef")
                    commit_hash, commit_author, commit_date = (
                        self._extract_commit_info_from_graphql_node(default_branch_ref)
                    )

                    repositories[repo_idx].last_commit_hash = commit_hash
                    repositories[repo_idx].last_commit_author = commit_author
                    repositories[repo_idx].last_commit_date = commit_date

        except Exception as e:
            logger.warning(format_error_log(
                "GIT-GENERAL-065",
                f"Failed to enrich repositories with commit info: {e}"
            ))

        return repositories

    def _parse_link_header_for_last_page(self, link_header: Optional[str]) -> int:
        """
        Parse GitHub's Link header to extract the last page number.

        GitHub pagination uses Link header format:
        <url?page=N>; rel="last", <url?page=M>; rel="next"

        Args:
            link_header: The Link header value from response

        Returns:
            Last page number, or 1 if not found
        """
        if not link_header:
            return 1

        # Find rel="last" link and extract page number
        # Format: <https://api.github.com/user/repos?page=5&per_page=30>; rel="last"
        last_pattern = re.compile(r'<[^>]*[?&]page=(\d+)[^>]*>;\s*rel="last"')
        match = last_pattern.search(link_header)

        if match:
            return int(match.group(1))

        return 1

    def _parse_repository(self, repo: dict) -> DiscoveredRepository:
        """
        Parse a GitHub repository into a DiscoveredRepository.

        Args:
            repo: GitHub repository data from API

        Returns:
            DiscoveredRepository model
        """
        # Parse pushed_at timestamp (last activity equivalent)
        last_activity = None
        pushed_at = repo.get("pushed_at")
        if pushed_at:
            try:
                last_activity = datetime.fromisoformat(pushed_at.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                pass

        # Extract commit info from API response (if available)
        # GitHub may include this in detailed repo responses or we use test data
        last_commit_hash = repo.get("_last_commit_hash")
        last_commit_author = repo.get("_last_commit_author")

        return DiscoveredRepository(
            platform="github",
            name=repo.get("full_name", ""),
            description=repo.get("description"),
            clone_url_https=repo.get("clone_url", ""),
            clone_url_ssh=repo.get("ssh_url", ""),
            default_branch=repo.get("default_branch", "main"),
            last_commit_hash=last_commit_hash,
            last_commit_author=last_commit_author,
            last_commit_date=None,
            last_activity=last_activity,
            is_private=repo.get("private", False),
        )

    def _check_rate_limit(self, response: httpx.Response) -> None:
        """
        Check if rate limit was exceeded and raise appropriate error.

        Args:
            response: HTTP response to check

        Raises:
            GitHubProviderError: If rate limit was exceeded
        """
        if response.status_code == 403:
            remaining = response.headers.get("X-RateLimit-Remaining", "")
            reset_time = response.headers.get("X-RateLimit-Reset", "")

            if remaining == "0":
                reset_msg = ""
                if reset_time:
                    try:
                        reset_dt = datetime.fromtimestamp(int(reset_time))
                        reset_msg = (
                            f" Rate limit resets at {reset_dt.strftime('%H:%M:%S')}"
                        )
                    except (ValueError, TypeError):
                        pass

                raise GitHubProviderError(f"GitHub API rate limit exceeded.{reset_msg}")

    def discover_repositories(
        self, page: int = 1, page_size: int = 50, search: Optional[str] = None
    ) -> RepositoryDiscoveryResult:
        """
        Discover repositories from GitHub API.

        Non-search mode uses GraphQL with commit info.
        Search mode uses REST search then enriches with GraphQL.

        Args:
            page: Page number (1-indexed)
            page_size: Number of repositories per page (max 100 for GitHub)
            search: Optional search query

        Returns:
            RepositoryDiscoveryResult with discovered repositories

        Raises:
            GitHubProviderError: If API call fails or token not configured
        """
        if not self.is_configured():
            raise GitHubProviderError(
                "GitHub token not configured. "
                "Please configure a GitHub token in the CI Tokens settings."
            )

        # Get indexed repos for filtering
        indexed_urls = self._get_indexed_canonical_urls()

        # GitHub API limits per_page to 100
        effective_page_size = min(page_size, 100)

        # Non-search mode: Use GraphQL for repository listing with commit info
        if not search:
            return self._discover_via_graphql(page, effective_page_size, indexed_urls)

        # Search mode: Use REST search API then enrich with GraphQL
        return self._discover_via_rest_search(
            page, effective_page_size, search, indexed_urls
        )

    def _discover_via_graphql(
        self, page: int, page_size: int, indexed_urls: Set[str]
    ) -> RepositoryDiscoveryResult:
        """
        Discover repositories via GraphQL with commit info and pagination.

        Falls back to REST API if GraphQL fails (graceful degradation).

        Args:
            page: Page number (1-indexed)
            page_size: Repositories per page
            indexed_urls: Set of already-indexed repository URLs

        Returns:
            RepositoryDiscoveryResult

        Raises:
            GitHubProviderError: If both GraphQL and REST API fail
        """
        try:
            # Paginate to the requested page
            cursor = None
            current_page = 1

            while current_page <= page:
                query = self._build_graphql_query(first=page_size, after=cursor)
                response = self._make_graphql_request(query)

                self._check_rate_limit(response)
                response.raise_for_status()

                data = response.json()

                if "data" not in data or "viewer" not in data["data"]:
                    raise GitHubProviderError("Invalid GraphQL response structure")

                viewer_data = data["data"]["viewer"]
                repos_data = viewer_data.get("repositories", {})

                # If we've reached the target page, process results
                if current_page == page:
                    nodes = repos_data.get("nodes", [])
                    total_count = repos_data.get("totalCount", 0)

                    # Parse repositories from GraphQL nodes
                    repositories: List[DiscoveredRepository] = []
                    for node in nodes:
                        # Extract URLs for exclusion check
                        https_url = node.get("url", "")
                        if https_url:
                            https_url = f"{https_url}.git"
                        ssh_url = node.get("sshUrl", "")

                        if not self._is_repo_indexed(https_url, ssh_url, indexed_urls):
                            repositories.append(self._parse_graphql_response(node))

                    # Calculate pagination
                    total_pages = (
                        (total_count + page_size - 1) // page_size
                        if total_count > 0
                        else 1
                    )

                    return RepositoryDiscoveryResult(
                        repositories=repositories,
                        total_count=total_count,
                        page=page,
                        page_size=page_size,
                        total_pages=total_pages,
                        platform="github",
                    )

                # Move to next page
                page_info = repos_data.get("pageInfo", {})
                if not page_info.get("hasNextPage"):
                    # Requested page doesn't exist, return empty result
                    return RepositoryDiscoveryResult(
                        repositories=[],
                        total_count=repos_data.get("totalCount", 0),
                        page=page,
                        page_size=page_size,
                        total_pages=(
                            (repos_data.get("totalCount", 0) + page_size - 1)
                            // page_size
                            if repos_data.get("totalCount", 0) > 0
                            else 1
                        ),
                        platform="github",
                    )

                cursor = page_info.get("endCursor")
                current_page += 1

            # Fallback: Should never reach here, but satisfy type checker
            return RepositoryDiscoveryResult(
                repositories=[],
                total_count=0,
                page=page,
                page_size=page_size,
                total_pages=1,
                platform="github",
            )

        except Exception as e:
            # Graceful degradation: Fall back to REST API without commit info
            logger.warning(format_error_log(
                "GIT-GENERAL-066",
                f"GraphQL discovery failed, falling back to REST API: {e}"
            ))
            return self._discover_via_rest_fallback(page, page_size, indexed_urls)

    def _discover_via_rest_fallback(
        self, page: int, page_size: int, indexed_urls: Set[str]
    ) -> RepositoryDiscoveryResult:
        """
        Fallback to REST API when GraphQL fails.

        Returns repositories without commit info.

        Args:
            page: Page number (1-indexed)
            page_size: Repositories per page
            indexed_urls: Set of already-indexed repository URLs

        Returns:
            RepositoryDiscoveryResult

        Raises:
            GitHubProviderError: If REST API request fails
        """
        endpoint = "user/repos"
        params = {
            "page": page,
            "per_page": page_size,
            "sort": "pushed",
            "direction": "desc",
            "affiliation": "owner,collaborator,organization_member",
        }

        try:
            response = self._make_api_request(endpoint, params=params)
            self._check_rate_limit(response)
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise GitHubProviderError(f"GitHub API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            if hasattr(e, "response") and e.response is not None:
                self._check_rate_limit(e.response)
            raise GitHubProviderError(
                f"GitHub API error: {e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}"
            ) from e
        except httpx.RequestError as e:
            raise GitHubProviderError(f"GitHub API request failed: {e}") from e

        repos = response.json()
        link_header = response.headers.get("Link", "")
        total_pages = self._parse_link_header_for_last_page(link_header)
        total_count = len(repos)
        if total_pages > 1:
            total_count = total_pages * page_size

        # Filter and parse repositories
        repositories: List[DiscoveredRepository] = []
        for repo in repos:
            https_url = repo.get("clone_url", "")
            ssh_url = repo.get("ssh_url", "")

            if not self._is_repo_indexed(https_url, ssh_url, indexed_urls):
                repositories.append(self._parse_repository(repo))

        return RepositoryDiscoveryResult(
            repositories=repositories,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            platform="github",
        )

    def _discover_via_rest_search(
        self, page: int, page_size: int, search: str, indexed_urls: Set[str]
    ) -> RepositoryDiscoveryResult:
        """
        Discover repositories via REST search API, then enrich with GraphQL.

        Args:
            page: Page number (1-indexed)
            page_size: Repositories per page
            search: Search query
            indexed_urls: Set of already-indexed repository URLs

        Returns:
            RepositoryDiscoveryResult

        Raises:
            GitHubProviderError: If REST search request fails
        """
        endpoint = "search/repositories"
        params = {
            "q": f"{search} in:name,description fork:true",
            "page": page,
            "per_page": page_size,
            "sort": "updated",
            "order": "desc",
        }

        try:
            response = self._make_api_request(endpoint, params=params)
            self._check_rate_limit(response)
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise GitHubProviderError(f"GitHub API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            if hasattr(e, "response") and e.response is not None:
                self._check_rate_limit(e.response)
            raise GitHubProviderError(
                f"GitHub API error: {e.response.status_code if hasattr(e, 'response') and e.response else 'unknown'}"
            ) from e
        except httpx.RequestError as e:
            raise GitHubProviderError(f"GitHub API request failed: {e}") from e

        response_data = response.json()
        repos = response_data.get("items", [])
        total_count = response_data.get("total_count", 0)
        total_pages = (
            (total_count + page_size - 1) // page_size if total_count > 0 else 1
        )

        # Filter out already-indexed repositories and parse
        repositories: List[DiscoveredRepository] = []
        for repo in repos:
            https_url = repo.get("clone_url", "")
            ssh_url = repo.get("ssh_url", "")

            if not self._is_repo_indexed(https_url, ssh_url, indexed_urls):
                repositories.append(self._parse_repository(repo))

        # Enrich with commit info via GraphQL
        repositories = self._enrich_with_commits_graphql(repositories)

        return RepositoryDiscoveryResult(
            repositories=repositories,
            total_count=total_count,
            page=page,
            page_size=page_size,
            total_pages=total_pages,
            platform="github",
        )
