"""File discovery and filtering for indexing."""

import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterator, List, Optional, Sequence
import pathspec

from ..config import OVERRIDE_FILENAME, Config, override_config_from_bytes
from .fetch_planner import plan_fetch_batches
from .file_facts import ContentUnavailableError
from ..services.override_filter_service import OverrideFilterService
<<<<<<< HEAD
from ..utils.path_confinement import is_resolved_within_root

if TYPE_CHECKING:
    from .repo_manifest import RepoManifest

#: Directories whose own .gitignore is never read (local walk and manifest alike).
logger = logging.getLogger(__name__)

_GITIGNORE_SKIP_DIRS = frozenset({".git", "__pycache__", ".mypy_cache", "node_modules"})
=======
from ..utils.path_confinement import is_indexable_location
from ..utils.source_text_decoding import decode_source_bytes
>>>>>>> upstream/master


class FileFinder:
    """Finds and filters files for indexing based on configuration."""

    def __init__(self, config: Config):
        self.config = config
        # Resolved ONCE per FileFinder instance (i.e. once per indexing
        # run), never per candidate file -- every containment check below
        # reuses this same value.
        self.resolved_codebase_dir = Path(self.config.codebase_dir).resolve()
<<<<<<< HEAD
        # Set for the duration of a delegated pass (see set_manifest). While set,
        # discovery reads the manifest and never the local disk.
        self._manifest: Optional["RepoManifest"] = None
