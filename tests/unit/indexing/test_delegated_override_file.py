"""A repository's own .code-indexer-override.yaml reaches a delegated pass.

``ConfigManager.load`` finds the override by probing this machine's disk
(``_find_override_file``). On a pod without the repository that probe never
finds it, so a delegated index silently ignored the repository's override --
different files indexed than the same repository indexed locally. The pass now
fetches it through the content backend (``FileFinder.set_manifest``).

Pinned: discovery under a delegated manifest equals the local walk with the
override applied; nothing reads the disk for it; a backend that cannot supply
it fails the pass; a malformed one is ignored with a warning, as locally; the
local load is byte-identical.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, List, Sequence, Set

import pytest

from code_indexer.config import (
    Config,
    IndexingConfig,
    _load_override_config,
    override_config_from_bytes,
)
from code_indexer.indexing.file_facts import ContentUnavailableError
from code_indexer.indexing.file_finder import FileFinder, attach_delegated_manifest
from code_indexer.indexing.repo_manifest import RepoManifest
from code_indexer.server.storage.shared.content_backend import (
    FetchedFile,
    LocalContentBackend,
)

OVERRIDE = b"""add_extensions: [txt]
remove_extensions: []
add_exclude_dirs: [generated]
add_include_dirs: []
force_include_patterns: ["vendor/keep/**"]
force_exclude_patterns: ["secret.py"]
"""


class _Delegated:
    """Serves ``source``'s files whatever root it is asked about."""

    is_local = False

    def __init__(self, source: Path) -> None:
        self._source = source
        self._inner = LocalContentBackend()
        self.fetches: List[List[str]] = []
        self.manifests: List[Any] = []

    def manifest(self, repo_path: Any, **k: Any) -> Any:
        self.manifests.append(k.get("prune_dirs"))
        return self._inner.manifest(self._source, **k)

    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        self.fetches.append(list(paths))
        return self._inner.fetch(self._source, paths)


class _OverrideFetchFails(_Delegated):
    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        if ".code-indexer-override.yaml" in paths:
            raise ConnectionError("workspace-mcp unreachable")
        return super().fetch(repo_path, paths)


class _OverrideRecordError(_Delegated):
    def fetch(self, repo_path: Any, paths: Sequence[str]) -> Iterator[FetchedFile]:
        if ".code-indexer-override.yaml" in paths:
            return iter(
                [
                    FetchedFile(
                        path=".code-indexer-override.yaml",
                        data=b"",
                        blob_sha=None,
                        error="EACCES",
                    )
                ]
            )
        return super().fetch(repo_path, paths)


def _write(root: Path, rel: str, data: bytes = b"x = 1\n") -> None:
    target = root / rel
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    _write(root, "main.py")
    _write(root, "secret.py")
    _write(root, "notes.txt", b"hello\n")
    _write(root, "generated/out.py")
    _write(root, "vendor/lib.py")
    _write(root, "vendor/keep/kept.py")
    _write(root, ".code-indexer-override.yaml", OVERRIDE)
    return root


def _config(root: Path) -> Config:
    config = Config(
        codebase_dir=root,
        file_extensions=["py"],
        exclude_dirs=["vendor"],
        indexing=IndexingConfig(max_file_size=10_000),
    )
    config.confine_to_codebase_root()
    return config


def _relative(root: Path, found: Iterator[Path]) -> Set[str]:
    return {p.relative_to(root).as_posix() for p in found}


def _local_walk_with_override(repo: Path) -> Set[str]:
    config = _config(repo)
    config.override_config = _load_override_config(repo / ".code-indexer-override.yaml")
    return _relative(repo, FileFinder(config).find_files())


def test_delegated_discovery_applies_the_repositorys_override(
    repo: Path, tmp_path: Path
) -> None:
    walked = _local_walk_with_override(repo)
    assert walked == {"main.py", "vendor/keep/kept.py"}, (
        "fixture: the override must change the local answer"
    )

    absent = tmp_path / "not-mounted"  # nothing here to probe or read
    backend = _Delegated(repo)
    finder = FileFinder(_config(absent))
    assert finder.config.override_config is None, "the local probe found nothing"
    attach_delegated_manifest(finder, backend, absent)

    assert _relative(absent, finder.find_files()) == walked


