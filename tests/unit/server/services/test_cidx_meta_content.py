"""cidx_meta_content: cidx-meta's shared content on a mount-less pod.

Under the local backend the seam is absent (None) and every caller keeps its
own disk code. Under a delegated backend reads go to the content backend and
writes to the content writer at the pod's cidx-meta path; failures raise.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from code_indexer.server.services import cidx_meta_content as cmc
from code_indexer.utils.blob_hash import git_blob_hash_bytes
from tests.unit.server.cidx_meta_seam_fakes import (
    FakeWriteError,
    MetaWorld,
)


@pytest.fixture
def meta_world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> MetaWorld:
    return MetaWorld(tmp_path, monkeypatch)


def _shared(world: MetaWorld) -> cmc.SharedCidxMeta:
    world.delegated()
    shared = cmc.shared_cidx_meta(world.pod_meta)
    assert shared is not None
    return shared


# ── selection ────────────────────────────────────────────────────────────


def test_local_backend_has_no_seam(meta_world: MetaWorld) -> None:
    assert cmc.shared_cidx_meta(meta_world.local_meta) is None
    assert cmc.shared_cidx_meta_for(meta_world.local_meta / "a.md") is None


def test_delegated_without_writer_refuses(meta_world: MetaWorld) -> None:
    meta_world.delegated(writer=False)
    with pytest.raises(cmc.CidxMetaUnavailableError, match="no content writer"):
        cmc.shared_cidx_meta(meta_world.pod_meta)


def test_for_path_finds_the_cidx_meta_root(meta_world: MetaWorld) -> None:
    meta_world.delegated()
    shared = cmc.shared_cidx_meta_for(meta_world.pod_meta / "memories" / "m.md")
    assert shared is not None and shared.root == str(meta_world.pod_meta)
    assert cmc.shared_cidx_meta_for(meta_world.pod_golden / "other" / "x.md") is None


def test_rel_confines_to_cidx_meta(meta_world: MetaWorld) -> None:
    shared = _shared(meta_world)
    assert shared.rel(meta_world.pod_meta / "memories" / "m.md") == "memories/m.md"
    assert shared.rel("a.md") == "a.md"
    for bad in (meta_world.pod_golden / "other.md", "../escape.md", "", "/etc/passwd"):
        with pytest.raises(PermissionError):
            shared.rel(bad)


# ── reads ────────────────────────────────────────────────────────────────


def test_reads_come_from_the_remote_content(meta_world: MetaWorld) -> None:
    meta_world.seed("repo.md", "# repo\n")
    shared = _shared(meta_world)
    with meta_world.forbid_local_io():
        assert shared.read_text(meta_world.pod_meta / "repo.md") == "# repo\n"
        data, sha = shared.read_with_sha("repo.md")
        assert sha == git_blob_hash_bytes(data)
        assert shared.exists("repo.md") and not shared.exists("absent.md")
        assert shared.blob_sha("absent.md") is None
        with pytest.raises(FileNotFoundError):
            shared.read_bytes("absent.md")


def test_a_backend_failure_is_never_absent(meta_world: MetaWorld) -> None:
    meta_world.seed("repo.md", "x")
    shared = _shared(meta_world)
    meta_world.backend.fail = ConnectionError("workspace-mcp down")
    with pytest.raises(cmc.CidxMetaUnavailableError, match="workspace-mcp down"):
        shared.exists("repo.md")
    with pytest.raises(cmc.CidxMetaUnavailableError):
        shared.entries()
    with pytest.raises(cmc.CidxMetaUnavailableError):
        cmc.has_visible_files(shared)


def test_an_unreadable_file_is_not_absent(meta_world: MetaWorld) -> None:
    (meta_world.remote_meta / "adir.md").mkdir()
    shared = _shared(meta_world)
    with pytest.raises(cmc.CidxMetaUnavailableError, match="could not be read"):
        shared.read_bytes("adir.md")


def test_list_names_is_one_directory_level(meta_world: MetaWorld) -> None:
    for rel in ("b.md", "a.md", "notes.txt", "memories/m.md", ".hidden.md"):
        meta_world.seed(rel, "x")
    shared = _shared(meta_world)
    with meta_world.forbid_local_io():
        assert shared.list_names("", ".md") == [".hidden.md", "a.md", "b.md"]
        assert shared.list_names("memories", ".md") == ["m.md"]
        assert shared.list_names(meta_world.pod_meta / "memories") == ["m.md"]


def test_entries_walk_every_page(meta_world: MetaWorld) -> None:
    for i in range(5):
        meta_world.seed(f"r{i}.md", "x")
    shared = _shared(meta_world)
    original = meta_world.backend.manifest

    def two_per_page(repo_path, **kwargs):
        kwargs["page_size"] = 2
        return original(repo_path, **kwargs)

    meta_world.backend.manifest = two_per_page  # type: ignore[method-assign]
    assert [e.path for e in shared.entries()] == [f"r{i}.md" for i in range(5)]


# ── writes ───────────────────────────────────────────────────────────────


def test_writes_go_to_the_writer_at_the_pod_path(meta_world: MetaWorld) -> None:
    shared = _shared(meta_world)
    pod_before = meta_world.pod_tree()
    with meta_world.forbid_local_io():
        shared.put_text("new/deep.md", "hello")
        assert meta_world.writer.calls[-1] == (
            "put",
            str(meta_world.pod_meta / "new" / "deep.md"),
            {},
        )
        assert (meta_world.remote_meta / "new" / "deep.md").read_text() == "hello"
        assert shared.delete("new/deep.md") is True
        assert shared.delete("new/deep.md") is False
    assert meta_world.pod_tree() == pod_before


def test_compare_and_swap(meta_world: MetaWorld) -> None:
    meta_world.seed("a.md", "one")
    shared = _shared(meta_world)
    sha = shared.blob_sha("a.md")

    with pytest.raises(cmc.CidxMetaConflictError):
        shared.put_text("a.md", "two", expected=None)  # must not exist
    shared.put_text("a.md", "two", expected=sha)
    with pytest.raises(cmc.CidxMetaConflictError):
        shared.put_text("a.md", "three", expected=sha)  # stale
    with pytest.raises(cmc.CidxMetaConflictError):
        shared.delete("a.md", expected=sha)
    assert (meta_world.remote_meta / "a.md").read_text() == "two"


def test_a_writer_failure_raises_unavailable(meta_world: MetaWorld) -> None:
    shared = _shared(meta_world)
    meta_world.writer.fail = FakeWriteError("EUNAVAILABLE", "down")
    with pytest.raises(cmc.CidxMetaUnavailableError, match="down"):
        shared.put_text("a.md", "x")
    with pytest.raises(cmc.CidxMetaUnavailableError):
        shared.delete("a.md")


def test_rename(meta_world: MetaWorld) -> None:
    meta_world.seed("old.md", "body")
    meta_world.seed("taken.md", "keep")
    meta_world.seed("old2.md", "two")
    shared = _shared(meta_world)

    assert shared.rename("absent.md", "x.md", overwrite=False) is False
    assert shared.rename("old2.md", "taken.md", overwrite=False) is False
    assert shared.rename("old.md", "new.md", overwrite=False) is True
    assert shared.rename("old2.md", "taken.md", overwrite=True) is True
    remote = meta_world.remote_tree()
    assert remote == {"new.md": b"body", "taken.md": b"two"}


# ── change detection ─────────────────────────────────────────────────────


def _set_mtime(path: Path, seconds: int) -> None:
    os.utime(path, (seconds, seconds))


def test_changed_since_uses_the_manifest_mtimes(meta_world: MetaWorld) -> None:
    meta_world.seed("a.md", "x")
    meta_world.seed(".hidden/new.md", "x")
    meta_world.seed(".code-indexer/state.json", "x")
    _set_mtime(meta_world.remote_meta / "a.md", 1_000)
    for hidden in (".hidden/new.md", ".code-indexer/state.json"):
        _set_mtime(meta_world.remote_meta / hidden, 9_000)
    shared = _shared(meta_world)
    with meta_world.forbid_local_io():
        assert cmc.changed_since(shared, 999) is True
        assert cmc.changed_since(shared, 1_000) is False  # hidden files ignored
        assert cmc.has_visible_files(shared) is True


def test_unknown_mtime_is_a_change(meta_world: MetaWorld) -> None:
    from code_indexer.server.storage.shared.content_backend import (
        ContentEntry,
        ContentPage,
    )

    shared = _shared(meta_world)
    meta_world.backend.manifest = lambda *a, **k: ContentPage(  # type: ignore[method-assign]
        entries=(ContentEntry("a.md", 1, "sha", False, None),), next_cursor=None
    )
    assert cmc.changed_since(shared, 10**12) is True


def test_empty_shared_content(meta_world: MetaWorld) -> None:
    meta_world.seed(".code-indexer/x.json", "{}")
    shared = _shared(meta_world)
    assert cmc.has_visible_files(shared) is False
    assert cmc.changed_since(shared, 0) is False


def test_has_files_with_extensions(meta_world: MetaWorld) -> None:
    meta_world.seed("docs/a.yaml", "x")
    meta_world.seed("node_modules/b.py", "x")
    meta_world.seed(".hidden/c.rs", "x")
    meta_world.seed(".d.go", "x")  # a hidden FILE counts, as in the local walk
    shared = _shared(meta_world)
    exclude = {"node_modules"}
    assert cmc.has_files_with_extensions(shared, {"yaml"}, exclude) is True
    assert cmc.has_files_with_extensions(shared, {"py"}, exclude) is False
    assert cmc.has_files_with_extensions(shared, {"rs"}, exclude) is False
    assert cmc.has_files_with_extensions(shared, {"go"}, exclude) is True
    assert cmc.has_files_with_extensions(shared, set(), exclude) is False


def test_local_and_shared_extension_scans_agree(meta_world: MetaWorld) -> None:
    from code_indexer.global_repos.refresh_scheduler import has_files_with_extensions

    for rel in ("docs/a.yaml", "node_modules/b.py", ".hidden/c.rs", ".d.go", "e.md"):
        meta_world.seed(rel, "x")
    shared = _shared(meta_world)
    exclude = {"node_modules", "__pycache__"}
    for exts in ({"yaml"}, {"py"}, {"rs"}, {"go"}, {"md"}, {"txt"}):
        assert cmc.has_files_with_extensions(shared, exts, exclude) == (
            has_files_with_extensions(str(meta_world.local_meta), exts, exclude)
        ), exts


# ── backup ───────────────────────────────────────────────────────────────


def test_backup_refused_only_when_delegated(meta_world: MetaWorld) -> None:
    cmc.refuse_backup_of_shared_content(meta_world.local_meta)  # local: no-op
    meta_world.delegated()
    with pytest.raises(cmc.CidxMetaBackupUnsupportedError, match="delegated"):
        cmc.refuse_backup_of_shared_content(meta_world.pod_meta)


def test_backup_sync_and_bootstrap_refuse_shared_content(
    meta_world: MetaWorld,
) -> None:
    from code_indexer.server.services.cidx_meta_backup import (
        CidxMetaBackupBootstrap,
        CidxMetaBackupSync,
    )

    meta_world.delegated()
    with meta_world.forbid_local_io():
        with pytest.raises(cmc.CidxMetaBackupUnsupportedError):
            CidxMetaBackupSync(str(meta_world.pod_meta), "master").sync()
        with pytest.raises(cmc.CidxMetaBackupUnsupportedError):
            CidxMetaBackupBootstrap().bootstrap(str(meta_world.pod_meta), "file:///x")


def test_the_local_io_guard_catches_local_access(meta_world: MetaWorld) -> None:
    """The proof the other tests rely on: local access under the pod's
    cidx-meta raises inside the guard, and only there."""
    import tempfile

    from tests.unit.server.cidx_meta_seam_fakes import LocalIOForbidden

    target = meta_world.pod_meta / "x.md"
    attempts = [
        lambda: target.read_text(),
        lambda: target.write_text("x"),
        lambda: target.exists(),
        lambda: list(meta_world.pod_meta.glob("*.md")),
        lambda: open(target, "w"),
        lambda: list(os.walk(meta_world.pod_meta)),
        lambda: os.replace(meta_world.local_meta / "a", target),
        lambda: tempfile.mkstemp(dir=str(meta_world.pod_meta)),
    ]
    with meta_world.forbid_local_io():
        for attempt in attempts:
            with pytest.raises(LocalIOForbidden):
                attempt()
        assert (meta_world.local_meta / "a.md").exists() is False  # elsewhere: fine
    assert target.exists() is False  # outside the block: unguarded
