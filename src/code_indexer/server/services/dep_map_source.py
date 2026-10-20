"""Where the dependency-map parsers read the map from.

``DepMapSource`` is every read the depmap parsers make. The filesystem
source reads the directory as it is (administrators); the catalog access
package provides a projected source holding only what a restricted caller
may see (``cidx_meta_access.depmap_source``). The parsers' graph, hub,
stale and hygiene logic runs unchanged on either. A domain description is
obtained with ``describe(entry)`` for the entries a door actually returns,
so a projected source projects only those.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Protocol

from code_indexer.server.services.dep_map_file_utils import (
    get_domain_md_files,
    load_domains_json,
)


class DepMapSource(Protocol):
    """The reads of one dependency-map directory.

    Names are file stems (``payments`` for ``payments.md``). A name the
    source does not hold reads as a missing file.
    """

    def is_present(self) -> bool:
        """True when the dependency-map directory exists."""

    def load_domains_json(self) -> List[Dict[str, Any]]:
        """The ``_domains.json`` entries ([] when absent or invalid)."""

    def describe(self, entry: Mapping[str, Any]) -> str:
        """The description of one ``load_domains_json`` entry as the caller
        may see it ("" when it has none)."""

    def domain_names(self) -> List[str]:
        """Stems of the domain documents (no ``_`` prefix), in listing order."""

    def has_domain(self, name: str) -> bool:
        """True when the document ``<name>.md`` exists."""

    def read_text(self, name: str) -> str:
        """The document ``<name>.md``; FileNotFoundError when absent."""

    def label(self, name: str) -> str:
        """Display label of ``<name>.md`` for diagnostics."""

    def resolved_label(self, name: str) -> str:
        """Label of ``<name>.md`` after symlink resolution."""

    def within(self, name: str) -> bool:
        """False when ``<name>.md`` resolves outside the directory."""


class FilesystemDepMapSource:
    """The dependency-map directory read as it is on disk."""

    def __init__(self, output_dir: Path) -> None:
        self._dir = Path(output_dir)
        self._base: Optional[Path] = None

    @property
    def output_dir(self) -> Path:
        return self._dir

    def _path(self, name: str) -> Path:
        return self._dir / f"{name}.md"

    def is_present(self) -> bool:
        return self._dir.exists()

    def load_domains_json(self) -> List[Dict[str, Any]]:
        return load_domains_json(self._dir)

    def describe(self, entry: Mapping[str, Any]) -> str:
        description = entry.get("description", "")
        return description if isinstance(description, str) else ""

    def domain_names(self) -> List[str]:
        return [f.stem for f in get_domain_md_files(self._dir)]

    def has_domain(self, name: str) -> bool:
        return self._path(name).exists()

    def read_text(self, name: str) -> str:
        return self._path(name).read_text(encoding="utf-8")

    def label(self, name: str) -> str:
        return str(self._path(name))

    def resolved_label(self, name: str) -> str:
        return str(self._path(name).resolve())

    def within(self, name: str) -> bool:
        if self._base is None:
            self._base = self._dir.resolve()
        try:
            self._path(name).resolve().relative_to(self._base)
        except ValueError:
            return False
        return True
