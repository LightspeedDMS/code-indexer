"""#2324: two server watches on one workspace (two workers, or two nodes on
shared storage) converge: a change one watch already applied is done for the
other, never an ERROR retried for the watch's lifetime.

Real pieces, in a guarded child: two watch handlers exactly as the server's
auto-watch builds them (`DaemonWatchManager._create_watch_handler`,
server_watch=True), each with its own long-lived SmartIndexer and vector
store, over one CHUNKS_DB workspace indexed by the real `cidx` CLI; the
embedding endpoint is the local fake.
"""

from __future__ import annotations

import json
import subprocess
import sys
from typing import Dict, List

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.unit.services.test_watch_cycle_searchable_2093 import (  # noqa: F401
    indexed_repo,
)

pytestmark = [pytest.mark.slow, pytest.mark.timeout(600)]

FIRST = "def gamma_first_marker():\n    return 'gamma GAMMAFIRSTTOKEN body'\n"
SECOND = "def gamma_second_marker():\n    return 'gamma GAMMASECONDTOKEN body'\n"

TWO_WATCHES = r"""
import json, logging, sys
from pathlib import Path
from code_indexer.daemon.watch_manager import DaemonWatchManager

repo, scenario, first, second = Path(sys.argv[1]), sys.argv[2], sys.argv[3], sys.argv[4]
errors = []


class Errors(logging.Handler):
    def emit(self, record):
        errors.append(f"{record.name}: {record.getMessage()}")


logging.getLogger().addHandler(Errors(level=logging.ERROR))
gamma = repo / "src" / "gamma.py"
a = DaemonWatchManager()._create_watch_handler(str(repo), None, server_watch=True)
b = DaemonWatchManager()._create_watch_handler(str(repo), None, server_watch=True)


def cycle(handler):
    handler.queue_changes([gamma])
    handler._process_pending_changes()


gamma.write_text(first)
cycle(a)
if scenario == "create_edit":
    cycle(b)
gamma.write_text(second)
cycle(b)
if scenario == "create_edit":
    cycle(a)
else:
    gamma.unlink()
    cycle(a)
    cycle(b)
owed = {}
for name, handler in (("a", a), ("b", b)):
    owed[name] = sorted(str(p) for p in handler._change_backoff.scheduled())
    handler.stop_watching()
print("RESULT " + json.dumps({"errors": errors, "owed": owed}))
"""


def _run(repo: FakeProviderRepo, scenario: str) -> Dict[str, object]:
    proc = subprocess.run(
        [sys.executable, "-c", TWO_WATCHES, str(repo.repo), scenario, FIRST, SECOND],
        cwd=repo.repo,
        env=repo.env,
        capture_output=True,
        text=True,
        timeout=400,
    )
    blocked = (
        repo.blocked_network_log.read_text()
        if repo.blocked_network_log.exists()
        else ""
    )
    assert blocked == "", f"two watches attempted network access: {blocked}"
    assert proc.returncode == 0, proc.stderr[-4000:]
    lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
    assert lines, proc.stdout[-3000:] + proc.stderr[-3000:]
    result: Dict[str, object] = json.loads(lines[-1][len("RESULT ") :])
    return result


def _search(repo: FakeProviderRepo, *args: str) -> str:
    proc = repo.query(*args, "--limit", "50")
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout + proc.stderr


def _hits(repo: FakeProviderRepo, token: str) -> List[str]:
    hits = []
    if token in _search(repo, f"gamma {token} body"):
        hits.append("semantic")
    # ^ why the file name, not the token; `cidx query --fts` output lists matching paths
    if "gamma.py" in _search(repo, token, "--fts"):
        hits.append("fts")
    return hits


def test_delete_already_applied_elsewhere_converges_quietly(
    indexed_repo: FakeProviderRepo,  # noqa: F811
) -> None:
    result = _run(indexed_repo, "delete")

    assert result["errors"] == []
    assert result["owed"] == {"a": [], "b": []}
    assert _hits(indexed_repo, "GAMMAFIRSTTOKEN") == []
    assert _hits(indexed_repo, "GAMMASECONDTOKEN") == []
    assert "KEEPTOKEN" in _search(indexed_repo, "keep marker KEEPTOKEN")


def test_create_and_edit_already_applied_elsewhere_converge_once(
    indexed_repo: FakeProviderRepo,  # noqa: F811
) -> None:
    indexed_repo.server.ledger.begin_run("two-watches")

    result = _run(indexed_repo, "create_edit")
    inputs = indexed_repo.server.ledger.run_stats("two-watches")["inputs"]

    assert result["errors"] == []
    assert result["owed"] == {"a": [], "b": []}
    assert inputs == 2
    assert _hits(indexed_repo, "GAMMAFIRSTTOKEN") == []
    assert _hits(indexed_repo, "GAMMASECONDTOKEN") == ["semantic", "fts"]
