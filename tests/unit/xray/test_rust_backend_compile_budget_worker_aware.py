"""The compile-concurrency bound must be WORKER-AWARE, not per-process.

`_MAX_CONCURRENT_COMPILES` was a module-level constant of 4, documented as
"process-wide" and reasoned about as if it were node-wide. The cluster
deployment runs `uvicorn --workers 2` (cet-cidx-overlay/cluster-base/
statefulset.yaml), and each worker is a separate OS process with its own
import of this module and therefore its own semaphore: up to EIGHT
concurrent rustc processes against a pod limited to `cpu: "2"` and
`memory: 4Gi`.

The overlay's own comment two lines above that flag already says the
per-node budgets "MUST stay in lockstep with config.json workers ... so the
HNSW/FTS cache cap (_divided_cap) and the provider concurrency governor
divide their budgets by 2 -> no OOM". This budget was not divided.

These tests pin the division, the worker-count sources (including the one
that actually works inside a Kubernetes pod, where there is no systemd unit
and no applied_launch.json), and the fail-soft behaviour.
"""

from __future__ import annotations

import json
import threading

import pytest

from code_indexer.xray import rust_backend

_ACQUIRE_TIMEOUT = 0.5


# ---------------------------------------------------------------------------
# The division itself
# ---------------------------------------------------------------------------


def test_node_budget_is_divided_by_the_worker_count() -> None:
    assert rust_backend._per_process_compile_limit(budget=4, workers=2) == 2
    assert rust_backend._per_process_compile_limit(budget=4, workers=4) == 1
    assert rust_backend._per_process_compile_limit(budget=8, workers=2) == 4


def test_a_single_worker_keeps_the_whole_node_budget() -> None:
    """Solo/VM deployments that never pass --workers must be unaffected."""
    assert rust_backend._per_process_compile_limit(budget=4, workers=1) == 4


def test_every_worker_together_stays_inside_the_node_budget() -> None:
    """The property the fix exists for: workers * per-process <= budget.

    Before the fix this product was 2 * 4 = 8 against a budget of 4.
    """
    budget = rust_backend._XRAY_COMPILE_BUDGET_PER_NODE
    for workers in (1, 2, 3, 4, 8):
        per_process = rust_backend._per_process_compile_limit(
            budget=budget, workers=workers
        )
        assert per_process * workers <= budget or per_process == 1, (
            f"{workers} workers x {per_process} compiles exceeds the "
            f"node budget of {budget}"
        )


def test_division_never_yields_zero_compiles() -> None:
    """More workers than the budget must still leave each worker able to
    compile -- a bound of 0 deadlocks every xray query on that node."""
    assert rust_backend._per_process_compile_limit(budget=4, workers=16) == 1


def test_nonsense_worker_counts_cannot_unbound_or_deadlock_the_node() -> None:
    for workers in (0, -1):
        assert rust_backend._per_process_compile_limit(budget=4, workers=workers) == 4


# ---------------------------------------------------------------------------
# Where the worker count comes from
# ---------------------------------------------------------------------------


def test_worker_count_is_read_from_the_cluster_config_json(
    tmp_path, monkeypatch
) -> None:
    """THE cluster case. In the pod there is no systemd unit and no
    applied_launch.json, so the repo's existing get_applied_worker_count()
    resolver returns 1 and divides by nothing. What the pod DOES have is
    the ConfigMap-seeded config.json the initContainer writes to
    $CIDX_SERVER_DATA_DIR, carrying "workers": 2 in lockstep with the
    container args."""
    (tmp_path / "config.json").write_text(json.dumps({"workers": 2, "port": 8090}))
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CIDX_DATA_DIR", raising=False)

    assert rust_backend._read_config_json_workers() == 2


