import json
import subprocess
import threading
import time
from pathlib import Path
from typing import List

import pytest

from perf_suite import cli, client
from perf_suite.corpus import DEFAULT_CORPUS_PATH, load_corpus

from .live_server import free_port, running_cidx_server_process

SEEDED_ADMIN = ("admin", "admin")
EXPECTED_LIMIT = 20
KILL_AFTER_REQUESTS = 12
KILL_WAIT_DEADLINE_S = 120.0


def _sent(perf: client.PerfClient) -> int:
    return sum(perf.tally().values())


@pytest.mark.timeout(60)
def test_breaker_trips_after_consecutive_connection_failures_and_stops_sending():
    perf = client.PerfClient(
        f"http://127.0.0.1:{free_port()}", "example-user", "example-pass"
    )
    try:
        perf._token = "example-token"
        perf._token_obtained_at = float("inf")
        limit = client.CONNECTION_LOSS_LIMIT
        for _ in range(limit - 1):
            with pytest.raises(client.TransportError) as failed:
                perf.rest("GET", "/health")
            assert failed.value.connection_lost
        with pytest.raises(client.TargetUnreachableError, match="unreachable"):
            perf.rest("GET", "/health")
        assert perf.unreachable is not None
        with pytest.raises(client.TargetUnreachableError):
            perf.rest("GET", "/health")
        assert _sent(perf) == limit
    finally:
        perf.close()


def _kill_when_busy(
    proc: "subprocess.Popen[bytes]", perf: client.PerfClient, at_death: List[int]
) -> None:
    deadline = time.monotonic() + KILL_WAIT_DEADLINE_S
    while _sent(perf) < KILL_AFTER_REQUESTS and time.monotonic() < deadline:
        time.sleep(0.01)
    proc.kill()
    proc.wait()
    at_death.append(_sent(perf))


@pytest.mark.slow
@pytest.mark.timeout(600)
def test_killed_target_aborts_run_writes_report_and_exits_1(tmp_path: Path):
    out_dir = tmp_path / "out"
    at_death: List[int] = []
    with running_cidx_server_process(tmp_path / "home") as (url, proc):
        perf = client.PerfClient(url, *SEEDED_ADMIN)
        try:
            perf.login()
            health_before, version = cli._health(perf)
            args = cli.parse_args(
                ["--url", url, "--username", "admin", "--password", "x", "--quick"]
                + ["--output-dir", str(out_dir)]
            )
            corpus = load_corpus(DEFAULT_CORPUS_PATH)
            aliases = {repo.url: "example-repo-global" for repo in corpus.repos}
            killer = threading.Thread(
                target=_kill_when_busy, args=(proc, perf, at_death)
            )
            killer.start()
            code = cli.measure_and_report(
                perf, corpus, aliases, 1, health_before, version, None, args
            )
            killer.join(timeout=KILL_WAIT_DEADLINE_S)
            sent_total = _sent(perf)
        finally:
            perf.close()
    assert code == cli.EXIT_FAIL
    assert at_death, "the server was never killed"
    assert client.CONNECTION_LOSS_LIMIT == EXPECTED_LIMIT
    # ^ invariant after the kill: at most the limit, one in-flight request (quick = c=1), one closing health
    assert sent_total - at_death[0] <= EXPECTED_LIMIT + 1 + 1, sent_total - at_death[0]
    saved = json.loads(next(out_dir.glob("perf_*.json")).read_text())
    assert saved["target_unreachable"]
    assert any("target unreachable" in r for r in saved["verdict"]["reasons"])
    cells = [
        cell
        for doors in saved["scenarios"].values()
        for by_conc in doors.values()
        for cell in (by_conc or {}).values()
    ]
    assert any(c["aborted"] == cli.NOT_RUN_UNREACHABLE for c in cells)
    assert saved["health_after"].startswith("unavailable")
    assert "target unreachable" in next(out_dir.glob("perf_*.md")).read_text()
