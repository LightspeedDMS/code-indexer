"""Bug #v10.4.11: DescriptionRefreshScheduler was unwired in production.

Root cause: global_lifecycle_manager initialisation failure (caught by
APP-GENERAL-015 try/except) leaves global_lifecycle_manager=None, which
causes refresh_scheduler=None, which skips the if-block that wires the four
D3 collaborators.  Result: _check_lifecycle_backfill_wiring() returns False
on every loop pass; the scheduler is a silent no-op forever.

Fix (lifespan.py):
  Tier 1 — _golden_repos_dir wired unconditionally (always available from
            server_data_dir regardless of lifecycle manager status).
  Tier 2 — ERROR-level startup check (APP-GENERAL-051) before
            description_refresh_scheduler.start() surfaces any None slots
            immediately at startup rather than 60 s into the first loop pass.

Tests use source-text inspection (the established pattern from
test_dep_map_927_lifespan_wiring.py) to verify the structural ordering
contracts that prevent this class of regression.
"""

from __future__ import annotations

from pathlib import Path

# Repo root is 4 parents above this test file:
# test_file -> startup -> server -> unit -> tests -> repo_root
_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _lifespan_source() -> str:
    return _LIFESPAN_PATH.read_text()


def _set_refresh_scheduler_pos(source: str) -> int:
    """Return character position of set_refresh_scheduler(refresh_scheduler) call,
    which is the anchor point for the description scheduler wiring block."""
    pos = source.find("set_refresh_scheduler(refresh_scheduler)")
    assert pos != -1, (
        "set_refresh_scheduler(refresh_scheduler) not found in lifespan.py — "
        "anchor for description scheduler wiring block is missing"
    )
    return pos


def test_all_four_collaborators_wired_when_global_lifecycle_manager_present():
    """When refresh_scheduler is available, all 4 D3 collaborator slots must be assigned.

    Source-order and block-membership checks:
    - _golden_repos_dir assignment exists BEFORE the conditional guard.
    - The three lifecycle-dependent slots (_lifecycle_invoker, _lifecycle_debouncer,
      _refresh_scheduler) are assigned AFTER 'if refresh_scheduler is not None:'
      but BEFORE the '_missing_slots = [' check that follows the block.
      This upper-bound proves block membership — the assignments cannot be
      somewhere else later in the file.

    Regression guard: Story #876 D3 wiring must survive refactors. If any slot
    assignment is dropped from lifespan.py, _check_lifecycle_backfill_wiring()
    will return False and the scheduler becomes a silent no-op.
    """
    source = _lifespan_source()
    anchor = _set_refresh_scheduler_pos(source)

    conditional_marker = "if refresh_scheduler is not None:"
    conditional_pos = source.find(conditional_marker, anchor)
    assert conditional_pos != -1, (
        f"'{conditional_marker}' not found after set_refresh_scheduler in lifespan.py"
    )

    # Upper bound: _missing_slots check comes after the conditional block ends.
    missing_slots_marker = "_missing_slots = ["
    missing_slots_pos = source.find(missing_slots_marker, anchor)
    assert missing_slots_pos != -1, (
        f"'{missing_slots_marker}' not found after set_refresh_scheduler in lifespan.py — "
        "Tier 2 missing-slots check is absent; cannot bound the conditional block"
    )

    # _golden_repos_dir must appear BEFORE the conditional (unconditional wiring).
    golden_dir_marker = (
        "description_refresh_scheduler._golden_repos_dir = Path(golden_repos_dir)"
    )
    golden_dir_pos = source.find(golden_dir_marker, anchor)
    assert golden_dir_pos != -1, (
        f"'{golden_dir_marker}' not found after set_refresh_scheduler in lifespan.py — "
        "unconditional _golden_repos_dir wiring (Bug #v10.4.11 Tier 1) is missing"
    )
    assert golden_dir_pos < conditional_pos, (
        f"_golden_repos_dir assignment (pos {golden_dir_pos}) must appear BEFORE "
        f"'if refresh_scheduler is not None:' (pos {conditional_pos}). "
        "When global_lifecycle_manager is None, refresh_scheduler is also None and "
        "the conditional block is skipped entirely — _golden_repos_dir would stay None."
    )

    # The three lifecycle-dependent slots must appear INSIDE the conditional block:
    # after the 'if' marker AND before the '_missing_slots' check that follows the block.
    for slot in (
        "_lifecycle_invoker",
        "_lifecycle_debouncer",
        "_refresh_scheduler",
    ):
        assign_marker = f"description_refresh_scheduler.{slot} ="
        assign_pos = source.find(assign_marker, conditional_pos)
        assert assign_pos != -1, (
            f"'{assign_marker}' not found after 'if refresh_scheduler is not None:' "
            f"in lifespan.py — lifecycle collaborator {slot!r} is not wired when "
            "global_lifecycle_manager is present (Bug #v10.4.11 regression)"
        )
        assert assign_pos < missing_slots_pos, (
            f"'{assign_marker}' (pos {assign_pos}) must appear before the "
            f"'_missing_slots = [' check (pos {missing_slots_pos}). "
            f"Assignment found outside the conditional block — block membership violated."
        )