def test_clearing_the_manifest_restores_the_locally_loaded_override(
    repo: Path, tmp_path: Path
) -> None:
    absent = tmp_path / "not-mounted"
    finder = FileFinder(_config(absent))
    local_patterns = finder._base_patterns
    backend = _Delegated(repo)
    finder.set_manifest(RepoManifest.build(absent, backend=backend), backend)
    assert finder.config.override_config is not None

    finder.set_manifest(None)

    assert finder.config.override_config is None
    assert finder.override_filter_service is None
    assert finder._base_patterns is local_patterns


def test_a_pruned_manifest_is_rebuilt_when_the_override_force_includes(
    repo: Path, tmp_path: Path
) -> None:
    """vendor/ is pruned by name before the override is known; its
    force-include pattern rescues vendor/keep, so the manifest is rebuilt."""
    absent = tmp_path / "not-mounted"
    backend = _Delegated(repo)
    finder = FileFinder(_config(absent))

    attach_delegated_manifest(finder, backend, absent)

    assert len(backend.manifests) == 2, "the pruned manifest was not rebuilt"
    assert "vendor" in (backend.manifests[0] or [])
    assert not backend.manifests[1]
    assert "vendor/keep/kept.py" in _relative(absent, finder.find_files())


def test_without_an_override_file_nothing_extra_is_fetched(
    repo: Path, tmp_path: Path
) -> None:
    (repo / ".code-indexer-override.yaml").unlink()
    absent = tmp_path / "not-mounted"
    backend = _Delegated(repo)
    finder = FileFinder(_config(absent))
    attach_delegated_manifest(finder, backend, absent)

    assert not any(".code-indexer-override.yaml" in f for f in backend.fetches)
    assert finder.config.override_config is None


def test_a_backend_that_cannot_fetch_the_override_fails_the_pass(
    repo: Path, tmp_path: Path
) -> None:
    absent = tmp_path / "not-mounted"
    backend = _OverrideFetchFails(repo)
    finder = FileFinder(_config(absent))
    with pytest.raises(ConnectionError):
        finder.set_manifest(RepoManifest.build(absent, backend=backend), backend)


def test_an_override_the_backend_reports_unreadable_fails_the_pass(
    repo: Path, tmp_path: Path
) -> None:
    absent = tmp_path / "not-mounted"
    backend = _OverrideRecordError(repo)
    finder = FileFinder(_config(absent))
    with pytest.raises(ContentUnavailableError, match="EACCES"):
        finder.set_manifest(RepoManifest.build(absent, backend=backend), backend)


def test_a_malformed_override_is_ignored_with_a_warning_as_locally(
    repo: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    (repo / ".code-indexer-override.yaml").write_bytes(b"add_extensions: [txt]\n")
    absent = tmp_path / "not-mounted"
    backend = _Delegated(repo)
    finder = FileFinder(_config(absent))
    finder.set_manifest(RepoManifest.build(absent, backend=backend), backend)

    assert finder.config.override_config is None
    assert "Failed to load override config" in caplog.text


def test_the_local_load_is_unchanged(repo: Path) -> None:
    """Same parsed value and same error messages as before the split."""
    path = repo / ".code-indexer-override.yaml"
    assert _load_override_config(path) == override_config_from_bytes(
        OVERRIDE, str(path)
    )
    path.write_bytes(b"[not, a, dict]\n")
    with pytest.raises(Exception) as local:
        _load_override_config(path)
    assert str(local.value) == (
        f"Failed to load override config from {path}: "
        "Override config must be a YAML dictionary"
    )
    path.write_bytes(b"a: [unclosed\n")
    with pytest.raises(Exception, match="Failed to parse override YAML file"):
        _load_override_config(path)
