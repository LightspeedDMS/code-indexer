"""EVO-64616: the global activation repair.

Golden rows with no ``<alias>-global`` registry row: the EVO-65484 backfill
inserted their rows (racing an in-flight add, or after a removal left the
clone), and nothing ever activated them. The repair activates each from the
index it already has -- never re-indexing -- under one cluster-wide
single-flight claim, and reports stale alias pointers.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services import global_activation_repair as repair
from code_indexer.server.services.job_tracker import DuplicateJobError, JobTracker
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.utils.index_store import (
    INDEX_STORE_ALIAS_FIELD,
    INDEX_VERSION_FIELD,
)

_STORE = "code_indexer.server.startup.index_store_wiring.server_index_store"
_ACTIVATOR = "code_indexer.global_repos.global_activation.GlobalActivator"
_OLD = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()


def _row(alias: str, **extra: Any) -> Dict[str, Any]:
    row: Dict[str, Any] = {
        "alias": alias,
        "repo_url": f"https://gitlab.example/{alias}.git",
        "clone_path": f"/golden/{alias}",
        "enable_temporal": False,
        "temporal_options": None,
    }
    row.update(extra)
    return row


def _indexed_clone(tmp_path: Path, alias: str) -> str:
    idx = tmp_path / alias / ".code-indexer" / "index" / "voyage-code-3"
    idx.mkdir(parents=True)
    (idx / "hnsw_index.bin").write_bytes(b"x")
    return str(tmp_path / alias)


class _LocalStore:
    is_local = True

    def index_root(self, repo_path: Any) -> Path:
        return Path(repo_path) / ".code-indexer"


class _DelegatedStore(_LocalStore):
    is_local = False

    def __init__(self, current: Optional[str] = None) -> None:
        self.current = current
        self.asked: List[str] = []

    def current_version_as(self, store_alias: str) -> Optional[str]:
        self.asked.append(store_alias)
        return self.current


class _Activator:
    """GlobalActivator stand-in: a registry and an alias manager."""

    def __init__(
        self,
        global_names: List[str],
        records: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        self.registry = MagicMock()
        self.registry.list_global_repos.return_value = [
            {"alias_name": n} for n in global_names
        ]
        self.registry.get_global_repo.return_value = None
        self.alias_manager = MagicMock()
        self.alias_manager.list_records.return_value = records or {}
        self.alias_manager.alias_exists.return_value = False


def _manager(job_tracker: Optional[Any] = None) -> MagicMock:
    manager = MagicMock()
    manager.golden_repos_dir = "/golden"
    manager.job_tracker = job_tracker
    manager._strip_credentials_if_delegated.side_effect = lambda url: url
    manager._resolve_golden_repo_authoritative.return_value = None
    return manager


def _tracker(active: Optional[Dict[str, str]] = None) -> MagicMock:
    tracker = MagicMock()
    tracker.active_job_id.side_effect = lambda op, alias: (active or {}).get(
        f"{op}:{alias}"
    )
    return tracker


def _run(manager: Any, rows: List[Dict[str, Any]], activator: _Activator) -> Any:
    with patch(_ACTIVATOR, return_value=activator):
        return repair.repair_global_activation(manager, rows)


@pytest.fixture(autouse=True)
def _fresh_failure_memory() -> Any:
    repair._FAILED_LAST_CYCLE.clear()
    yield
    repair._FAILED_LAST_CYCLE.clear()


@pytest.fixture
def no_index(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    """resolve_index_fields stand-in: every repo is already published."""
    fake = MagicMock(return_value=({"index_version": "v1"}, False))
    monkeypatch.setattr(repair, "resolve_index_fields", fake)
    return fake


class TestLifecycleJobInFlight:
    def test_no_tracker_means_nothing_in_flight(self) -> None:
        assert repair.lifecycle_job_in_flight(None, "a") is False

    def test_add_or_removal_in_flight(self) -> None:
        assert repair.lifecycle_job_in_flight(
            _tracker({"remove_golden_repo:a": "j1"}), "a"
        )
        assert repair.lifecycle_job_in_flight(_tracker({"add_golden_repo:a": "j"}), "a")

    def test_nothing_active(self) -> None:
        assert repair.lifecycle_job_in_flight(_tracker(), "a") is False

    def test_a_failed_lookup_raises(self) -> None:
        tracker = MagicMock()
        tracker.active_job_id.side_effect = RuntimeError("db down")
        with pytest.raises(RuntimeError):
            repair.lifecycle_job_in_flight(tracker, "a")


class TestSettings:
    def test_max_per_cycle_default_and_override(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(repair.MAX_PER_CYCLE_ENV, raising=False)
        assert repair.max_per_cycle() == repair.DEFAULT_MAX_PER_CYCLE
        monkeypatch.setenv(repair.MAX_PER_CYCLE_ENV, "7")
        assert repair.max_per_cycle() == 7

    @pytest.mark.parametrize("raw", ["0", "-3", "many"])
    def test_max_per_cycle_rejects_non_positive(
        self, monkeypatch: pytest.MonkeyPatch, raw: str
    ) -> None:
        monkeypatch.setenv(repair.MAX_PER_CYCLE_ENV, raw)
        assert repair.max_per_cycle() == repair.DEFAULT_MAX_PER_CYCLE

    @pytest.mark.parametrize(
        "raw,expected", [("true", True), ("1", True), ("no", False), ("", False)]
    )
    def test_stale_alias_cleanup_flag(
        self, monkeypatch: pytest.MonkeyPatch, raw: str, expected: bool
    ) -> None:
        monkeypatch.setenv(repair.STALE_ALIAS_CLEANUP_ENV, raw)
        assert repair.stale_alias_cleanup_enabled() is expected


class TestFindActivationCandidates:
    def test_only_rows_without_a_global_row(self) -> None:
        rows = [
            _row("zeta"),
            _row("alpha"),
            _row("active"),
            _row("cidx-meta", repo_url="local://cidx-meta"),
            _row("no-path", clone_path=None),
        ]
        found = repair.find_activation_candidates(rows, {"active-global"})
        assert [r["alias"] for r in found] == ["alpha", "zeta"]


class TestResolveIndexFields:
    def test_reuses_the_published_version(self) -> None:
        store = _DelegatedStore(current="v9")
        manager = _manager()
        with patch(_STORE, return_value=store):
            fields, published = repair.resolve_index_fields(manager, "/golden/steps")
        assert fields == {INDEX_STORE_ALIAS_FIELD: "steps", INDEX_VERSION_FIELD: "v9"}
        assert published is False
        assert store.asked == ["steps"]
        manager._publish_index.assert_not_called()

    def test_publishes_the_on_disk_index_when_none_is_published(
        self, tmp_path: Path
    ) -> None:
        clone = _indexed_clone(tmp_path, "steps")
        manager = _manager()
        manager._publish_index.return_value = {"index_version": "v1"}
        with patch(_STORE, return_value=_DelegatedStore()):
            fields, published = repair.resolve_index_fields(manager, clone)
        manager._publish_index.assert_called_once_with(clone, clone)
        assert (fields, published) == ({"index_version": "v1"}, True)

    def test_local_store_uses_the_index_in_place(self, tmp_path: Path) -> None:
        clone = _indexed_clone(tmp_path, "steps")
        with patch(_STORE, return_value=_LocalStore()):
            assert repair.resolve_index_fields(_manager(), clone) == (None, False)

    def test_no_index_anywhere_raises(self, tmp_path: Path) -> None:
        (tmp_path / "bare").mkdir()
        with patch(_STORE, return_value=_DelegatedStore()):
            with pytest.raises(repair.NoIndexError):
                repair.resolve_index_fields(_manager(), str(tmp_path / "bare"))


class TestStaleAliases:
    def test_find_stale_aliases(self) -> None:
        records: Dict[str, Dict[str, Any]] = {
            "kept-global": {},
            "registered-global": {},
            "orphan-global": {},
            "not-a-global-alias": {},
        }
        assert repair.find_stale_aliases(records, {"kept"}, {"registered-global"}) == [
            "orphan-global"
        ]

    def test_pointer_age(self) -> None:
        now = datetime(2026, 10, 20, 12, tzinfo=timezone.utc)
        doc = {"last_refresh": "2026-10-20T11:00:00+00:00"}
        assert repair._pointer_age_seconds(doc, now) == 3600
        assert repair._pointer_age_seconds({"created_at": "2026-10-20T11:00:00"}, now)
        assert repair._pointer_age_seconds({"created_at": "garbage"}, now) == 0


class TestRepairGlobalActivation:
    def test_activates_each_candidate_from_its_index(self, no_index: Any) -> None:
        tracker = _tracker()
        manager = _manager(tracker)
        activator = _Activator(["done-global"])

        result = _run(manager, [_row("steps"), _row("done")], activator)

        assert result.activated == ["steps"]
        kwargs = manager._activate_globally.call_args.kwargs
        assert kwargs["alias"] == "steps"
        assert kwargs["clone_path"] == "/golden/steps"
        assert kwargs["published"] == {"index_version": "v1"}
        assert kwargs["enable_temporal"] is False
        manager._strip_credentials_if_delegated.assert_called_once()
        tracker.register_job_if_no_conflict.assert_called_once()
        assert (
            tracker.register_job_if_no_conflict.call_args.kwargs["repo_alias"]
            == repair.REPAIR_SENTINEL_ALIAS
        )
        tracker.complete_job.assert_called_once()
        assert result.counts()["activated_global"] == 1
        manager._run_meta_description_hook.assert_called_once_with(
            "steps", "https://gitlab.example/steps.git", "/golden/steps"
        )

    def test_nothing_to_do_claims_nothing(self, no_index: Any) -> None:
        tracker = _tracker()
        result = _run(_manager(tracker), [_row("a")], _Activator(["a-global"]))
        assert result.candidates == []
        tracker.register_job_if_no_conflict.assert_not_called()

    def test_another_pod_holds_the_claim(self, no_index: Any) -> None:
        tracker = _tracker()
        tracker.register_job_if_no_conflict.side_effect = DuplicateJobError(
            operation_type=repair.REPAIR_OPERATION_TYPE,
            repo_alias=repair.REPAIR_SENTINEL_ALIAS,
            existing_job_id="other",
        )
        manager = _manager(tracker)
        result = _run(manager, [_row("a")], _Activator([]))
        assert result.single_flight_skipped is True
        manager._activate_globally.assert_not_called()

    def test_a_registry_read_failure_raises(self) -> None:
        activator = _Activator([])
        activator.registry.list_global_repos.side_effect = RuntimeError("pg down")
        with pytest.raises(RuntimeError):
            _run(_manager(_tracker()), [_row("a")], activator)

    def test_skips_a_repo_activated_since_the_listing(self, no_index: Any) -> None:
        manager = _manager(_tracker())
        activator = _Activator([])
        activator.registry.get_global_repo.return_value = {"alias_name": "a-global"}
        result = _run(manager, [_row("a")], activator)
        assert result.activated == []
        manager._activate_globally.assert_not_called()

    def test_skips_a_repo_with_an_add_in_flight(self, no_index: Any) -> None:
        manager = _manager(_tracker({"add_golden_repo:a": "job"}))
        result = _run(manager, [_row("a")], _Activator([]))
        assert result.skipped_in_flight == ["a"]
        manager._activate_globally.assert_not_called()

    def test_reports_no_index_and_failures_and_carries_on(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def fields(_manager: Any, clone_path: str) -> Any:
            if clone_path.endswith("bare"):
                raise repair.NoIndexError("none")
            return {"index_version": "v1"}, clone_path.endswith("fresh")

        monkeypatch.setattr(repair, "resolve_index_fields", fields)
        tracker = _tracker()
        manager = _manager(tracker)

        def activate(**kwargs: Any) -> None:
            if kwargs["alias"] == "boom":
                raise RuntimeError("cas refused")

        manager._activate_globally.side_effect = activate

        result = _run(
            manager, [_row("bare"), _row("boom"), _row("fresh")], _Activator([])
        )

        assert result.skipped_no_index == ["bare"]
        assert set(result.failed) == {"boom"}
        assert result.activated == ["fresh"]
        assert result.published == ["fresh"]
        # Only an activated repo gets its cidx-meta description.
        assert [
            c.args[0] for c in manager._run_meta_description_hook.call_args_list
        ] == ["fresh"]
        tracker.complete_job.assert_called_once()

    def test_caps_activations_per_cycle(
        self, monkeypatch: pytest.MonkeyPatch, no_index: Any
    ) -> None:
        monkeypatch.setenv(repair.MAX_PER_CYCLE_ENV, "2")
        result = _run(
            _manager(_tracker()), [_row("a"), _row("b"), _row("c")], _Activator([])
        )
        assert result.activated == ["a", "b"]
        assert result.deferred == ["c"]

    def test_unindexed_repos_never_starve_a_healable_one(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(repair.MAX_PER_CYCLE_ENV, "2")

        def fields(_manager: Any, clone_path: str) -> Any:
            if "bare" in clone_path:
                raise repair.NoIndexError("none")
            return {"index_version": "v1"}, True

        monkeypatch.setattr(repair, "resolve_index_fields", fields)
        rows = [_row(f"bare{i}") for i in range(5)] + [_row("zz-healable")]

        result = _run(_manager(_tracker()), rows, _Activator([]))

        assert result.activated == ["zz-healable"]
        assert len(result.skipped_no_index) == 5
        assert result.deferred == []

    def test_a_repo_that_failed_goes_last_next_cycle(
        self, monkeypatch: pytest.MonkeyPatch, no_index: Any
    ) -> None:
        monkeypatch.setenv(repair.MAX_PER_CYCLE_ENV, "1")
        manager = _manager(_tracker())

        def activate(**kwargs: Any) -> None:
            if kwargs["alias"] == "a-broken":
                raise RuntimeError("cas refused")

        manager._activate_globally.side_effect = activate
        rows = [_row("a-broken"), _row("b-fine")]

        first = _run(manager, rows, _Activator([]))
        second = _run(manager, rows, _Activator([]))

        assert set(first.failed) == {"a-broken"} and first.deferred == ["b-fine"]
        assert second.activated == ["b-fine"] and second.deferred == ["a-broken"]

    def test_an_unexpected_error_fails_the_claim(self, no_index: Any) -> None:
        tracker = _tracker()
        manager = _manager(tracker)
        with patch.object(repair, "_run_pass", side_effect=RuntimeError("boom")):
            with pytest.raises(RuntimeError):
                _run(manager, [_row("a")], _Activator([]))
        tracker.fail_job.assert_called_once()
        tracker.complete_job.assert_not_called()

    def test_solo_mode_runs_without_a_claim(self, no_index: Any) -> None:
        result = _run(_manager(None), [_row("a")], _Activator([]))
        assert result.activated == ["a"]


class TestStaleAliasSweep:
    def _records(self, stamp: str = _OLD) -> Dict[str, Dict[str, Any]]:
        return {"gone-global": {"last_refresh": stamp}, "kept-global": {}}

    def test_reports_without_a_claim_by_default(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv(repair.STALE_ALIAS_CLEANUP_ENV, raising=False)
        tracker = _tracker()
        activator = _Activator(["kept-global"], self._records())
        result = _run(_manager(tracker), [_row("kept")], activator)
        assert result.stale_aliases == ["gone-global"]
        activator.alias_manager.delete_alias.assert_not_called()
        tracker.register_job_if_no_conflict.assert_not_called()

    def test_removes_when_enabled_and_every_guard_holds(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(repair.STALE_ALIAS_CLEANUP_ENV, "true")
        tracker = _tracker()
        activator = _Activator(["kept-global"], self._records())
        result = _run(_manager(tracker), [_row("kept")], activator)
        activator.alias_manager.delete_alias.assert_called_once_with("gone-global")
        assert result.stale_aliases_removed == ["gone-global"]
        tracker.register_job_if_no_conflict.assert_called_once()

    @pytest.mark.parametrize("guard", ["young", "golden", "registry", "in_flight"])
    def test_any_failing_guard_keeps_the_pointer(
        self, monkeypatch: pytest.MonkeyPatch, guard: str
    ) -> None:
        monkeypatch.setenv(repair.STALE_ALIAS_CLEANUP_ENV, "true")
        stamp = datetime.now(timezone.utc).isoformat() if guard == "young" else _OLD
        activator = _Activator(["kept-global"], self._records(stamp))
        active = {"remove_golden_repo:gone": "j"} if guard == "in_flight" else {}
        manager = _manager(_tracker(active))
        if guard == "golden":
            manager._resolve_golden_repo_authoritative.return_value = object()
        if guard == "registry":
            activator.registry.get_global_repo.return_value = {"alias_name": "x"}
        result = _run(manager, [_row("kept")], activator)
        activator.alias_manager.delete_alias.assert_not_called()
        assert result.stale_aliases_removed == []

    def test_a_delete_that_did_not_take_is_a_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(repair.STALE_ALIAS_CLEANUP_ENV, "true")
        activator = _Activator(["kept-global"], self._records())
        activator.alias_manager.alias_exists.return_value = True
        result = _run(_manager(_tracker()), [_row("kept")], activator)
        assert "gone-global" in result.failed

    def test_no_golden_rows_calls_nothing_stale(self) -> None:
        activator = _Activator([], self._records())
        result = _run(_manager(_tracker()), [], activator)
        assert result.stale_aliases == []
        activator.alias_manager.list_records.assert_not_called()


class TestJobTrackerActiveJobId:
    def test_finds_the_active_job_and_ignores_others(self, tmp_path: Path) -> None:
        db = tmp_path / "jobs.db"
        DatabaseSchema(str(db)).initialize_database()
        tracker = JobTracker(str(db))
        tracker.register_job_if_no_conflict(
            job_id="j1",
            operation_type="add_golden_repo",
            username="seeder",
            repo_alias="steps",
        )
        assert tracker.active_job_id("add_golden_repo", "steps") == "j1"
        assert tracker.active_job_id("remove_golden_repo", "steps") is None
        tracker.complete_job("j1")
        assert tracker.active_job_id("add_golden_repo", "steps") is None
