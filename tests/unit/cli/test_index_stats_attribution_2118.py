"""Story #2118 slice 1a: a server-spawned `cidx index` child records its
embedding calls with the attribution the server passed it.

The server stamps one ``--stats-attribution=<json>`` argument on every
`cidx index` it spawns (server/utils/index_command_layout.py). The child
validates it BEFORE any provider call (a malformed value fails the run) and
installs it as the process-wide default read by
embedding_call_instrumentation._record_call -- process-wide because the
embedding calls run on VectorCalculationManager pool threads.

Drives the REAL `cidx` CLI (subprocess, the tree under test) in a temporary
git repository against the local fake VoyageAI server
(scripts/analysis/reembed_repro/fake_voyage_server.py, plain HTTP on
127.0.0.1, sentinel key): no real provider call is possible. The child finds
the stats store through CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR (a scratch
solo/SQLite server dir) exactly as a server-spawned child does, and the rows
are read back from that real SQLite database.
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path
from typing import Dict, Iterator, List

import pytest

import code_indexer
from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallRecord,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.embedding_stats_attribution import (
    StatsAttribution,
)
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SRC_DIR = Path(code_indexer.__file__).resolve().parents[1]
_SENTINEL_KEY = "issue-2118-fake-key"
_CLI_TIMEOUT_SECONDS = 120

pytestmark = pytest.mark.timeout(120)

_ATTRIBUTION = StatsAttribution(
    golden_repo_alias="example-repo-global",
    job_id="job-5678",
    node_id="node-a",
    purpose="refresh",
)


@pytest.fixture
def fake_provider() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


class _Child:
    def __init__(self, root: Path, server) -> None:
        self.server = server
        self.repo = root / "example-repo"
        (self.repo / "src").mkdir(parents=True)
        (self.repo / "src" / "service.py").write_text(
            "class ExampleService:\n    def run(self):\n        return 'ok'\n"
        )
        (self.repo / "src" / "util.py").write_text("def helper(x):\n    return x\n")
        home = root / "home"
        home.mkdir()
        self.server_dir = root / "server-dir"
        self.server_dir.mkdir()
        (self.server_dir / "config.json").write_text(
            json.dumps({"server_dir": str(self.server_dir), "storage_mode": "sqlite"})
        )
        self.env: Dict[str, str] = {
            "PATH": os.environ["PATH"],
            "HOME": str(home),
            "PYTHONPATH": str(_SRC_DIR),
            "PYTHONUSERBASE": site.getuserbase(),
            "VOYAGE_API_KEY": _SENTINEL_KEY,
            "CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR": str(self.server_dir),
        }
        self._git("init", "-q")
        self._git("add", "-A")
        self._git(
            "-c",
            "user.name=Example",
            "-c",
            "user.email=dev@example.com",
            "commit",
            "-qm",
            "initial",
        )
        self.cidx("init")
        config_path = self.repo / ".code-indexer" / "config.json"
        config = json.loads(config_path.read_text())
        config.setdefault("voyage_ai", {})["api_endpoint"] = (
            f"{server.base_url}/v1/embeddings"
        )
        config_path.write_text(json.dumps(config))

    def _git(self, *args: str) -> None:
        subprocess.run(["git", *args], cwd=self.repo, check=True, capture_output=True)

    def cidx(self, *args: str) -> "subprocess.CompletedProcess[str]":
        return subprocess.run(
            [sys.executable, "-m", "code_indexer.cli", *args],
            cwd=self.repo,
            env=self.env,
            capture_output=True,
            text=True,
            timeout=_CLI_TIMEOUT_SECONDS,
        )

    def rows(self) -> List[EmbeddingCallRecord]:
        db_path = self.server_dir / "data" / "cidx_server.db"
        return EmbeddingCallStatsSqliteBackend(str(db_path)).query(limit=1000)


def test_child_rows_carry_the_passed_attribution(tmp_path: Path, fake_provider) -> None:
    child = _Child(tmp_path, fake_provider)
    fake_provider.ledger.begin_run("attributed")

    proc = child.cidx("index", f"--stats-attribution={_ATTRIBUTION.to_json()}")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_stats("attributed")["inputs"] > 0
    rows = [r for r in child.rows() if r.call_type == "embed"]
    assert rows, "the child recorded no embedding call"
    assert {(r.golden_repo_alias, r.job_id, r.node_id, r.purpose) for r in rows} == {
        ("example-repo-global", "job-5678", "node-a", "refresh")
    }


@pytest.mark.parametrize(
    "value",
    [
        "not-json",
        '{"golden_repo_alias":"a","job_id":"j","node_id":"n","purpose":"query"}',
        '{"golden_repo_alias":"a","job_id":"j","node_id":"n"}',
    ],
)
def test_malformed_attribution_fails_before_any_provider_call(
    tmp_path: Path, fake_provider, value: str
) -> None:
    child = _Child(tmp_path, fake_provider)
    fake_provider.ledger.begin_run("malformed")

    proc = child.cidx("index", f"--stats-attribution={value}")

    assert proc.returncode != 0
    assert "stats-attribution" in proc.stderr + proc.stdout
    assert fake_provider.ledger.run_stats("malformed")["inputs"] == 0
    assert not (child.repo / ".code-indexer" / "index").exists() or not any(
        (child.repo / ".code-indexer" / "index").iterdir()
    )
