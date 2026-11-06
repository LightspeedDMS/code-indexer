"""Publishing a snapshot must not be blocked by a missing alias pointer.

The pointer is derived state -- the record that a repo is globally active lives in
the global registry -- and the caller has already built and validated the snapshot
being published. Raising failed the whole refresh AND left queries served from the
previous snapshot, so a missing pointer became permanently stale answers that look
healthy.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager


def _mgr(tmp_path: Path) -> AliasManager:
    return AliasManager(str(tmp_path / "aliases"))


def test_swap_recreates_a_missing_pointer_at_the_new_target(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    m.swap_alias("example-repo-global", "/snap/v2", "/snap/v1")

    data = json.loads((tmp_path / "aliases" / "example-repo-global.json").read_text())
    assert data["target_path"] == "/snap/v2"
    assert data["repo_name"] == "example-repo"


def test_swap_still_publishes_normally_when_the_pointer_exists(tmp_path: Path) -> None:
    m = _mgr(tmp_path)
    m.create_alias("example-repo-global", "/snap/v1", "example-repo")
    m.swap_alias("example-repo-global", "/snap/v2", "/snap/v1")

    data = json.loads((tmp_path / "aliases" / "example-repo-global.json").read_text())
    assert data["target_path"] == "/snap/v2"


def test_the_conflict_check_is_preserved_when_a_pointer_exists(tmp_path: Path) -> None:
    """Self-healing must not weaken the concurrent-swap guard: with a real pointer,
    a mismatched old_target is still rejected."""
    m = _mgr(tmp_path)
    m.create_alias("example-repo-global", "/snap/v1", "example-repo")
    with pytest.raises(ValueError):
        m.swap_alias("example-repo-global", "/snap/v3", "/snap/WRONG")


def test_recreated_pointer_is_readable_by_the_normal_path(tmp_path: Path) -> None:
    """A healed pointer must be a real alias, not a half-written file."""
    m = _mgr(tmp_path)
    m.swap_alias("example-repo-global", "/snap/v2", "/snap/v1")
    assert m.alias_exists("example-repo-global") is True
    assert m.read_alias("example-repo-global") == "/snap/v2"


def test_a_healed_pointer_can_be_swapped_again(tmp_path: Path) -> None:
    """The repo converges: after one self-heal the normal publish path takes over."""
    m = _mgr(tmp_path)
    m.swap_alias("example-repo-global", "/snap/v2", "/snap/v1")
    m.swap_alias("example-repo-global", "/snap/v3", "/snap/v2")
    assert m.read_alias("example-repo-global") == "/snap/v3"
