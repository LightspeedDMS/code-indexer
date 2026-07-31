"""Unit tests for the golden-repo SCIP lifecycle funnel (EVO-65021, stream-05).

Covers: the enable-scip/backend gate, both-entrypoints routing through the
config-selected backend, fail-soft (no alias / no workspace_id / builder error),
freshness/idempotency, and dedupe.
"""

from types import SimpleNamespace

import pytest

from code_indexer.server.services import scip_lifecycle
from code_indexer.server.services.scip_lifecycle import (
    ScipDelegationOutcome,
    funnel_scip_generation,
    record_scip_build_commit,
    scip_db_is_fresh,
)
from code_indexer.server.services.scip_resolution_queue import (
    DelegatedScipBuildQueue,
    get_scip_metrics,
    reset_scip_metrics,
)


@pytest.fixture(autouse=True)
def _clean_metrics():
    reset_scip_metrics()
    yield
    reset_scip_metrics()


def _local_cfg():
    return SimpleNamespace(scip_backend="local")


def _delegated_cfg():
    return SimpleNamespace(
        scip_backend="delegated",
        workspace_mcp=SimpleNamespace(endpoint="http://sb", api_key="k"),
    )


class _FakeGenerator:
    def generate(self, progress_callback=None):
        return SimpleNamespace(successful_projects=1, project_results=[])


class _CaptureQueue:
    """Deterministic stand-in for DelegatedScipBuildQueue.

    Captures enqueued builds without spawning the real worker thread (the real
    drain is covered by test_delegated_scip_build_queue). ``results`` scripts the
    True/False return of successive ``enqueue`` calls so the funnel's
    ENQUEUED/DEDUPED mapping can be asserted.
    """

    def __init__(self, results=None):
        self.builds = []
        self._results = list(results) if results is not None else None

    def enqueue(self, build):
        self.builds.append(build)
        if self._results:
            return self._results.pop(0)
        return True


def _queue(sleep=lambda _s: None):
    return DelegatedScipBuildQueue(sleep=sleep)


class TestGate:
    def test_local_backend_not_delegated(self, tmp_path):
        out = funnel_scip_generation(alias="r", repo_root=tmp_path, config=_local_cfg())
        assert out is ScipDelegationOutcome.NOT_DELEGATED

    def test_missing_backend_attr_defaults_local(self, tmp_path):
        out = funnel_scip_generation(
            alias="r", repo_root=tmp_path, config=SimpleNamespace()
        )
        assert out is ScipDelegationOutcome.NOT_DELEGATED


class TestDelegatedHappyPath:
    def test_enqueues_and_routes_generator(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        q = _CaptureQueue()
        gen = _FakeGenerator()
        out = funnel_scip_generation(
            alias="ts-repo",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=q,
            generator_builder=lambda *a, **k: gen,
        )
        assert out is ScipDelegationOutcome.ENQUEUED
        assert get_scip_metrics().get("delegated_enqueued") == 1
        assert len(q.builds) == 1
        build = q.builds[0]
        assert build.dedupe_key == "ts-repo@sha1"
        # run_build routes to the config-selected generator.
        result = build.run_build()
        assert result.successful_projects == 1
        # on_success records the freshness commit (idempotency, task #6).
        build.on_success(result)
        scip_dir = tmp_path / ".code-indexer" / "scip"
        assert (scip_dir / ".build_commit").read_text() == "sha1"

    def test_enqueue_true_false_maps_to_enqueued_then_deduped(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        q = _CaptureQueue(results=[True, False])
        kwargs = dict(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=q,
            generator_builder=lambda *a, **k: _FakeGenerator(),
        )
        assert funnel_scip_generation(**kwargs) is ScipDelegationOutcome.ENQUEUED
        assert funnel_scip_generation(**kwargs) is ScipDelegationOutcome.DEDUPED


class TestFailSoft:
    def test_no_alias_skips(self, tmp_path):
        out = funnel_scip_generation(
            alias=None, repo_root=tmp_path, config=_delegated_cfg()
        )
        assert out is ScipDelegationOutcome.SKIPPED
        assert get_scip_metrics().get("delegated_skipped_no_alias") == 1

    def test_no_workspace_id_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        # builder returns None → stream-01 seam #3 fail-soft (no workspace_id).
        out = funnel_scip_generation(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=_queue(),
            generator_builder=lambda *a, **k: None,
        )
        assert out is ScipDelegationOutcome.SKIPPED
        assert get_scip_metrics().get("delegated_skipped_no_workspace_id") == 1

    def test_builder_raising_is_swallowed(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")

        def _raise(*a, **k):
            raise RuntimeError("sandbox-mcp unreachable at index time")

        out = funnel_scip_generation(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=_queue(),
            generator_builder=_raise,
        )
        assert out is ScipDelegationOutcome.SKIPPED
        assert get_scip_metrics().get("delegated_skipped_error") == 1


class TestFreshness:
    def test_fresh_when_marker_matches_commit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        (scip_dir / "proj").mkdir(parents=True)
        (scip_dir / "proj" / "index.scip.db").write_text("db")
        record_scip_build_commit(scip_dir, "sha1")
        assert scip_db_is_fresh(tmp_path, scip_dir) is True

        out = funnel_scip_generation(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=_queue(),
            generator_builder=lambda *a, **k: _FakeGenerator(),
        )
        assert out is ScipDelegationOutcome.FRESH
        assert get_scip_metrics().get("delegated_fresh") == 1

    def test_not_fresh_when_commit_differs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha2")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        (scip_dir / "proj").mkdir(parents=True)
        (scip_dir / "proj" / "index.scip.db").write_text("db")
        record_scip_build_commit(scip_dir, "sha1")  # stale
        assert scip_db_is_fresh(tmp_path, scip_dir) is False

    def test_not_fresh_when_no_db(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        record_scip_build_commit(scip_dir, "sha1")
        assert scip_db_is_fresh(tmp_path, scip_dir) is False
