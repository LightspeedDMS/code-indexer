"""
Remote Branch Service for fetching branches from remote git repositories.

Provides functionality to:
1. Fetch branches from remote git URLs using git ls-remote
2. Filter out issue-tracker pattern branches (e.g., SCM-1234, PROJ-567)
3. Detect default branch from remote HEAD
4. Handle multiple repositories in batch

Following CLAUDE.md Foundation #1 (Anti-Mock): uses real git operations.
"""

import logging
import re
import subprocess
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse

from ..middleware.correlation import get_correlation_id

logger = logging.getLogger(__name__)

# Regex pattern for issue-tracker branch names
# Matches JIRA-style issue keys: uppercase letters followed by hyphen and numbers
# Examples: SCM-1234, A-1, AB-99, PROJ-567, X-9
# Also matches when these patterns appear within paths: feature/SCM-1234-hotfix
#
# Pattern explanation:
# - (?:^|/) - Start of string or after a slash (for paths like feature/SCM-1234)
# - ([A-Z]+-\d+) - Uppercase letters, hyphen, then digits
# - (?:$|[-/]) - End of string, hyphen, or slash (to catch SCM-1234-hotfix)
#
# Does NOT match lowercase words like: hotfix-123, bugfix-456
ISSUE_TRACKER_PATTERN = re.compile(r"(?:^|/)([A-Z]+-\d+)(?:$|[-/])")

# Regex pattern for SSH git URLs
# Format: git@<host>:<path>
# Examples: git@github.com:owner/repo.git, git@gitlab.com:group/subgroup/project.git
SSH_URL_PATTERN = re.compile(r"^git@([^:]+):(.+)$")


def _detect_platform_from_url(url: str) -> Optional[str]:
    """
    Detect platform (github/gitlab) from URL hostname.

    Uses hostname extraction to avoid false positives from path content.
    Only matches on the actual hostname, not on path or query string.

    Args:
        url: Git clone URL (SSH or HTTPS)

    Returns:
        'github', 'gitlab', or None if cannot detect
    """
    try:
        # Handle SSH URLs: git@host:path
        ssh_match = SSH_URL_PATTERN.match(url)
        if ssh_match:
            host = ssh_match.group(1).lower()
        else:
            # Handle HTTPS/HTTP URLs
            parsed = urlparse(url)
            host = (parsed.hostname or "").lower()

        # Check for exact domain or subdomain for GitHub
        if host == "github.com" or host.endswith(".github.com"):
            return "github"
        # Check for gitlab in hostname (gitlab.com, gitlab.company.com, etc.)
        if "gitlab" in host:
            return "gitlab"
    except Exception as e:
        logger.debug(
            f"Failed to parse URL for platform detection: {e}",
            extra={"correlation_id": get_correlation_id()},
        )
    return None


def _build_effective_url(
    clone_url: str, platform: Optional[str], credentials: Optional[str]
) -> str:
    """
    Build effective URL with credentials for git operations.

    Handles:
    1. SSH URLs (git@host:path) - converts to HTTPS with credentials
    2. HTTPS URLs - inserts credentials
    3. Platform-specific credential formats:
       - GitLab: oauth2:<token>
       - GitHub: <token>

    Args:
        clone_url: Original clone URL (SSH or HTTPS)
        platform: Platform name ('github' or 'gitlab'), or None for auto-detect
        credentials: API token for authentication

    Returns:
        Effective URL with credentials inserted, or original URL if no credentials
    """
    # No credentials - return original URL
    if not credentials:
        return clone_url

    # Detect platform from URL if not provided
    effective_platform = platform or _detect_platform_from_url(clone_url)

    # Handle SSH URLs: git@host:path -> https://[creds@]host/path
    ssh_match = SSH_URL_PATTERN.match(clone_url)
    if ssh_match:
        host = ssh_match.group(1)
        path = ssh_match.group(2)

        # Build credential prefix based on platform
        if effective_platform == "gitlab":
            # GitLab requires oauth2:<token> format
            cred_prefix = f"oauth2:{credentials}@"
        else:
            # GitHub uses just <token>@ format
            cred_prefix = f"{credentials}@"

        return f"https://{cred_prefix}{host}/{path}"

    # Handle HTTPS URLs
    if clone_url.startswith("https://"):
        # Parse URL parts
        url_without_scheme = clone_url[8:]  # Remove 'https://'

        # Remove any existing credentials from URL
        if "@" in url_without_scheme:
            # Has existing credentials, replace them
            at_pos = url_without_scheme.index("@")
            url_without_scheme = url_without_scheme[at_pos + 1 :]

        # Build credential prefix based on platform
        if effective_platform == "gitlab":
            cred_prefix = f"oauth2:{credentials}@"
        else:
            cred_prefix = f"{credentials}@"

        return f"https://{cred_prefix}{url_without_scheme}"

    # Unknown URL format - return original
    return clone_url


