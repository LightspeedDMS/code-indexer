"""Alias pointer readers: lenient for alias management, strict for identity.

``AliasManager.read_alias`` (and ``read_alias_from_disk``) keep answering
None for a pointer they cannot read, as their callers expect. The strict
reader used to establish catalog identity answers None only when there is
no pointer file and raises for one that exists but cannot be used. Real
files on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import (
    AliasManager,
    AliasPointerUnusable,
    read_alias_from_disk,
    read_alias_pointer_strict,
)

ALIAS = "example-repo-global"
UNREADABLE = ["[]", "{}", "{not json", "<dir>", "<dangling>"]
NON_STRING_TARGET = '{"target_path": ["x"]}'


def _aliases(tmp_path: Path, pointer: str) -> Path:
    aliases = tmp_path / "golden-repos" / "aliases"
    aliases.mkdir(parents=True)
    target = aliases / f"{ALIAS}.json"
    if pointer == "<dir>":
        target.mkdir()
    elif pointer == "<dangling>":
        target.symlink_to(aliases / "missing.json")
    else:
        target.write_text(pointer)
    return aliases


@pytest.mark.parametrize("pointer", UNREADABLE + [NON_STRING_TARGET])
def test_strict_reader_raises_for_a_pointer_that_cannot_be_used(
    tmp_path: Path, pointer: str
) -> None:
    aliases = _aliases(tmp_path, pointer)

    with pytest.raises(AliasPointerUnusable):
        read_alias_pointer_strict(aliases, ALIAS)


@pytest.mark.parametrize("pointer", UNREADABLE)
def test_alias_management_still_reads_an_unreadable_pointer_as_none(
    tmp_path: Path, pointer: str
) -> None:
    aliases = _aliases(tmp_path, pointer)

    assert AliasManager(str(aliases)).read_alias(ALIAS) is None
    assert read_alias_from_disk(aliases, ALIAS) is None


def test_missing_pointer_is_none_to_every_reader(tmp_path: Path) -> None:
    aliases = tmp_path / "golden-repos" / "aliases"

    assert read_alias_pointer_strict(aliases, ALIAS) is None
    assert read_alias_from_disk(aliases, ALIAS) is None
    assert not aliases.exists()
    assert AliasManager(str(aliases)).read_alias(ALIAS) is None


def test_valid_pointer_gives_its_target_to_every_reader(tmp_path: Path) -> None:
    aliases = _aliases(tmp_path, json.dumps({"target_path": "/srv/example"}))

    assert read_alias_pointer_strict(aliases, ALIAS) == "/srv/example"
    assert read_alias_from_disk(aliases, ALIAS) == "/srv/example"
    assert AliasManager(str(aliases)).read_alias(ALIAS) == "/srv/example"
