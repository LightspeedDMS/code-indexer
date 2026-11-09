"""issue-64616: a published index is checked out over rebuildable leftovers.

The served-index cache evicts a checkout by renaming its whole index root
away. The index backfill sweeps then re-create that root for their cooldown
markers, a SCIP build writes its output there, and a refresh that could not
index writes its language mappings. Such a root holds no index and no
configuration -- yet the checkout used to refuse it as unexplained state, so
the alias was never served again and its refresh failed on every cycle.

Pinned against a fake artifact service holding real trees:

* a root holding only ``REBUILDABLE_LEFTOVERS`` (or nothing) is checked out
  over; leftovers the version does not carry are kept, the version's own
  copy wins over a leftover of the same name;
* a failed checkout over leftovers leaves the target exactly as it was;
* a root holding anything else (a configuration, an unknown file) is still
  refused and left untouched;
* every index backfill policy's cooldown marker is a known leftover.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict, List

import pytest

from code_indexer.server.services import index_materializer as im
from code_indexer.server.services.fts_backfill.policy import FtsBackfillPolicy
from cidx_scip.server.backfill_policy import ScipBackfillPolicy
from code_indexer.server.services.semantic_backfill.policy import (
    SemanticBackfillPolicy,
)
from code_indexer.utils.index_store import (
    INDEX_DIRNAME,
    DelegatedIndexStore,
    published_index_fields,
)
from tests.unit.server.index_store_fakes import FakeArtifacts

REPO = "example-repo"
VERSION_MAPPINGS = "mappings published with the version"


def _build(dir_: Path) -> Path:
    built = dir_ / INDEX_DIRNAME
    (built / "index").mkdir(parents=True)
    (built / "index" / "hnsw.bin").write_bytes(b"published")
    (built / "config.json").write_text("{}")
    (built / "language-mappings.yaml").write_text(VERSION_MAPPINGS)
    return built


@pytest.fixture
def world(tmp_path: Path) -> Dict[str, Any]:
    artifacts = FakeArtifacts(tmp_path / "store")
    store = DelegatedIndexStore(artifacts)
    scratch = tmp_path / "scratch" / REPO
    version = store.publish(scratch, _build(scratch))
    # The base clone: the alias target of a repo never refreshed since it was
    # published in place, and every refresh's incremental base.
    target = tmp_path / "golden-repos" / REPO
    target.mkdir(parents=True)
    (target / "main.py").write_text("print('repository content')\n")
    fixes: List[Path] = []
    artifacts.calls.clear()
    return {
        "artifacts": artifacts,
        "store": store,
        "version": version,
        "target": target,
        "root": target / INDEX_DIRNAME,
        "fixes": fixes,
        "doc": {
            "target_path": str(target),
            **published_index_fields(REPO, str(version)),
        },
    }


def _ensure(world: Dict[str, Any]) -> None:
    im.ensure_published_index(
        world["target"],
        world["doc"],
        store=world["store"],
        fix_config=lambda p: world["fixes"].append(Path(p)),
    )


def _leave_leftovers(root: Path) -> None:
    """What an eviction followed by a SCIP backfill and a failed refresh leaves."""
    (root / "scip").mkdir(parents=True)
    (root / "scip" / "index.scip").write_bytes(b"scip output")
    (root / ".scip_reconcile_attempt").touch()
    (root / "language-mappings.yaml").write_text("written by a failed refresh")


# -- checked out over leftovers --------------------------------------------


def test_leftovers_only_root_is_checked_out_over(world: Dict[str, Any]) -> None:
    _leave_leftovers(world["root"])

    _ensure(world)

    root = world["root"]
    assert (root / "index" / "hnsw.bin").read_bytes() == b"published"
    assert (root / "config.json").is_file()
    assert (world["target"] / im.CHECKED_OUT_MARKER).read_text() == world["version"]
    assert not (world["target"] / im.IN_PROGRESS_MARKER).exists()
    assert not (world["target"] / im.LEFTOVERS_DIRNAME).exists()
    assert world["fixes"] == [world["target"]]
    assert (world["target"] / "main.py").is_file(), "repository content untouched"


def test_leftovers_the_version_lacks_are_kept(world: Dict[str, Any]) -> None:
    _leave_leftovers(world["root"])

    _ensure(world)

    root = world["root"]
    assert (root / "scip" / "index.scip").read_bytes() == b"scip output"
    assert (root / ".scip_reconcile_attempt").is_file(), "the cooldown survives"


def test_the_versions_own_copy_wins_over_a_leftover(world: Dict[str, Any]) -> None:
    _leave_leftovers(world["root"])

    _ensure(world)

    assert (world["root"] / "language-mappings.yaml").read_text() == VERSION_MAPPINGS


def test_an_empty_root_is_checked_out_over(world: Dict[str, Any]) -> None:
    world["root"].mkdir()

    _ensure(world)

    assert (world["root"] / "index" / "hnsw.bin").read_bytes() == b"published"


def test_a_failed_checkout_over_leftovers_leaves_them_as_they_were(
    world: Dict[str, Any],
) -> None:
    _leave_leftovers(world["root"])
    world["artifacts"].fail_checkout = True

    with pytest.raises(im.PublishedIndexUnavailableError, match="Could not check out"):
        _ensure(world)

    root = world["root"]
    assert sorted(p.name for p in root.iterdir()) == [
        ".scip_reconcile_attempt",
        "language-mappings.yaml",
        "scip",
    ]
    assert (root / "scip" / "index.scip").read_bytes() == b"scip output"
    assert not (world["target"] / im.IN_PROGRESS_MARKER).exists()
    assert not (world["target"] / im.LEFTOVERS_DIRNAME).exists()
    assert (world["target"] / "main.py").is_file()

    world["artifacts"].fail_checkout = False
    _ensure(world)
    assert (root / "index" / "hnsw.bin").read_bytes() == b"published"


# -- still refused ------------------------------------------------------------


@pytest.mark.parametrize("unexplained", ["config.json", "notes.txt"])
def test_any_other_state_is_still_refused_and_untouched(
    world: Dict[str, Any], unexplained: str
) -> None:
    _leave_leftovers(world["root"])
    (world["root"] / unexplained).write_text("mine")

    with pytest.raises(im.PublishedIndexUnavailableError, match="refusing"):
        _ensure(world)

    assert (world["root"] / unexplained).read_text() == "mine"
    assert (world["root"] / "scip" / "index.scip").is_file()
    assert world["artifacts"].calls == []


# -- the predicate --------------------------------------------------------------


def test_predicate_true_only_for_leftovers(tmp_path: Path) -> None:
    root = tmp_path / INDEX_DIRNAME
    assert not im.holds_only_rebuildable_leftovers(root), "missing"
    root.mkdir()
    assert im.holds_only_rebuildable_leftovers(root), "empty"
    _leave_leftovers(root)
    assert im.holds_only_rebuildable_leftovers(root)
    (root / "index").mkdir()
    assert not im.holds_only_rebuildable_leftovers(root), "holds an index"


def test_predicate_false_for_a_symlinked_root(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    link = tmp_path / INDEX_DIRNAME
    os.symlink(real, link)
    assert not im.holds_only_rebuildable_leftovers(link)


def test_predicate_false_for_a_file(tmp_path: Path) -> None:
    root = tmp_path / INDEX_DIRNAME
    root.write_text("not a directory")
    assert not im.holds_only_rebuildable_leftovers(root)


@pytest.mark.parametrize(
    "policy", [ScipBackfillPolicy, FtsBackfillPolicy, SemanticBackfillPolicy]
)
def test_every_backfill_cooldown_marker_is_a_known_leftover(policy: Any) -> None:
    """A backfill sweep writes its cooldown marker into an evicted index root;
    a marker missing here would make that root unexplained state again."""
    assert policy.attempt_marker in im.REBUILDABLE_LEFTOVERS


def test_no_leftovers_dir_is_left_after_success(world: Dict[str, Any]) -> None:
    _leave_leftovers(world["root"])
    stale = world["target"] / im.LEFTOVERS_DIRNAME
    stale.mkdir()
    (stale / "from-a-crash").write_text("x")

    _ensure(world)

    assert not stale.exists()
