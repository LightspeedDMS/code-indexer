"""Story #2122: the per-run provider-input meter of the ``cidx index`` child.

Every test drives the REAL ``cidx`` CLI (subprocess, the tree under test) in
a temporary non-git repository against the local fake VoyageAI server
(scripts/analysis/reembed_repro/fake_voyage_server.py, plain HTTP on
127.0.0.1, sentinel key) under the network guard of
tests/utils/cidx_fake_provider_repo.py: no real provider call is possible.
The fake records the sha256 of every input text it receives, so "sent" is
counted at the provider boundary, never inferred from the child.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Iterator

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2122-fake-key"
_EXIT_CODE_RUN_SPEND_DEFECT = 88

pytestmark = pytest.mark.timeout(240)

#: Text a planning defect sends although no resolver registered it as a miss.
_INJECTED_TEXT = "story-2122 injected text that no resolver registered"

#: Child-side test hook: every batch the indexer submits for embedding
#: carries one extra text the reuse resolver never looked up.
_INJECT_UNREGISTERED_TEXT = f"""
from code_indexer.services import vector_calculation_manager as _vcm

_original_submit = _vcm.VectorCalculationManager.submit_batch_task


def _submit_with_unregistered_text(self, chunk_texts, metadata, *args, **kwargs):
    return _original_submit(
        self, list(chunk_texts) + [{_INJECTED_TEXT!r}], metadata, *args, **kwargs
    )


_vcm.VectorCalculationManager.submit_batch_task = _submit_with_unregistered_text
"""


#: Child-side test hook: every Voyage request is sent a second time (a resend
#: defect). With max_retries 0 the run's allowance is one send per miss.
_RESEND_EVERY_REQUEST = """
from code_indexer.services import voyage_ai as _voyage

_original_request = _voyage.VoyageAIClient._make_sync_request


def _send_twice(self, texts, *args, **kwargs):
    _original_request(self, texts, *args, **kwargs)
    return _original_request(self, texts, *args, **kwargs)


_voyage.VoyageAIClient._make_sync_request = _send_twice
"""


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _run_spend_record(stdout: str) -> Dict[str, Any]:
    """The child's final ``--progress-json`` record (its last stdout line)."""
    lines = [line for line in stdout.splitlines() if line.strip()]
    assert lines, "the child printed nothing on stdout"
    record = json.loads(lines[-1])
    assert record.get("record") == "run_spend", f"last line: {lines[-1]}"
    return dict(record)


def _set_voyage_max_retries(repo: FakeProviderRepo, max_retries: int) -> None:
    config_path = repo.repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["voyage_ai"]["max_retries"] = max_retries
    config_path.write_text(json.dumps(config))


@pytest.fixture
def fake_provider() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def test_unregistered_input_exits_88_without_sending_it(
    tmp_path: Path, fake_provider
) -> None:
    """A text the reuse resolver did not register as a miss is refused
    before HTTP: the child exits 88 and the provider never receives it."""
    repo = FakeProviderRepo(
        tmp_path,
        fake_provider,
        _SENTINEL_KEY,
        extra_sitecustomize=_INJECT_UNREGISTERED_TEXT,
    )
    repo.write_files("defect", 3)
    fake_provider.ledger.begin_run("defect")

    proc = repo.cidx(*repo.index_args(), "--progress-json")

    assert proc.returncode == _EXIT_CODE_RUN_SPEND_DEFECT, (
        f"exit {proc.returncode}; stderr: {proc.stderr[-3000:]}"
    )
    sent = fake_provider.ledger.run_key_counts("defect")
    assert sent.get(_sha256(_INJECTED_TEXT), 0) == 0, (
        "the unregistered text reached the provider"
    )


def test_final_record_reports_inputs_sent_and_zero_on_second_run(
    tmp_path: Path, fake_provider
) -> None:
    """The final ``--progress-json`` record carries exactly what the fake
    received; a second run with nothing new reports ``inputs_sent: 0``."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("spend", 4)

    fake_provider.ledger.begin_run("first")
    proc = repo.cidx(*repo.index_args(), "--progress-json")
    assert proc.returncode == 0, proc.stderr[-3000:]
    first = _run_spend_record(proc.stdout)
    stats = fake_provider.ledger.run_stats("first")
    assert stats["inputs"] > 0
    assert first["inputs_sent"] == stats["inputs"]
    assert first["attempts"] == stats["requests"]

    fake_provider.ledger.begin_run("second")
    proc = repo.cidx(*repo.index_args(), "--progress-json")
    assert proc.returncode == 0, proc.stderr[-3000:]
    second = _run_spend_record(proc.stdout)
    assert second["inputs_sent"] == 0
    assert second["attempts"] == 0
    assert fake_provider.ledger.run_stats("second")["inputs"] == 0


def test_retry_fault_counts_two_attempts_within_bound(
    tmp_path: Path, fake_provider
) -> None:
    """A provider 500 then success is two attempts, both counted, and stays
    within ``misses * (1 + max_retries)``: the run succeeds."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("retry", 1)
    fake_provider.ledger.begin_run("retry")
    fake_provider.ledger.fail_next_requests(1, status=500)

    proc = repo.cidx(*repo.index_args(), "--progress-json")

    assert proc.returncode == 0, proc.stderr[-3000:]
    record = _run_spend_record(proc.stdout)
    stats = fake_provider.ledger.run_stats("retry")
    failed = fake_provider.ledger.failed_stats("retry")
    assert failed["failed_requests"] == 1
    assert stats["requests"] == 1
    assert record["attempts"] == 2
    assert record["inputs_sent"] == stats["inputs"] + failed["failed_inputs"]


def test_resend_beyond_the_allowance_exits_88_before_http(
    tmp_path: Path, fake_provider
) -> None:
    """With max_retries 0 each miss may be sent once: a resend of a batch
    that already succeeded is refused before HTTP and the run exits 88."""
    repo = FakeProviderRepo(
        tmp_path,
        fake_provider,
        _SENTINEL_KEY,
        extra_sitecustomize=_RESEND_EVERY_REQUEST,
    )
    _set_voyage_max_retries(repo, 0)
    repo.write_files("bound", 1)
    fake_provider.ledger.begin_run("bound")

    proc = repo.cidx(*repo.index_args(), "--progress-json")

    assert proc.returncode == _EXIT_CODE_RUN_SPEND_DEFECT, (
        f"exit {proc.returncode}; stderr: {proc.stderr[-3000:]}"
    )
    stats = fake_provider.ledger.run_stats("bound")
    assert stats["requests"] == 1, "the resend reached the provider"
    assert stats["dup_same_run"] == 0
    assert _run_spend_record(proc.stdout)["inputs_sent"] == stats["inputs"]
