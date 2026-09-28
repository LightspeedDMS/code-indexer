"""Server-context indexing resumes from resume state the server itself wrote.

Server-spawned indexing (``trust_resume_state=False``) uses the
``.code-indexer/metadata-<provider>.json`` resume file, which is repository
content, only when it carries a seal only the server can produce (an HMAC
keyed by a key held in the server data directory, outside every repository
tree). Resume state written by a server-context run carries that seal, so
an interrupted server index resumes exactly as it did before; state without
a valid server seal (never sealed, changed after sealing, sealed for a
different file or under a different key) is not trusted and the run falls
back to a disk-vs-database reconcile.

All tests drive the REAL ``SmartIndexer.smart_index()`` entry point against
a real git repository, a real ``FilesystemVectorStore`` and a real
deterministic (non-network) embedding provider. The interruption is a
real cancellation through the progress callback's ``"INTERRUPT"`` return.
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.config import Config
from code_indexer.services.embedding_provider import (
    BatchEmbeddingResult,
    EmbeddingProvider,
    EmbeddingResult,
)
from code_indexer.services.resume_state_seal import (
    RESUME_SEAL_FIELD,
    RESUME_SEAL_KEY_FILENAME,
    load_or_create_resume_seal_key,
)
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

_VECTOR_DIM = 16
_BYTE_MAX_VALUE = 255.0
_VECTOR_SCALE = 2.0
_VECTOR_OFFSET = 1.0
_FAKE_MAX_TOKENS = 8192
_FILE_COUNT = 12
_INTERRUPT_AFTER_FILE_PROGRESS_CALLS = 3
_RESUME_MARKER = "Resuming interrupted operation"
_RECONCILE_MARKER = "Reconcile:"


def _deterministic_embedding(text: str) -> List[float]:
    import hashlib

    digest = hashlib.sha256(text.encode("utf-8")).digest()
    return [
        (digest[i % len(digest)] / _BYTE_MAX_VALUE) * _VECTOR_SCALE - _VECTOR_OFFSET
        for i in range(_VECTOR_DIM)
    ]


class _DeterministicHashEmbeddingProvider(EmbeddingProvider):
    """Real, fully-working local EmbeddingProvider (no network, no mocks)."""

    def get_embedding(
        self,
        text: str,
        model: Optional[str] = None,
        embedding_purpose: Optional[str] = None,
    ) -> List[float]:
        return _deterministic_embedding(text)

    def get_embeddings_batch(
        self, texts: List[str], model: Optional[str] = None
    ) -> List[List[float]]:
        return [_deterministic_embedding(t) for t in texts]

    def get_embedding_with_metadata(
        self, text: str, model: Optional[str] = None
    ) -> EmbeddingResult:
        return EmbeddingResult(
            embedding=_deterministic_embedding(text), model=self.get_current_model()
        )

    def get_embeddings_batch_with_metadata(
        self, texts: List[str], model: Optional[str] = None
    ) -> BatchEmbeddingResult:
        return BatchEmbeddingResult(
            embeddings=[_deterministic_embedding(t) for t in texts],
            model=self.get_current_model(),
        )

    def health_check(self, *, test_api: bool = False) -> bool:
        return True

    def get_model_info(self) -> Dict[str, int]:
        return {"dimensions": _VECTOR_DIM, "max_tokens": _FAKE_MAX_TOKENS}

    def get_provider_name(self) -> str:
        return "deterministic-test-provider"

    def get_current_model(self) -> str:
        return "deterministic-test-model"

    def supports_batch_processing(self) -> bool:
        return True

    def _get_model_token_limit(self) -> int:
        return _FAKE_MAX_TOKENS


@pytest.fixture(autouse=True)
def _isolated_server_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """The seal key lives in the server data directory; never the real one."""
    server_dir = tmp_path / "server-data"
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(server_dir))
    return server_dir


def _run_git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    )


def _build_repo(repo: Path) -> List[Path]:
    repo.mkdir()
    subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
    _run_git(repo, "config", "user.email", "test@example.com")
    _run_git(repo, "config", "user.name", "Test")
    (repo / ".gitignore").write_text(".code-indexer/\n")
    files = []
    for i in range(_FILE_COUNT):
        f = repo / f"module_{i}.py"
        f.write_text(f"def function_{i}():\n    return {i}\n" * 5)
        files.append(f)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial")
    return files


def _metadata_path(repo: Path) -> Path:
    return repo / ".code-indexer" / "metadata-deterministic-test-provider.json"


def _make_indexer(repo: Path) -> SmartIndexer:
    """A brand-new SmartIndexer, as a freshly spawned `cidx index` builds."""
    config = Config(codebase_dir=repo)
    embedding_provider = _DeterministicHashEmbeddingProvider()
    vector_store = FilesystemVectorStore(base_path=repo / ".code-indexer" / "index")
    vector_store.ensure_provider_aware_collection(config, embedding_provider)
    return SmartIndexer(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=vector_store,
        metadata_path=_metadata_path(repo),
    )


def _interrupted_server_run(repo: Path) -> None:
    """A real server-context run cancelled part-way through embedding."""
    calls = {"file_progress": 0}

    def interrupting_callback(
        current: int, total: int, path: Path, info: Optional[str] = None, **_: Any
    ) -> Optional[str]:
        if total and total > 0:
            calls["file_progress"] += 1
            if calls["file_progress"] >= _INTERRUPT_AFTER_FILE_PROGRESS_CALLS:
                return "INTERRUPT"
        return None

    stats = _make_indexer(repo).smart_index(
        force_full=False,
        trust_resume_state=False,
        progress_callback=interrupting_callback,
    )
    assert stats.cancelled, "test setup: the first run must really be interrupted"
    stored = json.loads(_metadata_path(repo).read_text())
    assert stored["status"] == "in_progress", "test setup: run must be resumable"


def _server_run_collecting_info(repo: Path) -> List[str]:
    infos: List[str] = []

    def collecting_callback(
        current: int, total: int, path: Path, info: Optional[str] = None, **_: Any
    ) -> None:
        if info:
            infos.append(info)

    _make_indexer(repo).smart_index(
        force_full=False,
        trust_resume_state=False,
        progress_callback=collecting_callback,
    )
    return infos


def _took_resume_path(infos: List[str]) -> bool:
    return any(_RESUME_MARKER in i for i in infos)


def _took_reconcile_path(infos: List[str]) -> bool:
    return any(_RECONCILE_MARKER in i for i in infos)


class TestInterruptedServerIndexResumes:
    def test_interrupted_server_index_resumes_without_reconcile(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _build_repo(repo)
        _interrupted_server_run(repo)

        infos = _server_run_collecting_info(repo)

        assert _took_resume_path(infos), (
            "An interrupted server-context index must resume from the resume "
            f"state the server itself wrote. Progress info: {infos}"
        )
        assert not _took_reconcile_path(infos), (
            "An interrupted server-context index must not fall back to a full "
            f"disk-vs-database reconcile. Progress info: {infos}"
        )
        stored = json.loads(_metadata_path(repo).read_text())
        assert stored["status"] == "completed"

    def test_every_server_save_is_sealed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        _build_repo(repo)
        _interrupted_server_run(repo)
        stored = json.loads(_metadata_path(repo).read_text())
        assert isinstance(stored.get(RESUME_SEAL_FIELD), str)

    def test_local_cli_run_does_not_seal_and_drops_stale_seal(
        self, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        _build_repo(repo)
        _interrupted_server_run(repo)
        _make_indexer(repo).smart_index(force_full=False)
        stored = json.loads(_metadata_path(repo).read_text())
        assert RESUME_SEAL_FIELD not in stored


def _write_resume_file(repo: Path, content: Dict[str, Any]) -> None:
    path = _metadata_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(content, indent=2))


def _unsealed_resume_state(files: List[Path]) -> Dict[str, Any]:
    """Well-formed, resumable resume state that carries no seal."""
    return {
        "status": "in_progress",
        "embedding_provider": "deterministic-test-provider",
        "embedding_model": "deterministic-test-model",
        "files_to_index": [str(f) for f in files[:2]],
        "total_files_to_index": 2,
        "current_file_index": 0,
        "completed_files": [],
        "failed_file_paths": [],
    }


class TestStateWithoutValidSealIgnored:
    def test_unsealed_resume_file_is_not_resumed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        files = _build_repo(repo)
        _write_resume_file(repo, _unsealed_resume_state(files))

        infos = _server_run_collecting_info(repo)

        assert not _took_resume_path(infos), (
            f"Server indexing resumed from state without a server seal: {infos}"
        )
        assert _took_reconcile_path(infos)

    def test_resume_file_with_invalid_seal_is_not_resumed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        files = _build_repo(repo)
        state = _unsealed_resume_state(files)
        state[RESUME_SEAL_FIELD] = "0" * 64
        _write_resume_file(repo, state)

        infos = _server_run_collecting_info(repo)

        assert not _took_resume_path(infos)
        assert _took_reconcile_path(infos)

    def test_edited_server_sealed_state_is_not_resumed(self, tmp_path: Path) -> None:
        repo = tmp_path / "repo"
        files = _build_repo(repo)
        _interrupted_server_run(repo)
        stored = json.loads(_metadata_path(repo).read_text())
        stored["files_to_index"] = [str(files[0])]
        stored["total_files_to_index"] = 1
        stored["current_file_index"] = 0
        _write_resume_file(repo, stored)  # seal kept, content changed

        infos = _server_run_collecting_info(repo)

        assert not _took_resume_path(infos)
        assert _took_reconcile_path(infos)

    def test_sealed_state_copied_from_another_repo_is_not_resumed(
        self, tmp_path: Path
    ) -> None:
        source = tmp_path / "source"
        _build_repo(source)
        _interrupted_server_run(source)
        target = tmp_path / "target"
        _build_repo(target)
        _write_resume_file(target, json.loads(_metadata_path(source).read_text()))

        infos = _server_run_collecting_info(target)

        assert not _took_resume_path(infos)
        assert _took_reconcile_path(infos)

    def test_sealed_state_is_not_trusted_under_a_different_server_key(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        _build_repo(repo)
        _interrupted_server_run(repo)
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path / "other-server"))

        infos = _server_run_collecting_info(repo)

        assert not _took_resume_path(infos)
        assert _took_reconcile_path(infos)

    def test_local_cli_still_trusts_unsealed_state(self, tmp_path: Path) -> None:
        """Local CLI indexing is unchanged: its own resume file is trusted."""
        repo = tmp_path / "repo"
        files = _build_repo(repo)
        _write_resume_file(repo, _unsealed_resume_state(files))
        infos: List[str] = []

        def collecting_callback(
            current: int, total: int, path: Path, info: Optional[str] = None, **_: Any
        ) -> None:
            if info:
                infos.append(info)

        _make_indexer(repo).smart_index(
            force_full=False, progress_callback=collecting_callback
        )
        assert _took_resume_path(infos)


class TestSealKey:
    def test_key_is_created_in_server_data_dir_with_owner_only_mode(
        self, tmp_path: Path, _isolated_server_data_dir: Path
    ) -> None:
        key = load_or_create_resume_seal_key()
        key_path = _isolated_server_data_dir / RESUME_SEAL_KEY_FILENAME
        assert key is not None and len(key) == 32
        assert key_path.read_bytes() == key
        assert stat.S_IMODE(os.stat(key_path).st_mode) == 0o600

    def test_key_is_stable_across_loads(self) -> None:
        assert load_or_create_resume_seal_key() == load_or_create_resume_seal_key()

    def test_wrong_length_key_is_rejected(
        self, _isolated_server_data_dir: Path
    ) -> None:
        _isolated_server_data_dir.mkdir(parents=True)
        (_isolated_server_data_dir / RESUME_SEAL_KEY_FILENAME).write_bytes(b"short")
        assert load_or_create_resume_seal_key() is None

    def test_unusable_server_dir_yields_no_key(self, tmp_path: Path) -> None:
        blocker = tmp_path / "not-a-dir"
        blocker.write_text("file, not a directory")
        assert load_or_create_resume_seal_key(blocker) is None

    def test_unavailable_key_means_untrusted_resume(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        _build_repo(repo)
        _interrupted_server_run(repo)
        blocker = tmp_path / "blocked"
        blocker.write_text("file, not a directory")
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(blocker))

        infos = _server_run_collecting_info(repo)

        assert not _took_resume_path(infos)
        assert _took_reconcile_path(infos)
