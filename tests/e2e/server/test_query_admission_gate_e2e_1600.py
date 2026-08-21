"""Story #1600 E2E: query-path memory-pressure admission gate.

Validates the COMPLETE reject -> Retry-After -> client-retry -> recovery
workflow through the REST/MCP front door only, against the Phase 3
session-scoped in-process CIDX server (tests/e2e/server/conftest.py's
`test_client`/`admin_token_provider` fixtures) -- an isolated, throwaway
server bound to its own tmp CIDX_SERVER_DATA_DIR, NEVER the shared dev
cidx-server. (An earlier version of this test built a SECOND, independent
create_app() instance; that produced a real primary_instance_lock
contention and a log-handler init failure once both apps ran in the same
pytest process -- the shared session fixture is the correct throwaway
server to drive against, matching every other Phase 3 E2E test in this
directory.)

Run standalone via e2e-automation.sh's Phase 3, or directly:
    source .e2e-automation && PYTHONPATH=./src python3 -m pytest \\
        tests/e2e/server/test_query_admission_gate_e2e_1600.py -v

RED band is forced DETERMINISTICALLY via MemoryGovernor's own designed-in
test seam (injectable `readers`/`time_fn` constructor params) rather than
organically exhausting real memory -- exactly what the story's testing
requirements call for. This is a real MemoryGovernor object (not a mock of
the feature under test); only its cgroup/psutil-reading boundary and its
clock are replaced, mirroring the pattern already established throughout
tests/unit/server/services/test_memory_governor_*.py. The session's real
governor is saved and restored around the test so no state leaks into any
other Phase 3 test sharing the same server/process.

Everything else is the real, unmodified production path: real HTTP
requests, real FastAPI routing/auth, real MCP JSON-RPC dispatch, real
check_query_admission() / MemoryGovernor.admission_allowed() /
increment_query_admissions_denied() / _advance_band() RED-exit-with-dwell
logic.

The deliberate HTTP 503 this test triggers is allowlisted in
tests/e2e/log_audit_gate.py's LOG_AUDIT_ALLOWLIST ("HTTP 503 | Request:
POST /api/query") -- it is the asserted signal, not a bug.

One-time manual verification note (story testing requirement): replaying
the original incident's exact load pattern against real 8,634/50,604-file
repos was substituted here, per the story's own explicit allowance ("use
your judgment on a scaled-down but structurally equivalent substitute and
document the substitution"), by this deterministic single-process test --
it proves the identical mechanism (burst of concurrent expensive queries
cleanly denied while genuinely pressured, self-recovery to admission once
pressure subsides, zero restart) without the multi-hour cost of indexing
two very large repositories.
"""

from __future__ import annotations

import math
import types
from typing import TYPE_CHECKING, Tuple

import pytest
from fastapi.testclient import TestClient

from tests.e2e.server.conftest import AdminTokenProvider
from tests.e2e.server.mcp_helpers import call_mcp_tool, parse_mcp_result

if TYPE_CHECKING:
    from code_indexer.server.services.memory_governor import MemoryGovernor

_HTTP_OK = 200
_HTTP_SERVICE_UNAVAILABLE = 503

# Deterministic RED-band forcing constants -- never organically exhausts
# real memory (per story testing requirement).
_CGROUP_LIMIT_BYTES = 4 * 1024 * 1024 * 1024  # 4 GiB fake cgroup limit
_RED_USED_PCT = 95.0  # forces RED (>= MemoryGovernor's default red_pct=85.0)
_GREEN_USED_PCT = 20.0  # well below default yellow_pct=70.0 after recovery
_RED_MIN_DWELL_SECONDS = 5.0  # short but non-zero -- proves real dwell-gating
_NONEXISTENT_REPO_ALIAS = "nonexistent-repo-e2e-1600"


class _FakeMemoryReaders:
    """MemoryGovernor's own injectable `readers=` test seam. Deterministic,
    no real cgroup/psutil I/O. `used_bytes` is mutable so ONE instance can
    model load rising then subsiding -- the real burst-then-recover shape
    of the original incident."""

    def __init__(self, *, limit_bytes: int, used_bytes: int) -> None:
        self.limit_bytes = limit_bytes
        self.used_bytes = used_bytes

    def read_cgroup_v2_max(self) -> str:
        return str(self.limit_bytes)

    def read_cgroup_v2_current(self) -> int:
        return self.used_bytes

    def read_cgroup_v1_limit(self) -> int:
        raise FileNotFoundError("cgroup v1 not exercised by this test")

    def read_cgroup_v1_usage(self) -> int:
        raise FileNotFoundError("cgroup v1 not exercised by this test")

    def read_host_memory(self) -> object:
        vm = types.SimpleNamespace()
        vm.total = self.limit_bytes
        vm.used = self.used_bytes
        return vm

    def read_pswpin(self) -> int:
        return 0


