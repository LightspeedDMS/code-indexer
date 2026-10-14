"""SCIP project auto-discovery module."""

from ..utils.index_store import INDEX_DIRNAME
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Protocol, Set, Tuple
from xml.etree import ElementTree


# EVO-66190: directories that never hold a real project root. Pruned during the single
# discovery walk so a large repo's VCS/index internals aren't traversed (a .git tree can
# itself be tens of thousands of files). Safe: no build file inside these is a project.
DISCOVERY_PRUNE_DIRS: frozenset = frozenset({".git", INDEX_DIRNAME})


# Build file mappings: filename/pattern -> (language, build_system)
# NOTE: C# uses glob patterns (*.sln, *.csproj) because solution/project filenames vary.
# All other languages use exact filenames (pom.xml, package.json, etc.)
BUILD_FILE_MAPPINGS: Dict[str, Tuple[str, str]] = {
    "pom.xml": ("java", "maven"),
    "build.gradle": ("java", "gradle"),
    "build.gradle.kts": ("kotlin", "gradle"),
    "package.json": ("typescript", "npm"),
    "pyproject.toml": ("python", "poetry"),
    "setup.py": ("python", "setuptools"),
    "requirements.txt": ("python", "pip"),
    "*.sln": ("csharp", "solution"),
    "*.csproj": ("csharp", "project"),
    "go.mod": ("go", "module"),
}

# Build file priority: lower number = higher priority
# Used for deduplication when multiple build files exist in same directory
BUILD_FILE_PRIORITY: Dict[str, int] = {
    # EVO-66190: a multi-module build root (Gradle settings file) is emitted as a synthetic
    # project keyed on its settings file. Give it top priority so a co-located non-JVM build
    # file (e.g. a root package.json for tooling) can NOT silently overwrite the root project
    # in the per-directory dedup below (without this it would default to priority 999).
    "settings.gradle": 1,
    "settings.gradle.kts": 1,
    "pyproject.toml": 1,
    "setup.py": 2,
    "requirements.txt": 3,
    "pom.xml": 1,
    "build.gradle": 1,
    "build.gradle.kts": 1,
    "package.json": 1,
    "*.sln": 1,
    "*.csproj": 2,
    "go.mod": 1,
}


# EVO-66190: a Gradle multi-project build is driven by a single settings file at the
# root (it declares the `include`d subprojects). The subprojects' own build.gradle files
# are NOT independently buildable — shared plugins/versions/inter-module deps live at the
# root — so scip-java must run once at the root, not per subproject.
GRADLE_SETTINGS_FILES: Tuple[str, ...] = ("settings.gradle", "settings.gradle.kts")


class BuildFileSource(Protocol):
    """Where discovery asks its questions about a repository's files.

    Discovery needs three things from a repository: the files under it (pruned of
    :data:`DISCOVERY_PRUNE_DIRS`), whether one path exists, and the text of a ``pom.xml``.
    :class:`LocalBuildFileSource` answers them from this machine's disk exactly as
    discovery always has (same calls, same order). A pod without the repository
    passes a source backed by the content seam instead
    (``server/services/scip_repo_reads.py``) -- discovery itself never learns
    which one it has.
    """

    def iter_files(self) -> Iterator[Path]:
        """Every file under the repository root (absolute paths), pruned."""
        ...

    def exists(self, path: Path) -> bool:
        """Whether *path* (under the repository root) exists."""
        ...

    def root_glob(self, pattern: str) -> bool:
        """Whether a file directly in the root matches *pattern* (e.g. ``*.sln``)."""
        ...

    def read_text(self, path: Path) -> Optional[str]:
        """*path*'s text (undecodable bytes ignored), or None if unreadable."""
        ...


class LocalBuildFileSource:
    """The repository on this machine's disk: discovery's behaviour before the seam."""

    def __init__(self, repo_root: Path) -> None:
        self.repo_root = Path(repo_root)

    def iter_files(self) -> Iterator[Path]:
        for dirpath, dirnames, filenames in os.walk(
            self.repo_root, onerror=lambda _e: None
        ):
            # Prune in place so os.walk does not descend into VCS/index internals.
            dirnames[:] = [d for d in dirnames if d not in DISCOVERY_PRUNE_DIRS]
            base = Path(dirpath)
            for filename in filenames:
                yield base / filename

    def exists(self, path: Path) -> bool:
        return path.exists()

    def root_glob(self, pattern: str) -> bool:
        return any(self.repo_root.glob(pattern))

    def read_text(self, path: Path) -> Optional[str]:
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None