@dataclass
class BranchFetchRequest:
    """Request to fetch branches for a single repository."""

    clone_url: str
    platform: str  # "github" or "gitlab"


@dataclass
class BranchFetchResult:
    """Result of fetching branches for a repository."""

    success: bool
    branches: List[str] = field(default_factory=list)
    default_branch: Optional[str] = None
    error: Optional[str] = None


def filter_issue_tracker_branches(branches: List[str]) -> List[str]:
    """
    Filter out branches that contain issue-tracker patterns.

    Issue tracker patterns match: [A-Za-z]+-\d+ (e.g., SCM-1234, A-1, AB-99)

    These patterns are excluded because they typically represent:
    - Jira tickets (PROJ-123)
    - GitLab/GitHub issue branches
    - Other ticket system references

    Args:
        branches: List of branch names to filter

    Returns:
        List of branches that do NOT contain issue-tracker patterns

    Examples:
        >>> filter_issue_tracker_branches(["main", "SCM-1234", "feature/login"])
        ["main", "feature/login"]
        >>> filter_issue_tracker_branches(["feature/SCM-1234-hotfix", "main"])
        ["main"]
    """
    if not branches:
        return []

    filtered = []
    for branch in branches:
        if not ISSUE_TRACKER_PATTERN.search(branch):
            filtered.append(branch)

    return filtered


def extract_branch_names_from_ls_remote(ls_remote_output: str) -> List[str]:
    """
    Extract branch names from git ls-remote output.

    The git ls-remote --heads output format is:
    <commit_hash>\t<ref_name>

    Where ref_name is like: refs/heads/main, refs/heads/feature/login

    Args:
        ls_remote_output: Raw output from git ls-remote --heads

    Returns:
        List of branch names (without refs/heads/ prefix)

    Example:
        Input: "abc123\trefs/heads/main\ndef456\trefs/heads/develop"
        Output: ["main", "develop"]
    """
    if not ls_remote_output or not ls_remote_output.strip():
        return []

    branches = []
    for line in ls_remote_output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # Format: <hash>\t<ref>
        parts = line.split("\t")
        if len(parts) >= 2:
            ref = parts[1]
            # Only process refs/heads/ (branches), not refs/tags/
            if ref.startswith("refs/heads/"):
                branch_name = ref[len("refs/heads/") :]
                branches.append(branch_name)

    return branches


def _extract_default_branch_from_ls_remote(ls_remote_output: str) -> Optional[str]:
    """
    Extract default branch from git ls-remote output.

    Looks for the HEAD ref which indicates the default branch.
    Format: "ref: refs/heads/main\tHEAD" or "<hash>\tHEAD"

    Args:
        ls_remote_output: Raw output from git ls-remote (not --heads)

    Returns:
        Default branch name if found, None otherwise
    """
    if not ls_remote_output:
        return None

    for line in ls_remote_output.strip().split("\n"):
        line = line.strip()
        if not line:
            continue

        # Look for symbolic ref format: ref: refs/heads/main	HEAD
        if line.startswith("ref: refs/heads/") and "\t" in line:
            # Extract branch name between "refs/heads/" and "\t"
            parts = line.split("\t")
            if len(parts) >= 1 and parts[0].startswith("ref: refs/heads/"):
                return parts[0][len("ref: refs/heads/") :]

    return None


