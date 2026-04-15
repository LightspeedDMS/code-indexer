"""
GitLab Repository Provider for CIDX Server.

Implements repository discovery from GitLab API, supporting both gitlab.com
and self-hosted GitLab instances.
"""

import base64
import json
import logging
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, List, Optional, Set, Tuple, Union

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

_CURSOR_VERSION = 1
_CURSOR_PLATFORM = "gitlab"
_FILL_SAFETY_CAP = 5
_VALID_CURSOR_MODES = frozenset({"rest"})


@dataclass
class _CursorState:
    """Decoded cursor state for GitLab pagination."""

    source: Optional[Union[str, int]]  # REST page number
    skip: int  # items to skip within the first fetched batch
    mode: str = "rest"  # GitLab only uses REST


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
                                commit_info["commit_date"] = datetime.fromisoformat(  # type: ignore[assignment]
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

        except Exception as e:
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

    def _encode_cursor(
        self, source: Optional[Union[str, int]], skip: int, mode: str = "rest"
    ) -> str:
        """
        Encode a cursor payload as an opaque base64-JSON string.

        Args:
            source: REST page number (integer) or None for first page
            skip: Non-negative items to skip within the first fetched batch
            mode: Must be a recognized cursor mode (always "rest" for GitLab)

        Raises:
            ValueError: If skip is negative or mode is not recognized
        """
        if skip < 0:
            raise ValueError(f"skip must be non-negative, got {skip}")
        if mode not in _VALID_CURSOR_MODES:
            raise ValueError(f"mode {mode!r} is not a recognized cursor mode")
        payload = {
            "v": _CURSOR_VERSION,
            "platform": _CURSOR_PLATFORM,
            "source": source,
            "skip": skip,
            "mode": mode,
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def _decode_cursor_payload(self, token: str) -> Optional[dict]:
        """Decode base64 token to JSON dict; return None on any decode failure."""
        import binascii

        if not isinstance(token, str):
            logger.debug(
                format_error_log("GL-CURSOR-000", "GitLab cursor token is not a string")
            )
            return None
        try:
            raw = base64.b64decode(token)
        except (binascii.Error, TypeError, ValueError) as exc:
            logger.debug(
                format_error_log("GL-CURSOR-001", f"GitLab cursor base64 failed: {exc}")
            )
            return None
        try:
            return json.loads(raw)  # type: ignore[no-any-return]  # json.loads returns Any
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug(
                format_error_log("GL-CURSOR-002", f"GitLab cursor JSON failed: {exc}")
            )
            return None

    def _validate_cursor_metadata(self, payload: dict) -> bool:
        """Validate version, platform and mode fields; log and return False on mismatch."""
        if payload.get("v") != _CURSOR_VERSION:
            logger.debug(
                format_error_log(
                    "GL-CURSOR-003",
                    f"GitLab cursor version mismatch (got {payload.get('v')!r})",
                )
            )
            return False
        if payload.get("platform") != _CURSOR_PLATFORM:
            logger.debug(
                format_error_log(
                    "GL-CURSOR-004",
                    f"GitLab cursor platform mismatch (got {payload.get('platform')!r})",
                )
            )
            return False
        mode = payload.get("mode", "rest")
        if mode not in _VALID_CURSOR_MODES:
            logger.debug(
                format_error_log(
                    "GL-CURSOR-007",
                    f"GitLab cursor mode={mode!r} not recognized",
                )
            )
            return False
        return True

    def _extract_cursor_fields(self, payload: dict) -> Optional[_CursorState]:
        """Extract source, skip, mode from a validated payload dict."""
        try:
            source = payload["source"]
            skip = int(payload["skip"])
            mode = str(payload.get("mode", "rest"))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug(
                format_error_log("GL-CURSOR-005", f"GitLab cursor field error: {exc}")
            )
            return None
        if skip < 0:
            logger.debug(
                format_error_log(
                    "GL-CURSOR-006", f"GitLab cursor skip={skip} is negative"
                )
            )
            return None
        return _CursorState(source=source, skip=skip, mode=mode)

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
        response = self._make_api_request_checked("projects", params=params)
        projects = response.json()
        source_total = int(response.headers.get("x-total", "0")) or None
        total_pages = int(response.headers.get("x-total-pages", "1"))
        has_more = page < total_pages
        next_source: Optional[int] = page + 1 if has_more else None
        parsed = [self._parse_project(p) for p in projects]
        return parsed, next_source, has_more, source_total

    def _collect_unindexed_from_batch(
        self,
        batch: List[DiscoveredRepository],
        skip: int,
        kept: List[DiscoveredRepository],
        target: int,
        indexed_urls: Set[str],
    ) -> Tuple[List[DiscoveredRepository], Optional[int]]:
        """
        Walk batch from skip offset, collect unindexed repos until target reached.

        Returns:
            (updated_kept, stop_index_or_None)
        """
        for i, repo in enumerate(batch):
            if i < skip:
                continue
            if self._is_repo_indexed(
                repo.clone_url_https, repo.clone_url_ssh, indexed_urls
            ):
                continue
            kept.append(repo)
            if len(kept) == target:
                return kept, i
        return kept, None

    def _build_result(
        self,
        kept: List[DiscoveredRepository],
        page_size: int,
        has_next_page: bool,
        next_cursor: Optional[str],
        partial_due_to_cap: bool,
        source_total: Optional[int],
    ) -> RepositoryDiscoveryResult:
        """Build a RepositoryDiscoveryResult for GitLab from collected data."""
        return RepositoryDiscoveryResult(
            repositories=kept,
            page_size=page_size,
            platform="gitlab",
            has_next_page=has_next_page,
            next_cursor=next_cursor,
            partial_due_to_cap=partial_due_to_cap,
            source_total=source_total,
        )

    def _decode_cursor(self, token: Optional[str]) -> Optional[_CursorState]:
        """
        Decode an opaque cursor string into a _CursorState.

        Returns None for any invalid input, triggering a silent restart.
        """
        if token is None:
            return None
        payload = self._decode_cursor_payload(token)
        if payload is None:
            return None
        if not self._validate_cursor_metadata(payload):
            return None
        return self._extract_cursor_fields(payload)

    def _init_discovery_state(self, cursor: Optional[str]) -> _CursorState:
        """Return decoded cursor state or a fresh initial state (page 1)."""
        decoded = self._decode_cursor(cursor)
        if decoded is not None:
            return decoded
        return _CursorState(source=None, skip=0, mode="rest")

    def _result_from_stop(
        self,
        kept: List[DiscoveredRepository],
        page_size: int,
        stop_idx: int,
        batch_len: int,
        has_more: bool,
        next_source: Optional[Union[str, int]],
        state: _CursorState,
        source_total: Optional[int],
    ) -> RepositoryDiscoveryResult:
        """Build result when page_size target was hit inside a batch."""
        is_last_in_batch = stop_idx == batch_len - 1
        if is_last_in_batch and not has_more:
            return self._build_result(kept, page_size, False, None, False, source_total)
        next_skip = 0 if is_last_in_batch else stop_idx + 1
        next_src = next_source if is_last_in_batch else state.source
        enc = self._encode_cursor(next_src, next_skip, "rest")
        return self._build_result(kept, page_size, True, enc, False, source_total)

    def _run_fill_loop(
        self,
        state: _CursorState,
        effective: int,
        page_size: int,
        search: Optional[str],
        indexed_urls: Set[str],
    ) -> RepositoryDiscoveryResult:
        """Run the filter-fill loop and return a RepositoryDiscoveryResult."""
        kept: List[DiscoveredRepository] = []
        source_total: Optional[int] = None
        batches_fetched = 0
        while len(kept) < effective and batches_fetched < _FILL_SAFETY_CAP:
            batch, next_source, has_more, batch_total = self._fetch_batch_rest(
                source=state.source if isinstance(state.source, int) else None,
                batch_size=effective,
                search=search,
            )
            batches_fetched += 1
            if source_total is None and batch_total is not None:
                source_total = batch_total
            kept, stop_idx = self._collect_unindexed_from_batch(
                batch, state.skip, kept, effective, indexed_urls
            )
            if stop_idx is not None:
                return self._result_from_stop(
                    kept,
                    page_size,
                    stop_idx,
                    len(batch),
                    has_more,
                    next_source,
                    state,
                    source_total,
                )
            state = _CursorState(source=next_source, skip=0, mode="rest")
            if not has_more:
                return self._build_result(
                    kept, page_size, False, None, False, source_total
                )
        cap_cursor = self._encode_cursor(state.source, state.skip, "rest")
        return self._build_result(kept, page_size, True, cap_cursor, True, source_total)

    def discover_repositories(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        search: Optional[str] = None,
    ) -> RepositoryDiscoveryResult:
        """
        Discover repositories from GitLab using cursor-based pagination.

        Runs a filter-fill loop: fetches source pages until page_size unindexed
        repositories are collected or the safety cap is reached.

        Args:
            cursor: Opaque token from a previous call (None for first page)
            page_size: Target number of unindexed repos to return
            search: Optional search query (server-side filtering via API)

        Returns:
            RepositoryDiscoveryResult with cursor fields

        Raises:
            GitLabProviderError: If not configured or API call fails
        """
        if not self.is_configured():
            raise GitLabProviderError(
                "GitLab token not configured. "
                "Please configure a GitLab token in the CI Tokens settings."
            )
        if page_size < 1:
            raise GitLabProviderError("page_size must be at least 1")
        indexed_urls = self._get_indexed_canonical_urls()
        state = self._init_discovery_state(cursor)
        result = self._run_fill_loop(state, page_size, page_size, search, indexed_urls)
        # Enrich with commit info via GraphQL (REST doesn't provide it)
        if result.repositories:
            self._enrich_repositories_with_commits(result.repositories)
        return result
