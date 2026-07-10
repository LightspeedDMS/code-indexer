"""Tests for the lazy trigram-index background rebuild (EVO-64221).

When a regex search finds no compatible index, a one-shot background build is
kicked off so the index self-heals before the next scheduled golden-repo refresh.
Guarded by an in-progress flag, a cooldown, and an env toggle.
"""

import threading
import time

import pytest

import code_indexer.global_repos.regex_search as rs


@pytest.fixture(autouse=True)
def _reset_lazy_state(monkeypatch):
    monkeypatch.delenv("CIDX_TRIGRAM_LAZY_BUILD", raising=False)
    with rs._lazy_build_lock:
        rs._lazy_build_in_progress.clear()
        rs._lazy_build_last_attempt.clear()
    yield
    with rs._lazy_build_lock:
        rs._lazy_build_in_progress.clear()
        rs._lazy_build_last_attempt.clear()


def _patch_build(monkeypatch, record, event=None):
    def fake_build(self, repo_path, file_list=None):
        record.append(repo_path)
        if event is not None:
            event.set()
        return 0

    monkeypatch.setattr(
        "code_indexer.global_repos.trigram_index_manager.TrigramIndexManager.build",
        fake_build,
    )


def test_triggers_background_build(tmp_path, monkeypatch):
    calls = []
    done = threading.Event()
    _patch_build(monkeypatch, calls, done)
    rs._maybe_trigger_lazy_index_build(tmp_path)
    assert done.wait(5), "background build was not started"
    assert calls == [tmp_path]


def test_skipped_within_cooldown(tmp_path, monkeypatch):
    calls = []
    _patch_build(monkeypatch, calls)
    rs._lazy_build_last_attempt[str(tmp_path)] = time.monotonic()  # recent attempt
    rs._maybe_trigger_lazy_index_build(tmp_path)
    time.sleep(0.2)
    assert calls == []  # backed off


def test_skipped_when_in_progress(tmp_path, monkeypatch):
    calls = []
    _patch_build(monkeypatch, calls)
    rs._lazy_build_in_progress.add(str(tmp_path))  # a build is "running"
    rs._maybe_trigger_lazy_index_build(tmp_path)
    time.sleep(0.2)
    assert calls == []


def test_disabled_by_env(tmp_path, monkeypatch):
    monkeypatch.setenv("CIDX_TRIGRAM_LAZY_BUILD", "0")
    calls = []
    _patch_build(monkeypatch, calls)
    rs._maybe_trigger_lazy_index_build(tmp_path)
    time.sleep(0.2)
    assert calls == []


def test_build_failure_is_swallowed_and_clears_in_progress(tmp_path, monkeypatch):
    done = threading.Event()

    def boom(self, repo_path, file_list=None):
        try:
            raise RuntimeError("build blew up")
        finally:
            done.set()

    monkeypatch.setattr(
        "code_indexer.global_repos.trigram_index_manager.TrigramIndexManager.build",
        boom,
    )
    rs._maybe_trigger_lazy_index_build(tmp_path)
    assert done.wait(5)
    # give the finally-block a moment to run after build raises
    for _ in range(50):
        with rs._lazy_build_lock:
            if str(tmp_path) not in rs._lazy_build_in_progress:
                break
        time.sleep(0.02)
    with rs._lazy_build_lock:
        assert str(tmp_path) not in rs._lazy_build_in_progress  # cleared on failure


def test_single_build_under_concurrent_triggers(tmp_path, monkeypatch):
    calls = []
    started = threading.Event()
    release = threading.Event()

    def slow_build(self, repo_path, file_list=None):
        calls.append(repo_path)
        started.set()
        release.wait(5)  # hold the "build" so a second trigger overlaps it
        return 0

    monkeypatch.setattr(
        "code_indexer.global_repos.trigram_index_manager.TrigramIndexManager.build",
        slow_build,
    )
    rs._maybe_trigger_lazy_index_build(tmp_path)
    assert started.wait(5)
    rs._maybe_trigger_lazy_index_build(tmp_path)  # while first still running
    release.set()
    time.sleep(0.2)
    assert calls == [tmp_path]  # exactly one build despite two triggers
