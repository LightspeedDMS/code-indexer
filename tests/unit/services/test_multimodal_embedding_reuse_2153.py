"""Bug #2153: an image-bearing chunk already committed in a multimodal
collection's store is never sent to the multimodal provider again.

Every test drives the REAL ``cidx`` CLI (subprocess, the tree under test) in
a temporary non-git repository against the local fake VoyageAI server
(scripts/analysis/reembed_repro/fake_voyage_server.py, plain HTTP on
127.0.0.1, sentinel key) under the network guard of
tests/utils/cidx_fake_provider_repo.py: no real provider call is possible.
``VoyageMultimodalClient`` pins its endpoint to the vendor host, so a child
hook re-points it at the fake's ``/v1/multimodalembeddings`` (routing only;
the request and response bodies are the real protocol). The fake counts each
multimodal input by the sha256 of its text, so "sent" is counted at the
provider boundary.

The shared code path is ``FileChunkingManager``'s multimodal branch, used by
both multimodal clients (Voyage and Cohere); only the Voyage client can be
driven against the fake.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Sequence

import pytest

from tests.utils.cidx_fake_provider_repo import FakeProviderRepo
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2153-fake-key"
_DOC_COUNT = 4

pytestmark = pytest.mark.timeout(240)


def _route_multimodal_to(base_url: str) -> str:
    """Child-side hook: the multimodal client talks to the fake."""
    return f"""
from code_indexer.services import voyage_multimodal as _vm

_original_init = _vm.VoyageMultimodalClient.__init__


def _init_on_fake(self, *args, **kwargs):
    _original_init(self, *args, **kwargs)
    self.config.api_endpoint = {base_url + "/v1/multimodalembeddings"!r}


_vm.VoyageMultimodalClient.__init__ = _init_on_fake
"""


@pytest.fixture
def fake_provider() -> Iterator[Any]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _repo(root: Path, server: Any) -> FakeProviderRepo:
    return FakeProviderRepo(
        root,
        server,
        _SENTINEL_KEY,
        extra_sitecustomize=_route_multimodal_to(server.base_url),
    )


def _write_image(repo: FakeProviderRepo, name: str, marker: str) -> None:
    # The indexer validates extension and size only; distinct bytes per image.
    (repo.repo / "manuals" / name).write_bytes(
        b"\x89PNG\r\n\x1a\n" + f"example-image-{marker}".encode()
    )


def _write_doc(repo: FakeProviderRepo, index: int, version: str = "v1") -> None:
    docs = repo.repo / "manuals"
    docs.mkdir(exist_ok=True)
    _write_image(repo, f"diagram_{index}.png", f"{index}")
    (docs / f"guide_{index}.md").write_text(
        f"# Example guide {index} ({version})\n\n"
        f"The example service flow number {index}, revision {version}.\n\n"
        f"![diagram {index}](diagram_{index}.png)\n"
    )


def _multimodal_keys(repo: FakeProviderRepo, doc: Path) -> List[str]:
    """sha256 of each image-bearing chunk text of ``doc`` (the fake's key)."""
    from code_indexer.config import ConfigManager
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

    chunker = FixedSizeChunker(
        ConfigManager(repo.repo / ".code-indexer" / "config.json").load()
    )
    return [
        hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        for chunk in chunker.chunk_file(doc, repo.repo)
        if chunk.get("images")
    ]


def _run_spend(stdout: str) -> Dict[str, Any]:
    lines = [line for line in stdout.splitlines() if line.strip()]
    record = json.loads(lines[-1])
    assert record.get("record") == "run_spend", f"last line: {lines[-1]}"
    return dict(record)


def _index_initial(
    repo: FakeProviderRepo, fake_provider: Any, index_args: Sequence[str] = ()
) -> None:
    repo.write_files("code", 3)
    for i in range(_DOC_COUNT):
        _write_doc(repo, i)
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*(index_args or repo.index_args()))
    assert proc.returncode == 0, proc.stderr[-3000:]
    sent = fake_provider.ledger.run_multimodal_key_counts("initial")
    assert sum(sent.values()) == _DOC_COUNT, f"initial multimodal sends: {sent}"


