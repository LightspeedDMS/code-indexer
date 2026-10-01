"""Story #1676 AC6 regression guard: lifespan.py's TelemetryManager startup
block must resolve this process's cluster node identity via the SAME shared
resolver (resolve_cluster_node_id) the rest of the cluster code treats as
authoritative, and pass it through to get_telemetry_manager() as
cluster_node_id.

This suite mirrors the established pattern in
tests/unit/server/startup/test_lifespan_clone_backend_wiring_bug1044.py:
  1. Source-text guards: verify the wiring assignments/imports exist and are
     ordered correctly in lifespan.py.
  2. Runtime guard: exec()'s the ACTUAL sliced production source (not a
     hand-written replica) under a cluster-configured, a solo-mode, and an
     empty-node_id server_config, using pytest's monkeypatch fixture (which
     restores module state automatically) to intercept get_telemetry_manager,
     and asserts the captured cluster_node_id equals what the REAL
     resolve_cluster_node_id() resolver independently computes for that same
     input -- never a hardcoded expected string.

exec() of the real sliced source (rather than a hand-written replica) is
used deliberately, same rationale as Bug #1044/#1462's sibling suite: a
hand-written replica of the wiring logic could pass even if lifespan.py's
actual source drifts from it, silently defeating the regression guard.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from code_indexer.server.utils.cluster_node_id import resolve_cluster_node_id

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


class TestLifespanTelemetryClusterNodeIdSourceGuard:
    """Source-text guard: lifespan.py must resolve + thread cluster_node_id
    into get_telemetry_manager() inside the TelemetryManager startup block."""

    def test_resolve_cluster_node_id_imported_in_telemetry_block(self):
        source = _LIFESPAN_PATH.read_text()
        telemetry_block_start = source.index(
            "Startup: Initialize TelemetryManager for OTEL"
        )
        telemetry_block = source[telemetry_block_start : telemetry_block_start + 4000]

        assert (
            "from code_indexer.server.utils.cluster_node_id import" in telemetry_block
        ), (
            "Story #1676 AC6: lifespan.py's TelemetryManager startup block "
            "must import resolve_cluster_node_id from the shared resolver "
            "module -- never a second/competing identity scheme."
        )
        assert "resolve_cluster_node_id(" in telemetry_block

    def test_get_telemetry_manager_called_with_cluster_node_id_kwarg(self):
        source = _LIFESPAN_PATH.read_text()
        assert "cluster_node_id=_telemetry_cluster_node_id" in source, (
            "Story #1676 AC6: get_telemetry_manager() must be called with "
            "cluster_node_id=<value resolved via resolve_cluster_node_id()>."
        )

    def test_resolver_import_appears_before_get_telemetry_manager_call(self):
        source = _LIFESPAN_PATH.read_text()

        import_pos = source.find(
            "from code_indexer.server.utils.cluster_node_id import"
        )
        call_pos = source.find("cluster_node_id=_telemetry_cluster_node_id")

        assert import_pos != -1
        assert call_pos != -1
        assert import_pos < call_pos, (
            "resolve_cluster_node_id import must appear before the "
            "get_telemetry_manager(..., cluster_node_id=...) call it feeds."
        )


def _make_server_config(node_id_or_none):
    """Build the minimal SimpleNamespace shape the sliced wiring block reads
    (server_config.telemetry_config.*, server_config.cluster.node_id).
    node_id_or_none=None simulates solo mode (no cluster section at all)."""
    cluster = (
        None if node_id_or_none is None else SimpleNamespace(node_id=node_id_or_none)
    )
    return SimpleNamespace(
        telemetry_config=SimpleNamespace(
            enabled=True,
            service_name="cidx-server",
            collector_endpoint="http://localhost:4317",
            collector_protocol="http",
        ),
        cluster=cluster,
    )


def _extract_wiring_block_source() -> str:
    """Slice the real telemetry-construction snippet out of lifespan.py and
    dedent it to column 0 so it can be exec()'d standalone.

    Boundaries: starts at the lazy 'from code_indexer.server.telemetry
    import get_telemetry_manager' line (unique in the file) and ends right
    after 'app.state.telemetry_manager = telemetry_manager' (also unique /
    immediately follows the construction call).
    """
    source = _LIFESPAN_PATH.read_text()
    start = source.index(
        "from code_indexer.server.telemetry import get_telemetry_manager"
    )
    end_marker = "app.state.telemetry_manager = telemetry_manager"
    end = source.index(end_marker, start) + len(end_marker)
    block = source[start:end]

    base_indent = " " * 16
    dedented_lines = []
    for line in block.splitlines():
        if line.startswith(base_indent):
            dedented_lines.append(line[len(base_indent) :])
        elif line.strip() == "":
            dedented_lines.append("")
        else:
            dedented_lines.append(line)
    return "\n".join(dedented_lines)


class TestLifespanTelemetryClusterNodeIdRuntimeWiring:
    """Runtime guard: exec()'s the real sliced wiring block from lifespan.py
    and verifies the resolved node id matches the REAL resolver's
    independently-computed result for the same input, for a
    cluster-configured node_id, solo mode (cluster=None), and an
    empty-string node_id."""

    def _run_wiring_block(self, monkeypatch, server_config):
        """exec() the real, sliced production wiring block. get_telemetry_
        manager is intercepted via monkeypatch (auto-restored) purely to
        capture the cluster_node_id it was called with; the resolver itself
        (resolve_cluster_node_id, imported inside the exec'd block) is left
        real/unpatched so this test proves genuine integration, not a
        hand-written stand-in for the resolver's precedence rules."""
        import code_indexer.server.telemetry as real_telemetry_module

        captured = {}

        def _stub_get_telemetry_manager(config, cluster_node_id=None):
            captured["cluster_node_id"] = cluster_node_id
            return SimpleNamespace(cluster_node_id=cluster_node_id)

        monkeypatch.setattr(
            real_telemetry_module, "get_telemetry_manager", _stub_get_telemetry_manager
        )

        app = SimpleNamespace(state=SimpleNamespace())
        exec_globals = {"server_config": server_config, "app": app}
        exec(  # noqa: S102 - intentional exec of real production source slice
            compile(
                _extract_wiring_block_source(),
                "<lifespan_telemetry_wiring_1676>",
                "exec",
            ),
            exec_globals,
        )
        return app, captured

    def test_cluster_configured_node_id_matches_real_resolver(self, monkeypatch):
        server_config = _make_server_config("explicit-cluster-node")
        expected = resolve_cluster_node_id(
            {"cluster": {"node_id": "explicit-cluster-node"}}
        )

        app, captured = self._run_wiring_block(monkeypatch, server_config)

        assert captured["cluster_node_id"] == expected
        assert app.state.telemetry_manager.cluster_node_id == expected

    def test_solo_mode_no_cluster_config_matches_real_resolver_fallback(
        self, monkeypatch
    ):
        """server_config.cluster is None (solo/non-cluster deployment) --
        must match resolve_cluster_node_id(None)'s existing fallback,
        unchanged."""
        server_config = _make_server_config(None)
        expected = resolve_cluster_node_id(None)

        _app, captured = self._run_wiring_block(monkeypatch, server_config)

        assert captured["cluster_node_id"] == expected

    def test_empty_string_node_id_matches_real_resolver_fallback(self, monkeypatch):
        """A ClusterConfig with node_id="" (the dataclass default) must
        fall back exactly as resolve_cluster_node_id({"cluster": {"node_id":
        ""}}) does -- proving this call site defers entirely to the
        resolver's precedence rules rather than reimplementing them."""
        server_config = _make_server_config("")
        expected = resolve_cluster_node_id({"cluster": {"node_id": ""}})

        _app, captured = self._run_wiring_block(monkeypatch, server_config)

        assert captured["cluster_node_id"] == expected
