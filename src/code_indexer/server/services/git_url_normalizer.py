"""
Git URL Normalization Service for CIDX Server.

Provides comprehensive git URL normalization to enable matching between different
URL formats (HTTP vs SSH, with/without .git suffix, etc.) for repository discovery.

Supported URL forms:
  - https://[userinfo@]host[:port]/owner/repo[.git]
  - http://[userinfo@]host[:port]/owner/repo[.git]
  - ssh://[user@]host[:port]/owner/repo[.git]
  - user@host:owner/repo[.git] (SSH scp-style, any user)
  - /absolute/path             (filesystem path)
  - file:///absolute/path      (file URI — normalized to same form as /absolute/path)
  - ~/relative/path            (tilde-expanded to absolute path)

Remote URLs are parsed by the single shared parser,
``code_indexer.utils.git_remote_url.parse_git_remote_url``.
"""

import os

from pydantic import BaseModel

from code_indexer.utils.credential_redaction import mask_url_credentials
from code_indexer.utils.git_remote_url import GitRemoteUrl, parse_git_remote_url


class GitUrlNormalizationError(Exception):
    """Exception raised when git URL normalization fails."""

    pass


class NormalizedGitUrl(BaseModel):
    """Normalized git URL representation."""

    original_url: str
    canonical_form: str
    domain: str
    user: str
    repo: str

    def __eq__(self, other) -> bool:
        """Compare NormalizedGitUrl objects based on canonical form."""
        if not isinstance(other, NormalizedGitUrl):
            return False
        return self.canonical_form == other.canonical_form

    def __hash__(self) -> int:
        """Hash based on canonical form for use in sets/dicts."""
        return hash(self.canonical_form)


class GitUrlNormalizer:
    """Service for normalizing git URLs to canonical forms."""

    def normalize(self, git_url: object) -> NormalizedGitUrl:
        """
        Normalize a git URL to canonical form.

        Accepts HTTPS, SSH, and three filesystem-path forms:
          - /absolute/path
          - file:///absolute/path  (normalized to same canonical as /absolute/path)
          - ~/relative/path        (expanded via os.path.expanduser)

        Args:
            git_url: The git URL or filesystem path to normalize.
                     Must be a non-empty string.

        Returns:
            NormalizedGitUrl object with canonical representation

        Raises:
            GitUrlNormalizationError: If the value is not a non-empty string,
                                      or does not match any supported format.
        """
        if not isinstance(git_url, str):
            raise GitUrlNormalizationError(
                f"Git URL must be a string, got {type(git_url).__name__}"
            )

        if not git_url.strip():
            raise GitUrlNormalizationError("Git URL cannot be empty")

        git_url = git_url.strip()

        # Remote forms take priority. The identity is the forge web host and
        # the repository path: userinfo and the SSH port are not part of it.
        remote = parse_git_remote_url(git_url)
        if remote is not None:
            return self._create_normalized_url(git_url, remote)

        # Try filesystem path forms (after remote forms to preserve priority)
        if git_url.startswith("file:///"):
            # Strip the file:// scheme prefix to obtain the absolute path
            abs_path = git_url[len("file://") :].rstrip("/")
            return self._create_filesystem_url(git_url, abs_path)

        if git_url.startswith("~/"):
            abs_path = os.path.expanduser(git_url).rstrip("/")
            return self._create_filesystem_url(git_url, abs_path)

        if git_url.startswith("/"):
            abs_path = git_url.rstrip("/")
            return self._create_filesystem_url(git_url, abs_path)

        # If no patterns match, it's not a valid git URL
        raise GitUrlNormalizationError(
            f"Invalid git URL format: {mask_url_credentials(git_url)}"
        )

    def _create_filesystem_url(
        self, original_url: str, abs_path: str
    ) -> NormalizedGitUrl:
        """
        Create a NormalizedGitUrl for a local filesystem path.

        The canonical form is ``local/<abs_path>`` so that any two calls with
        the same absolute path (whether supplied as /abs, file:///abs, or ~/rel)
        produce the same canonical form, enabling repository discovery equality.

        Args:
            original_url: The original URL string as provided by the caller.
            abs_path: The resolved absolute path (no trailing slash).

        Returns:
            NormalizedGitUrl with domain="local".

        Raises:
            GitUrlNormalizationError: If abs_path has fewer than two components
                                       (cannot derive both user and repo).
        """
        if not abs_path or abs_path == "/":
            raise GitUrlNormalizationError(
                "Filesystem path cannot be empty or bare '/'"
            )

        repo = os.path.basename(abs_path)
        parent = os.path.dirname(abs_path)

        if not repo or not parent or parent == "/":
            raise GitUrlNormalizationError(
                f"Filesystem path must have at least two components: {abs_path}"
            )

        canonical_form = f"local/{abs_path}"

        return NormalizedGitUrl(
            original_url=original_url,
            canonical_form=canonical_form,
            domain="local",
            user=parent,
            repo=repo,
        )

    def _create_normalized_url(
        self, original_url: str, remote: GitRemoteUrl
    ) -> NormalizedGitUrl:
        """
        Create a normalized URL object from a parsed remote URL; its
        canonical form is the remote's identity (``web_host/repo_path``).

        Raises:
            GitUrlNormalizationError: If the path has no owner/repo
        """
        if not remote.repo_path:
            raise GitUrlNormalizationError("Repository path cannot be empty")
        try:
            user, repo = remote.owner_repo()
        except ValueError:
            raise GitUrlNormalizationError(
                f"Invalid repository path format: {remote.repo_path}"
            ) from None

        return NormalizedGitUrl(
            original_url=original_url,
            canonical_form=remote.identity,
            domain=remote.web_host,
            user=user,
            repo=repo,
        )

    def are_equivalent(self, url1: str, url2: str) -> bool:
        """
        Check if two git URLs are equivalent (normalize to same canonical form).

        Args:
            url1: First git URL
            url2: Second git URL

        Returns:
            True if URLs are equivalent, False otherwise
        """
        try:
            normalized1 = self.normalize(url1)
            normalized2 = self.normalize(url2)
            return normalized1.canonical_form == normalized2.canonical_form
        except GitUrlNormalizationError:
            return False

    def get_canonical_form(self, git_url: str) -> str:
        """
        Get the canonical form of a git URL.

        Args:
            git_url: The git URL to normalize

        Returns:
            Canonical form string

        Raises:
            GitUrlNormalizationError: If URL cannot be normalized
        """
        normalized = self.normalize(git_url)
        return normalized.canonical_form
