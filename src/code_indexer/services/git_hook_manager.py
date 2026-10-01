"""
Git hook manager for branch change detection.

This module manages git hooks to detect branch changes during indexing operations.
When a branch switch occurs, it updates the progressive metadata file to ensure
subsequent file indexing uses the correct branch information.
"""

from pathlib import Path
from typing import Optional


class GitHookManager:
    """Manages git hooks for branch change detection."""

    # Bug #1514: bash env-var name used by the generated hook to resolve the
    # repo root fresh on EVERY invocation (via `git rev-parse
    # --show-toplevel`), instead of embedding an install-time absolute path.
    # This project creates repository copies via full-tree copy operations
    # (CoW reflink `cp --reflink=auto -a` for golden-repo base clones and
    # activated-repo clones, or a raw filesystem copy used to seed a golden
    # repo) rather than `git clone` -- `git clone` would create a FRESH
    # `.git/hooks` directory, but a raw filesystem copy carries
    # `.git/hooks/post-checkout` over byte-for-byte, stale absolute path
    # baked in and all. The marker's presence in an existing hook file's
    # content also serves as the "already on the dynamic-path
    # implementation" signal ensure_hook_installed() checks to decide
    # whether an old-style (install-time-absolute-path) hook needs to be
    # self-healed.
    _DYNAMIC_PATH_MARKER = "CIDX_HOOK_REPO_ROOT"

    def __init__(self, repo_path: Path, metadata_file: Optional[Path] = None):
        """
        Initialize git hook manager.

        Args:
            repo_path: Path to the git repository
            metadata_file: Path to the progressive metadata file to update
        """
        self.repo_path = Path(repo_path)
        self.metadata_file = metadata_file
        self.hooks_dir = self.repo_path / ".git" / "hooks"

    def is_git_repository(self) -> bool:
        """Check if the path is a git repository."""
        return (self.repo_path / ".git").exists()

    def _relative_metadata_path(self) -> str:
        """Return metadata_file's path relative to repo_path (POSIX form).

        Bug #1514: the hook must never bake an absolute, install-time path
        -- only the RELATIVE suffix (e.g. ".code-indexer/metadata.json") is
        safe to embed; the repo root itself is re-resolved at hook-run
        time via `git rev-parse --show-toplevel`.

        Falls back to the absolute path string if metadata_file is not
        located under repo_path -- defensive only, since every current
        caller passes a metadata file under codebase_dir/.code-indexer/.
        """
        assert self.metadata_file is not None
        try:
            return (
                Path(self.metadata_file)
                .resolve()
                .relative_to(self.repo_path.resolve())
                .as_posix()
            )
        except ValueError:
            return str(self.metadata_file)

    def install_branch_change_hook(self) -> None:
        """Install post-checkout hook to detect branch changes."""
        if not self.is_git_repository():
            raise ValueError(f"Not a git repository: {self.repo_path}")

        if not self.metadata_file:
            raise ValueError("Metadata file path is required for hook installation")

        hook_file = self.hooks_dir / "post-checkout"

        # Ensure hooks directory exists
        self.hooks_dir.mkdir(parents=True, exist_ok=True)

        # Generate hook content
        hook_content = self._generate_hook_content()

        if hook_file.exists():
            # Preserve existing hook and append our code
            existing_content = hook_file.read_text()
            if "# Code Indexer Branch Tracking" not in existing_content:
                # Add our hook to existing content
                new_content = existing_content.rstrip() + "\n\n" + hook_content
                hook_file.write_text(new_content)
        else:
            # Create new hook file
            hook_file.write_text(f"#!/bin/bash\n\n{hook_content}")

        # Make executable
        hook_file.chmod(0o755)

    def _generate_hook_content(self) -> str:
        """Generate the hook script content.

        Bug #1514: the metadata file's directory is resolved DYNAMICALLY
        every time the hook runs (via `git rev-parse --show-toplevel`
        joined with the relative suffix computed at install time), never
        embedded as a fixed absolute path. This makes the hook
        self-correcting across any full-tree copy of the repository --
        the exact operation this project performs for golden-repo base
        clones, activated-repo CoW clones, and versioned snapshots.
        """
        marker = self._DYNAMIC_PATH_MARKER
        relative_metadata = self._relative_metadata_path()
        python_script = f"""
# Code Indexer Branch Tracking ({marker})
# This section updates the progressive metadata file when branch changes occur.
# Bug #1514: the metadata path is resolved from the repo's CURRENT location
# at hook-run time -- never baked in as an install-time absolute path -- so
# this hook file keeps working correctly after being copied wholesale
# (CoW reflink clone, rsync, raw filesystem copy) to a different directory.

# Check if this is a branch switch (not file checkout)
if [ "$3" = "1" ]; then
    # Get current branch name
    CURRENT_BRANCH=$(git symbolic-ref --short HEAD 2>/dev/null || echo "unknown")
    # Resolve the repo root fresh on every invocation.
    {marker}=$(git rev-parse --show-toplevel 2>/dev/null)

    if [ -n "${marker}" ]; then
        {marker}="${marker}" python3 -c "
import sys
import json
import errno
import fcntl
import os
from pathlib import Path

metadata_file = Path(os.environ['{marker}']) / '{relative_metadata}'
if metadata_file.exists():
    try:
        with open(metadata_file, 'r+') as f:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX)
            except OSError as e:
                if e.errno == errno.EBADF:
                    fcntl.lockf(f.fileno(), fcntl.LOCK_EX)
                else:
                    raise
            f.seek(0)
            try:
                data = json.load(f)
                data['current_branch'] = '$CURRENT_BRANCH'
                f.seek(0)
                f.truncate()
                json.dump(data, f, indent=2)
            except (json.JSONDecodeError, KeyError):
                # If corrupted or missing structure, skip update
                pass
    except (OSError, IOError):
        # File access issues, skip update
        pass
"
    fi
fi
"""
        return python_script

    def ensure_hook_installed(self) -> None:
        """Ensure the branch change hook is installed, installing if missing.

        Bug #1514 self-heal: a hook file that already contains the
        "# Code Indexer Branch Tracking" marker but NOT the dynamic-path
        marker was inherited from a full-tree copy of a DIFFERENT
        directory (an old-style hook bakes an install-time absolute path
        that stays wrong for this copy's own location forever). Such a
        hook is removed and reinstalled so it starts resolving its own
        current location at run time, instead of being left in place
        just because *a* Code Indexer hook marker is present.
        """
        if not self.is_git_repository():
            return  # Not a git repo, nothing to do

        hook_file = self.hooks_dir / "post-checkout"

        if not hook_file.exists():
            self.install_branch_change_hook()
            return

        content = hook_file.read_text()

        if "# Code Indexer Branch Tracking" not in content:
            self.install_branch_change_hook()
            return

        if self._DYNAMIC_PATH_MARKER not in content:
            # Old-style hook (install-time absolute path baked in),
            # inherited verbatim from a copy of a different directory --
            # repair it in place.
            self.remove_hook()
            self.install_branch_change_hook()

    def remove_hook(self) -> None:
        """Remove our branch tracking hook from post-checkout."""
        if not self.is_git_repository():
            return

        hook_file = self.hooks_dir / "post-checkout"
        if not hook_file.exists():
            return

        content = hook_file.read_text()

        # Remove our section
        lines = content.split("\n")
        filtered_lines = []
        skip_section = False

        for line in lines:
            if "# Code Indexer Branch Tracking" in line:
                skip_section = True
                continue
            elif (
                skip_section
                and line.strip() == ""
                and not line.startswith(" ")
                and not line.startswith("\t")
            ):
                skip_section = False
                continue
            elif not skip_section:
                filtered_lines.append(line)

        new_content = "\n".join(filtered_lines).strip()

        if new_content == "#!/bin/bash" or not new_content:
            # If only shebang left or empty, remove the file
            hook_file.unlink()
        else:
            hook_file.write_text(new_content + "\n")