def test_error_logged_when_global_lifecycle_manager_missing():
    """APP-GENERAL-051 ERROR check must appear in lifespan.py BEFORE the .start() call.

    Source-order checks:
    - 'APP-GENERAL-051' appears before 'description_refresh_scheduler.start()'.
    - '_missing_slots = [' appears before 'description_refresh_scheduler.start()'.

    Both checks must be ordered before .start() to surface the misconfiguration
    to operators at startup, not 60 s into the first loop pass.

    Regression guard: if the Tier 2 startup check is removed or moved after
    .start(), the production misconfiguration becomes invisible at startup.
    """
    source = _lifespan_source()
    anchor = _set_refresh_scheduler_pos(source)

    start_marker = "description_refresh_scheduler.start()"
    start_pos = source.find(start_marker, anchor)
    assert start_pos != -1, (
        f"'{start_marker}' not found after set_refresh_scheduler in lifespan.py"
    )

    error_code_marker = "APP-GENERAL-051"
    error_code_pos = source.find(error_code_marker, anchor)
    assert error_code_pos != -1, (
        f"'{error_code_marker}' not found after set_refresh_scheduler in lifespan.py — "
        "Tier 2 startup misconfiguration check (Bug #v10.4.11) is missing"
    )
    assert error_code_pos < start_pos, (
        f"APP-GENERAL-051 (pos {error_code_pos}) must appear BEFORE "
        f"'description_refresh_scheduler.start()' (pos {start_pos}). "
        "Operators must see the misconfiguration log at startup, not after the "
        "scheduler has already started in a broken state."
    )

    missing_slots_marker = "_missing_slots = ["
    missing_slots_pos = source.find(missing_slots_marker, anchor)
    assert missing_slots_pos != -1, (
        f"'{missing_slots_marker}' not found after set_refresh_scheduler in lifespan.py — "
        "Tier 2 missing-slots check is absent"
    )
    assert missing_slots_pos < start_pos, (
        f"'_missing_slots = [' check (pos {missing_slots_pos}) must appear BEFORE "
        f"'description_refresh_scheduler.start()' (pos {start_pos}). "
        "The check fires after .start() — operators would not see it during startup."
    )


def test_golden_repos_dir_always_wired_even_without_global_lifecycle_manager():
    """_golden_repos_dir must be wired unconditionally, outside the refresh_scheduler guard.

    Source-order check:
    - 'description_refresh_scheduler._golden_repos_dir = Path(golden_repos_dir)'
      must appear BEFORE 'if refresh_scheduler is not None:' in the description
      scheduler block.

    This is the Tier 1 fix for Bug #v10.4.11: golden_repos_dir is always
    derivable from server_data_dir, so wiring it unconditionally ensures at
    least one slot is valid even when global_lifecycle_manager is None.  The
    ERROR log (APP-GENERAL-051) then accurately lists only the truly missing
    lifecycle-dependent slots.
    """
    source = _lifespan_source()
    anchor = _set_refresh_scheduler_pos(source)

    golden_dir_marker = (
        "description_refresh_scheduler._golden_repos_dir = Path(golden_repos_dir)"
    )
    conditional_marker = "if refresh_scheduler is not None:"

    golden_dir_pos = source.find(golden_dir_marker, anchor)
    conditional_pos = source.find(conditional_marker, anchor)

    assert golden_dir_pos != -1, (
        f"'{golden_dir_marker}' not found after set_refresh_scheduler in lifespan.py — "
        "unconditional _golden_repos_dir wiring (Bug #v10.4.11 Tier 1) is missing"
    )
    assert conditional_pos != -1, (
        f"'{conditional_marker}' not found after set_refresh_scheduler in lifespan.py"
    )
    assert golden_dir_pos < conditional_pos, (
        f"Bug #v10.4.11 regression: _golden_repos_dir assignment (pos {golden_dir_pos}) "
        f"must appear BEFORE 'if refresh_scheduler is not None:' (pos {conditional_pos}). "
        "If global_lifecycle_manager is None, refresh_scheduler is None, the conditional "
        "block is skipped, and _golden_repos_dir stays None — the scheduler becomes a "
        "silent no-op on every loop pass."
    )
