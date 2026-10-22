"""MetaDirectoryUpdater's stub creation never overwrites a real description.

A registration's description is written atomically without the catalog lock
when that lock is busy, so it can land between the updater's missing-file
check and its stub write. Stub creation is exclusive: a file that exists by
then is left untouched.

The interleaving is forced deterministically: the description path reports
"missing" exactly once (the updater's check) while the real description is
already on disk.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.meta_directory_updater import MetaDirectoryUpdater

ALIAS = "example-repo"
REAL = "# example-repo\n\nGenerated description.\n"


def _updater(meta_dir: Path) -> MetaDirectoryUpdater:
    registry = MagicMock()
    registry.list_global_repos.return_value = [{"alias_name": f"{ALIAS}-global"}]
    return MetaDirectoryUpdater(str(meta_dir), registry)


def test_description_landing_after_the_check_is_not_overwritten(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir()
    target = meta_dir / f"{ALIAS}.md"
    updater = _updater(meta_dir)
    # The updater lists the directory before the description lands ...
    monkeypatch.setattr(
        updater, "_get_existing_description_aliases", lambda known_aliases=None: set()
    )
    # ... and its exists() check runs just before the writer's os.replace.
    target.write_text(REAL, encoding="utf-8")
    real_exists = Path.exists
    reported: Dict[str, List[Any]] = {"missing": []}

    def _exists_once_missing(self: Path) -> bool:
        if self == target and not reported["missing"]:
            reported["missing"].append(self)
            return False
        return real_exists(self)

    monkeypatch.setattr(Path, "exists", _exists_once_missing)

    updater.update()

    assert reported["missing"] == [target]
    assert target.read_text(encoding="utf-8") == REAL


def test_missing_description_still_gets_a_stub(tmp_path: Path) -> None:
    meta_dir = tmp_path / "cidx-meta"
    meta_dir.mkdir()

    _updater(meta_dir).update()

    stub = (meta_dir / f"{ALIAS}.md").read_text(encoding="utf-8")
    assert stub == f"# {ALIAS}\n\nGolden repository: {ALIAS}\n"