=======
>>>>>>> upstream/master
        self._create_gitignore_spec()
        # Kept so ending a delegated pass restores it without re-reading a disk
        # that, on a delegated pod, may not hold the repository.
        self._local_exclude_spec = self.exclude_spec
        # The override ConfigManager.load found on this machine, and the
        # patterns built from it -- restored when a delegated pass ends (a pass
        # applies the repository's own override, fetched through its backend).
        self._local_override_config = getattr(config, "override_config", None)
        self._local_base_patterns = self._base_patterns

        self._install_override_filters()

    def _install_override_filters(self) -> None:
        """Build the override filter and force-include spec from the config's
        current ``override_config`` (none when it is absent or a test Mock)."""
        config = self.config
        self.override_filter_service = None
        self._force_include_spec = None
        # Only initialize if we have a real OverrideConfig object, not a Mock
        if (
            hasattr(config, "override_config")
            and config.override_config is not None
            and not str(type(config.override_config)).startswith(
                "<class 'unittest.mock"
            )
        ):
            try:
                self.override_filter_service = OverrideFilterService(
                    config.override_config
                )
                # Store force_include_spec for directory walk optimization
                if config.override_config.force_include_patterns:
                    self._force_include_spec = pathspec.PathSpec.from_lines(
                        "gitwildmatch", config.override_config.force_include_patterns
                    )
            except (TypeError, AttributeError):
                # If initialization fails (e.g., due to mock objects), skip override filtering
                pass

    def _all_exclude_dirs(self) -> List[str]:
        """Base + override exclude directories, as discovery applies them."""
        all_exclude_dirs = list(self.config.exclude_dirs)
        if (
            hasattr(self.config, "override_config")
            and self.config.override_config is not None
            and not str(type(self.config.override_config)).startswith(
                "<class 'unittest.mock"
            )
        ):
            try:
                # Defensive: Ensure add_exclude_dirs exists and is iterable
                if hasattr(self.config.override_config, "add_exclude_dirs"):
                    override_dirs = self.config.override_config.add_exclude_dirs
                    if override_dirs and isinstance(override_dirs, (list, tuple)):
                        all_exclude_dirs.extend(override_dirs)
            except (TypeError, AttributeError):
                # Skip override dirs if initialization fails (e.g., mock objects)
                pass
        return all_exclude_dirs

    def manifest_prune_dirs(self) -> List[str]:
        """Directory names a delegated manifest may skip without reading them.

        A manifest hashes every file it walks, and a golden repo's own
        ``.code-indexer`` and ``node_modules`` are often most of the tree (one
        manifest of agent-orchestrator hashed 3.26 GB for 512 tracked files).
        The backend prunes by NAME at any depth, so a name qualifies only when
        THIS finder would prune it wherever it appears -- asked of
        ``_should_prune_dir`` itself at three depths, never re-derived:

        * a literal, single-component exclude dir (no ``/``, no glob);
        * and no force-include patterns at all: one can rescue a directory by
          its ancestor's name (``_should_prune_dir``), which a name-only prune
          cannot express. Those repos keep today's full walk.

        Pruning only what discovery would discard anyway is what keeps the file
        set -- and so every point id -- identical to an unpruned run.
        """
        if self._force_include_spec is not None:
            return []
        names: List[str] = []
        for name in self._all_exclude_dirs():
            if (
                not name
                or "/" in name
                or any(c in name for c in "*?[]!")
                or name in names
            ):
                continue
            if all(
                self._should_prune_dir(at) for at in (name, f"a/{name}", f"a/b/{name}")
            ):
                names.append(name)
        return names

    def _create_gitignore_spec(self) -> None:
        """Create pathspec for excluded directories."""
        patterns = self._exclude_dir_and_common_patterns()
        self._base_patterns = list(patterns)

        # Check for .gitignore files (root and nested)
        self._add_gitignore_patterns(self.config.codebase_dir, patterns)

        self.exclude_spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def _exclude_dir_and_common_patterns(self) -> List[str]:
        """The exclude patterns that come from configuration, not .gitignore."""
        patterns = []

        # Add patterns for all excluded directories
        for exclude_dir in self._all_exclude_dirs():
            # Use /** suffix to exclude all contents under the directory
            # This works for both single-level ("temp") and multi-level ("help/Source") paths
            patterns.append(f"{exclude_dir}/**")  # Matches from root: help/Source/**
            patterns.append(
                f"**/{exclude_dir}/**"
            )  # Matches nested: any/path/help/Source/**

        # Add common patterns
        patterns.extend(
            [
                # Python bytecode and cache
                "*.pyc",
                "*.pyo",
                "*.pyd",
                "__pycache__/",
                ".mypy_cache/",
                ".pytest_cache/",
                ".coverage",
                ".tox/",
                ".nox/",
                # Compiled binaries
                "*.so",
                "*.dylib",
                "*.dll",
                # OS artifacts
                ".DS_Store",
                "Thumbs.db",
                # Temporary files
                "*.tmp",
                "*.temp",
                "*.swp",
                "*.swo",
                "*~",
                # Common build/dist directories
                "node_modules/",
                "build/",
                "dist/",
                "target/",
                ".git/",
                # Code indexer configuration files
                OVERRIDE_FILENAME,
            ]
        )
        return patterns

    def set_manifest(
        self, manifest: Optional["RepoManifest"], backend: Optional[Any] = None
    ) -> None:
        """Discover from ``manifest`` (a delegated pass) or, with None, the disk.

        While set, ``find_files`` enumerates the manifest, sizes and mtimes come
        from it, and the .gitignore files are fetched through ``backend`` -- the
        same root and first-level files the local walk reads. Clearing it
        restores the spec built from the disk at construction, without reading
        the disk again.
        """
        self._manifest = manifest
        self._fetched_negations: List[str] = []
        if manifest is None:
            self._apply_override(self._local_override_config)
            self.exclude_spec = self._local_exclude_spec
            return
        self._apply_override(self._fetched_override_config(manifest, backend))
        patterns = list(self._base_patterns)
        patterns.extend(self._fetched_gitignore_patterns(manifest, backend))
        self.exclude_spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)

    def _apply_override(self, override_config: Any) -> None:
        """Make ``override_config`` the one discovery applies.

        A no-op when it already is -- every local pass, and a delegated pass of a
        repository without its own override file -- so their filters and
        patterns are exactly the ones built at construction.
        """
        if override_config is getattr(self.config, "override_config", None):
            return
        self.config.override_config = override_config
        self._install_override_filters()
        if override_config is self._local_override_config:
            self._base_patterns = self._local_base_patterns
        else:
            self._base_patterns = self._exclude_dir_and_common_patterns()

    def _fetched_override_config(
        self, manifest: "RepoManifest", backend: Optional[Any]
    ) -> Any:
        """The repository's own override file, read through ``backend``.

        ``ConfigManager.load`` looks for it on this machine's disk, which on a
        pod without the repository never holds it -- the index would silently
        ignore the repository's override. So a delegated pass asks the manifest
        and fetches it. Not listed: the locally loaded override stands (on such a
        pod, one found in a parent directory -- the local walk's next stop).
        Unparseable: logged and ignored, as ``ConfigManager.load`` does -- the
        repository-root file shadows any parent one there too. A backend that
        cannot supply a listed file raises: never read as "no override".
        """
        if backend is None or OVERRIDE_FILENAME not in manifest:
            return self._local_override_config
        origin = str(Path(self.config.codebase_dir) / OVERRIDE_FILENAME)
        record = next(
            (
                r
                for r in backend.fetch(self.config.codebase_dir, [OVERRIDE_FILENAME])
                if r.path == OVERRIDE_FILENAME
            ),
            None,
        )
        if record is None or record.error is not None or record.data is None:
            reason = "not returned" if record is None else record.error
            raise ContentUnavailableError(f"{origin}: {reason}")
        try:
            return override_config_from_bytes(record.data, origin)
        except Exception as e:
            logger.warning(f"Failed to load override config from {origin}: {e}")
            return None

    def _fetched_gitignore_patterns(
        self, manifest: "RepoManifest", backend: Optional[Any]
    ) -> List[str]:
        """The local walk's .gitignore rules, fetched in as few calls as possible.

        One call in practice; the paths go through the same planner as every
        other fetch, so a repository with thousands of top-level directories
        is split into requests the server accepts instead of one it refuses.

        Root first, then first-level directories in sorted order (the local walk
        uses iterdir order; the nested patterns are prefixed with their own
        directory, so the order between directories does not change a match).
        An unreadable .gitignore is skipped, as the local reader skips one.
        """
        listed = set(manifest.paths())
        wanted = [".gitignore"] if ".gitignore" in listed else []
        top_dirs = sorted(
            {
                rel.split("/", 1)[0]
                for rel in listed
                if "/" in rel and rel.split("/", 1)[0] not in _GITIGNORE_SKIP_DIRS
            }
        )
        wanted += [f"{d}/.gitignore" for d in top_dirs if f"{d}/.gitignore" in listed]
        if not wanted or backend is None:
            return []
        patterns: List[str] = []
        for batch in plan_fetch_batches(wanted, manifest.size):
            for record in backend.fetch(self.config.codebase_dir, batch):
                if record.error is not None:
                    continue
                relative_dir = (
                    record.path.rsplit("/", 1)[0] if "/" in record.path else None
                )
                text = record.data.decode("utf-8", errors="ignore")
                self._fetched_negations.extend(_negations(text))
                patterns.extend(_gitignore_lines(text, relative_dir))
        return patterns

    def negation_could_rescue(self, pruned: Sequence[str], backend: Any) -> bool:
        """Whether a .gitignore negation could re-include a path under ``pruned``.

        A negation (``!dist/``, ``!keep.py``) is the only way a .gitignore
        rescues a path. The root and first-level files are the ones the
        attached manifest just fetched; a pruned top-level directory's own
        .gitignore -- which a pruned manifest no longer lists -- is fetched
        directly. Call after ``set_manifest``.
        """
        return bool(pruned) and (
            any(_could_rescue(n, pruned) for n in self._fetched_negations)
            or self._pruned_gitignores_negate(pruned, backend)
        )

    def _pruned_gitignores_negate(self, prune: Sequence[str], backend: Any) -> bool:
        """Whether a pruned top-level directory's own .gitignore has a negation.

        The local walk reads first-level .gitignore files (except the skip set),
        so ``<pruned>/.gitignore`` is one the unpruned run would have read.
        """
        wanted = [
            f"{name}/.gitignore" for name in prune if name not in _GITIGNORE_SKIP_DIRS
        ]
        if not wanted:
            return False
        for record in backend.fetch(self.config.codebase_dir, wanted):
            # Inside a pruned directory, ANY negation rescues something there.
            if record.error is None and _negations(
                record.data.decode("utf-8", errors="ignore")
            ):
                return True
        return False

    def _add_gitignore_patterns(self, directory: Path, patterns: list) -> None:
        """Add patterns from .gitignore files recursively."""
        gitignore_path = directory / ".gitignore"
        # A .gitignore is read only from a location the indexer may read: never
        # one resolving into the repository's .git (nor, in server context,
        # one resolving outside the codebase root).
        if gitignore_path.exists() and is_indexable_location(
            gitignore_path,
            self.resolved_codebase_dir,
            self.config.confined_to_codebase_root,
        ):
            try:
                with open(gitignore_path, "r", encoding="utf-8", errors="ignore") as f:
                    text = f.read()
                relative_dir = (
                    None
                    if directory == self.config.codebase_dir
                    else directory.relative_to(self.config.codebase_dir).as_posix()
                )
                patterns.extend(_gitignore_lines(text, relative_dir))
            except (OSError, UnicodeDecodeError):
                pass  # Skip files that can't be read

        # Recursively check subdirectories for .gitignore files
        # But only go one level deep to avoid performance issues
        try:
            for subdir in directory.iterdir():
                if (
                    subdir.is_dir()
                    and subdir.name not in _GITIGNORE_SKIP_DIRS
                    and directory == self.config.codebase_dir
                ):  # Only check immediate subdirectories
                    self._add_gitignore_patterns(subdir, patterns)
        except (OSError, PermissionError):
            pass

    def _is_text_file(self, file_path: Path) -> bool:
        """Check if a file is likely a text file."""
        try:
            # Check extension first
            if file_path.suffix.lstrip(".") in self.config.file_extensions:
                return True

            # For files without extension or unknown extensions,
            # try to read a small portion to check if it's text
            with open(file_path, "rb") as f:
                chunk = f.read(1024)
                if not chunk:
                    return False

                # Check for null bytes (common in binary files)
                if b"\x00" in chunk:
                    return False

                # Same encoding fallback indexing and retrieval use (Bug #1991)
                try:
                    decode_source_bytes(chunk)
                    return True
                except ValueError:
                    return False

        except (OSError, IOError):
            return False

    def is_eligible(self, file_path: Path) -> bool:
        """Public wrapper for the eligibility decision a fresh
        ``find_files()`` walk applies to a single file (size, extension,
        exclude patterns, text-file check, override filters). No behaviour
        change from ``_should_include_file()`` -- exists so callers outside
        this class (e.g. SmartIndexer's resume-path safety check) do not
        need to reach into a private method."""
        return self._should_include_file(file_path)

