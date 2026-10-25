from __future__ import annotations

import json
from pathlib import Path
from typing import Iterator

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo, unique_python_text
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2198-fake-key"
_LEASE_TOKEN = "quokkaleasetokenword"
_LEASE_DIR = ".snapshot-reader-leases"

pytestmark = pytest.mark.timeout(240)


@pytest.fixture
def fake_provider() -> Iterator[object]:
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


# ^ why flag-gated exclusion off, not a seeded store; the child itself builds the rows an older release stored
def _legacy_exclusion_off(flag: Path) -> str:
    return (
        "import os\n"
        f"if os.path.exists({str(flag)!r}):\n"
        "    import code_indexer.indexing.file_finder as _ff\n"
        "    import code_indexer.services.override_filter_service as _ofs\n"
        "    _ff.in_never_indexed_directory = lambda path: False\n"
        "    _ofs.is_never_indexed = lambda path: False\n"
    )


def _catalog_repo(tmp_path: Path, server, flag: Path) -> FakeProviderRepo:
    repo = FakeProviderRepo(
        tmp_path, server, _SENTINEL_KEY, extra_sitecustomize=_legacy_exclusion_off(flag)
    )
    (repo.repo / "src" / "catalog_entry.py").write_text(
        unique_python_text("catalog_entry", 6)
    )
    leases = repo.repo / _LEASE_DIR
    leases.mkdir()
    (leases / "0123abcd-node-1-4567-lease.json").write_text(
        json.dumps(
            {
                "snapshot_path": f"/srv/example/{_LEASE_TOKEN}/v_1",
                "lease_id": f"node-1-{_LEASE_TOKEN}",
                "updated_at": 1.0,
                "ttl_seconds": 120.0,
            }
        )
    )
    return repo


def _lease_rows(repo: FakeProviderRepo) -> list:
    return [p for p in repo.stored_rows().values() if _LEASE_DIR in p]


def test_lease_file_is_never_stored_or_searchable(
    tmp_path: Path, fake_provider
) -> None:
    repo = _catalog_repo(tmp_path, fake_provider, tmp_path / "legacy-flag")

    proc = repo.cidx(*repo.index_args(), "--fts")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _lease_rows(repo) == []
    assert any(p.endswith("catalog_entry.py") for p in repo.stored_rows().values())
    fts = repo.query(_LEASE_TOKEN, "--fts")
    assert fts.returncode == 0, fts.stderr[-2000:]
    assert _LEASE_DIR not in fts.stdout, fts.stdout[-2000:]


def test_lease_row_from_an_older_run_leaves_at_the_next_reconcile(
    tmp_path: Path, fake_provider
) -> None:
    flag = tmp_path / "legacy-flag"
    repo = _catalog_repo(tmp_path, fake_provider, flag)
    flag.write_text("on")
    legacy = repo.cidx(*repo.index_args(), "--fts")
    flag.unlink()
    assert legacy.returncode == 0, legacy.stderr[-3000:]
    assert _lease_rows(repo), "the simulated older run did not index the lease"
    assert _LEASE_DIR in repo.query(_LEASE_TOKEN, "--fts").stdout

    proc = repo.cidx(*repo.index_args(), "--fts", "--reconcile")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert _lease_rows(repo) == []
    assert any(p.endswith("catalog_entry.py") for p in repo.stored_rows().values())
    fts = repo.query(_LEASE_TOKEN, "--fts")
    assert fts.returncode == 0, fts.stderr[-2000:]
    assert _LEASE_DIR not in fts.stdout, fts.stdout[-2000:]
