import threading
import time
from pathlib import Path
from typing import Dict, List

import pytest
from tokenizers import Tokenizer
from tokenizers.models import WordLevel

from code_indexer.services.embedded_voyage_tokenizer import VoyageTokenizer

THREADS = 8
LOAD_DELAY_SECONDS = 0.05
BARRIER_TIMEOUT_SECONDS = 5


@pytest.fixture(autouse=True)
def clear_tokenizer_cache():
    VoyageTokenizer._tokenizer_cache.clear()
    yield
    VoyageTokenizer._tokenizer_cache.clear()


@pytest.fixture
def tokenizer_json(tmp_path: Path) -> Path:
    path = tmp_path / "tokenizer.json"
    Tokenizer(WordLevel({"hello": 0, "[UNK]": 1}, unk_token="[UNK]")).save(str(path))
    return path


class CountingResolver:
    def __init__(self, result: Path, fail: bool = False) -> None:
        self.result = result
        self.fail = fail
        self.calls: Dict[str, int] = {}
        self._lock = threading.Lock()

    def __call__(self, model: str) -> Path:
        with self._lock:
            self.calls[model] = self.calls.get(model, 0) + 1
        # ^ why sleep in the seam, not none; widens the race so an unlocked load is seen > 1
        time.sleep(LOAD_DELAY_SECONDS)
        if self.fail:
            raise RuntimeError("tokenizer load failed")
        return self.result


def _call_concurrently(model: str) -> List[object]:
    barrier = threading.Barrier(THREADS, timeout=BARRIER_TIMEOUT_SECONDS)
    results: List[object] = [None] * THREADS

    def worker(index: int) -> None:
        try:
            barrier.wait()
            results[index] = VoyageTokenizer._get_tokenizer(model)
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
    tokenizer_json: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = CountingResolver(tokenizer_json)
    monkeypatch.setattr(
        VoyageTokenizer, "_resolve_hf_cache_path", staticmethod(resolver)
    )

    results = _call_concurrently("voyage-code-3")

    assert resolver.calls == {"voyage-code-3": 1}
    assert all(isinstance(r, Tokenizer) for r in results)
    assert len({id(r) for r in results}) == 1
    assert VoyageTokenizer._get_tokenizer("voyage-code-3") is results[0]
    assert resolver.calls == {"voyage-code-3": 1}


def test_failed_load_raises_in_every_caller_and_is_retried(
    tokenizer_json: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = CountingResolver(tokenizer_json, fail=True)
    monkeypatch.setattr(
        VoyageTokenizer, "_resolve_hf_cache_path", staticmethod(resolver)
    )

    results = _call_concurrently("voyage-code-3")

    assert all(isinstance(r, RuntimeError) for r in results)
    assert "voyage-code-3" not in VoyageTokenizer._tokenizer_cache

    calls_after_failure = resolver.calls["voyage-code-3"]
    resolver.fail = False
    loaded = VoyageTokenizer._get_tokenizer("voyage-code-3")

    assert isinstance(loaded, Tokenizer)
    assert resolver.calls["voyage-code-3"] == calls_after_failure + 1


def test_distinct_models_each_load_once(
    tokenizer_json: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = CountingResolver(tokenizer_json)
    monkeypatch.setattr(
        VoyageTokenizer, "_resolve_hf_cache_path", staticmethod(resolver)
    )

    first = _call_concurrently("voyage-code-3")
    second = _call_concurrently("voyage-3")

    assert resolver.calls == {"voyage-code-3": 1, "voyage-3": 1}
    assert first[0] is not second[0]