<<<<<<< HEAD
    def file_size(self, file_path: Path) -> Optional[int]:
        """The size discovery judges ``file_path`` by, or None when unknown.

        The manifest's under a delegated pass (nothing touches the disk), a
        ``stat()`` otherwise.
        """
        if self._manifest is not None:
            return self._manifest.size(file_path)
        try:
            return file_path.stat().st_size
        except OSError:
            return None

    def _over_size_limit(self, size: Optional[int]) -> bool:
        return size is not None and size > self.config.indexing.max_file_size

    def exceeds_max_file_size(self, file_path: Path) -> bool:
        """True when ``file_path``'s size is known and over ``max_file_size``.

        The walk's own size gate, for callers that select files without the
        walk (the git-delta incremental path). An unknown size answers False:
        such a caller decides what an unreadable file means, not this gate.
        """
        return self._over_size_limit(self.file_size(file_path))

=======
>>>>>>> upstream/master
    def _should_include_file(self, file_path: Path) -> bool:
        """Check if a file should be included in indexing."""
        try:
            # Cheap, string-only filtering (extension + exclude patterns;
            # no syscalls) runs FIRST. When no override filter is
            # configured -- the common case -- a name-excluded file is
            # excluded outright, skipping the containment check and the
            # size stat() below entirely. This short-circuit is safe only
            # in the override's absence: override_filter_service.
            # force_include_patterns can otherwise rescue a name-excluded
            # file, so that path still needs both checks to run.
            base_result = self._get_base_filtering_result(file_path)
            if not self.override_filter_service and not base_result:
