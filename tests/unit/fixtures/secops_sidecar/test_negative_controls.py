"""Negative controls: each fidelity check must FAIL when its rule is broken.

The sidecar accepts ``--self-test-defect NAME`` (harness: ``self_test_defect``)
to deliberately break exactly one rule.  It exists only so these tests can
prove the checks discriminate; nothing else starts a defective sidecar.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

import pytest

from tests.fixtures.secops_sidecar.harness import SidecarHandle, start_sidecar
from tests.unit.fixtures.secops_sidecar.checks import (
    hidden_event_not_searchable,
    identical_batch_stored_once,
    rejected_batches_store_nothing,
    rejected_event_not_searchable,
)

Check = Callable[[SidecarHandle], bool]

CONTROLS = [
    ("store_before_validate", rejected_batches_store_nothing),
    ("no_dedup", identical_batch_stored_once),
    ("search_ignores_hide", hidden_event_not_searchable),
    ("search_includes_rejected", rejected_event_not_searchable),
]


@pytest.mark.parametrize("defect, check", CONTROLS, ids=[c[0] for c in CONTROLS])
def test_check_passes_on_a_correct_sidecar(
    sidecar: SidecarHandle, defect: str, check: Check
) -> None:
    assert check(sidecar) is True


@pytest.mark.parametrize("defect, check", CONTROLS, ids=[c[0] for c in CONTROLS])
def test_check_fails_when_the_rule_is_broken(
    sidecar_scratch_dir: Path, defect: str, check: Check
) -> None:
    broken = start_sidecar(sidecar_scratch_dir, self_test_defect=defect)
    try:
        assert check(broken) is False
    finally:
        broken.stop()


def test_unknown_defect_name_is_refused(sidecar_scratch_dir: Path) -> None:
    with pytest.raises(RuntimeError, match="READY"):
        start_sidecar(sidecar_scratch_dir, self_test_defect="not_a_defect")
