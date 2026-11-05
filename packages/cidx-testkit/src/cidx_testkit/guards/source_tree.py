"""The source a static guard scans: several distributions, one import closure.

A guard asks "what can the server REACH?": the first-party import closure of
some seed modules. Once seams move into their own distributions
(``cidx_sdk``, ``cidx_backend_*``) that closure crosses distribution
boundaries -- the server reaches ``LocalContentBackend`` through the content
seam even though it lives in ``cidx_backend_workspace`` -- so a guard that only
walked ``code_indexer`` would silently stop checking every implementation that
moved out. :class:`SourceTree` follows imports into every package it is given.

Keys: a module of the PRIMARY package is named by its path relative to that
package (``server/app.py``), exactly as the guards' committed data files have
always named it; a module of any other package is ``<package>/<path>``
(``cidx_backend_workspace/content.py``).
"""

from __future__ import annotations

import ast
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple


@dataclass(frozen=True)
class SourceTree:
    """Top-level import packages and the directories that hold them.

    ``primary`` must be a key of ``packages``.
    """

    primary: str
    packages: Mapping[str, Path]
    _cache: Dict[Tuple[str, ...], Tuple[Path, ...]] = field(
        default_factory=dict, compare=False, repr=False
    )

    @property
    def primary_root(self) -> Path:
        return self.packages[self.primary]

    def package_of(self, path: Path) -> Tuple[str, Path]:
        """The (package name, package directory) holding *path*."""
        resolved = path.resolve()
        for name, root in self.packages.items():
            try:
                resolved.relative_to(root.resolve())
            except ValueError:
                continue
            return name, root
        raise ValueError(f"{path} is in none of {sorted(self.packages)}")

    def key(self, path: Path) -> str:
        """The guard key of *path* (see the module docstring)."""
        name, root = self.package_of(path)
        rel = path.resolve().relative_to(root.resolve()).as_posix()
        return rel if name == self.primary else f"{name}/{rel}"

    def path_for(self, key: str) -> Path:
        """The file a guard key names."""
        head, _, rest = key.partition("/")
        if head in self.packages and head != self.primary and rest:
            return self.packages[head] / rest
        return self.primary_root / key

    def module_path(self, package: str, dotted: str) -> Optional[Path]:
        """The file of module *dotted* inside *package* ("" for the package)."""
        root = self.packages.get(package)
        if root is None:
            return None
        base = root.joinpath(*dotted.split(".")) if dotted else root
        if dotted and base.with_suffix(".py").is_file():
            return base.with_suffix(".py")
        if (base / "__init__.py").is_file():
            return base / "__init__.py"
        return None

    def first_party_imports(
        self, path: Path, *, relative: bool = True, imported_names: bool = True
    ) -> Set[Tuple[str, str]]:
        """``(package, dotted module)`` for every first-party import in *path*,
        at any depth (functions and ``TYPE_CHECKING`` blocks included).

        ``relative`` follows ``from . import x`` too; ``imported_names`` adds
        ``module.name`` for each ``from module import name`` (it may be a
        submodule)."""
        tree = ast.parse(path.read_text(encoding="utf-8"))
        own, root = self.package_of(path)
        package_parts = path.resolve().relative_to(root.resolve()).parent.parts
        found: Set[Tuple[str, str]] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.level and not relative:
                    continue
                if node.level:
                    keep = len(package_parts) - (node.level - 1)
                    base = package_parts[: max(keep, 0)]
                    tail = tuple(node.module.split(".")) if node.module else ()
                    target = (own, ".".join(base + tail))
                elif node.module and node.module.split(".")[0] in self.packages:
                    head, _, rest = node.module.partition(".")
                    target = (head, rest)
                else:
                    continue
                found.add(target)
                if not imported_names:
                    continue
                for alias in node.names:
                    found.add(
                        (target[0], ".".join(p for p in (target[1], alias.name) if p))
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    head, _, rest = alias.name.partition(".")
                    if head in self.packages:
                        found.add((head, rest))
        return found

    def closure(
        self,
        seeds: Sequence[str],
        exempt: Sequence[str] = (),
        *,
        relative: bool = True,
        imported_names: bool = True,
        ancestors: bool = True,
    ) -> Tuple[Path, ...]:
        """Every module reachable from *seeds* (keys of the primary package; a
        directory is taken whole), sorted by key. With ``ancestors``, importing
        ``a.b.c`` runs every ancestor ``__init__`` too, so those are in the
        closure with whatever they import. A module whose key starts with an
        *exempt* prefix is not followed (nor scanned). ``relative`` and
        ``imported_names`` are passed to :meth:`first_party_imports`; a guard
        keeps the reachability rules it was written with."""
        cache_key = (
            tuple(seeds)
            + ("|",)
            + tuple(exempt)
            + (str(relative), str(imported_names), str(ancestors))
        )
        cached = self._cache.get(cache_key)
        if cached is not None:
            return cached
        stack: List[Path] = []
        for seed in seeds:
            path = self.primary_root / seed
            stack.extend(sorted(path.rglob("*.py")) if path.is_dir() else [path])
        seen: Dict[str, Path] = {}
        while stack:
            path = stack.pop()
            key = self.key(path)
            if key in seen or _exempt(key, exempt):
                continue
            seen[key] = path
            name, root = self.package_of(path)
            rel = path.resolve().relative_to(root.resolve())
            for parent in rel.parents if ancestors else ():
                init = root / parent / "__init__.py"
                if parent != Path(".") and init.is_file():
                    stack.append(init)
            for package, dotted in self.first_party_imports(
                path, relative=relative, imported_names=imported_names
            ):
                nxt = self.module_path(package, dotted)
                if nxt is not None and self.key(nxt) not in seen:
                    stack.append(nxt)
        result = tuple(seen[k] for k in sorted(seen))
        self._cache[cache_key] = result
        return result


def _exempt(key: str, exempt: Sequence[str]) -> bool:
    return any(key == d or key.startswith(d.rstrip("/") + "/") for d in exempt)


def in_tree_source_tree(repo_root: Path, primary: str) -> SourceTree:
    """The packages of every in-tree distribution ``pip install <repo_root>``
    would install (see ``cidx_testkit.registry``), with *primary* first."""
    from cidx_testkit.registry import in_tree_source_dirs

    packages: Dict[str, Path] = {}
    for source in in_tree_source_dirs(repo_root):
        for child in sorted(source.iterdir()):
            if (child / "__init__.py").is_file():
                packages.setdefault(child.name, child)
    if primary not in packages:
        raise ValueError(f"{primary!r} is not a package of {repo_root}")
    return SourceTree(primary=primary, packages=packages)


def all_files(tree: SourceTree) -> List[Path]:
    """Every ``.py`` file of every package in *tree*, sorted by key."""
    files = [
        path for root in tree.packages.values() for path in sorted(root.rglob("*.py"))
    ]
    return sorted(files, key=tree.key)