<<<<<<< HEAD
                return False

            # Server context only (config.confined_to_codebase_root): reject
            # any candidate whose resolved location (following symlinks,
            # collapsing '..') is not strictly inside the resolved codebase
            # root -- a symlink in-tree can have an eligible name/extension
            # while its real target lies outside the codebase directory
            # entirely. Local CLI indexing follows such symlinks.
            #
            # os.walk() is called with followlinks=False, which means it
            # never descends into a symlinked directory -- every
            # directory in file_path's chain (as actually walked) is
            # therefore a real, non-symlink directory, and file_path can
            # only escape the codebase root through its OWN final
            # component being a symlink. is_symlink() is a single lstat
            # on that final component; the full resolve()+containment
            # check (several stats, one per path component) only runs
            # when it fires, instead of unconditionally on every file.
            if self._manifest is not None:
                # Delegated: the manifest answers, and nothing touches the disk.
                # A symlink is never followed (its target may be outside the
                # root, and fetch refuses it); an in-root target is listed, and
                # indexed, as its own entry. Absent means it was not there when
                # the pass began -- not eligible, never a stat.
                if self._manifest.is_symlink(file_path) is not False:
                    return False
            elif (
                self.config.confined_to_codebase_root
                and file_path.is_symlink()
                and not is_resolved_within_root(file_path, self.resolved_codebase_dir)
            ):
                return False

            # Check file size: unknown (absent from the manifest, or no stat)
            # is not eligible.
            size = self.file_size(file_path)
            if size is None or self._over_size_limit(size):
                return False

