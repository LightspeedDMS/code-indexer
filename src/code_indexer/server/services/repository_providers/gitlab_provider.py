"""
GitLab Repository Provider for CIDX Server.

Implements repository discovery from GitLab API, supporting both gitlab.com
and self-hosted GitLab instances.
"""

import logging
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Set, Tuple, TypedDict
from typing_extensions import NotRequired

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

_DISCOVERY_REST_PAGE_SIZE = 100  # repos per page for exhaustive all-fetch
_DISCOVERY_MAX_PAGES = 10000  # hard upper bound; GitLabProviderError if exceeded
_ENRICH_BATCH_SIZE = 10  # GitLab GraphQL multiplex limit per request


class _CommitInfo(TypedDict):
    """Parsed commit information returned by _parse_multiplex_response."""

    commit_hash: Optional[str]
    commit_author: Optional[str]
    commit_date: Optional[datetime]
    last_activity: NotRequired[str]


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
        from code_indexer.server.services.config_service import get_config_service

        config = get_config_service().get_config()
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
                    logger.debug("Skipping un-normalizable indexed URL: %r", repo_url)

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
                logger.debug(
                    "Skipping un-normalizable URL during indexed check: %r", url
                )
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
            payload["variables"] = variables  # type: ignore[assignment]

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
    lastActivityAt
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
            Dict mapping full_path to _CommitInfo with keys:
            - commit_hash: 7-character SHA (or None)
            - commit_author: Author name (or None)
            - commit_date: datetime object (or None)
        """
        result: dict[str, _CommitInfo] = {}
        data = graphql_response.get("data") or {}

        for idx, full_path in enumerate(full_paths):
            alias = f"project{idx}"
            project_data = data.get(alias) or {}

            commit_info: _CommitInfo = {
                "commit_hash": None,
                "commit_author": None,
                "commit_date": None,
            }

            # Extract project-level lastActivityAt (ISO 8601 string, pass through as-is)
            last_activity_at = project_data.get("lastActivityAt")
            if last_activity_at:
                commit_info["last_activity"] = last_activity_at

            repository = project_data.get("repository") or {}
            tree = repository.get("tree") or {}
            last_commit = tree.get("lastCommit") or {}

            if last_commit:
                # Extract commit hash (7 chars)
                sha = last_commit.get("sha")
                if sha:
                    commit_info["commit_hash"] = sha[: self.SHORT_SHA_LENGTH]

                # Extract author name
                author = last_commit.get("author") or {}
                commit_info["commit_author"] = author.get("name")

                # Extract commit date — stored as datetime for DiscoveredRepository model
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
                    logger.error(
                        format_error_log(
                            "GIT-GENERAL-067",
                            f"GitLab GraphQL error {response.status_code}: {response.text}",
                        )
                    )
                response.raise_for_status()

                # Parse response and update repositories
                graphql_data = response.json()
                commit_map = self._parse_multiplex_response(graphql_data, full_paths)

                for repo in batch_repos:
                    commit_info = commit_map.get(repo.name, {})
                    repo.last_commit_hash = commit_info.get("commit_hash")
                    repo.last_commit_author = commit_info.get("commit_author")
                    repo.last_commit_date = commit_info.get("commit_date")

        except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as e:
            # Graceful degradation: Log warning but don't fail discovery
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-068",
                    f"Failed to enrich repositories with commit info: {e}",
                )
            )

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

        return DiscoveredRepository(  # type: ignore[call-arg]
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

    def _make_api_request_checked(
        self, endpoint: str, params: Optional[dict] = None
    ) -> httpx.Response:
        """
        Make a REST API request and translate transport/HTTP errors to GitLabProviderError.

        Returns:
            Successful HTTP response

        Raises:
            GitLabProviderError: On timeout, HTTP error, or network failure
        """
        try:
            response = self._make_api_request(endpoint, params=params)
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise GitLabProviderError(f"GitLab API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            raise GitLabProviderError(
                f"GitLab API error: {e.response.status_code}"
            ) from e
        except httpx.RequestError as e:
            raise GitLabProviderError(f"GitLab API request failed: {e}") from e
        return response

    def _fetch_batch_rest(
        self, source: Optional[int], batch_size: int, search: Optional[str]
    ) -> Tuple[List[DiscoveredRepository], Optional[int], bool, Optional[int]]:
        """
        Fetch one page via GitLab REST /projects.

        Returns:
            (parsed_repos_unfiltered, next_source, has_more, source_total)

        Raises:
            GitLabProviderError: On API errors
        """
        page = source if isinstance(source, int) else 1
        params: dict = {
            "membership": "true",
            "page": page,
            "per_page": batch_size,
            "order_by": "last_activity_at",
            "sort": "desc",
        }
        if search:
            params["search"] = search
            params["search_namespaces"] = "true"
        response = self._make_api_request_checked("projects", params=params)
        projects = response.json()
        source_total = int(response.headers.get("x-total", "0")) or None
        total_pages = int(response.headers.get("x-total-pages", "1"))
        has_more = page < total_pages
        next_source: Optional[int] = page + 1 if has_more else None
        parsed = [self._parse_project(p) for p in projects]
        return parsed, next_source, has_more, source_total

    def _fetch_all_pages_rest(self) -> Tuple[List[DiscoveredRepository], int]:
        """
        Exhaust all GitLab REST pages and return (repos, source_total).

        Raises:
            GitLabProviderError: On HTTP failure or if page cap exceeded.
        """
        if not (isinstance(_DISCOVERY_MAX_PAGES, int) and _DISCOVERY_MAX_PAGES > 0):
            raise GitLabProviderError("_DISCOVERY_MAX_PAGES must be a positive integer")
        if not (
            isinstance(_DISCOVERY_REST_PAGE_SIZE, int) and _DISCOVERY_REST_PAGE_SIZE > 0
        ):
            raise GitLabProviderError(
                "_DISCOVERY_REST_PAGE_SIZE must be a positive integer"
            )
        all_repos: List[DiscoveredRepository] = []
        source_total = 0
        for page in range(1, _DISCOVERY_MAX_PAGES + 1):
            batch, _next, has_more, batch_total = self._fetch_batch_rest(
                source=page, batch_size=_DISCOVERY_REST_PAGE_SIZE, search=None
            )
            if batch_total is not None:
                source_total = batch_total
            all_repos.extend(batch)
            if not has_more:
                return all_repos, source_total
        raise GitLabProviderError(
            f"Exhaustive fetch exceeded maximum of {_DISCOVERY_MAX_PAGES} pages"
        )

    def _map_repos_to_dicts(
        self,
        repos: List[DiscoveredRepository],
        indexed_urls: Set[str],
        hidden_identifiers: Set[str],
    ) -> List[dict]:
        """Filter indexed repos and annotate each remaining repo with is_hidden."""
        if repos is None:
            raise ValueError("repos must not be None")
        if indexed_urls is None:
            raise ValueError("indexed_urls must not be None")
        if hidden_identifiers is None:
            raise ValueError("hidden_identifiers must not be None")
        out = []
        for repo in repos:
            if self._is_repo_indexed(
                repo.clone_url_https, repo.clone_url_ssh, indexed_urls
            ):
                continue
            hidden = (
                f"gitlab:{repo.clone_url_ssh}" in hidden_identifiers
                or f"gitlab:{repo.clone_url_https}" in hidden_identifiers
            )
            out.append(
                {
                    "platform": repo.platform,
                    "name": repo.name,
                    "description": repo.description,
                    "clone_url_https": repo.clone_url_https,
                    "clone_url_ssh": repo.clone_url_ssh,
                    "default_branch": repo.default_branch,
                    "is_hidden": hidden,
                    "is_private": repo.is_private,
                }
            )
        return out

    def discover_all_repositories(
        self,
        indexed_urls: Set[str],
        hidden_identifiers: Set[str],
    ) -> dict:
        """
        Exhaustively fetch all unregistered GitLab repositories in a single pass.

        Raises:
            ValueError: If indexed_urls or hidden_identifiers is None.
            GitLabProviderError: On upstream failure or page cap exceeded.
        """
        if indexed_urls is None:
            raise ValueError("indexed_urls must not be None")
        if hidden_identifiers is None:
            raise ValueError("hidden_identifiers must not be None")
        if not self.is_configured():
            raise GitLabProviderError(
                "GitLab token not configured. "
                "Please configure a GitLab token in the CI Tokens settings."
            )
        all_repos, source_total = self._fetch_all_pages_rest()
        out = self._map_repos_to_dicts(all_repos, indexed_urls, hidden_identifiers)
        total_unregistered = sum(1 for r in out if not r["is_hidden"])
        return {
            "repositories": out,
            "total_source": source_total,
            "total_unregistered": total_unregistered,
        }

    def _resolve_full_path_from_url(self, clone_url: str) -> str:
        """
        Extract GitLab project full path from a clone URL.

        Examples:
            https://gitlab.com/group/project.git  ->  group/project
            git@gitlab.com:group/project.git      ->  group/project

        Raises:
            GitLabProviderError: if the derived path is empty.
        """
        if not clone_url:
            raise GitLabProviderError("clone_url must not be empty")
        url = clone_url
        # Strip scheme (https:// or similar)
        for prefix in ("https://", "http://", "git://"):
            if url.startswith(prefix):
                url = url[len(prefix) :]
                break
        # Handle SSH form: git@host:path
        if "@" in url and ":" in url:
            url = url.split(":", 1)[1]
        else:
            # Strip host (first path segment after scheme removal)
            parts = url.split("/", 1)
            if len(parts) == 2:
                url = parts[1]
            else:
                url = parts[0]
        # Strip .git suffix and leading/trailing slashes
        if url.endswith(".git"):
            url = url[:-4]
        url = url.strip("/")
        if not url:
            raise GitLabProviderError(
                f"Could not derive full path from clone URL: {clone_url!r}"
            )
        return url

    def enrich_repositories(self, clone_urls: List[str]) -> dict:
        """
        Enrich repositories with latest commit metadata via GitLab GraphQL.

        Chunks clone_urls into batches of at most 10 and issues one GraphQL
        multiplex query per batch.  Per-repo failures (null project or errors
        field) are soft-failed: the repo is omitted from the result but the
        batch continues.

        Args:
            clone_urls: List of HTTPS clone URLs to enrich.

        Returns:
            Dict mapping input clone_url to commit-info dict with keys
            commit_hash, commit_author, commit_date.  Repos that could not be
            enriched are absent from the dict.
        """
        if clone_urls is None:
            raise GitLabProviderError("clone_urls must not be None")
        if not clone_urls:
            return {}

        result: dict = {}

        for batch_start in range(0, len(clone_urls), _ENRICH_BATCH_SIZE):
            batch = clone_urls[batch_start : batch_start + _ENRICH_BATCH_SIZE]
            full_paths = []
            url_by_path: dict = {}
            for url in batch:
                try:
                    path = self._resolve_full_path_from_url(url)
                    full_paths.append(path)
                    url_by_path[path] = url
                except GitLabProviderError:
                    logger.warning(f"Could not resolve full path for URL: {url!r}")

            if not full_paths:
                continue

            query = self._build_multiplex_query(full_paths)
            try:
                response = self._make_graphql_request(query)
                response.raise_for_status()
                body = response.json()
            except (
                httpx.TimeoutException,
                httpx.HTTPStatusError,
                httpx.RequestError,
            ) as exc:
                logger.warning(f"GitLab GraphQL enrich batch failed: {exc}")
                continue

            commit_info_by_path = self._parse_multiplex_response(body, full_paths)
            for path, commit_info in commit_info_by_path.items():
                original_url = url_by_path.get(path)
                if original_url:
                    # Copy and convert commit_date datetime to ISO string for JSON serialization
                    serializable = dict(commit_info)
                    raw_date = serializable.get("commit_date")
                    if raw_date is not None:
                        serializable["commit_date"] = raw_date.isoformat()
                    result[original_url] = serializable

        return result

    def discover_repositories(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        search: Optional[str] = None,
        hidden_identifiers: Optional[Set[str]] = None,
    ) -> RepositoryDiscoveryResult:
        """
        Server-side cursor pagination removed in Story #754.

        Use discover_all_repositories() for exhaustive fetch with client-side
        pagination instead.

        Raises:
            NotImplementedError: Always — cursor pagination removed in Story #754.
        """
        raise NotImplementedError(
            "Server-side cursor pagination removed in Story #754. "
            "Use discover_all_repositories() instead."
        )