class RemoteBranchService:
    """
    Service for fetching branch information from remote git repositories.

    Uses git ls-remote to fetch branch information without cloning.
    Automatically filters out issue-tracker pattern branches.
    """

    def __init__(self, timeout: int = 30):
        """
        Initialize the remote branch service.

        Args:
            timeout: Timeout in seconds for git operations (default: 30)
        """
        self.timeout = timeout

    def fetch_remote_branches(
        self,
        clone_url: str,
        platform: Optional[str] = None,
        credentials: Optional[str] = None,
    ) -> BranchFetchResult:
        """
        Fetch branches from a remote git repository.

        Uses git ls-remote to list branches without cloning.
        Automatically filters out issue-tracker pattern branches.

        Args:
            clone_url: The git clone URL (HTTPS or SSH)
            platform: Platform name ("github" or "gitlab") - optional
            credentials: Optional credentials token for authentication

        Returns:
            BranchFetchResult with branches and default branch info
        """
        try:
            # Build the git URL with credentials if provided
            # Handles SSH URLs, HTTPS URLs, and platform-specific auth formats
            effective_url = _build_effective_url(clone_url, platform, credentials)

            # Fetch branch list using git ls-remote --heads
            branches_cmd = ["git", "ls-remote", "--heads", effective_url]
            result = subprocess.run(
                branches_cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={"GIT_TERMINAL_PROMPT": "0"},  # Disable interactive prompts
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip() if result.stderr else "Unknown error"
                logger.warning(
                    f"git ls-remote failed for {clone_url}: {error_msg}",
                    extra={"correlation_id": get_correlation_id()},
                )
                return BranchFetchResult(
                    success=False,
                    branches=[],
                    default_branch=None,
                    error=error_msg,
                )

            # Extract branch names from output
            all_branches = extract_branch_names_from_ls_remote(result.stdout)

            # Filter out issue-tracker pattern branches
            filtered_branches = filter_issue_tracker_branches(all_branches)

            # Try to detect default branch
            default_branch = self._detect_default_branch(
                effective_url, filtered_branches
            )

            return BranchFetchResult(
                success=True,
                branches=filtered_branches,
                default_branch=default_branch,
                error=None,
            )

        except subprocess.TimeoutExpired:
            error_msg = f"Timeout fetching branches (>{self.timeout}s)"
            logger.warning(
                f"git ls-remote timeout for {clone_url}",
                extra={"correlation_id": get_correlation_id()},
            )
            return BranchFetchResult(
                success=False,
                branches=[],
                default_branch=None,
                error=error_msg,
            )
        except Exception as e:
            error_msg = str(e)
            # SECURITY: Do not use exc_info=True here - stack traces could expose
            # effective_url which may contain embedded credentials
            logger.error(
                f"Error fetching branches for {clone_url}: {error_msg}",
                extra={"correlation_id": get_correlation_id()},
            )
            return BranchFetchResult(
                success=False,
                branches=[],
                default_branch=None,
                error=error_msg,
            )

    def _detect_default_branch(
        self, url: str, available_branches: List[str]
    ) -> Optional[str]:
        """
        Detect the default branch for a repository.

        First tries to read symbolic-ref from remote, then falls back
        to common default branch names.

        Args:
            url: The git URL to check
            available_branches: List of available branches

        Returns:
            Default branch name if detected, None otherwise
        """
        try:
            # Try to get symbolic-ref for HEAD
            cmd = ["git", "ls-remote", "--symref", url, "HEAD"]
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
                env={"GIT_TERMINAL_PROMPT": "0"},
            )

            if result.returncode == 0 and result.stdout:
                default = _extract_default_branch_from_ls_remote(result.stdout)
                if default and default in available_branches:
                    return default

        except (subprocess.TimeoutExpired, Exception) as e:
            logger.debug(
                f"Could not detect default branch via symref: {e}",
                extra={"correlation_id": get_correlation_id()},
            )

        # Fallback: check common default branch names
        common_defaults = ["main", "master", "develop", "development", "trunk"]
        for candidate in common_defaults:
            if candidate in available_branches:
                return candidate

        # If nothing found but we have branches, return the first one
        if available_branches:
            return available_branches[0]

        return None

    def fetch_branches_for_repos(
        self, requests: List[BranchFetchRequest]
    ) -> Dict[str, BranchFetchResult]:
        """
        Fetch branches for multiple repositories.

        Args:
            requests: List of BranchFetchRequest objects

        Returns:
            Dictionary mapping clone_url to BranchFetchResult
        """
        results: Dict[str, BranchFetchResult] = {}

        for request in requests:
            result = self.fetch_remote_branches(
                clone_url=request.clone_url,
                platform=request.platform,
                credentials=None,  # Credentials handled separately
            )
            results[request.clone_url] = result

        return results