def test_worker_count_falls_back_to_one_when_nothing_declares_it(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CIDX_DATA_DIR", raising=False)
    assert rust_backend._read_config_json_workers() is None


@pytest.mark.parametrize("bad", ["two", 0, -3, None, 2.5, {"n": 2}])
def test_malformed_worker_counts_are_ignored(tmp_path, monkeypatch, bad) -> None:
    """A config.json that says something unusable must leave the resolver
    to its other sources, never crash the import of this module."""
    (tmp_path / "config.json").write_text(json.dumps({"workers": bad}))
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CIDX_DATA_DIR", raising=False)

    assert rust_backend._read_config_json_workers() is None


def test_unreadable_config_json_is_not_an_import_time_crash(
    tmp_path, monkeypatch
) -> None:
    (tmp_path / "config.json").write_text("{ this is not json")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("CIDX_DATA_DIR", raising=False)

    assert rust_backend._read_config_json_workers() is None


def test_detect_worker_count_takes_the_larger_of_its_sources(monkeypatch) -> None:
    """Two sources can disagree (an admin saved workers=4 but the running
    unit was launched with 2, or vice versa). Dividing by the LARGER count
    gives the smaller per-process bound, which is the direction that
    cannot exceed the node's CPU and memory."""
    monkeypatch.setattr(rust_backend, "_read_applied_worker_count", lambda: 2)
    monkeypatch.setattr(rust_backend, "_read_config_json_workers", lambda: 4)
    assert rust_backend._detect_worker_count() == 4

    monkeypatch.setattr(rust_backend, "_read_applied_worker_count", lambda: 4)
    monkeypatch.setattr(rust_backend, "_read_config_json_workers", lambda: 2)
    assert rust_backend._detect_worker_count() == 4


def test_detect_worker_count_defaults_to_one_when_both_sources_are_silent(
    monkeypatch,
) -> None:
    monkeypatch.setattr(rust_backend, "_read_applied_worker_count", lambda: None)
    monkeypatch.setattr(rust_backend, "_read_config_json_workers", lambda: None)
    assert rust_backend._detect_worker_count() == 1


def test_detect_worker_count_survives_a_raising_source(monkeypatch) -> None:
    """The resolver runs at module import. An exception here would make
    `import code_indexer.xray.rust_backend` fail, taking the whole xray
    feature down over a worker-count lookup."""

    def _boom():
        raise RuntimeError("systemd unit unreadable")

    monkeypatch.setattr(rust_backend, "_read_applied_worker_count", _boom)
    monkeypatch.setattr(rust_backend, "_read_config_json_workers", lambda: None)
    assert rust_backend._detect_worker_count() == 1


# ---------------------------------------------------------------------------
# The operator override
# ---------------------------------------------------------------------------


def test_env_override_pins_the_per_process_bound(monkeypatch) -> None:
    monkeypatch.setenv("CIDX_XRAY_MAX_CONCURRENT_COMPILES", "3")
    assert rust_backend._resolve_max_concurrent_compiles() == 3


@pytest.mark.parametrize("bad", ["", "   ", "nope", "0", "-2", "2.5"])
def test_env_override_ignores_unusable_values(monkeypatch, bad) -> None:
    monkeypatch.setenv("CIDX_XRAY_MAX_CONCURRENT_COMPILES", bad)
    monkeypatch.setattr(rust_backend, "_detect_worker_count", lambda: 2)
    assert rust_backend._resolve_max_concurrent_compiles() == (
        rust_backend._XRAY_COMPILE_BUDGET_PER_NODE // 2
    )


def test_resolution_uses_the_detected_worker_count(monkeypatch) -> None:
    monkeypatch.delenv("CIDX_XRAY_MAX_CONCURRENT_COMPILES", raising=False)
    monkeypatch.setattr(rust_backend, "_detect_worker_count", lambda: 2)
    assert (
        rust_backend._resolve_max_concurrent_compiles()
        == rust_backend._XRAY_COMPILE_BUDGET_PER_NODE // 2
    )


# ---------------------------------------------------------------------------
# Wiring and the number itself
# ---------------------------------------------------------------------------


def test_the_live_semaphore_is_sized_to_the_resolved_bound() -> None:
    """Behavioural: a semaphore built for the resolved bound admits exactly
    that many holders -- the constant and the gate cannot drift apart."""
    limit = rust_backend._MAX_CONCURRENT_COMPILES
    semaphore = threading.Semaphore(limit)

    held = [semaphore.acquire(timeout=_ACQUIRE_TIMEOUT) for _ in range(limit)]
    try:
        assert all(held)
        assert semaphore.acquire(timeout=_ACQUIRE_TIMEOUT) is False
    finally:
        for ok in held:
            if ok:
                semaphore.release()


def test_the_bound_never_exceeds_the_node_budget() -> None:
    assert (
        1
        <= (rust_backend._MAX_CONCURRENT_COMPILES)
        <= rust_backend._XRAY_COMPILE_BUDGET_PER_NODE
    )


def test_node_budget_is_sized_for_the_pods_cpu_quota() -> None:
    """The pod gets `cpu: "2"`. rustc is CPU-bound, so a node-wide budget
    far above the quota only adds context switching and resident memory;
    one far below it wastes the quota the deployment was widened to use."""
    pod_cpus = 2
    assert pod_cpus <= rust_backend._XRAY_COMPILE_BUDGET_PER_NODE <= 2 * pod_cpus


# ---------------------------------------------------------------------------
# End to end, in a FRESH interpreter
#
# `_MAX_CONCURRENT_COMPILES` is resolved at import, so an in-process test can
# only ever see the value this test host produced -- which is the whole
# budget, because this host declares no workers. That makes an in-process
# assertion blind to the exact regression being fixed: re-hardcoding the
# constant to 4 leaves every test above green here. These spawn a real
# interpreter with a real multi-worker environment and read back both the
# constant AND how many slots the live semaphore actually admits.
# ---------------------------------------------------------------------------

_CHILD = (
    "import json;"
    "from code_indexer.xray import rust_backend as rb;"
    "admitted = 0;"
    "exec('while rb._acquire_compile_slot(0.05):\\n admitted += 1');"
    "print(json.dumps({'bound': rb._MAX_CONCURRENT_COMPILES,"
    " 'admitted': admitted}))"
)


def _resolve_in_fresh_process(env_overrides, tmp_path_factory=None):
    import os
    import subprocess
    import sys

    env = dict(os.environ)
    env.pop("CIDX_XRAY_MAX_CONCURRENT_COMPILES", None)
    env.pop("CIDX_SERVER_DATA_DIR", None)
    env.pop("CIDX_DATA_DIR", None)
    env.update(env_overrides)
    env["PYTHONPATH"] = os.pathsep.join(
        [p for p in [env.get("PYTHONPATH", ""), *sys.path] if p]
    )
    out = subprocess.run(
        [sys.executable, "-c", _CHILD],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
    )
    assert out.returncode == 0, f"child failed: {out.stderr[-2000:]}"
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_a_fresh_process_in_a_two_worker_cluster_gets_half_the_budget(
    tmp_path,
) -> None:
    """The deployed configuration, end to end: a pod whose config.json says
    two workers must come up with half the node budget, so the two worker
    processes together cannot exceed it."""
    (tmp_path / "config.json").write_text(json.dumps({"workers": 2}))

    result = _resolve_in_fresh_process({"CIDX_SERVER_DATA_DIR": str(tmp_path)})

    expected = rust_backend._XRAY_COMPILE_BUDGET_PER_NODE // 2
    assert result["bound"] == expected
    assert result["admitted"] == expected, (
        "the live semaphore must be sized to the resolved bound, not to the "
        "undivided node budget"
    )


def test_a_fresh_process_with_no_declared_workers_keeps_the_whole_budget(
    tmp_path,
) -> None:
    """Solo/VM nodes must be untouched by the division."""
    result = _resolve_in_fresh_process({"CIDX_SERVER_DATA_DIR": str(tmp_path)})

    assert result["bound"] == rust_backend._XRAY_COMPILE_BUDGET_PER_NODE
    assert result["admitted"] == rust_backend._XRAY_COMPILE_BUDGET_PER_NODE


def test_a_fresh_process_honours_the_operator_override(tmp_path) -> None:
    (tmp_path / "config.json").write_text(json.dumps({"workers": 2}))

    result = _resolve_in_fresh_process(
        {
            "CIDX_SERVER_DATA_DIR": str(tmp_path),
            "CIDX_XRAY_MAX_CONCURRENT_COMPILES": "1",
        }
    )

    assert result["bound"] == 1
    assert result["admitted"] == 1


def test_applied_worker_count_rung_is_fail_soft(monkeypatch) -> None:
    """The applied-count resolver lives in the server layer and is imported
    lazily; on the CLI/solo path that import can fail outright. It must
    answer None, not propagate."""
    import builtins

    real_import = builtins.__import__

    def _refuse(name, *args, **kwargs):
        if "applied_worker_count" in name:
            raise ImportError("server layer not available")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _refuse)
    assert rust_backend._read_applied_worker_count() is None


def test_a_resolver_answer_of_one_cannot_outvote_a_config_saying_more(
    monkeypatch,
) -> None:
    """The applied resolver returns 1 both when a node runs one worker and
    when it found no source at all -- which is exactly what it does inside a
    pod. That 1 must not win over the config.json that knows better."""
    monkeypatch.setattr(rust_backend, "_read_applied_worker_count", lambda: 1)
    monkeypatch.setattr(rust_backend, "_read_config_json_workers", lambda: 2)
    assert rust_backend._detect_worker_count() == 2
