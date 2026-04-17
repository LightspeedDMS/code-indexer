"""
GitHub Repository Provider for CIDX Server.

Implements repository discovery from GitHub API, supporting user repositories
and organization repositories accessible via personal access token.
"""

import base64
import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime
from typing import TYPE_CHECKING, Any, List, Optional, Set, Tuple, Union

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
_CURSOR_PLATFORM = "github"
_FILL_SAFETY_CAP = 5  # max source batches fetched per user page
GITHUB_MAX_PAGE_SIZE = 100  # GitHub API hard limit for per_page


class GitHubProviderError(Exception):
    """Exception raised for GitHub provider errors."""

    pass


@dataclass
class _CursorState:
    """Decoded cursor state for GitHub pagination."""

    source: Optional[Union[str, int]]  # GraphQL endCursor or REST page number
    skip: int  # items to skip within the first fetched batch
    mode: str = "graphql"  # active backend: "graphql", "rest", or "search"


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
            logger.warning(
                format_error_log(
                    "GIT-GENERAL-065",
                    f"Failed to enrich repositories with commit info: {e}",
                )
            )

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

    def _make_api_request_checked(
        self, endpoint: str, params: Optional[dict] = None
    ) -> httpx.Response:
        """
        Make a REST API request and translate transport/HTTP errors to GitHubProviderError.

        Returns:
            Successful HTTP response

        Raises:
            GitHubProviderError: On timeout, HTTP error, or network failure
        """
        try:
            response = self._make_api_request(endpoint, params=params)
            self._check_rate_limit(response)
            response.raise_for_status()
        except httpx.TimeoutException as e:
            raise GitHubProviderError(f"GitHub API request timed out: {e}") from e
        except httpx.HTTPStatusError as e:
            if hasattr(e, "response") and e.response is not None:
                self._check_rate_limit(e.response)
            status = (
                e.response.status_code
                if hasattr(e, "response") and e.response
                else "unknown"
            )
            raise GitHubProviderError(f"GitHub API error: {status}") from e
        except httpx.RequestError as e:
            raise GitHubProviderError(f"GitHub API request failed: {e}") from e
        return response

    _VALID_CURSOR_MODES = frozenset({"graphql", "rest", "search"})

    def _encode_cursor(
        self, source: Optional[Union[str, int]], skip: int, mode: str = "graphql"
    ) -> str:
        """
        Encode a cursor payload as an opaque base64-JSON string.

        Args:
            source: GraphQL endCursor string or REST integer page number
            skip: Non-negative items to skip within the first fetched batch
            mode: Active backend: "graphql", "rest", or "search"
        """
        payload = {
            "v": _CURSOR_VERSION,
            "platform": _CURSOR_PLATFORM,
            "source": source,
            "skip": skip,
            "mode": mode,
        }
        return base64.b64encode(json.dumps(payload).encode()).decode()

    def _decode_cursor_payload(self, token: str) -> Optional[dict]:
        """Decode base64 token to JSON dict; return None on failure."""
        import binascii

        try:
            raw = base64.b64decode(token)
        except (binascii.Error, ValueError) as exc:
            logger.debug(
                format_error_log(
                    "GIT-CURSOR-001", f"GitHub cursor base64 failed: {exc}"
                )
            )
            return None
        try:
            return json.loads(raw)  # type: ignore[no-any-return]  # json.loads returns Any
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.debug(
                format_error_log("GIT-CURSOR-002", f"GitHub cursor JSON failed: {exc}")
            )
            return None

    def _validate_cursor_metadata(self, payload: dict) -> bool:
        """Validate version, platform and mode fields; log and return False on mismatch."""
        if payload.get("v") != _CURSOR_VERSION:
            logger.debug(
                format_error_log(
                    "GIT-CURSOR-003",
                    f"GitHub cursor version mismatch (got {payload.get('v')!r})",
                )
            )
            return False
        if payload.get("platform") != _CURSOR_PLATFORM:
            logger.debug(
                format_error_log(
                    "GIT-CURSOR-004",
                    f"GitHub cursor platform mismatch (got {payload.get('platform')!r})",
                )
            )
            return False
        mode = payload.get("mode", "graphql")
        if mode not in self._VALID_CURSOR_MODES:
            logger.debug(
                format_error_log(
                    "GIT-CURSOR-007",
                    f"GitHub cursor mode={mode!r} not recognized",
                )
            )
            return False
        return True

    def _extract_cursor_fields(self, payload: dict) -> Optional[_CursorState]:
        """Extract source, skip, mode from a validated payload dict."""
        try:
            source = payload["source"]
            skip = int(payload["skip"])
            mode = str(payload.get("mode", "graphql"))
        except (KeyError, TypeError, ValueError) as exc:
            logger.debug(
                format_error_log("GIT-CURSOR-005", f"GitHub cursor field error: {exc}")
            )
            return None
        if skip < 0:
            logger.debug(
                format_error_log(
                    "GIT-CURSOR-006", f"GitHub cursor skip={skip} is negative"
                )
            )
            return None
        return _CursorState(source=source, skip=skip, mode=mode)

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

    def _fetch_batch_graphql(
        self, source: Optional[str], batch_size: int
    ) -> Tuple[List[DiscoveredRepository], Optional[str], bool, Optional[int]]:
        """
        Fetch one batch via GraphQL.

        Returns:
            (parsed_repos_unfiltered, next_source, has_more, source_total)

        Raises:
            GitHubProviderError: On API errors
        """
        query = self._build_graphql_query(first=batch_size, after=source)
        try:
            response = self._make_graphql_request(query)
        except httpx.TimeoutException as e:
            raise GitHubProviderError(f"GitHub API request timed out: {e}") from e
        self._check_rate_limit(response)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as e:
            status = (
                e.response.status_code
                if hasattr(e, "response") and e.response
                else "unknown"
            )
            raise GitHubProviderError(f"GitHub API error: {status}") from e

        data = response.json()
        if "data" not in data or "viewer" not in data["data"]:
            raise GitHubProviderError("Invalid GraphQL response structure")

        repos_data = data["data"]["viewer"].get("repositories", {})
        nodes = repos_data.get("nodes", [])
        page_info = repos_data.get("pageInfo", {})
        source_total: Optional[int] = repos_data.get("totalCount")
        has_more = bool(page_info.get("hasNextPage"))
        next_source: Optional[str] = page_info.get("endCursor")
        parsed = [self._parse_graphql_response(n) for n in nodes]
        return parsed, next_source, has_more, source_total

    def _fetch_batch_rest(
        self, source: Optional[int], batch_size: int
    ) -> Tuple[List[DiscoveredRepository], Optional[int], bool, Optional[int]]:
        """
        Fetch one batch via REST user/repos endpoint (GraphQL fallback).

        Returns:
            (parsed_repos_unfiltered, next_source, has_more, source_total)

        Raises:
            GitHubProviderError: On API errors
        """
        page = source if isinstance(source, int) else 1
        params = {
            "page": page,
            "per_page": batch_size,
            "sort": "pushed",
            "direction": "desc",
            "affiliation": "owner,collaborator,organization_member",
        }
        response = self._make_api_request_checked("user/repos", params=params)
        repos = response.json()
        link_header = response.headers.get("Link", "")
        last_page = self._parse_link_header_for_last_page(link_header)
        has_more = page < last_page
        next_source: Optional[int] = page + 1 if has_more else None
        source_total: Optional[int] = last_page * batch_size if last_page > 1 else None
        parsed = [self._parse_repository(r) for r in repos]
        return parsed, next_source, has_more, source_total

    def _fetch_batch_search(
        self, source: Optional[int], batch_size: int, search: str
    ) -> Tuple[List[DiscoveredRepository], Optional[int], bool, Optional[int]]:
        """
        Fetch one batch via REST search/repositories endpoint.

        Returns:
            (parsed_repos_unfiltered, next_source, has_more, source_total)

        Raises:
            GitHubProviderError: On API errors
        """
        page = source if isinstance(source, int) else 1
        params = {
            "q": f"{search} in:name,description fork:true",
            "page": page,
            "per_page": batch_size,
            "sort": "updated",
            "order": "desc",
        }
        response = self._make_api_request_checked("search/repositories", params=params)
        data = response.json()
        repos = data.get("items", [])
        source_total: Optional[int] = data.get("total_count")
        total_count = source_total or 0
        total_pages = (
            (total_count + batch_size - 1) // batch_size if total_count > 0 else 1
        )
        has_more = page < total_pages
        next_source: Optional[int] = page + 1 if has_more else None
        parsed = [self._parse_repository(r) for r in repos]
        return parsed, next_source, has_more, source_total

    def _collect_unindexed_from_batch(
        self,
        batch: List[DiscoveredRepository],
        skip: int,
        kept: List[DiscoveredRepository],
        target: int,
        indexed_urls: Set[str],
        hidden_identifiers: Optional[Set[str]] = None,
    ) -> Tuple[List[DiscoveredRepository], Optional[int]]:
        """
        Walk batch from skip offset, collect unindexed repos until target reached.

        Returns:
            (updated_kept, stop_index_or_None)
            stop_index is the batch index where target was hit (inclusive); None if not hit.
        """
        _hidden = hidden_identifiers or set()
        for i, repo in enumerate(batch):
            if i < skip:
                continue
            if self._is_repo_indexed(
                repo.clone_url_https, repo.clone_url_ssh, indexed_urls
            ):
                continue
            # Story #719: skip hidden repos (check both SSH and HTTPS variants)
            if _hidden and (
                f"github:{repo.clone_url_ssh}" in _hidden
                or f"github:{repo.clone_url_https}" in _hidden
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
        """Build a RepositoryDiscoveryResult for GitHub from collected data."""
        return RepositoryDiscoveryResult(
            repositories=kept,
            page_size=page_size,
            platform="github",
            has_next_page=has_next_page,
            next_cursor=next_cursor,
            partial_due_to_cap=partial_due_to_cap,
            source_total=source_total,
        )

    def _init_discovery_state(
        self, cursor: Optional[str], search: Optional[str]
    ) -> _CursorState:
        """Return decoded cursor state or a fresh initial state."""
        decoded = self._decode_cursor(cursor)
        if decoded is not None:
            return decoded
        initial_mode = "search" if search else "graphql"
        return _CursorState(source=None, skip=0, mode=initial_mode)

    def _fetch_next_batch(
        self,
        state: _CursorState,
        effective_page_size: int,
        search: Optional[str],
    ) -> Tuple[
        List[DiscoveredRepository], Optional[Union[str, int]], bool, Optional[int], str
    ]:
        """
        Dispatch one batch fetch; raises GitHubProviderError on failure.

        Returns (batch, next_source, has_more, source_total, active_mode).
        """
        return self._dispatch_fetch_batch(state, effective_page_size, search)

    def _run_fill_loop(
        self,
        state: _CursorState,
        effective: int,
        page_size: int,
        search: Optional[str],
        indexed_urls: Set[str],
        hidden_identifiers: Optional[Set[str]] = None,
    ) -> RepositoryDiscoveryResult:
        """
        Run the filter-fill loop and return a RepositoryDiscoveryResult.

        Fetches source batches until effective unindexed repos are collected
        or SAFETY_CAP is reached.
        """
        kept: List[DiscoveredRepository] = []
        source_total: Optional[int] = None
        batches_fetched = 0
        while len(kept) < effective and batches_fetched < _FILL_SAFETY_CAP:
            batch, next_source, has_more, batch_total, active_mode = (
                self._fetch_next_batch(state, effective, search)
            )
            batches_fetched += 1
            if source_total is None and batch_total is not None:
                source_total = batch_total
            kept, stop_idx = self._collect_unindexed_from_batch(
                batch, state.skip, kept, effective, indexed_urls, hidden_identifiers
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
                    active_mode,
                    source_total,
                )
            state = _CursorState(source=next_source, skip=0, mode=active_mode)
            if not has_more:
                return self._build_result(
                    kept, page_size, False, None, False, source_total
                )
        cap_cursor = self._encode_cursor(state.source, state.skip, state.mode)
        return self._build_result(kept, page_size, True, cap_cursor, True, source_total)

    def _result_from_stop(
        self,
        kept: List[DiscoveredRepository],
        page_size: int,
        stop_idx: int,
        batch_len: int,
        has_more: bool,
        next_source: Optional[Union[str, int]],
        state: _CursorState,
        active_mode: str,
        source_total: Optional[int],
    ) -> RepositoryDiscoveryResult:
        """Build result when page_size target was hit inside a batch."""
        is_last_in_batch = stop_idx == batch_len - 1
        if is_last_in_batch and not has_more:
            return self._build_result(kept, page_size, False, None, False, source_total)
        next_skip = 0 if is_last_in_batch else stop_idx + 1
        next_src = next_source if is_last_in_batch else state.source
        enc = self._encode_cursor(next_src, next_skip, active_mode)
        return self._build_result(kept, page_size, True, enc, False, source_total)

    def _dispatch_fetch_batch(
        self,
        state: _CursorState,
        effective_page_size: int,
        search: Optional[str],
    ) -> Tuple[
        List[DiscoveredRepository], Optional[Union[str, int]], bool, Optional[int], str
    ]:
        """
        Dispatch to the correct batch fetcher based on cursor mode and search flag.

        Returns:
            (batch, next_source, has_more, source_total, active_mode)

        Raises:
            GitHubProviderError: On API errors
        """
        if search:
            batch, next_source, has_more, source_total = self._fetch_batch_search(
                source=state.source if isinstance(state.source, int) else None,
                batch_size=effective_page_size,
                search=search,
            )
            return batch, next_source, has_more, source_total, "search"

        if state.mode == "rest":
            batch, next_source, has_more, source_total = self._fetch_batch_rest(
                source=state.source if isinstance(state.source, int) else None,
                batch_size=effective_page_size,
            )
            return batch, next_source, has_more, source_total, "rest"

        try:
            batch, gql_next, has_more, source_total = self._fetch_batch_graphql(
                source=state.source if isinstance(state.source, str) else None,
                batch_size=effective_page_size,
            )
            return batch, gql_next, has_more, source_total, "graphql"
        except httpx.RequestError:
            logger.warning(
                "GraphQL batch fetch failed (network error); falling back to REST user/repos. "
                "GraphQL cursor invalidated — restarting from REST page 1."
            )
            batch, next_source, has_more, source_total = self._fetch_batch_rest(
                source=1,
                batch_size=effective_page_size,
            )
            return batch, next_source, has_more, source_total, "rest"

    def discover_repositories(
        self,
        cursor: Optional[str] = None,
        page_size: int = 50,
        search: Optional[str] = None,
        hidden_identifiers: Optional[Set[str]] = None,
    ) -> RepositoryDiscoveryResult:
        """
        Discover repositories from GitHub using cursor-based pagination.

        Runs a filter-fill loop: fetches source batches until page_size unindexed
        repositories are collected or the safety cap is reached.

        Args:
            cursor: Opaque token from a previous call (None for first page)
            page_size: Target number of unindexed repos to return
            search: Optional search query (routes to REST search endpoint)

        Returns:
            RepositoryDiscoveryResult with cursor fields

        Raises:
            GitHubProviderError: If not configured or API call fails
        """
        if not self.is_configured():
            raise GitHubProviderError(
                "GitHub token not configured. "
                "Please configure a GitHub token in the CI Tokens settings."
            )
        if page_size < 1:
            raise GitHubProviderError("page_size must be at least 1")
        indexed_urls = self._get_indexed_canonical_urls()
        effective = min(page_size, GITHUB_MAX_PAGE_SIZE)
        state = self._init_discovery_state(cursor, search)
        return self._run_fill_loop(
            state, effective, page_size, search, indexed_urls, hidden_identifiers
        )
