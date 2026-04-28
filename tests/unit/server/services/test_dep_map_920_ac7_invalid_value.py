"""
Story #920 AC7: Invalid per-type flag value raises ValueError at construction.

MESSI Rule 2 Anti-Fallback + Rule 13 Anti-Silent-Failure: invalid config must
raise loudly at startup, not silently default to dry_run.

Tests (exhaustive list):
  test_invalid_value_raises_value_error[<param>-<bad_value>]  (parametrized x4)
  test_error_message_contains_valid_choices[<param>-<bad_value>]  (parametrized x4)
  test_error_message_contains_offending_value[<param>-<bad_value>]  (parametrized x4)
"""

import pytest

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor

# ---------------------------------------------------------------------------
# Parametrize: (kwarg_name, bad_value) for all four per-type flags
# ---------------------------------------------------------------------------

_PARAM_CASES = [
    ("graph_repair_self_loop", "enable"),  # typo: missing 'd'
    ("graph_repair_malformed_yaml", "Enable"),  # wrong case
    ("graph_repair_garbage_domain", "yes"),  # wrong value entirely
    ("graph_repair_bidirectional_mismatch", "true"),  # boolean string, not valid
]


def _make_executor(**kwargs) -> DepMapRepairExecutor:
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("param_name,bad_value", _PARAM_CASES)
def test_invalid_value_raises_value_error(param_name: str, bad_value: str) -> None:
    """AC7: Any per-type flag with invalid value raises ValueError at construction."""
    with pytest.raises(ValueError):
        _make_executor(**{param_name: bad_value})


@pytest.mark.parametrize("param_name,bad_value", _PARAM_CASES)
def test_error_message_contains_valid_choices(param_name: str, bad_value: str) -> None:
    """AC7: ValueError message lists all valid choices for any per-type flag."""
    with pytest.raises(ValueError) as exc_info:
        _make_executor(**{param_name: bad_value})
    msg = str(exc_info.value)
    assert "disabled" in msg, (
        f"Expected 'disabled' in error message for {param_name}: {msg!r}"
    )
    assert "dry_run" in msg, (
        f"Expected 'dry_run' in error message for {param_name}: {msg!r}"
    )
    assert "enabled" in msg, (
        f"Expected 'enabled' in error message for {param_name}: {msg!r}"
    )


@pytest.mark.parametrize("param_name,bad_value", _PARAM_CASES)
def test_error_message_contains_offending_value(
    param_name: str, bad_value: str
) -> None:
    """AC7: ValueError message contains the bad value that was passed."""
    with pytest.raises(ValueError) as exc_info:
        _make_executor(**{param_name: bad_value})
    msg = str(exc_info.value)
    assert bad_value in msg, (
        f"Expected offending value {bad_value!r} in error message for {param_name}: {msg!r}"
    )