=======
                return False

            # Server context (config.confined_to_codebase_root): reject any
            # candidate whose resolved location (following symlinks,
            # collapsing '..') is not strictly inside the resolved codebase
            # root -- a symlink in-tree can have an eligible name/extension
            # while its real target lies outside the codebase directory
            # entirely. Local CLI indexing follows such symlinks. In EVERY
            # context a symlink resolving into the repository's own .git
            # (e.g. ``link.py -> .git/config``) is refused.
            #
            # os.walk() is called with followlinks=False, which means it
            # never descends into a symlinked directory -- every
            # directory in file_path's chain (as actually walked) is
            # therefore a real, non-symlink directory, and file_path can
            # only escape the codebase root through its OWN final
            # component being a symlink. is_symlink() is a single lstat
            # on that final component; the full resolve()+location
            # check (several stats, one per path component) only runs
            # when it fires, instead of unconditionally on every file.
            if file_path.is_symlink() and not is_indexable_location(
                file_path,
                self.resolved_codebase_dir,
                self.config.confined_to_codebase_root,
            ):
                return False

            # Check file size
            if file_path.stat().st_size > self.config.indexing.max_file_size:
                return False

>>>>>>> upstream/master
            # Apply override filtering if available
            if self.override_filter_service:
                relative_path = file_path.relative_to(self.config.codebase_dir)
                return self.override_filter_service.should_include_file(
                    relative_path, base_result
                )

            return base_result

        except (OSError, ValueError):
            return False

    def matches_exclude_pattern(self, relative_path: str) -> bool:
        """Return True if relative_path matches the canonical exclude_spec
        pathspec (Bug #1467).

        Pure string-pattern matching -- deliberately independent of whether
        the file still exists on disk, so callers classifying DELETED files
        (e.g. git-diff-based incremental discovery, which must still detect
        a removed file even though it can no longer be stat()'d) can safely
        reuse this SAME canonical exclusion source find_files() applies,
        rather than reimplementing a divergent, incomplete subset of it.
        """
        return bool(self.exclude_spec.match_file(relative_path))

    def _get_base_filtering_result(self, file_path: Path) -> bool:
        """Get base filtering result from config and gitignore rules."""
        try:
            # Check if file extension is in allowed list
            extension = file_path.suffix.lstrip(".")
            if extension not in self.config.file_extensions:
                return False

            # Check against exclude patterns
            relative_path = file_path.relative_to(self.config.codebase_dir)
            if self.exclude_spec.match_file(str(relative_path)):
                return False

            # Check if it's a text file
            return self._is_text_file(file_path)

        except (OSError, ValueError):
            return False

    def _should_prune_dir(self, dir_str: str) -> bool:
        """Whether discovery skips everything under this repo-relative directory.

        One decision for both discovery modes: the local walk asks it as it
        descends, the manifest asks it of every ancestor of a path.
        """
        # Check if directory matches exclude patterns
        if not self.exclude_spec.match_file(dir_str + "/"):
            return False
        # Before pruning, check if force_include_patterns might match files in this dir
        if self._force_include_spec:
            # Check if any force_include pattern could match files under this directory
            # by testing if pattern would match a hypothetical file in this directory
            test_path = dir_str + "/test_file.txt"
            if self._force_include_spec.match_file(test_path):
                return False
            # Also check glob patterns like "projects/**/*.jsonl"
            for pattern in (
                self.config.override_config.force_include_patterns
                if hasattr(self.config, "override_config")
                and self.config.override_config
                and self.config.override_config.force_include_patterns
                else []
            ):
                if pattern.startswith(dir_str) or dir_str.startswith(
                    pattern.split("/")[0]
                ):
                    return False
        return True

    def _find_files_in_manifest(self, manifest: "RepoManifest") -> Iterator[Path]:
        """The walk's answer, from the manifest: same pruning, same filters.

        A path is skipped when ANY ancestor directory would have been pruned --
        the walk never descends into a pruned directory, so nothing under it is
        ever offered. Symlinks are never yielded (see _should_include_file),
        which also covers the walk's dangling-link skip.
        """
        root = Path(self.config.codebase_dir)
        pruned: Dict[str, bool] = {}
        for rel in manifest.paths():
            parts = rel.split("/")
            skip = False
            for depth in range(1, len(parts)):
                ancestor = "/".join(parts[:depth])
                if ancestor not in pruned:
                    pruned[ancestor] = self._should_prune_dir(ancestor)
                if pruned[ancestor]:
                    skip = True
                    break
            if skip:
                continue
            file_path = root / rel
            if self._should_include_file(file_path):
                yield file_path

    def find_files(self) -> Iterator[Path]:
        """Find all files that should be indexed."""
        # Debug logging
        import datetime

        debug_file = os.path.expanduser("~/.tmp/cidx_debug.log")
        os.makedirs(os.path.dirname(debug_file), exist_ok=True)

        with open(debug_file, "a") as f:
            f.write(
                f"[{datetime.datetime.now().isoformat()}] FileFinder.find_files started for {self.config.codebase_dir}\n"
            )
            f.flush()

        if self._manifest is not None:
            yield from self._find_files_in_manifest(self._manifest)
            return

        if not self.config.codebase_dir.exists():
            raise ValueError(
                f"Codebase directory does not exist: {self.config.codebase_dir}"
            )

        if not self.config.codebase_dir.is_dir():
            raise ValueError(
                f"Codebase path is not a directory: {self.config.codebase_dir}"
            )

        for root, dirs, files in os.walk(self.config.codebase_dir):
            root_path = Path(root)

            # Filter directories to avoid walking into excluded ones
            # But don't prune directories that might contain force-included files
            dirs_to_remove = []
            for dir_name in dirs:
                dir_path = root_path / dir_name
                relative_dir = dir_path.relative_to(self.config.codebase_dir)
                dir_str = str(relative_dir)

                if self._should_prune_dir(dir_str):
                    dirs_to_remove.append(dir_name)

            for dir_name in dirs_to_remove:
                dirs.remove(dir_name)

            # Process files in current directory
            for file_name in files:
                file_path = root_path / file_name