def _is_maven_aggregator(pom_path: Path, source: BuildFileSource) -> bool:
    """Return True if a ``pom.xml`` declares a ``<modules>`` reactor.

    An aggregator/parent pom builds its child modules as one reactor, so scip-java must
    run at the aggregator, not per child module. Namespace-agnostic (Maven poms carry an
    xmlns); falls back to a cheap substring check on a malformed/partial pom.
    """
    text = source.read_text(pom_path)
    if text is None:
        return False
    try:
        root = ElementTree.fromstring(text)
    except ElementTree.ParseError:
        return "<modules>" in text or "<modules " in text
    return any(el.tag.rsplit("}", 1)[-1] == "modules" for el in root.iter())


def has_root_build_file(
    repo_dir: Path, source: Optional[BuildFileSource] = None
) -> bool:
    """True if *repo_dir* has a build file at its ROOT directory.

    A root build file means :meth:`ProjectDiscovery.discover` emits a project at
    ``relative_path='.'`` (a Gradle multi-project via ``settings.gradle``, or a Maven/
    single-Gradle/npm/python/go/C# project at the top level), whose delegated build writes
    the TOP-LEVEL ``.code-indexer/scip/index.scip.db``. Cheap: root-level ``exists``/
    ``glob`` checks only, never a tree walk. Shared by the reconcile's presence check and
    the funnel's freshness check so both agree on when a top-level index is required.

    *source* answers the questions (default: this machine's disk).
    """
    files = source if source is not None else LocalBuildFileSource(repo_dir)
    for name in GRADLE_SETTINGS_FILES:
        if files.exists(repo_dir / name):
            return True
    for name in BUILD_FILE_MAPPINGS:
        if "*" in name:  # glob pattern (e.g. *.sln, *.csproj) — root level only
            if files.root_glob(name):
                return True
        elif files.exists(repo_dir / name):
            return True
    return False


@dataclass
class DiscoveredProject:
    """Represents a discovered project with its metadata."""

    relative_path: Path
    language: str
    build_system: str
    build_file: Path


