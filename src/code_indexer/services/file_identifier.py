"""
Generic file identification service that works with or without git.

This service provides a unified interface for file identification and metadata
extraction, automatically detecting git repositories and providing enhanced
metadata when available, while gracefully falling back to filesystem-based
identification when git is not available.
"""

import hashlib
import logging
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Any, Optional

from ..config import Config
from ..utils.git_runner import run_git_command, is_git_repository
from ..indexing.repo_manifest import RepoManifest
from ..utils.blob_hash import git_blob_hash, git_blob_hash_bytes
from .git_head import probe_backend_repository, read_head_branch, read_head_commit

logger = logging.getLogger(__name__)


class FileIdentifier:
    """
    Provides git-aware file identification with fallback to filesystem-based identification.

    This class automatically detects whether the project is in a git repository and
    provides enhanced metadata when git is available, while ensuring compatibility
    with non-git projects.
    """

    def __init__(
        self,
        project_dir: Path,
        config: Optional[Config] = None,
        manifest: Optional["RepoManifest"] = None,
        git_backend: Optional[Any] = None,
    ):
        """
        Initialize FileIdentifier with project directory and optional configuration.

        Args:
            project_dir: Path to the project directory
            config: Optional project configuration for file filtering
            manifest: a manifest of the repository taken at the start of this
                indexing pass. When it can vouch for a file, the blob hash comes
                from it and the file is NOT opened to hash it -- on the
                golden-repos NFS mount that is one round trip per file removed.
                None keeps the read, so every existing caller is unchanged.
            git_backend: the git seam (git-mcp) for a repository this machine
                does not hold. HEAD's branch and commit are asked of it, and the
                repository counts as git-addressable. None keeps local git.
        """
        self._manifest = manifest

        self.project_dir = project_dir
        self.config = config
        self.git_available = self._detect_git()
        # A manifest supplies git blob hashes by construction, so a repository it
        # can describe IS git-addressable whether or not this container can see a
        # .git directory. Without this a delegated pod probes a path it has no
        # mount for, decides "not a git repo", and never asks the manifest for
        # the hashes it is holding -- found by running the seam on EKS, where
        # every hash came back None while the backend was answering perfectly.
        if manifest is not None:
            self.git_available = True
        self._project_id: Optional[str] = None
        # Issue #676: Memoized per-run invariants — branch and commit hash do not
        # change during a single indexing run, so we fetch them once and cache.
        self._cached_branch: Optional[str] = None
        self._cached_commit_hash: Optional[str] = None
        # Likewise a git backend: git-mcp answers for the repository whether or
        # not this machine can see a .git directory -- but only for a path that
        # IS a repository. The server stamps the backend on every spawn,
        # including non-git sources (cidx-meta, local:// folders); treating one
        # as git-addressable would switch its file signatures to git hashes and
        # change its point ids. So the backend is asked -- and ONLY its explicit
        # "not a git repository" makes the path non-git: a transient failure
        # must not demote a real repository (that would change ITS point ids).
        self._git_backend = git_backend
        self._backend_repo = False
        if git_backend is not None:
            # The resolved commit is kept as this run's commit (same question),
            # so a real repository pays no extra call.
            self._backend_repo, self._cached_commit_hash = probe_backend_repository(
                project_dir, git_backend
            )
            if manifest is None:
                self.git_available = self._backend_repo

    def set_manifest(self, manifest: Optional["RepoManifest"]) -> None:
        """Point at the manifest for the CURRENT pass, or None for no manifest.

        The manifest describes the repository as of when it was built, so its
        lifetime has to be one pass. An indexer is reused -- watch mode holds one
        for a whole session, and the reprocess pass runs on the same instance --
        and a manifest held across passes would keep serving the blob hash a file
        had before it was edited. The new bytes would then be chunked under the
        old hash, and since the point UUID is seeded from that hash the chunks
        would be written over the previous version's ids: silent
        re-identification, which is the exact failure this value's own docstring
        warns about.
        """
        self._manifest = manifest
        # Same reason as in __init__: attaching a manifest makes the repository
        # git-addressable, and detaching one restores the local probe's answer.
        if manifest is not None:
            self.git_available = True
        elif self._git_backend is not None:
            self.git_available = self._backend_repo
        else:
            self.git_available = self._detect_git()

    def _detect_git(self) -> bool:
        """
        Detect if this project is in a git repository.

        Returns:
            True if git repository is detected, False otherwise
        """
        return is_git_repository(self.project_dir)

    def get_project_id(self) -> str:
        """
        Get a unique project identifier (PUBLIC API).

        This method is the single source of truth for project_id detection.
        Use this method whenever you need to determine the project identifier
        to ensure consistency across the codebase.

        IMPORTANT: This is a PUBLIC API (not private with _) to enable other
        components (e.g., config_fixer) to use it. See Bug #85.

        Returns:
            Project identifier string (git repo name or directory name)

        Algorithm:
            1. If git repository with remote: extract repo name from remote URL
            2. Otherwise: use directory name as fallback
            3. Normalize: lowercase, replace underscores with dashes
        """
        if self._project_id is not None:
            return self._project_id

        if self.git_available:
            try:
                # The origin URL, from the git seam when one is configured: on a
                # pod without the repository local git has nothing to ask, and
                # the directory-name fallback below would re-identify every
                # chunk (the project id seeds each point id). Only git's own
                # answer (CalledProcessError, e.g. no such remote) falls back; a
                # transport failure propagates rather than silently do that.
                if self._git_backend is not None:
                    origin_url = str(
                        self._git_backend.remote_get_url(
                            str(self.project_dir), "origin"
                        )
                    ).strip()
                else:
                    origin_url = run_git_command(
                        ["git", "remote", "get-url", "origin"],
                        cwd=self.project_dir,
                    ).stdout.strip()

                # Extract repository name from URL
                if origin_url:
                    # Handle various URL formats (https, ssh, etc.)
                    repo_name = origin_url.split("/")[-1]
                    if repo_name.endswith(".git"):
                        repo_name = repo_name[:-4]
                    self._project_id = repo_name.lower().replace("_", "-")
                    assert self._project_id is not None
                    return self._project_id
            except subprocess.CalledProcessError:
                pass
            except OSError:
                # Locally: git missing or the cwd gone, as before. Through the
                # seam an OSError is the transport (ConnectionError is one).
                if self._git_backend is not None:
                    raise

        # Fallback to directory name
        self._project_id = self.project_dir.name.lower().replace("_", "-")
        assert self._project_id is not None
        return self._project_id

    def _get_file_content_hash(
        self, file_path: Path, content: Optional[bytes] = None
    ) -> str:
        """
        Generate SHA256 hash of file content.

        Args:
            file_path: Path to the file

        Returns:
            SHA256 hash of file content with 'sha256:' prefix
        """
        if content is not None:
            # Already in hand: hashing it avoids opening the file again, and
            # removes the window in which the file could change between the two
            # reads and leave the two hashes describing different bytes.
            return "sha256:" + hashlib.sha256(content).hexdigest()
        hasher = hashlib.sha256()
        try:
            with open(file_path, "rb") as f:
                for chunk in iter(lambda: f.read(8192), b""):
                    hasher.update(chunk)
            return f"sha256:{hasher.hexdigest()}"
        except (IOError, OSError):
            # Fallback hash for unreadable files
            return f"sha256:error-{abs(hash(str(file_path)))}"

    def _should_index_file(self, file_path: str) -> bool:
        """
        Check if a file should be indexed based on configuration.

        Args:
            file_path: Relative path to the file

        Returns:
            True if file should be indexed, False otherwise
        """
        if not self.config:
            # Default behavior without config - common code extensions
            common_extensions = {
                "py",
                "js",
                "ts",
                "tsx",
                "java",
                "c",
                "cpp",
                "go",
                "rs",
                "rb",
                "php",
                "pl",
                "pm",
                "pod",
                "t",
                "psgi",
                "sh",
                "bash",
                "html",
                "css",
                "md",
                "json",
                "yaml",
                "yml",
            }
            return any(file_path.endswith(f".{ext}") for ext in common_extensions)

        # Use configuration if available
        file_extension = Path(file_path).suffix.lstrip(".")

        # Check if extension is allowed
        if file_extension not in self.config.file_extensions:
            return False

        # Check if path should be excluded
        path_parts = Path(file_path).parts
        for exclude_dir in self.config.exclude_dirs:
            if exclude_dir in path_parts:
                return False

        return True

    def get_file_metadata(
        self,
        file_path: Path,
        content: Optional[bytes] = None,
        defer_content_hash: bool = False,
    ) -> Dict[str, Any]:
        """
        Get comprehensive file metadata (git-aware or fallback).

        Args:
            file_path: Path to the file

        Returns:
            Dictionary containing file metadata
        """
        rel_path = str(file_path.relative_to(self.project_dir))

        metadata = {
            "project_id": self.get_project_id(),
            "file_path": rel_path,
            # The sha256 is the one value that cannot come from the manifest: it is
            # a different algorithm, it is stored, and it is the point-UUID
            # signature when git is unavailable. So it needs the bytes. Deferring
            # it lets a caller that will read the file anyway compute it there,
            # instead of this phase reading the whole file just to hash it.
            # The deferring caller MUST fill it before the metadata is used.
            "file_hash": (
                None
                if (content is None and defer_content_hash)
                else self._get_file_content_hash(file_path, content)
            ),
            "indexed_at": datetime.now(timezone.utc).isoformat() + "Z",
            "git_available": self.git_available,
        }

        if self.git_available:
            metadata.update(self._get_git_metadata(file_path, content))
        else:
            metadata.update(self._get_filesystem_metadata(file_path))

        return metadata

    def _get_cached_branch(self) -> Optional[str]:
        """Return the current branch name, fetched exactly once per FileIdentifier instance.

        Asked of the git backend when one was given (a pod that does not hold
        the repository asks git-mcp), else of local git -- see
        ``services.git_head``. A failure degrades to "unknown".

        Issue #676: the branch is invariant for the duration of an indexing run.
        After the first call the result is memoized; subsequent calls return the
        cached value without asking again.

        Returns:
            Branch name string (e.g. "development"), a "detached-<sha>" synthetic
            name when HEAD is detached, "unknown" when git commands fail, or None
            when git is not available.
        """
        if self._cached_branch is None and self.git_available:
            self._cached_branch = read_head_branch(self.project_dir, self._git_backend)
        return self._cached_branch

    def _get_cached_commit_hash(self) -> Optional[str]:
        """Return the current HEAD commit hash, fetched exactly once per FileIdentifier instance.

        Issue #676: the commit hash is invariant for the duration of an indexing run.
        After the first call the result is memoized (including failure: stored as
        "unknown") so subsequent calls never ask again.

        Returns:
            Full 40-character commit hash string, "unknown" when the git command
            fails, or None when git is not available.
        """
        if self._cached_commit_hash is None and self.git_available:
            self._cached_commit_hash = read_head_commit(
                self.project_dir, self._git_backend
            )
        return self._cached_commit_hash

    def _get_git_metadata(
        self, file_path: Path, content: Optional[bytes] = None
    ) -> Dict[str, Any]:
        """
        Get git-specific metadata for a file.

        No subprocess runs here. The blob hash is the only genuinely per-file
        value, and it is computed in-process from the file's bytes (see
        ``utils/blob_hash``). ``branch`` and ``commit_hash`` are invariant within
        an indexing run and come from the memoized helpers ``_get_cached_branch``
        and ``_get_cached_commit_hash``.

        Args:
            file_path: Path to the file

        Returns:
            Dictionary containing git metadata (git_hash, branch, commit_hash)
        """
        git_metadata: Dict[str, Optional[str]] = {
            "git_hash": None,
            "branch": self._get_cached_branch(),
            "commit_hash": self._get_cached_commit_hash(),
        }

        # The blob hash is a pure function of the file's bytes, so it is computed
        # in-process rather than by forking git once per file. Returns None (hash
        # left unset, caller falls back to file_hash) when the file cannot be
        # read -- the same outcome the old non-zero git exit produced. See
        # utils/blob_hash.py for the equivalence argument and its one limit.
        # Order matters, cheapest first: bytes already in hand, then the
        # manifest (no I/O at all), then finally reading the file.
        #
        # A manifest MISS is not an error and must not be treated as "no hash" --
        # the file may have been added after the pass began, or be a symlink the
        # manifest reports but never follows. Falling through to the read keeps
        # the answer correct; trusting a None here would change the point UUID of
        # every such chunk, silently.
        if content is not None:
            git_metadata["git_hash"] = git_blob_hash_bytes(content)
        else:
            from_manifest = (
                self._manifest.blob_sha(file_path)
                if self._manifest is not None
                else None
            )
            git_metadata["git_hash"] = (
                from_manifest if from_manifest is not None else git_blob_hash(file_path)
            )

        return git_metadata

    def _get_filesystem_metadata(self, file_path: Path) -> Dict[str, Any]:
        """
        Get filesystem-based metadata when git is not available.

        Args:
            file_path: Path to the file

        Returns:
            Dictionary containing filesystem metadata
        """
        try:
            stat = file_path.stat()
            return {"file_mtime": int(stat.st_mtime), "file_size": stat.st_size}
        except (OSError, IOError):
            return {"file_mtime": 0, "file_size": 0}

    def get_file_signature(self, metadata: Dict[str, Any]) -> str:
        """
        Get a unique signature for a file based on its content.

        Args:
            metadata: File metadata dictionary

        Returns:
            Unique signature string for the file content
        """
        if metadata.get("git_available") and metadata.get("git_hash"):
            return str(metadata["git_hash"])
        else:
            return str(metadata["file_hash"])

    def create_point_id(self, metadata: Dict[str, Any], chunk_index: int = 0) -> str:
        """
        Create a unique point ID for vector storage.

        Args:
            metadata: File metadata dictionary
            chunk_index: Index of the chunk within the file

        Returns:
            Unique point ID string
        """
        signature = self.get_file_signature(metadata)
        project_id = metadata["project_id"]

        return f"{project_id}:{signature}:{chunk_index}"
