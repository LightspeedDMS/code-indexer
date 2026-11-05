"""In-tree registration reads what the pyproject files declare, nothing else."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

from cidx_sdk.seams.resolver import extra_entry_points
from cidx_testkit.registry import (
    child_pythonpath,
    declared_entry_points,
    in_tree_distributions,
    register_in_tree_entry_points,
    unregister,
    withdraw_installed_distribution,
    write_in_tree_metadata,
)

_GROUP = "code_indexer.content_backend"


def _example_repo(root: Path) -> Path:
    (root / "packages" / "backend" / "src" / "example_backend").mkdir(parents=True)
    (
        root / "packages" / "backend" / "src" / "example_backend" / "__init__.py"
    ).write_text("def make():\n    return 'example'\n")
    (root / "packages" / "backend" / "pyproject.toml").write_text(
        '[project]\nname = "example-backend"\ndependencies = []\n'
        '[project.entry-points."code_indexer.content_backend"]\n'
        'local = "example_backend:make"\n'
        '[project.entry-points."console_scripts"]\nignored = "x:y"\n'
    )
    (root / "unrelated").mkdir()
    (root / "unrelated" / "pyproject.toml").write_text(
        '[project]\nname = "unrelated"\n'
        '[project.entry-points."code_indexer.content_backend"]\n'
        'local = "nope:nope"\n'
    )
    (root / "pyproject.toml").write_text(
        '[project]\nname = "example-core"\n'
        'dependencies = ["example-backend @ {root:uri}/packages/backend", "click"]\n'
    )
    return root


def test_only_the_root_and_its_path_dependencies_count(tmp_path: Path) -> None:
    repo = _example_repo(tmp_path)
    names = [p.name for p in in_tree_distributions(repo)]
    assert names == [repo.name, "backend"]
    declared = declared_entry_points(repo)
    assert list(declared) == [_GROUP]
    assert [(e.name, e.value) for e in declared[_GROUP]] == [
        ("local", "example_backend:make")
    ]


def test_registration_is_visible_to_the_resolver_and_withdrawn(tmp_path: Path) -> None:
    repo = _example_repo(tmp_path)
    before = dict(extra_entry_points().get(_GROUP, {}))
    registered = register_in_tree_entry_points(repo)
    try:
        assert extra_entry_points()[_GROUP]["local"].value == "example_backend:make"
    finally:
        unregister(registered)
        # restore whatever the session had registered under the same name
        from cidx_sdk.seams.resolver import add_entry_points

        add_entry_points(_GROUP, list(before.values()))


def test_a_child_process_finds_the_declared_entry_points(tmp_path: Path) -> None:
    repo = _example_repo(tmp_path / "repo")
    path = child_pythonpath(repo, tmp_path / "meta")
    probe = (
        "from importlib.metadata import entry_points;"
        "(ep,) = entry_points(group='code_indexer.content_backend');"
        "print(ep.name, ep.load()())"
    )
    # -S: no site-packages, so an installed distribution declaring the same
    # group (cidx-backend-workspace's "local") is not found next to it.
    done = subprocess.run(
        [sys.executable, "-S", "-c", probe],
        env={"PYTHONPATH": path},
        capture_output=True,
        text=True,
        check=True,
    )
    assert done.stdout.strip() == "local example"


def test_a_withdrawn_distribution_is_not_found_until_restored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import importlib.metadata as metadata

    meta = tmp_path / "meta"
    write_in_tree_metadata(_example_repo(tmp_path / "repo"), meta)
    monkeypatch.syspath_prepend(str(meta))

    def names() -> set:
        return {d.metadata["Name"] for d in metadata.distributions()}

    def backends() -> set:
        return {ep.value for ep in metadata.entry_points(group=_GROUP)}

    assert "example-backend" in names()
    restore = withdraw_installed_distribution("Example_Backend")
    try:
        assert "example-backend" not in names()
        assert "example_backend:make" not in backends()
        assert "pytest" in names(), "other distributions stay visible"
    finally:
        restore()
    assert "example-backend" in names()
