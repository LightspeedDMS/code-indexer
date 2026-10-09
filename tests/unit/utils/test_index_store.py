"""The IndexStore seam: where a repository's cidx state lives.

Two things are pinned here:

* **Migrated call sites go through the seam.** With a store whose root is
  somewhere else entirely, every migrated function must read and write THERE --
  not at ``<repo>/.code-indexer``. A site that still joins the literal passes a
  local-only test and fails this one.
* **The ratchet.** Direct ``.code-indexer`` derivations may only go down. Each
  file's count is recorded in ``index_store_ratchet.json``; a file over its
  count, or a new file with any, fails the build. A file UNDER its count fails
  too, until the baseline is lowered -- otherwise the freed headroom would let a
  new derivation in unnoticed.

Counted: string constants with a path component that IS the directory --
``.code-indexer``, ``.code-indexer/...``, ``.../.code-indexer``, ``!.code-indexer/**``.
Docstrings are prose and are not counted.

Lower the baseline after migrating sites:

    PYTHONPATH=src python3 tests/unit/utils/test_index_store.py --write
"""

from __future__ import annotations

import ast
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Union

import pytest

from code_indexer.utils import index_store
from code_indexer.utils.index_store import INDEX_DIRNAME, LocalIndexStore

_SRC = Path(__file__).resolve().parents[3] / "src" / "code_indexer"
_BASELINE = Path(__file__).with_name("index_store_ratchet.json")
#: The seam itself defines the name; it is the one place allowed to.
_EXEMPT = {"utils/index_store.py"}


# ── the ratchet ─────────────────────────────────────────────────────────────


def names_the_index_dir(value: str) -> bool:
    """True when some path component of ``value`` IS the index directory.

    Components, not substrings: ``.code-indexer-override.yaml`` is a different
    file. A leading ``!`` (a negated glob) is not part of the path.
    """
    return INDEX_DIRNAME in value.lstrip("!").split("/")


def _docstring_nodes(tree: ast.Module) -> set:
    found = set()
    for node in ast.walk(tree):
        if isinstance(
            node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)
        ):
            body = getattr(node, "body", [])
            if (
                body
                and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
            ):
                found.add(id(body[0].value))
    return found


def count_derivations(source: str) -> int:
    tree = ast.parse(source)
    docstrings = _docstring_nodes(tree)
    return sum(
        1
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
        and names_the_index_dir(node.value)
    )


def scan() -> Dict[str, int]:
    counts = {}
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if rel in _EXEMPT:
            continue
        n = count_derivations(path.read_text(encoding="utf-8"))
        if n:
            counts[rel] = n
    return counts


def test_direct_index_dir_derivations_only_go_down() -> None:
    baseline = json.loads(_BASELINE.read_text(encoding="utf-8"))
    found = scan()
    grew = {
        f: (baseline.get(f, 0), n) for f, n in found.items() if n > baseline.get(f, 0)
    }
    shrank = {
        f: (n, found.get(f, 0)) for f, n in baseline.items() if found.get(f, 0) < n
    }
    assert not grew, (
        "new direct `.code-indexer` derivations -- ask the IndexStore instead "
        f"(code_indexer.utils.index_store.index_root): {grew}"
    )
    assert not shrank, (
        "sites migrated behind the seam -- lower the baseline so the headroom "
        f"cannot be reused (see this module's docstring): {shrank}"
    )


class TestTheCounter:
    def test_counts_a_join_and_a_glob_but_not_a_docstring(self) -> None:
        src = (
            '"""Lives in .code-indexer/index."""\n'
            "def f(r):\n"
            '    """The .code-indexer dir."""\n'
            '    return r / ".code-indexer", "!.code-indexer/**", "a/.code-indexer"\n'
        )
        assert count_derivations(src) == 3

    def test_an_unrelated_string_is_not_counted(self) -> None:
        assert count_derivations('X = ".code-indexer-override.yaml"\n') == 0

    def test_the_scan_sees_the_known_heavy_files(self) -> None:
        found = scan()
        assert found.get("cli.py", 0) > 10, "the scan is probably broken"


# ── the seam ────────────────────────────────────────────────────────────────


def test_the_local_store_is_todays_layout(tmp_path: Path) -> None:
    assert LocalIndexStore().index_root(tmp_path) == tmp_path / ".code-indexer"
    assert index_store.index_root(str(tmp_path)) == tmp_path / ".code-indexer"