<<<<<<< HEAD
                # Skip dangling symlinks — e.g. Node cpSync converts
                # relative symlinks to absolute ones pointing at a now-
                # deleted temp clone dir. stat()/read() on these raises
                # FileNotFoundError which crashed the entire indexing job.
                if file_path.is_symlink() and not file_path.exists():
                    continue

=======
>>>>>>> upstream/master
                # Debug: Log file being checked. Wrapped in try/except
                # OSError so a symlink loop or broken link does not crash
                # the walk before containment filtering gets to run.
                try:
                    file_is_large_or_named = (
                        file_name == "ruff_output.json"
                        or file_path.stat().st_size > 500000
                    )
                except OSError:
                    file_is_large_or_named = False
                if file_is_large_or_named:
                    with open(debug_file, "a") as f:
                        f.write(
                            f"[{datetime.datetime.now().isoformat()}] Checking large file: {file_path} ({file_path.stat().st_size} bytes)\n"
                        )
                        f.flush()

                if self._should_include_file(file_path):
                    yield file_path

    def find_modified_files(self, since_timestamp: float) -> Iterator[Path]:
        """Find files modified since the given timestamp."""
        for file_path in self.find_files():
            if self._manifest is not None:
                # Unknown mtime: skipped, as the walk skips a file it cannot stat.
                mtime = self._manifest.mtime(file_path)
                if mtime is not None and mtime > since_timestamp:
                    yield file_path
                continue
            try:
                if file_path.stat().st_mtime > since_timestamp:
                    yield file_path
            except OSError:
                continue

    def find_indexed_files(self, vector_store_client) -> set:
        """Get set of all file paths currently in the index."""
        try:
            # Get all points from the collection
            response = vector_store_client.client.post(
                f"/collections/{vector_store_client.config.collection}/points/scroll",
                json={
                    "limit": 10000,  # Large number to get all points
                    "with_payload": True,
                    "with_vector": False,
                },
            )
            response.raise_for_status()

            result = response.json()
            indexed_files = set()

            for point in result.get("result", {}).get("points", []):
                if "path" in point.get("payload", {}):
                    indexed_files.add(point["payload"]["path"])

            return indexed_files
        except Exception:
            return set()

    def find_deleted_files(self, vector_store_client) -> set:
        """Find files that are indexed but no longer exist on filesystem."""
        indexed_files = self.find_indexed_files(vector_store_client)
        current_files = set()

        # Get current files as relative paths
        for file_path in self.find_files():
            try:
                relative_path = str(file_path.relative_to(self.config.codebase_dir))
                current_files.add(relative_path)
            except ValueError:
                continue

        # Return files that are indexed but don't exist anymore
        return indexed_files - current_files

    def get_file_stats(self) -> dict:
        """Get statistics about discoverable files."""
        total_files = 0
        total_size = 0
        extensions: Dict[str, int] = {}

        for file_path in self.find_files():
            total_files += 1
            try:
                size = (
                    self._manifest.size(file_path) or 0
                    if self._manifest is not None
                    else file_path.stat().st_size
                )
                total_size += size

                ext = file_path.suffix.lstrip(".") or "no_extension"
                extensions[ext] = extensions.get(ext, 0) + 1

            except OSError:
                continue

        return {
            "total_files": total_files,
            "total_size": total_size,
            "extensions": extensions,
        }


