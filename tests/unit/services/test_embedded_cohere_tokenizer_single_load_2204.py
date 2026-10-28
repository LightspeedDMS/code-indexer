import threading
import time
from pathlib import Path
from typing import List

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel

import code_indexer.services.embedded_cohere_tokenizer as mod

THREADS = 8
LOAD_DELAY_SECONDS = 0.05
BARRIER_TIMEOUT_SECONDS = 5


@pytest.fixture(autouse=True)
def clear_tokenizer_cache():
    mod._tokenizer_cache.clear()
    yield
    mod._tokenizer_cache.clear()


@pytest.fixture
def cache_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    Tokenizer(WordLevel({"hello": 0, "[UNK]": 1}, unk_token="[UNK]")).save(
        str(tmp_path / "tokenizer.json")
    )
    monkeypatch.setattr(mod, "_get_cache_dir", lambda model: str(tmp_path))
    return tmp_path


class CountingImporter:
    def __init__(self, fail: bool = False) -> None:
        self.fail = fail
        self.calls = 0
        self._lock = threading.Lock()

    def __call__(self):
        with self._lock:
            self.calls += 1
        # ^ why sleep in the seam, not none; widens the race so an unlocked load is seen > 1
        time.sleep(LOAD_DELAY_SECONDS)
        if self.fail:
            raise ImportError("tokenizers unavailable")
        return Tokenizer


def _call_concurrently(model: str) -> List[object]:
    barrier = threading.Barrier(THREADS, timeout=BARRIER_TIMEOUT_SECONDS)
    results: List[object] = [None] * THREADS

    def worker(index: int) -> None:
        try:
            barrier.wait()
            results[index] = mod._get_tokenizer(model)
        except BaseException as exc:
            results[index] = exc

    threads = [
        threading.Thread(target=worker, args=(i,), daemon=True) for i in range(THREADS)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)
    assert not any(thread.is_alive() for thread in threads)
    return results


def test_concurrent_first_callers_load_once_and_share_object(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = CountingImporter()
    monkeypatch.setattr(mod, "_import_tokenizer_class", importer)

    results = _call_concurrently("embed-v4.0")

    assert importer.calls == 1
    assert all(isinstance(r, Tokenizer) for r in results)
    assert len({id(r) for r in results}) == 1
    assert mod._get_tokenizer("embed-v4.0") is results[0]
    assert importer.calls == 1


def test_failed_load_runs_once_and_every_caller_gets_fallback(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = CountingImporter(fail=True)
    monkeypatch.setattr(mod, "_import_tokenizer_class", importer)

    results = _call_concurrently("embed-v4.0")

    assert results == [None] * THREADS
    assert importer.calls == 1
    assert mod.count_tokens(["abcdef"], model="embed-v4.0") == 3
    assert importer.calls == 1


def test_distinct_models_each_load_once(
    cache_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    importer = CountingImporter()
    monkeypatch.setattr(mod, "_import_tokenizer_class", importer)

    first = _call_concurrently("embed-v4.0")
    second = _call_concurrently("embed-english-v3.0")

    assert importer.calls == 2
    assert first[0] is not second[0]
