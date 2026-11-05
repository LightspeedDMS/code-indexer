"""Make a source-tree run resolve seams exactly as an install would.

The seam resolver finds providers through installed distribution metadata. A
test run from the source tree (``PYTHONPATH=src``, or an editable install made
before a distribution was added) has no metadata for the in-repo
distributions, so the ``local`` providers they declare would be missing and
every seam would refuse to resolve -- correctly, for a real deployment.

:func:`register_in_tree_entry_points` reads the entry points those
distributions DECLARE in their ``pyproject.toml`` -- the repository root and
every in-repo path dependency it lists (``name @ {root:uri}/<path>``),
recursively -- and adds them to the resolver (``cidx_sdk.seams.add_entry_points``).
The declaration in ``pyproject.toml`` stays the single source of truth: no
provider is named twice, and nothing is registered that ``pip install .``
would not install. A registration replaces an installed entry of the same
name, so a stale installed copy cannot add a second provider.
"""

from __future__ import annotations

import importlib
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, Set

from cidx_sdk.seams.resolver import add_entry_points, remove_entry_points

if sys.version_info >= (3, 11):
    import tomllib as _toml
else:
    import tomli as _toml

#: The entry-point groups the core reads (features and seams).
GROUP_PREFIX = "code_indexer."

# "name @ {root:uri}/packages/x" or "{root:parent:parent:uri}/x"
_PATH_DEPENDENCY = re.compile(r"@\s*\{root((?::parent)*):uri\}/(?P<path>[^\s;,]+)")


@dataclass(frozen=True)
class DeclaredEntryPoint:
    """One entry point as a ``pyproject.toml`` declares it."""

    group: str
    name: str
    value: str
    distribution: str

    def load(self) -> Any:
        """Import ``module:attr`` the way ``importlib.metadata`` would."""
        module_name, _, attr = self.value.partition(":")
        target: Any = importlib.import_module(module_name.strip())
        for part in filter(None, attr.strip().split(".")):
            target = getattr(target, part)
        return target


def _read(pyproject: Path) -> Mapping[str, Any]:
    with pyproject.open("rb") as handle:
        return _toml.load(handle)


def _path_dependencies(project_dir: Path, data: Mapping[str, Any]) -> Iterator[Path]:
    for requirement in data.get("project", {}).get("dependencies", []):
        match = _PATH_DEPENDENCY.search(str(requirement))
        if match is None:
            continue
        base = project_dir
        for _ in range(match.group(1).count(":parent")):
            base = base.parent
        yield (base / match.group("path")).resolve()


def in_tree_distributions(repo_root: Path) -> List[Path]:
    """The project directories ``pip install <repo_root>`` would install from
    this tree: the root and its in-repo path dependencies, recursively."""
    seen: Set[Path] = set()
    order: List[Path] = []
    stack = [Path(repo_root).resolve()]
    while stack:
        project = stack.pop()
        if project in seen or not (project / "pyproject.toml").is_file():
            continue
        seen.add(project)
        order.append(project)
        stack.extend(_path_dependencies(project, _read(project / "pyproject.toml")))
    return order


def declared_entry_points(repo_root: Path) -> Dict[str, List[DeclaredEntryPoint]]:
    """Every ``code_indexer.*`` entry point the in-tree distributions declare."""
    by_group: Dict[str, List[DeclaredEntryPoint]] = {}
    for project in in_tree_distributions(repo_root):
        data = _read(project / "pyproject.toml")
        distribution = str(data.get("project", {}).get("name", project.name))
        groups = data.get("project", {}).get("entry-points", {})
        for group, entries in groups.items():
            if not group.startswith(GROUP_PREFIX):
                continue
            for name, value in entries.items():
                by_group.setdefault(group, []).append(
                    DeclaredEntryPoint(group, name, str(value), distribution)
                )
    return by_group