class _ElsewhereStore:
    """An index store whose root is nowhere near the repository."""

    def __init__(self, root: Path) -> None:
        self.root = root

    def index_root(self, repo_path: Union[str, Path]) -> Path:
        return self.root / Path(repo_path).name


@pytest.fixture
def elsewhere(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Any:
    store = _ElsewhereStore(tmp_path / "index-store")
    monkeypatch.setattr(index_store, "_DEFAULT", store)
    repo = tmp_path / "repo"
    repo.mkdir()
    return repo, store.index_root(repo)


def test_trigram_index_location_comes_from_the_store(elsewhere: Any) -> None:
    from code_indexer.global_repos.trigram_index_manager import trigram_index_dir

    repo, root = elsewhere
    assert trigram_index_dir(repo) == root / "trigram_index"


def test_fts_presence_and_skip_marker_come_from_the_store(elsewhere: Any) -> None:
    from code_indexer.server.services import fts_index_builder as fts

    repo, root = elsewhere
    assert not fts.fts_index_present(repo)
    (root / fts.FTS_INDEX_DIRNAME).mkdir(parents=True)
    (root / fts.FTS_INDEX_DIRNAME / fts._FTS_MARKER).write_text("{}")
    (root / fts._SKIP_MARKER).write_text("")
    assert fts.fts_index_present(repo)
    assert fts.fts_backfill_skip_present(repo)
    assert not (repo / ".code-indexer").exists(), "something wrote into the repo"


def test_semantic_query_reads_repo_config_from_the_store(elsewhere: Any) -> None:
    """proxy_mode lives in the repo's config.json; it must be read from the store."""
    import logging

    from code_indexer.server.query.semantic_query_manager import SemanticQueryManager

    repo, root = elsewhere
    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.logger = logging.getLogger("test")
    assert manager._is_composite_repository(repo) is False
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"proxy_mode": True}))
    assert manager._is_composite_repository(repo) is True
    assert not (repo / ".code-indexer").exists()


# ── SCIP locations ─────────────────────────────────────────────────────────


def test_scip_dir_comes_from_the_store(elsewhere: Any) -> None:
    from code_indexer.scip.paths import scip_index_dir

    repo, root = elsewhere
    assert scip_index_dir(repo) == root / "scip"


def test_the_scip_generator_writes_where_the_store_says(elsewhere: Any) -> None:
    from code_indexer.scip.generator import SCIPGenerator

    repo, root = elsewhere
    assert SCIPGenerator(repo).scip_dir == root / "scip"


