"""Story #2118 slice 1a: the branch-change re-index child stores its rows.

``GoldenRepoManager._cb_cidx_index`` spawns `cidx index --fts` with a
``--stats-attribution``; the child can only WRITE rows when its environment
carries CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR (the server dir), exactly like
every other server `cidx index` spawn. Without it the child installs a
NoOpWriter and the attribution is lost.

Real pieces: the spawn site itself, the `cidx` child (the tree under test),
the stats SQLite DB the child writes, read back with the real backend.

Network barrier (process boundary): `cidx` on PATH is a shim that runs the
child inside its own network namespace (``unshare -rn``) whose only
interface is loopback, so no request can leave the machine whatever the
child's HTTP client does. The local fake provider is started INSIDE that
namespace, on a namespace-private port. The shim records a self-check: a
UDP connect to TEST-NET-1 (needs only a route; sends nothing) must fail
inside the namespace, while the test proves the same connect succeeds
outside it, so the check discriminates.

Other stand-ins: the server config service returns a ServerConfig for a
scratch solo server dir (never ~/.cidx-server), and the shim drops ONLY
``--server-managed-provider-settings``, which pins the provider endpoint
URL (the namespace makes it unreachable anyway; the fake is what answers).
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
from pathlib import Path

import pytest

import code_indexer
from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager
from code_indexer.server.services import config_service
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.utils.config_manager import ServerConfig

_SENTINEL_KEY = "issue-2118-fake-key"
_SRC_DIR = Path(code_indexer.__file__).resolve().parents[1]
_HARNESS_DIR = (
    Path(__file__).resolve().parents[4] / "scripts" / "analysis" / "reembed_repro"
)
#: Namespace-private: nothing else listens inside the child's namespace.
_FAKE_PORT = 18118
#: RFC 5737 TEST-NET-1, never a real destination.
_PROBE = ("192.0.2.1", 9)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(120)]

_INNER = """
import json, socket, subprocess, sys
sys.path.insert(0, {harness!r})
from fake_voyage_server import FakeVoyageServer

subprocess.run(["ip", "link", "set", "lo", "up"], check=True)
probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
try:
    probe.connect({probe!r})
    barrier = "open"
except OSError:
    barrier = "closed"
finally:
    probe.close()
server = FakeVoyageServer({key!r})
server.start("127.0.0.1", {port})
server.ledger.begin_run("child")
try:
    args = [a for a in sys.argv[2:] if a != "--server-managed-provider-settings"]
    rc = subprocess.run([sys.executable, "-m", "code_indexer.cli", *args]).returncode
    report = {{
        "argv": args[:1],
        "barrier": barrier,
        "inputs": server.ledger.run_stats("child")["inputs"],
        "violations": server.ledger.violations(),
    }}
    with open(sys.argv[1], "a") as fh:
        fh.write(json.dumps(report) + "\\n")
finally:
    server.stop()
sys.exit(rc)
"""


def _install_namespaced_cidx(bin_dir: Path, report: Path) -> None:
    bin_dir.mkdir()
    inner = bin_dir / "inner.py"
    inner.write_text(
        _INNER.format(
            harness=str(_HARNESS_DIR), probe=_PROBE, key=_SENTINEL_KEY, port=_FAKE_PORT
        )
    )
    shim = bin_dir / "cidx"
    shim.write_text(
        f"#!{sys.executable}\n"
        "import os, sys\n"
        "os.execvp('unshare', ['unshare', '-rn', '--', "
        f"{sys.executable!r}, {str(inner)!r}, {str(report)!r}, *sys.argv[1:]])\n"
    )
    shim.chmod(0o755)


def _make_base_clone(path: Path) -> None:
    (path / "src").mkdir(parents=True)
    (path / "src" / "service.py").write_text(
        "class ExampleService:\n    def run(self):\n        return 'ok'\n"
    )
    git = ["git", "-c", "user.name=Example", "-c", "user.email=dev@example.com"]
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run([*git, "commit", "-qm", "initial"], cwd=path, check=True)
    subprocess.run(["cidx", "init"], cwd=path, check=True, capture_output=True)
    config_path = path / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config.setdefault("voyage_ai", {})["api_endpoint"] = (
        f"http://127.0.0.1:{_FAKE_PORT}/v1/embeddings"
    )
    config_path.write_text(json.dumps(config))


class _ConfigService:
    def __init__(self, config: ServerConfig) -> None:
        self._config = config

    def get_config(self) -> ServerConfig:
        return self._config


def _probe_connects_outside_namespace() -> bool:
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        probe.connect(_PROBE)
        return True
    except OSError:
        return False
    finally:
        probe.close()


def test_branch_change_index_child_stores_attributed_rows(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert shutil.which("unshare") and shutil.which("ip"), (
        "this test needs `unshare` and `ip` to isolate the child's network"
    )
    assert _probe_connects_outside_namespace(), (
        "the barrier self-check cannot discriminate on a host without a route"
    )
    server_dir = tmp_path / "server-dir"
    server_dir.mkdir()
    (server_dir / "config.json").write_text(
        json.dumps({"server_dir": str(server_dir), "storage_mode": "sqlite"})
    )
    monkeypatch.setattr(
        config_service,
        "get_config_service",
        lambda: _ConfigService(
            ServerConfig(server_dir=str(server_dir), storage_mode="sqlite")
        ),
    )
    report = tmp_path / "child-report.jsonl"
    _install_namespaced_cidx(tmp_path / "bin", report)
    monkeypatch.setenv("PATH", f"{tmp_path / 'bin'}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("PYTHONPATH", str(_SRC_DIR))
    monkeypatch.setenv("VOYAGE_API_KEY", _SENTINEL_KEY)
    monkeypatch.delenv("CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR", raising=False)
    base_clone = tmp_path / "golden-repos" / "example-repo"
    _make_base_clone(base_clone)

    GoldenRepoManager(data_dir=str(tmp_path / "data"))._cb_cidx_index(str(base_clone))

    [index_run] = [
        r
        for r in map(json.loads, report.read_text().splitlines())
        if r["argv"] == ["index"]
    ]
    assert index_run["barrier"] == "closed", "the child could reach the network"
    assert index_run["violations"] == []
    assert index_run["inputs"] > 0, "the branch-change child sent nothing"
    db_path = server_dir / "data" / "cidx_server.db"
    assert db_path.exists(), "the child wrote no embedding_call_stats store"
    rows = [
        r
        for r in EmbeddingCallStatsSqliteBackend(str(db_path)).query(limit=1000)
        if r.call_type == "embed"
    ]
    assert rows, "the branch-change child stored no embedding_call_stats row"
    assert {(r.golden_repo_alias, r.job_id, r.purpose) for r in rows} == {
        ("example-repo-global", "nojob:golden_repo_change_branch", "index")
    }
