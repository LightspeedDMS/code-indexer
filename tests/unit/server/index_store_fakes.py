"""An in-memory-ish artifact service for IndexStore tests.

Holds REAL directory trees per (alias, version), with the same contract the
``code_indexer.index_artifacts`` plugin client has: ``current_version``,
``publish(alias, base_version, source_dir)`` (compare-and-swap on the base),
``checkout(alias, dest_dir, version)`` (refuses an existing dest) and
``delete_alias(alias)``.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Dict, List, Optional


class FakeArtifacts:
    """An artifact service holding real trees: alias -> [(version, dir)]."""

    is_local = False

    def __init__(self, root: Path) -> None:
        self.root = root
        self.versions: Dict[str, List[str]] = {}
        self.calls: List[tuple] = []
        self.fail_publish = False
        self.fail_checkout = False
        self.fail_delete = False

    def current_version(self, alias: str) -> Optional[str]:
        history = self.versions.get(alias)
        return history[-1] if history else None

    def publish(self, alias: str, base_version: Optional[str], source_dir: str) -> str:
        self.calls.append(("publish", alias, base_version, source_dir))
        if self.fail_publish:
            raise OSError("artifact service unreachable")
        if base_version != self.current_version(alias):
            raise RuntimeError("ECONFLICT")
        version = f"v{len(self.versions.get(alias, [])) + 1:032d}"
        shutil.copytree(source_dir, self.root / alias / version)
        self.versions.setdefault(alias, []).append(version)
        return version

    def checkout(self, alias: str, dest_dir: str, version: Optional[str] = None) -> str:
        self.calls.append(("checkout", alias, dest_dir, version))
        if self.fail_checkout:
            raise RuntimeError("EDIGEST")
        chosen = version or self.current_version(alias)
        if chosen is None or not (self.root / alias / chosen).is_dir():
            raise RuntimeError(f"ENOENT: {alias}@{chosen}")
        dest = Path(dest_dir)
        try:
            dest.mkdir()
        except FileExistsError:
            raise RuntimeError("EINVAL: dest exists")
        # Writes only under dest -- like the real client, which never stats
        # dest's parent (on a mount-less pod that is the repository path).
        source = self.root / alias / chosen
        for path in sorted(source.rglob("*")):
            target = dest / path.relative_to(source)
            if path.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(path.read_bytes())
        return chosen

    def delete_alias(self, alias: str) -> int:
        self.calls.append(("delete_alias", alias))
        if self.fail_delete:
            raise OSError("artifact service unreachable")
        deleted = self.versions.pop(alias, [])
        shutil.rmtree(self.root / alias, ignore_errors=True)
        return len(deleted)