def attach_delegated_manifest(
    finder: "FileFinder", backend: Any, root: Optional[Path] = None
) -> "RepoManifest":
    """Build a pass's manifest through ``backend`` and attach it to ``finder``.

    Pruned of the finder's ``manifest_prune_dirs()`` -- unless a .gitignore
    negation could re-include something under a pruned directory, in which
    case it is rebuilt unpruned, so the discovered file set is always the
    unpruned one. Negations are rare, so the common case keeps the saving.
    The decision follows what was ACTUALLY pruned: a backend that cannot prune
    is built once.
    """
    from .repo_manifest import RepoManifest

    root = Path(root if root is not None else finder.config.codebase_dir)
    manifest = RepoManifest.build(
        root, backend=backend, prune_dirs=finder.manifest_prune_dirs()
    )
    finder.set_manifest(manifest, backend)
    if manifest.pruned and (
        # The repository's own override (fetched by set_manifest) may forbid
        # pruning a directory the first build skipped (force-include patterns).
        not set(manifest.pruned) <= set(finder.manifest_prune_dirs())
        or finder.negation_could_rescue(manifest.pruned, backend)
    ):
        logger.info(
            "a .gitignore negation or the repository's override could re-include "
            "a pruned directory; rebuilding the manifest unpruned"
        )
        manifest = RepoManifest.build(root, backend=backend)
        finder.set_manifest(manifest, backend)
    return manifest


def _negations(text: str) -> List[str]:
    """A .gitignore's negation patterns (the text after ``!``), as written."""
    return [
        line.strip()[1:]
        for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        if line.strip().startswith("!")
    ]


def _could_rescue(negation: str, pruned: Sequence[str]) -> bool:
    """Whether one negation, read relative to its own .gitignore, could
    re-include a path under a directory named in ``pruned``.

    A single component matches at any depth (``!keep.py``); a component that
    is a pruned name, or a glob, can reach into one. ``!gen/keep.py`` cannot.
    """
    parts = [p for p in negation.strip("/").split("/") if p]
    if len(parts) <= 1:
        return True
    return any(p in pruned or any(c in p for c in "*?[") for p in parts)


def _gitignore_lines(text: str, relative_dir: Optional[str]) -> List[str]:
    """Patterns from one .gitignore's text, as the walk has always read them.

    Universal newlines (the text-mode read the walk used), comments and blanks
    dropped, and a nested file's patterns scoped to its own directory unless
    anchored with a leading "/".
    """
    patterns: List[str] = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if relative_dir is not None and not line.startswith("/"):
            line = f"{relative_dir}/{line}"
        patterns.append(line)
    return patterns