def test_the_live_golden_scip_db_comes_from_the_store(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.multi import scip_multi_service as sms

    repo, root = elsewhere
    monkeypatch.setattr(sms, "_get_golden_repos_dir", lambda: str(repo.parent))
    service = sms.SCIPMultiService.__new__(sms.SCIPMultiService)
    got = service._live_golden_scip_db_path(f"{repo.name}-global")
    assert got == root / "scip" / "index.scip.db"


def test_scip_query_finds_an_alias_db_through_the_store(
    elsewhere: Any, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The alias points at a versioned snapshot; its SCIP db is found where the
    store keeps the SNAPSHOT's index -- not the live golden dir's, which is empty
    here, so the live-fallback branch cannot answer for it."""
    from code_indexer.server.services.scip_query_service import SCIPQueryService
    from code_indexer.utils import index_store as store_module

    repo, _ = elsewhere
    snapshot = tmp_path / "versioned" / "v_1"
    snapshot.mkdir(parents=True)
    snapshot_root = store_module.index_root(snapshot)
    (snapshot_root / "scip").mkdir(parents=True)
    (snapshot_root / "scip" / "index.scip.db").write_bytes(b"")
    service = SCIPQueryService.__new__(SCIPQueryService)
    service._golden_repos_dir = repo.parent
    monkeypatch.setattr(
        service, "_resolve_alias_scip_root", lambda alias: snapshot, raising=False
    )
    found = service.find_scip_files(repository_alias=repo.name)
    assert found == [snapshot_root / "scip" / "index.scip.db"]
    assert not (snapshot / ".code-indexer").exists()


def test_the_delegated_scip_funnel_clears_the_marker_in_the_store(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from types import SimpleNamespace

    from code_indexer.server.services import scip_lifecycle

    repo, root = elsewhere
    seen: List[Path] = []
    monkeypatch.setattr(
        scip_lifecycle, "remove_scip_build_commit", lambda d: seen.append(Path(d))
    )
    try:
        scip_lifecycle.funnel_scip_generation(
            alias="repo",
            repo_root=repo,
            config=SimpleNamespace(scip_backend="delegated"),
            clear=True,
        )
    except Exception:
        pass  # only the directory it was handed matters here
    assert seen == [root / "scip"]


def test_self_healing_retries_write_scip_into_the_store(elsewhere: Any) -> None:
    import threading
    from types import SimpleNamespace

    from code_indexer.server.services.scip_self_healing import (
        SCIPSelfHealingService,
    )

    repo, root = elsewhere
    (repo / "proj").mkdir()
    job = SimpleNamespace(
        language_resolution_status={
            "proj/": {"language": "no-such-language", "build_system": "x"}
        }
    )
    service = SCIPSelfHealingService.__new__(SCIPSelfHealingService)
    service.repo_root = repo
    service.job_manager = SimpleNamespace(_lock=threading.Lock(), jobs={"j": job})

    service._retry_scip_for_project("j", "proj/")

    assert (root / "scip").is_dir(), "the retry did not create the store's SCIP dir"
    assert not (repo / ".code-indexer").exists(), "the retry wrote into the repo"


def test_the_backfill_freshness_check_reads_the_store(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services import scip_lifecycle
    from code_indexer.server.services.scip_backfill_policy import ScipBackfillPolicy

    repo, root = elsewhere
    seen: List[Path] = []

    def fresh(repo_root: Path, scip_dir: Path) -> bool:
        seen.append(Path(scip_dir))
        return True

    monkeypatch.setattr(scip_lifecycle, "scip_db_is_fresh", fresh)
    policy = ScipBackfillPolicy.__new__(ScipBackfillPolicy)
    assert policy.is_fresh(repo) is True
    assert seen == [root / "scip"]


def test_scip_remote_config_is_read_from_the_store(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer import cli_scip
    from code_indexer.mode_detection import command_mode_detector

    repo, root = elsewhere
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"server_url": "https://x"}))
    monkeypatch.setattr(command_mode_detector, "find_project_root", lambda cwd: repo)

    assert cli_scip._get_remote_config() == {"server_url": "https://x"}


_SCIP_QUERY_COMMANDS = [
    ["status"],
    ["definition", "Foo"],
    ["references", "Foo"],
    ["dependencies", "Foo"],
    ["dependents", "Foo"],
    ["impact", "Foo"],
    ["callchain", "Foo", "Bar"],
    ["context", "Foo"],
]


@pytest.mark.parametrize("args", _SCIP_QUERY_COMMANDS, ids=lambda a: a[0])
def test_every_scip_cli_command_reads_the_stores_scip_dir(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch, args: List[str]
) -> None:
    """Each command loads SCIP status from scip_index_dir(cwd); a site that
    still joined the literal would hand the tracker the repo's own path."""
    from click.testing import CliRunner

    from code_indexer import cli_scip, disabled_commands
    from code_indexer.scip import status as scip_status

    repo, root = elsewhere
    seen: List[Path] = []

    class _Recording:
        def __init__(self, scip_dir: Path) -> None:
            seen.append(Path(scip_dir))

        def load(self) -> Any:
            raise SystemExit(7)

    monkeypatch.setattr(scip_status, "StatusTracker", _Recording)
    # Mode detection is its own (not yet migrated) .code-indexer site; stub it
    # rather than plant a marker inside the repo.
    monkeypatch.setattr(disabled_commands, "detect_current_mode", lambda: "local")
    monkeypatch.chdir(repo)
    CliRunner().invoke(cli_scip.scip_group, args)

    assert seen == [root / "scip"], f"{args[0]}: {seen}"


# ── per-repo config and mode detection ─────────────────────────────────────


def test_config_discovery_finds_the_config_in_the_store(elsewhere: Any) -> None:
    from code_indexer.config import ConfigManager

    repo, root = elsewhere
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    (repo / "sub").mkdir()

    manager = ConfigManager.create_with_backtrack(repo / "sub")

    assert manager.config_path == root / "config.json"
    assert manager.project_root == repo, "discovery did not record the repo"
    assert ConfigManager.find_config_path(repo / "sub") == root / "config.json"


@pytest.mark.parametrize("cwd_gone", [False, True])
def test_discovery_from_the_cwd_records_the_repo(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch, cwd_gone: bool
) -> None:
    """No start_dir: the walk starts at the cwd -- or, when the cwd was deleted,
    at the temp dir -- and the root must still be the repo, not the store's
    parent that config_path.parent.parent would name."""
    import tempfile

    from code_indexer.config import ConfigManager

    repo, root = elsewhere
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    (repo / "sub").mkdir()
    if cwd_gone:
        doomed = repo / "doomed"
        doomed.mkdir()
        monkeypatch.chdir(doomed)
        doomed.rmdir()
        monkeypatch.setattr(tempfile, "tempdir", str(repo / "sub"))
    else:
        monkeypatch.chdir(repo / "sub")

    manager = ConfigManager.create_with_backtrack()

    assert manager.config_path == root / "config.json"
    assert manager.project_root == repo


def test_with_no_config_anywhere_the_default_is_rooted_at_the_start(
    elsewhere: Any,
) -> None:
    from code_indexer.config import ConfigManager

    repo, root = elsewhere

    manager = ConfigManager.create_with_backtrack(repo)

    assert manager.config_path == root / "config.json"
    assert manager.project_root == repo


@pytest.mark.parametrize("socket_base", [None, "custom-sockets"])
def test_the_daemon_socket_is_keyed_by_the_repo_not_the_store(
    elsewhere: Any, monkeypatch: pytest.MonkeyPatch, socket_base: Any
) -> None:
    """Keyed by the store, every repo under one store parent could share a socket."""
    from code_indexer.config import ConfigManager
    from code_indexer.daemon import socket_helper

    repo, root = elsewhere
    root.mkdir(parents=True)
    (root / "config.json").write_text("{}")
    keyed_by: List[Path] = []

    def record(name: str, result: Any) -> None:
        def fake(repo_path: Path, *args: Any) -> Any:
            keyed_by.append(Path(repo_path))
            return result

        monkeypatch.setattr(socket_helper, name, fake)

    record("generate_repo_hash", "hash")
    record("generate_socket_path", repo.parent / "s.sock")
    record("create_mapping_file", None)
    monkeypatch.setattr(socket_helper, "ensure_socket_directory", lambda *a: None)
    manager = ConfigManager.create_with_backtrack(repo)
    base = str(repo.parent / socket_base) if socket_base else None
    daemon = {**ConfigManager.DAEMON_DEFAULTS, "socket_base": base}
    monkeypatch.setattr(manager, "get_daemon_config", lambda: daemon)

    manager.get_socket_path()

    assert keyed_by == [repo, repo], keyed_by


def test_a_relative_codebase_dir_resolves_against_the_repo_not_the_store(
    elsewhere: Any,
) -> None:
    """The reverse mapping config -> repo: with the index elsewhere, the old
    config_path.parent.parent would land in the store, not the repository."""
    from code_indexer.config import ConfigManager

    repo, root = elsewhere
    root.mkdir(parents=True)
    (root / "config.json").write_text(json.dumps({"codebase_dir": "."}))

    config = ConfigManager.create_with_backtrack(repo).load()

    assert config.codebase_dir == repo.resolve()


def test_mode_detection_finds_the_project_through_the_store(elsewhere: Any) -> None:
    from code_indexer.mode_detection.command_mode_detector import (
        CommandModeDetector,
        find_project_root,
    )

    repo, root = elsewhere
    root.mkdir(parents=True)
    (repo / "deep").mkdir()

    assert find_project_root(repo / "deep") == repo
    assert CommandModeDetector(repo).config_dir == root


def test_a_directly_built_manager_keeps_todays_layout(tmp_path: Path) -> None:
    """No recorded root: the local backend's <repo>/.code-indexer/config.json."""
    from code_indexer.config import ConfigManager

    manager = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    assert manager.project_root == tmp_path


if __name__ == "__main__":
    if "--write" in sys.argv:
        _BASELINE.write_text(json.dumps(scan(), indent=1, sort_keys=True) + "\n")
        print(f"wrote {_BASELINE}")
    else:
        print(__doc__)
