"""
Story #920 AC1: Per-type flags default to "dry_run" when not specified.

Tests (exhaustive list):
  test_all_four_flags_default_to_dry_run
  test_resolved_values_stored_as_attributes
  test_log_line_emitted_at_init
  test_explicit_none_resolves_to_dry_run
"""

from code_indexer.server.services.dep_map_health_detector import DepMapHealthDetector
from code_indexer.server.services.dep_map_index_regenerator import IndexRegenerator
from code_indexer.server.services.dep_map_repair_executor import DepMapRepairExecutor


def _make_executor(**kwargs) -> DepMapRepairExecutor:
    """Build executor with real deps and optional per-type flag overrides."""
    return DepMapRepairExecutor(
        health_detector=DepMapHealthDetector(),
        index_regenerator=IndexRegenerator(),
        **kwargs,
    )


def test_all_four_flags_default_to_dry_run() -> None:
    """AC1: No per-type args → all four resolve to 'dry_run'."""
    ex = _make_executor()
    assert ex._graph_repair_self_loop == "dry_run", (
        f"Expected 'dry_run', got {ex._graph_repair_self_loop!r}"
    )
    assert ex._graph_repair_malformed_yaml == "dry_run", (
        f"Expected 'dry_run', got {ex._graph_repair_malformed_yaml!r}"
    )
    assert ex._graph_repair_garbage_domain == "dry_run", (
        f"Expected 'dry_run', got {ex._graph_repair_garbage_domain!r}"
    )
    assert ex._graph_repair_bidirectional_mismatch == "dry_run", (
        f"Expected 'dry_run', got {ex._graph_repair_bidirectional_mismatch!r}"
    )


def test_resolved_values_stored_as_attributes() -> None:
    """AC1: Explicit values are stored correctly in instance attributes."""
    ex = _make_executor(
        graph_repair_self_loop="enabled",
        graph_repair_malformed_yaml="disabled",
        graph_repair_garbage_domain="dry_run",
        graph_repair_bidirectional_mismatch="enabled",
    )
    assert ex._graph_repair_self_loop == "enabled"
    assert ex._graph_repair_malformed_yaml == "disabled"
    assert ex._graph_repair_garbage_domain == "dry_run"
    assert ex._graph_repair_bidirectional_mismatch == "enabled"


def test_log_line_emitted_at_init(capsys) -> None:
    """AC1: Constructor logs a single line with all four resolved per-type values."""
    log_messages = []

    def capture_journal(msg: str) -> None:
        log_messages.append(msg)

    _make_executor(
        journal_callback=capture_journal,
        graph_repair_self_loop="enabled",
        graph_repair_malformed_yaml="dry_run",
        graph_repair_garbage_domain="disabled",
        graph_repair_bidirectional_mismatch="enabled",
    )

    flag_log = [m for m in log_messages if "per-type flags" in m]
    assert len(flag_log) == 1, (
        f"Expected exactly 1 per-type flags log line, got {len(flag_log)}: {log_messages}"
    )
    line = flag_log[0]
    assert "self_loop=enabled" in line, f"Missing self_loop=enabled in: {line!r}"
    assert "malformed_yaml=dry_run" in line, (
        f"Missing malformed_yaml=dry_run in: {line!r}"
    )
    assert "garbage_domain=disabled" in line, (
        f"Missing garbage_domain=disabled in: {line!r}"
    )
    assert "bidirectional_mismatch=enabled" in line, (
        f"Missing bidirectional_mismatch=enabled in: {line!r}"
    )


def test_explicit_none_resolves_to_dry_run() -> None:
    """AC1: Passing None explicitly for any flag resolves to 'dry_run'."""
    ex = _make_executor(
        graph_repair_self_loop=None,
        graph_repair_malformed_yaml=None,
        graph_repair_garbage_domain=None,
        graph_repair_bidirectional_mismatch=None,
    )
    assert ex._graph_repair_self_loop == "dry_run"
    assert ex._graph_repair_malformed_yaml == "dry_run"
    assert ex._graph_repair_garbage_domain == "dry_run"
    assert ex._graph_repair_bidirectional_mismatch == "dry_run"
