from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, List

import pytest

from tests.utils.real_process_guard import CIDX, REPORT_ENV


# ^ coupled with tests/utils/real_process_guard.py:_on_spawn; report mode records each spawn and never raises
def record_cidx_spawns(
    report_dir: Path, monkeypatch: pytest.MonkeyPatch
) -> Callable[[], List[dict]]:
    report = report_dir / "real-process-spawns.jsonl"
    monkeypatch.setenv(REPORT_ENV, str(report))

    def rows() -> List[dict]:
        if not report.exists():
            return []
        lines = report.read_text().splitlines()
        return [r for r in map(json.loads, lines) if r["kind"] == CIDX]

    return rows
