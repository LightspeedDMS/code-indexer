"""The lifecycle description writer and the meta-directory updater's stubs
put catalog files in place through the shared staged writer: the content is
staged outside cidx-meta and moved in whole, at the catalog-wide mode 0644,
so a backup stage-all or a snapshot never sees a temp or partial file. The
updater keeps its exclusive create: a real description that appears before
the stub lands is never overwritten.

Real writers and filesystem; ``os.replace``/``os.link`` are wrapped by spies
that inspect the catalog at the instant a file moves in, then do the real
call.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterator, List, Set

import pytest

from code_indexer.global_repos.lifecycle_batch_runner import write_meta_md
from code_indexer.global_repos.meta_directory_updater import MetaDirectoryUpdater

ALIAS = "example-repo"
REAL = "# example-repo\n\nA real description.\n"


@pytest.fixture(autouse=True)
def umask_077() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


@pytest.fixture
def catalog(tmp_path: Path) -> Path:
    path = tmp_path / "golden-repos" / "cidx-meta"
    path.mkdir(parents=True)
    return path


def _files(catalog: Path) -> Set[str]:
    return {p.name for p in catalog.iterdir()}


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def _spy(monkeypatch: pytest.MonkeyPatch, name: str, catalog: Path) -> List[Set[str]]:
    """Record the catalog's files each time *name* moves a file into it."""
    real = getattr(os, name)
    seen: List[Set[str]] = []

    def _observed(src: Any, dst: Any, *args: Any, **kwargs: Any) -> None:
        if Path(os.fspath(dst)).parent.resolve() == catalog.resolve():
            seen.append(_files(catalog))
        real(src, dst, *args, **kwargs)

    monkeypatch.setattr(os, name, _observed)
    return seen


def test_lifecycle_write_is_staged_and_world_readable(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch, "replace", catalog)
    scheduler = SimpleNamespace(acquire_write_lock=None, release_write_lock=None)

    write_meta_md(
        alias=ALIAS,
        description_body="A refreshed description.",
        lifecycle_frontmatter={"lifecycle": {"confidence": "high"}},
        already_locked=True,
        refresh_scheduler=scheduler,
        golden_repos_dir=catalog.parent,
    )

    target = catalog / f"{ALIAS}.md"
    assert seen == [set()], "a temp file was visible inside cidx-meta"
    assert _mode(target) == 0o644
    assert "A refreshed description." in target.read_text()


def _updater(catalog: Path) -> MetaDirectoryUpdater:
    registry = SimpleNamespace(
        list_global_repos=lambda: [{"alias_name": f"{ALIAS}-global"}]
    )
    return MetaDirectoryUpdater(str(catalog), registry)  # type: ignore[arg-type]


def test_updater_stub_is_staged_and_world_readable(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = _spy(monkeypatch, "link", catalog)

    _updater(catalog).update()

    stub = catalog / f"{ALIAS}.md"
    assert seen == [set()], "the stub was written in place"
    assert _mode(stub) == 0o644
    assert stub.read_text().startswith(f"# {ALIAS}")


def test_updater_never_overwrites_a_description_that_lands_first(
    catalog: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    real_link = os.link
    target = catalog / f"{ALIAS}.md"

    def _description_lands_first(src: Any, dst: Any, *a: Any, **k: Any) -> None:
        if Path(os.fspath(dst)) == target:
            target.write_text(REAL)
        real_link(src, dst, *a, **k)

    monkeypatch.setattr(os, "link", _description_lands_first)

    _updater(catalog).update()

    assert target.read_text() == REAL, "the stub overwrote a real description"
    assert _files(catalog) == {target.name}
