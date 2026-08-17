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
    remove_scip_build_commit,
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

    def test_builder_returned_none_skips(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        # builder returns None → stream-01 seam #3 DEFENSIVE guard. Under Option C
        # (EVO-65365) the production builder always returns a generator for a
        # golden repo, so a None indicates a builder-seam/config anomaly (or, as
        # here, a test injecting the seam) — the funnel fails soft, not crashes.
        out = funnel_scip_generation(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=_queue(),
            generator_builder=lambda *a, **k: None,
        )
        assert out is ScipDelegationOutcome.SKIPPED
        assert get_scip_metrics().get("delegated_skipped_builder_returned_none") == 1

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

    def test_root_build_repo_stale_nested_only_is_NOT_fresh(
        self, tmp_path, monkeypatch
    ):
        """EVO-66190 2nd gate: a root-buildable repo (root settings.gradle) with only a
        stale per-subproject DB + a matching marker must NOT read fresh — else the funnel
        skips the rebuild the reconcile enqueued."""
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        (tmp_path / "settings.gradle").write_text("include 'proj'")  # root build file
        scip_dir = tmp_path / ".code-indexer" / "scip"
        (scip_dir / "proj").mkdir(parents=True)
        (scip_dir / "proj" / "index.scip.db").write_text(
            "db"
        )  # nested only, no root DB
        record_scip_build_commit(scip_dir, "sha1")
        assert scip_db_is_fresh(tmp_path, scip_dir) is False

    def test_root_build_repo_with_top_level_db_is_fresh(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        (tmp_path / "settings.gradle").write_text("include 'proj'")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        (scip_dir / "index.scip.db").write_text(
            "db"
        )  # root build landed the top-level DB
        record_scip_build_commit(scip_dir, "sha1")
        assert scip_db_is_fresh(tmp_path, scip_dir) is True

    def test_no_index_marker_is_fresh_without_db(self, tmp_path, monkeypatch):
        """Freshness thrash fix: a build where every project was an unsupported
        language produced no DB and never will. A ``no-index`` marker at HEAD is
        fresh WITHOUT a DB so the repo does not re-enqueue forever."""
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        record_scip_build_commit(scip_dir, "sha1", produced_index=False)
        assert (scip_dir / ".build_commit").read_text().strip() == "sha1 no-index"
        assert scip_db_is_fresh(tmp_path, scip_dir) is True

    def test_no_index_marker_not_fresh_when_commit_differs(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha2")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        record_scip_build_commit(scip_dir, "sha1", produced_index=False)
        assert scip_db_is_fresh(tmp_path, scip_dir) is False

    def test_produced_index_marker_still_requires_db(self, tmp_path, monkeypatch):
        """A produced-index build writes the bare commit and STILL requires the
        DB on disk (so a later DB loss forces a rebuild)."""
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        record_scip_build_commit(scip_dir, "sha1", produced_index=True)
        assert (scip_dir / ".build_commit").read_text().strip() == "sha1"
        # DB missing → not fresh even though the marker matches HEAD.
        assert scip_db_is_fresh(tmp_path, scip_dir) is False

    def test_not_fresh_when_marker_missing(self, tmp_path, monkeypatch):
        # Finding-6 (6): every repo that had a SCIP index built BEFORE EVO-65021
        # has a built .scip.db but NO .build_commit marker. On first upgrade it
        # MUST be treated as not-fresh (rebuild), never fresh — otherwise the
        # entire existing fleet is skipped forever.
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        (scip_dir / "proj").mkdir(parents=True)
        (scip_dir / "proj" / "index.scip.db").write_text("db")
        # No marker written.
        assert scip_db_is_fresh(tmp_path, scip_dir) is False

    def test_funnel_enqueues_when_marker_missing(self, tmp_path, monkeypatch):
        # The missing-marker branch drives a real rebuild through the funnel.
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        (scip_dir / "proj").mkdir(parents=True)
        (scip_dir / "proj" / "index.scip.db").write_text("db")
        q = _CaptureQueue()
        out = funnel_scip_generation(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            golden_repo_manager=object(),
            build_queue=q,
            generator_builder=lambda *a, **k: _FakeGenerator(),
        )
        assert out is ScipDelegationOutcome.ENQUEUED
        assert get_scip_metrics().get("delegated_fresh") is None

    def test_record_build_commit_none_or_empty_is_noop(self, tmp_path):
        # Finding-6 (7): None/empty commit must not write a bogus marker.
        scip_dir = tmp_path / ".code-indexer" / "scip"
        record_scip_build_commit(scip_dir, None)
        record_scip_build_commit(scip_dir, "")
        assert not (scip_dir / ".build_commit").exists()


class TestActivatedRepoNamespace:
    """CRITICAL-2 / MAJOR: an activated alias is not resolved via the golden
    manager, and it falls through to the LOCAL path (NOT_DELEGATED) rather than
    being force-skipped (which would suppress the only path that can index it)."""

    def test_activated_repo_returns_not_delegated_without_golden_resolution(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")

        def _must_not_resolve(*_a, **_k):
            pytest.fail(
                "activated alias must not be resolved through the golden manager"
            )

        grm = SimpleNamespace(resolve_workspace_id=_must_not_resolve)
        gen_calls = []
        out = funnel_scip_generation(
            alias="my-repo",  # == golden alias in the common case — the trap
            repo_root=tmp_path,
            config=_delegated_cfg(),
            activated_repo=True,
            golden_repo_manager=grm,
            build_queue=_queue(),
            generator_builder=lambda *a, **k: gen_calls.append(1),
        )
        # NOT_DELEGATED → the caller runs its local subprocess path (which can
        # index the per-user checkout and honours --clear).
        assert out is ScipDelegationOutcome.NOT_DELEGATED
        assert get_scip_metrics().get("delegated_not_supported_activated_repo") == 1
        # No generator built, nothing enqueued against the wrong workspace.
        assert gen_calls == []

    def test_activated_repo_with_clear_returns_not_delegated(
        self, tmp_path, monkeypatch
    ):
        """activated_repo=True + clear=True together → NOT_DELEGATED (the local
        path honours --clear). Guards the reachability gap the reviewer flagged:
        the only production caller passing clear also passes activated_repo."""
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        grm = SimpleNamespace(
            resolve_workspace_id=lambda *_a, **_k: pytest.fail("must not resolve")
        )
        out = funnel_scip_generation(
            alias="my-repo",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            clear=True,
            activated_repo=True,
            golden_repo_manager=grm,
            build_queue=_queue(),
            generator_builder=lambda *a, **k: pytest.fail("must not build"),
        )
        assert out is ScipDelegationOutcome.NOT_DELEGATED


class TestClearRebuild:
    """MAJOR-5: clear=True bypasses the freshness short-circuit and drops the marker."""

    def test_clear_bypasses_freshness_and_drops_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(scip_lifecycle, "get_current_commit", lambda _p: "sha1")
        scip_dir = tmp_path / ".code-indexer" / "scip"
        (scip_dir / "proj").mkdir(parents=True)
        (scip_dir / "proj" / "index.scip.db").write_text("db")
        record_scip_build_commit(scip_dir, "sha1")  # marker == HEAD → would be FRESH
        assert scip_db_is_fresh(tmp_path, scip_dir) is True

        q = _CaptureQueue()
        out = funnel_scip_generation(
            alias="r",
            repo_root=tmp_path,
            config=_delegated_cfg(),
            clear=True,
            golden_repo_manager=object(),
            build_queue=q,
            generator_builder=lambda *a, **k: _FakeGenerator(),
        )
        # A rebuild-from-scratch must NOT be answered FRESH...
        assert out is ScipDelegationOutcome.ENQUEUED
        assert get_scip_metrics().get("delegated_fresh") is None
        # ...and the stale marker is removed so a failed rebuild can't read fresh.
        assert not (scip_dir / ".build_commit").exists()

    def test_remove_marker_when_absent_is_safe(self, tmp_path):
        scip_dir = tmp_path / ".code-indexer" / "scip"
        scip_dir.mkdir(parents=True)
        # No marker present — must not raise.
        remove_scip_build_commit(scip_dir)
        assert not (scip_dir / ".build_commit").exists()


class TestFunnelGoldenScip:
    """EVO-65365: the ONE home for the golden ``-global``→bare normalization.

    Every golden-repo SCIP entrypoint funnels through ``funnel_golden_scip``, so
    the CRITICAL-1 invariant (normalize the ``-global`` REGISTRY alias to the
    BARE golden alias before handing it to the sandbox — otherwise it resolves to
    a non-existent ``.../<alias>-global`` path and every golden repo silently
    loses SCIP) lives and is regression-tested in exactly one place.
    """

    def test_global_suffix_normalized_to_bare_and_args_passed_through(
        self, tmp_path, monkeypatch
    ):
        from pathlib import Path

        from code_indexer.server.services.scip_lifecycle import funnel_golden_scip

        captured = {}

        def _fake_funnel(*, alias, repo_root, config):
            captured["alias"] = alias
            captured["repo_root"] = repo_root
            captured["config"] = config
            return ScipDelegationOutcome.ENQUEUED

        monkeypatch.setattr(scip_lifecycle, "funnel_scip_generation", _fake_funnel)

        cfg = _delegated_cfg()
        out = funnel_golden_scip("my-repo-global", str(tmp_path), cfg)

        assert out is ScipDelegationOutcome.ENQUEUED
        # The ``-global`` registry suffix is stripped to the bare golden alias.
        assert captured["alias"] == "my-repo"
        # repo_root is coerced to a Path and the config is threaded through.
        assert captured["repo_root"] == Path(tmp_path)
        assert captured["config"] is cfg

    def test_bare_alias_unchanged(self, tmp_path, monkeypatch):
        from code_indexer.server.services.scip_lifecycle import funnel_golden_scip

        captured = {}

        def _fake_funnel(*, alias, repo_root, config):
            captured["alias"] = alias
            return ScipDelegationOutcome.NOT_DELEGATED

        monkeypatch.setattr(scip_lifecycle, "funnel_scip_generation", _fake_funnel)

        funnel_golden_scip("my-repo", str(tmp_path), _local_cfg())
        # removesuffix strips only a trailing ``-global``; a bare alias is intact.
        assert captured["alias"] == "my-repo"