def register_in_tree_entry_points(repo_root: Path) -> Dict[str, List[str]]:
    """Register the in-tree declarations with the seam resolver.

    Returns group -> registered names, for :func:`unregister`.
    """
    registered: Dict[str, List[str]] = {}
    for group, entries in declared_entry_points(repo_root).items():
        add_entry_points(group, entries)
        registered[group] = [entry.name for entry in entries]
    return registered


def unregister(registered: Mapping[str, List[str]]) -> None:
    """Withdraw what :func:`register_in_tree_entry_points` added."""
    for group, names in registered.items():
        remove_entry_points(group, names)


# ── child processes ──────────────────────────────────────────────────────────


def in_tree_source_dirs(repo_root: Path) -> List[Path]:
    """The import roots of the in-tree distributions (``src`` layouts)."""
    dirs = []
    for project in in_tree_distributions(repo_root):
        source = project / "src"
        dirs.append(source if source.is_dir() else project)
    return dirs


def write_in_tree_metadata(repo_root: Path, dest: Path) -> Path:
    """Write ``*.dist-info`` metadata for the in-tree distributions into *dest*.

    A child process (``python -m code_indexer.cli ...``) does not run this
    test kit, so :func:`register_in_tree_entry_points` cannot reach it. With
    *dest* on its ``PYTHONPATH`` the child's ``importlib.metadata`` finds the
    declared entry points exactly as it would for an install -- and, being
    earlier on the path, in place of a stale installed copy of the same
    distribution.
    """
    dest.mkdir(parents=True, exist_ok=True)
    by_distribution: Dict[str, List[DeclaredEntryPoint]] = {}
    for entries in declared_entry_points(repo_root).values():
        for entry in entries:
            by_distribution.setdefault(entry.distribution, []).append(entry)
    for distribution, entries in by_distribution.items():
        normalized = re.sub(r"[-_.]+", "_", distribution).lower()
        info = dest / f"{normalized}-0.0.0.dist-info"
        info.mkdir(exist_ok=True)
        (info / "METADATA").write_text(
            f"Metadata-Version: 2.1\nName: {distribution}\nVersion: 0.0.0\n",
            encoding="utf-8",
        )
        lines: List[str] = []
        for group in sorted({entry.group for entry in entries}):
            lines.append(f"[{group}]")
            lines.extend(
                f"{entry.name} = {entry.value}"
                for entry in entries
                if entry.group == group
            )
            lines.append("")
        (info / "entry_points.txt").write_text("\n".join(lines), encoding="utf-8")
    return dest


def child_pythonpath(repo_root: Path, metadata_dir: Path, *extra: Path) -> str:
    """A ``PYTHONPATH`` under which a child process imports and resolves the
    in-tree distributions as installed ones: their metadata, their sources,
    then *extra*."""
    write_in_tree_metadata(repo_root, metadata_dir)
    parts = [metadata_dir, *in_tree_source_dirs(repo_root), *extra]
    return os.pathsep.join(str(part) for part in parts)


# ── withdrawing an installed distribution ────────────────────────────────────


def _normalized(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def withdraw_installed_distribution(name: str) -> Callable[[], None]:
    """Hide the installed distribution *name* from ``importlib.metadata`` in
    this process, as if it were uninstalled; returns a function that undoes it.

    :func:`remove_entry_points` withdraws only what the SDK resolver
    registered from the source tree. Where the distribution is really
    installed (``pip install .``, as in CI and the image), its metadata
    still lists its entry points, and anything that walks
    ``importlib.metadata.distributions()`` (the core's feature discovery)
    or calls ``entry_points()`` -- which walks it -- would still find them.
    A removability probe calls both. Patching the module attribute covers
    every caller that looks it up at call time, so call this before
    importing code that binds ``distributions`` by name at import.
    """
    import importlib.metadata as metadata

    hidden = _normalized(name)
    real = metadata.distributions

    def distributions(**kwargs: Any) -> Iterator[Any]:
        for dist in real(**kwargs):
            if _normalized(dist.metadata["Name"] or "") != hidden:
                yield dist

    setattr(metadata, "distributions", distributions)

    def restore() -> None:
        setattr(metadata, "distributions", real)

    return restore