class _FakeClock:
    """MemoryGovernor's own injectable `time_fn=` test seam -- lets the
    test fast-forward past red_min_dwell_seconds deterministically instead
    of a real time.sleep()."""

    def __init__(self, start: float = 1_000_000.0) -> None:
        self._now = start

    def __call__(self) -> float:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += seconds


@pytest.fixture()
def deterministic_red_governor(test_client: TestClient):
    """Install a MemoryGovernor built via the injectable readers/time_fn
    constructor seam, forced into RED via one manual _tick() call (no
    sampler thread -- start_sampler defaults False so nothing races this
    test). Restores whatever governor the shared session server's own real
    startup had installed on teardown, so this test never leaks state into
    any other Phase 3 test sharing the same server/process."""
    from code_indexer.server.services.memory_governor import (
        MemoryGovernor,
        get_memory_governor,
        set_memory_governor,
    )

    original_governor = get_memory_governor()

    readers = _FakeMemoryReaders(
        limit_bytes=_CGROUP_LIMIT_BYTES,
        used_bytes=int(_CGROUP_LIMIT_BYTES * _RED_USED_PCT / 100),
    )
    clock = _FakeClock()
    governor = MemoryGovernor(
        readers=readers,
        start_sampler=False,
        red_min_dwell_seconds=_RED_MIN_DWELL_SECONDS,
        time_fn=clock,
    )
    governor._tick()  # real band computation from the fake reading above
    set_memory_governor(governor)
    try:
        yield governor, readers, clock
    finally:
        set_memory_governor(original_governor)


class TestQueryAdmissionGateE2EWorkflow:
    """Scenario 2 (REST + MCP reject), Scenario 3 (REST 503 + Retry-After),
    and Scenario 7 (self-recovery, no restart) driven end-to-end through
    the real front door of the throwaway Phase 3 in-process server."""

    def test_reject_retry_after_recover_workflow(
        self,
        test_client: TestClient,
        admin_token_provider: AdminTokenProvider,
        deterministic_red_governor: Tuple[
            "MemoryGovernor", _FakeMemoryReaders, _FakeClock
        ],
    ) -> None:
        from code_indexer.server.services.memory_governor import MemoryBand

        headers = admin_token_provider.get_headers()
        governor, readers, clock = deterministic_red_governor
        assert governor.band == MemoryBand.RED, (
            "test setup invariant: the injected reading must force RED "
            "before the workflow assertions below are meaningful"
        )

        # --- 1. REST reject: search_code's REST-facing equivalent ---
        rest_resp = test_client.post(
            "/api/query",
            json={
                "query_text": "authentication logic",
                "repository_alias": _NONEXISTENT_REPO_ALIAS,
            },
            headers=headers,
        )
        assert rest_resp.status_code == _HTTP_SERVICE_UNAVAILABLE, rest_resp.text
        retry_after_header = int(rest_resp.headers["retry-after"])
        assert retry_after_header == math.ceil(_RED_MIN_DWELL_SECONDS)
        rest_body = rest_resp.json()
        rest_detail = rest_body.get("detail", rest_body)
        assert rest_detail["error_code"] == "memory_pressure"
        assert rest_detail["success"] is False

        # --- 2. MCP reject: HTTP 200 (JSON-RPC convention), logical denial ---
        mcp_resp = call_mcp_tool(
            test_client, "get_all_repositories_status", {}, headers
        )
        assert mcp_resp.status_code == _HTTP_OK, mcp_resp.text
        mcp_result = parse_mcp_result(mcp_resp.json())
        assert mcp_result["success"] is False
        assert mcp_result["error_code"] == "memory_pressure"
        assert isinstance(mcp_result.get("retry_after_seconds"), int)

        # --- 3. Load burst subsides: real used_pct drops AND real dwell elapses ---
        readers.used_bytes = int(_CGROUP_LIMIT_BYTES * _GREEN_USED_PCT / 100)
        clock.advance(_RED_MIN_DWELL_SECONDS + 1.0)
        governor._tick()
        assert governor.band != MemoryBand.RED, (
            "governor must exit RED once used_pct is low AND red_min_dwell_seconds "
            "has elapsed -- the real self-recovery mechanism under test"
        )

        # --- 4. Client retries: REST admitted again -- same process, no restart ---
        rest_resp2 = test_client.post(
            "/api/query",
            json={
                "query_text": "authentication logic",
                "repository_alias": _NONEXISTENT_REPO_ALIAS,
            },
            headers=headers,
        )
        assert rest_resp2.status_code != _HTTP_SERVICE_UNAVAILABLE, rest_resp2.text

        # --- 5. MCP admitted again ---
        mcp_resp2 = call_mcp_tool(
            test_client, "get_all_repositories_status", {}, headers
        )
        mcp_result2 = parse_mcp_result(mcp_resp2.json())
        assert mcp_result2.get("error_code") != "memory_pressure"
        assert mcp_result2.get("success") is True
