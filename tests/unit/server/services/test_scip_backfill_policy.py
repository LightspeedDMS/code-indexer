"""EVO-66445: unit tests for the SCIP backfill policy (granular sub-counts + enqueue)."""

from __future__ import annotations

from pathlib import Path
from typing import Any, List

import cidx_scip.server.backfill_policy as mod
from cidx_scip.server.backfill_policy import ScipBackfillPolicy


class _GRM:
    def __init__(self, present: set[str]) -> None:
        self._present = present

    def _scip_present(self, repo_path: Path) -> bool:
        return repo_path.name in self._present


class _Proj:
    def __init__(self, language: str) -> None:
        self.language = language


def test_is_present_delegates_to_scip_present(tmp_path: Path, monkeypatch) -> None:
    """Presence is the feature's ``reconcile.scip_present`` (it left the golden
    repo manager with SCIP, modular phase 4d)."""
    import cidx_scip.server.reconcile as reconcile

    monkeypatch.setattr(reconcile, "scip_present", lambda path: path.name == "built")
    p = ScipBackfillPolicy(_GRM(set()), {"python"}, object())
    assert p.is_present(tmp_path / "built") is True
    assert p.is_present(tmp_path / "other") is False


def test_eligibility_tracks_not_discoverable_and_language_skipped(
    tmp_path, monkeypatch
) -> None:
    routes = {"none": [], "wrong": [_Proj("ruby")], "ok": [_Proj("python")]}

    class _PD:
        def __init__(self, repo_path: Path, source: Any = None) -> None:
            self._name = repo_path.name

        def discover(self) -> List[Any]:
            return routes[self._name]

    monkeypatch.setattr(mod, "ProjectDiscovery", _PD, raising=False)
    import cidx_scip.discovery as disc

    monkeypatch.setattr(disc, "ProjectDiscovery", _PD)

    p = ScipBackfillPolicy(_GRM(set()), {"python", "typescript"}, object())
    assert p.eligibility(tmp_path / "none") is None
    assert p.eligibility(tmp_path / "wrong") is None
    assert p.eligibility(tmp_path / "ok") == {"alias": "ok"}
    assert p.not_discoverable == 1
    assert p.language_skipped == 1


def test_enqueue_funnels_delegated_scip(tmp_path, monkeypatch) -> None:
    calls: List[tuple] = []

    class _Outcome:
        value = "enqueued"

    import cidx_scip.server.lifecycle as sl

    monkeypatch.setattr(
        sl,
        "funnel_golden_scip",
        lambda alias, root, cfg: calls.append((alias, root, cfg)) or _Outcome(),
    )
    p = ScipBackfillPolicy(_GRM(set()), {"python"}, "CFG")
    p.enqueue("svc", tmp_path / "svc", {"alias": "svc"})
    assert calls == [("svc", str(tmp_path / "svc"), "CFG")]
