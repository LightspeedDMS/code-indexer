"""Every test of a fleet-concurrency invariant must actually run in CI.

Both CI jobs are curated ALLOWLISTS -- ``smoke`` names files, and
``build:image``'s in-image step runs ``tests/unit/xray/`` plus six MCP files.
Anything not named is never *collected*, so unlike a gated test it does not even
appear as a skip. There is no number anywhere that goes down when coverage is
lost, which is why it went unnoticed: the background-work audit's five changes
all merged on green pipelines with none of their tests ever collected.

Adding the files to the list fixes the instance. This fixes the class, the only
way a list can be kept honest: **derive the requirement instead of maintaining
it**. A test that imports one of the fleet-invariant seams below is, by that
fact, a test CI must run -- nobody has to remember to add it here.

Deliberately narrow. This is not "all tests must run in CI" (smoke would take an
hour); it is "the handful of seams where being wrong costs a fleet-wide
incident, and where the failure is silent rather than loud".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CI = _REPO / ".gitlab-ci.yml"
_TESTS = _REPO / "tests" / "unit"

# Importing any of these means the test covers a mechanism that decides WHERE
# background work runs across the fleet. Each one, wrong, is a multi-pod
# incident: duplicated destructive sweeps, N-times-oversubscribed pools, or work
# that silently runs nowhere.
_FLEET_INVARIANT_SEAMS = (
    "leader_gate",  # M1: who may run fleet-wide work
    "worker_budget",  # R4: node budget vs per-process budget
    "POD_PULL_OPS",  # M2: which ops are work-stealable
    "DistributedJobClaimer",  # M2: the claim itself
    "register_job_if_no_conflict",  # M4: the race-to-claim single-flight
)

# Paths CI runs wholesale rather than file-by-file (build:image's in-image step).
_DIRECTORY_COVERED = ("tests/unit/xray/",)


def _ci_text() -> str:
    return _CI.read_text(encoding="utf-8")


def _named_in_ci(rel: str) -> bool:
    text = _ci_text()
    if rel in text:
        return True
    return any(rel.startswith(d) and d in text for d in _DIRECTORY_COVERED)


def _tests_touching_invariants() -> Set[str]:
    found = set()
    for path in sorted(_TESTS.rglob("test_*.py")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover -- unreadable file is not our business
            continue
        # Match the seam as a whole word so a passing mention in prose does not
        # drag an unrelated test into the required set.
        if any(re.search(rf"\b{re.escape(s)}\b", body) for s in _FLEET_INVARIANT_SEAMS):
            found.add(str(path.relative_to(_REPO)))
    return found


def test_the_ci_file_is_where_we_think_it_is() -> None:
    """Fail loudly rather than vacuously if the layout moves.

    Every assertion below is of the form "X appears in this file". If the file
    were missing or empty, they would all pass by doing nothing -- the exact
    fail-open shape this suite exists to catch elsewhere.
    """
    assert _CI.is_file(), f"{_CI} not found; the checks below would pass vacuously"
    assert "python -m pytest" in _ci_text(), "no pytest invocation in .gitlab-ci.yml"


def test_the_invariant_scan_finds_something() -> None:
    """Same guard, one level up: an empty required set passes everything."""
    found = _tests_touching_invariants()
    assert len(found) >= 5, (
        f"only {len(found)} tests matched the fleet-invariant seams "
        f"{_FLEET_INVARIANT_SEAMS}; the scan is probably broken, and a broken "
        f"scan silently requires nothing"
    )


@pytest.mark.parametrize("rel", sorted(_tests_touching_invariants()))
def test_a_fleet_invariant_test_is_named_in_ci(rel: str) -> None:
    assert _named_in_ci(rel), (
        f"{rel} covers a fleet-concurrency invariant but no CI job collects it.\n"
        f"Both CI jobs are allowlists, so an unlisted file does not skip -- it "
        f"is never collected, and nothing reports a smaller number.\n"
        f"Add it to the `smoke` job's pytest arguments in .gitlab-ci.yml, or, if "
        f"it genuinely does not belong there, stop importing "
        f"{' / '.join(_FLEET_INVARIANT_SEAMS)}."
    )
