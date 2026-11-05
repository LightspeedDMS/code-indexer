"""Filename-mode Phase 1: an exhausted driver-regex budget is a Phase 1 timeout.

``PatternBudgetExhausted`` subclasses ``TimeoutError``; the filename walk's
``except TimeoutError`` converts only the manifest deadline, so the budget
error must be converted where the pattern is matched -- to the same
``XRayPhase1TimeoutError`` (with the partial candidates) the content mode
raises -- instead of escaping ``run()`` as a raw exception.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

import pytest

from code_indexer.utils.bounded_regex import BoundedPattern, PatternBudgetExhausted
from cidx_xray.search_engine import XRayPhase1TimeoutError, XRaySearchEngine


def test_budget_exhaustion_becomes_phase1_timeout_with_partials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    for name in ("a.py", "b.py", "c.py"):
        (tmp_path / name).write_text("x = 1\n")
    calls = {"n": 0}

    def _search(self: BoundedPattern, text: str) -> Optional[Any]:
        calls["n"] += 1
        if calls["n"] > 1:
            raise PatternBudgetExhausted("budget spent")
        return True

    monkeypatch.setattr(BoundedPattern, "search", _search)

    with pytest.raises(XRayPhase1TimeoutError) as caught:
        XRaySearchEngine()._run_phase1_filename(
            repo_path=tmp_path,
            driver_regex=".*",
            include_patterns=[],
            exclude_patterns=[],
            timeout_seconds=600,
        )
    assert len(caught.value.candidates) == 1
    assert caught.value.__cause__ is None