class ProjectDiscovery:
    """Discovers buildable projects in a repository."""

    def __init__(self, repo_root: Path, source: Optional[BuildFileSource] = None):
        """
        Initialize project discovery.

        Args:
            repo_root: Root directory of the repository to scan
            source: Where the repository's files are asked about. Default: this
                machine's disk (:class:`LocalBuildFileSource`).
        """
        self.repo_root = Path(repo_root)
        self._source: BuildFileSource = (
            source if source is not None else LocalBuildFileSource(self.repo_root)
        )

    def _get_build_file_pattern(self, filename: str) -> str:
        """
        Get the BUILD_FILE_MAPPINGS pattern that matches a filename.

        Args:
            filename: Actual filename (e.g., "MyApp.sln", "pom.xml")

        Returns:
            Pattern key from BUILD_FILE_MAPPINGS (e.g., "*.sln", "pom.xml")
        """
        # First try exact match
        if filename in BUILD_FILE_MAPPINGS:
            return filename

        # Try glob pattern match
        for pattern in BUILD_FILE_MAPPINGS.keys():
            if "*" in pattern:
                # Extract extension from pattern (e.g., "*.sln" -> ".sln")
                pattern_ext = pattern.replace("*", "")
                if filename.endswith(pattern_ext):
                    return pattern

        return filename  # Fallback to filename itself

    def _walk_build_files(
        self,
    ) -> Tuple[List[Path], List[Path], Dict[str, List[Path]]]:
        """Collect every recognized build file in a SINGLE traversal of the source.

        EVO-66190: discovery previously ran ~13 independent ``rglob`` passes — Gradle
        settings (×2), ``pom.xml``, and one per :data:`BUILD_FILE_MAPPINGS` pattern — each
        a FULL walk of the repo tree. On a large monolith over NFS that was minutes (a
        ~75k-file repo measured ~80 s *per* pass, ~5.8 min total) and, worse, ``rglob``
        raised ``FileNotFoundError`` when a directory vanished mid-walk on a live golden
        tree. One walk collects them all instead (``LocalBuildFileSource``: one ``os.walk``
        that prunes VCS/index dirs (:data:`DISCOVERY_PRUNE_DIRS`) and swallows per-directory
        errors so a concurrent mutation can't abort the whole discovery; a content-seam
        source: one paged manifest with the same prune list).

        Returns ``(settings_files, pom_files, files_by_pattern)`` where ``files_by_pattern``
        maps each ``BUILD_FILE_MAPPINGS`` key to its matching paths in walk order.
        """
        exact_names = {name for name in BUILD_FILE_MAPPINGS if "*" not in name}
        glob_patterns = [
            (name, name[1:]) for name in BUILD_FILE_MAPPINGS if name.startswith("*")
        ]  # e.g. ("*.sln", ".sln")
        settings_names = frozenset(GRADLE_SETTINGS_FILES)

        settings_files: List[Path] = []
        pom_files: List[Path] = []
        files_by_pattern: Dict[str, List[Path]] = {
            name: [] for name in BUILD_FILE_MAPPINGS
        }

        for path in self._source.iter_files():
            filename = path.name
            if filename in settings_names:
                settings_files.append(path)
            if filename == "pom.xml":
                pom_files.append(path)
            if filename in exact_names:
                files_by_pattern[filename].append(path)
            else:
                for pattern, suffix in glob_patterns:
                    if filename.endswith(suffix):
                        files_by_pattern[pattern].append(path)
                        break
        return settings_files, pom_files, files_by_pattern

    @staticmethod
    def _gradle_roots(settings_files: List[Path]) -> Set[Path]:
        """Directories that own a Gradle settings file (multi-project build roots)."""
        return {settings_file.parent for settings_file in settings_files}

    def _maven_aggregator_roots(self, pom_files: List[Path]) -> Set[Path]:
        """Directories whose pom.xml is a Maven aggregator (declares ``<modules>``)."""
        return {
            pom.parent for pom in pom_files if _is_maven_aggregator(pom, self._source)
        }

    @staticmethod
    def _under_a_root(directory: Path, roots: Set[Path]) -> bool:
        """True if a strict ancestor of *directory* is one of *roots*."""
        return any(ancestor in roots for ancestor in directory.parents)

    def discover(self) -> List[DiscoveredProject]:
        """
        Discover all buildable projects in the repository.

        Scans for known build files (pom.xml, package.json, pyproject.toml, etc.) and
        creates one DiscoveredProject per project.

        EVO-66190: a Gradle multi-project (a ``settings.gradle[.kts]`` root) or a Maven
        aggregator (a pom declaring ``<modules>``) is a SINGLE build — scip-java runs once
        at the root and indexes every subproject into one top-level index. Such a root
        therefore yields exactly one project and its JVM subprojects are suppressed (they
        are not independently buildable). Non-JVM build files (package.json, go.mod, ...)
        inside a JVM monorepo remain their own projects.

        When multiple build files exist in the same directory, only the highest
        priority build file is used (based on BUILD_FILE_PRIORITY).

        Returns:
            List of DiscoveredProject instances
        """
        settings_files, pom_files, files_by_pattern = self._walk_build_files()
        gradle_roots = self._gradle_roots(settings_files)
        maven_roots = self._maven_aggregator_roots(pom_files)
        build_roots = gradle_roots | maven_roots

        seen_dirs: Dict[Path, DiscoveredProject] = {}

        # 1. One project per TOP-LEVEL multi-module build root. A nested root (composite
        #    build / nested aggregator) is built by its outer root, so skip it.
        for root in gradle_roots:
            if self._under_a_root(root, build_roots):
                continue
            settings_file = next(
                (
                    root / name
                    for name in GRADLE_SETTINGS_FILES
                    if self._source.exists(root / name)
                ),
                root / GRADLE_SETTINGS_FILES[0],
            )
            seen_dirs[root] = DiscoveredProject(
                relative_path=root.relative_to(self.repo_root),
                language="java",  # scip-java indexes java + kotlin subprojects
                build_system="gradle",
                build_file=settings_file.relative_to(self.repo_root),
            )
        for root in maven_roots:
            if root in seen_dirs or self._under_a_root(root, build_roots):
                continue
            seen_dirs[root] = DiscoveredProject(
                relative_path=root.relative_to(self.repo_root),
                language="java",
                build_system="maven",
                build_file=(root / "pom.xml").relative_to(self.repo_root),
            )

        # 2. Scan build files; suppress JVM subprojects that belong to a build root above.
        for build_file_name, (language, build_system) in BUILD_FILE_MAPPINGS.items():
            for build_file in files_by_pattern[build_file_name]:
                # Get project directory (parent of build file)
                project_dir = build_file.parent

                # EVO-66190: a gradle/maven build file at-or-under a multi-module root is
                # a subproject of that root's single build — never index it standalone.
                if build_file_name in ("build.gradle", "build.gradle.kts") and (
                    project_dir in gradle_roots
                    or self._under_a_root(project_dir, gradle_roots)
                ):
                    continue
                if build_file_name == "pom.xml" and (
                    project_dir in maven_roots
                    or self._under_a_root(project_dir, maven_roots)
                ):
                    continue

                # Check if we've already seen this directory
                if project_dir in seen_dirs:
                    existing = seen_dirs[project_dir]
                    current_priority = BUILD_FILE_PRIORITY.get(build_file_name, 999)
                    existing_pattern = self._get_build_file_pattern(
                        existing.build_file.name
                    )
                    existing_priority = BUILD_FILE_PRIORITY.get(existing_pattern, 999)
                    # Skip if current build file has lower priority (higher number)
                    if current_priority >= existing_priority:
                        continue

                relative_path = project_dir.relative_to(self.repo_root)
                relative_build_file = build_file.relative_to(self.repo_root)

                project = DiscoveredProject(
                    relative_path=relative_path,
                    language=language,
                    build_system=build_system,
                    build_file=relative_build_file,
                )
                seen_dirs[project_dir] = project

        return list(seen_dirs.values())
