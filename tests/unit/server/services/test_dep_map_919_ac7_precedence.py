"""
Story #919 AC7: is_effective_dry_run composition rule tests.

Verifies:
  is_effective_dry_run(True, "enabled")   -> True  (invocation overrides per-type)
  is_effective_dry_run(False, "dry_run")  -> True  (per-type flag respected)
  is_effective_dry_run(False, "enabled")  -> False (neither flag active)
  is_effective_dry_run(False, "disabled") -> False (disabled means not dry-run)
  is_effective_dry_run(True, "dry_run")   -> True  (both flags active)

Tests (exhaustive list):
  test_invocation_true_overrides_per_type_enabled
  test_per_type_dry_run_flag_respected
  test_per_type_enabled_not_dry_run
  test_per_type_disabled_not_dry_run
  test_invocation_true_with_per_type_dry_run_still_true
"""

from code_indexer.server.services.dep_map_repair_executor import is_effective_dry_run


def test_invocation_true_overrides_per_type_enabled() -> None:
    """AC7: invocation_dry_run=True takes precedence over per_type_flag='enabled'."""
    assert is_effective_dry_run(True, "enabled") is True


def test_per_type_dry_run_flag_respected() -> None:
    """AC7: per_type_flag='dry_run' makes it a dry run even when invocation is False."""
    assert is_effective_dry_run(False, "dry_run") is True


def test_per_type_enabled_not_dry_run() -> None:
    """AC7: per_type_flag='enabled' is not dry-run mode."""
    assert is_effective_dry_run(False, "enabled") is False


def test_per_type_disabled_not_dry_run() -> None:
    """AC7: per_type_flag='disabled' is not dry-run mode."""
    assert is_effective_dry_run(False, "disabled") is False


def test_invocation_true_with_per_type_dry_run_still_true() -> None:
    """AC7: invocation=True AND per_type='dry_run' both active -> still True."""
    assert is_effective_dry_run(True, "dry_run") is True