@pytest.mark.parametrize("layout", ["chunks_db", "sharded_json"])
def test_no_change_reconcile_sends_no_committed_multimodal_chunk(
    tmp_path: Path, fake_provider: Any, layout: str
) -> None:
    """The incident: a full-plan run over unchanged image-bearing documents
    must not pay the multimodal provider again for any of them, whatever the
    collection layout."""
    index_args = ["index", f"--new-collection-layout={layout}"]
    repo = _repo(tmp_path, fake_provider)
    _index_initial(repo, fake_provider, index_args)

    fake_provider.ledger.begin_run("reconcile")
    proc = repo.cidx(*index_args, "--reconcile", "--progress-json")

    assert proc.returncode == 0, proc.stderr[-3000:]
    resent = fake_provider.ledger.run_multimodal_key_counts("reconcile")
    assert sum(resent.values()) == 0, (
        f"a no-change reconcile re-sent {sum(resent.values())} committed "
        f"multimodal input(s): {resent}"
    )
    assert fake_provider.ledger.run_stats("reconcile")["inputs"] == 0
    assert _run_spend(proc.stdout)["inputs_sent"] == 0


def test_changed_and_new_documents_are_each_sent_exactly_once(
    tmp_path: Path, fake_provider: Any
) -> None:
    """Only real misses pay: a changed image-bearing document and a new one
    are each sent once; the unchanged documents are reused."""
    repo = _repo(tmp_path, fake_provider)
    _index_initial(repo, fake_provider)
    _write_doc(repo, 1, version="v2")
    _write_doc(repo, _DOC_COUNT)
    docs = repo.repo / "manuals"
    expected = {
        key: 1
        for doc in (docs / "guide_1.md", docs / f"guide_{_DOC_COUNT}.md")
        for key in _multimodal_keys(repo, doc)
    }
    assert len(expected) == 2

    fake_provider.ledger.begin_run("delta")
    proc = repo.cidx(*repo.index_args(), "--reconcile", "--progress-json")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_multimodal_key_counts("delta") == expected
    assert _run_spend(proc.stdout)["inputs_sent"] == 2


def test_changed_image_under_unchanged_text_is_sent_once(
    tmp_path: Path, fake_provider: Any
) -> None:
    """The reuse key covers the image bytes the provider is sent: a replaced
    image under byte-identical document text is a miss, never a stale
    reuse of the old image's embedding."""
    repo = _repo(tmp_path, fake_provider)
    _index_initial(repo, fake_provider)
    doc = repo.repo / "manuals" / "guide_2.md"
    _write_image(repo, "diagram_2.png", "2-replaced")
    # Plan the document again: a newer mtime, byte-identical text.
    future = time.time() + 120
    os.utime(doc, (future, future))
    expected = {key: 1 for key in _multimodal_keys(repo, doc)}
    assert len(expected) == 1

    fake_provider.ledger.begin_run("image-changed")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_multimodal_key_counts("image-changed") == expected


def test_corrupt_multimodal_store_aborts_before_any_provider_call(
    tmp_path: Path, fake_provider: Any
) -> None:
    """A multimodal reuse-lookup error is never read as "nothing stored":
    the run aborts with the fatal chunk-store exit code and pays nothing."""
    from code_indexer.services.index_failure_exit_codes import (
        EXIT_CODE_CHUNK_STORE_ENVIRONMENT,
    )

    repo = _repo(tmp_path, fake_provider)
    _index_initial(repo, fake_provider)
    store = repo.repo / ".code-indexer" / "index" / "voyage-multimodal-3" / "chunks.db"
    conn = sqlite3.connect(str(store))
    try:
        damaged = conn.execute(
            "UPDATE chunks SET data = ? WHERE path = ?",
            (b"not-a-zstd-frame", "manuals/guide_0.md"),
        ).rowcount
        conn.commit()
    finally:
        conn.close()
    assert damaged == 1
    future = time.time() + 120
    os.utime(repo.repo / "manuals" / "guide_0.md", (future, future))

    fake_provider.ledger.begin_run("after-corruption")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert fake_provider.ledger.run_multimodal_key_counts("after-corruption") == {}
    assert fake_provider.ledger.run_stats("after-corruption")["inputs"] == 0
    assert proc.returncode == EXIT_CODE_CHUNK_STORE_ENVIRONMENT, (
        proc.returncode,
        proc.stderr[-3000:],
    )
