"""directory_tree never follows a symlink inside the served tree to a place
outside the repository root, for catalog and non-catalog repositories.

Real directories and symlinks on disk, real alias registry files, real
catalog fixture and group store. Neutral names only.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)
from tests.unit.server.mcp.test_cidx_meta_directory_tree_serving import (
    _register,
    _tree,
)

OUTSIDE_FILE = "outside_only.txt"


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path / "catalog")


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    target = tmp_path / "outside"
    target.mkdir()
    (target / OUTSIDE_FILE).write_text("outside\n", encoding="utf-8")
    return target


@pytest.fixture
def golden_root(tmp_path: Path, fx: CidxMetaFixture) -> Path:
    root = tmp_path / "golden-repos"
    _register(root, "cidx-meta", fx.root)
    return root


def _granted_repo(tmp_path: Path, golden_root: Path, outside: Path) -> None:
    repo = tmp_path / "golden-alpha"
    repo.mkdir()
    (repo / "inside.txt").write_text("inside\n", encoding="utf-8")
    (repo / "linked").symlink_to(outside, target_is_directory=True)
    _register(golden_root, ALPHA, repo)


def test_non_catalog_tree_never_follows_a_symlink_outside_the_root(
    tmp_path: Path, fx: CidxMetaFixture, golden_root: Path, outside: Path
) -> None:
    _granted_repo(tmp_path, golden_root, outside)

    tree = _tree(fx, golden_root, {"repository_alias": ALPHA, "max_depth": 5})
    through = _tree(fx, golden_root, {"repository_alias": ALPHA, "path": "linked"})

    assert tree["success"] is True, tree
    assert "inside.txt" in tree["tree_string"]
    assert OUTSIDE_FILE not in json.dumps(tree)
    assert through == {"success": False, "error": "Path does not exist: linked"}


def test_catalog_tree_never_follows_a_symlink_outside_the_root(
    fx: CidxMetaFixture, golden_root: Path, outside: Path
) -> None:
    link = fx.root / "xray-patterns" / "__any__" / "linked"
    link.symlink_to(outside, target_is_directory=True)
    linked_path = "xray-patterns/__any__/linked"

    tree = _tree(
        fx, golden_root, {"repository_alias": "cidx-meta-global", "max_depth": 5}
    )
    through = _tree(
        fx, golden_root, {"repository_alias": "cidx-meta-global", "path": linked_path}
    )

    assert tree["success"] is True, tree
    assert "p.yaml" in tree["tree_string"]
    assert OUTSIDE_FILE not in json.dumps(tree)
    assert through == {"success": False, "error": f"Path does not exist: {linked_path}"}
