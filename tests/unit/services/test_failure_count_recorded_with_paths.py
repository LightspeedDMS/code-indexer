"""EVO-64616: a run's ``failed_files`` count always comes with its paths.

A count with no recorded path (``failed_files`` > ``failed_file_paths``)
cannot be retried file by file. The full-index path used to leave exactly
that: it added the run's failures to the counter and never recorded which
files failed (on tools-test, jplanner had failed_files=5605 and an empty
list). Now every completed run -- full, incremental, reconcile -- records its
failed paths and sets the counter to THIS run's count, so a clean run clears
it.

Drives the REAL ``cidx`` CLI in a temporary non-git repository against the
local fake VoyageAI server; the failing provider is an unroutable loopback
endpoint with retries off.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "evo-64616-fake-key"
_DEAD_ENDPOINT = "http://127.0.0.1:9/v1/embeddings"

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def fake_provider() -> Iterator[object]:
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _set_endpoint(repo: FakeProviderRepo, endpoint: str) -> None:
    config_path = repo.repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["voyage_ai"]["api_endpoint"] = endpoint
    config["voyage_ai"]["max_retries"] = 0
    config_path.write_text(json.dumps(config))


def test_a_failing_full_run_records_every_failed_path_and_a_clean_run_clears_them(
    tmp_path: Path, fake_provider
) -> None:
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", 3)
    live_endpoint = json.loads(
        (repo.repo / ".code-indexer" / "config.json").read_text()
    )["voyage_ai"].get("api_endpoint")
    _set_endpoint(repo, _DEAD_ENDPOINT)

    failing = repo.cidx(*repo.index_args())

    assert failing.returncode == 1, failing.stdout[-2000:]
    metadata = repo.metadata()
    recorded = metadata["failed_file_paths"]
    assert isinstance(recorded, list)
    failed = sorted(Path(str(p)).name for p in recorded)
    assert failed == ["base_000.py", "base_001.py", "base_002.py"]
    assert metadata["failed_files"] == 3, "a count without its paths"

    _set_endpoint(repo, live_endpoint)
    clean = repo.cidx(*repo.index_args())

    assert clean.returncode == 0, clean.stdout[-2000:]
    metadata = repo.metadata()
    assert metadata["failed_file_paths"] == []
    assert metadata["failed_files"] == 0, "a clean run kept the stale count"
