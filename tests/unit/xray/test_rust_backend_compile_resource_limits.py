"""Per-compile resource bounds on the xray-cli/rustc subprocess.

The compile path already bounds TIME (`RUSTC_COMPILE_TIMEOUT`, 120s, with a
process-group kill) and COUNT (`_MAX_CONCURRENT_COMPILES`). Nothing bounded
how much MEMORY or SCRATCH DISK one compile could take, and the source being
compiled is a user-supplied Rust evaluator.

That gap is not theoretical. A 150,000-statement function -- ordinary
straight-line Rust, no loops for rustc's const-eval step limit to catch and
no dead constants for DCE to absorb -- compiles in 58s (inside the 120s
timeout) with a measured 4.0 GiB peak RSS, against a pod whose entire memory
limit is 4Gi. Under the RLIMIT_DATA set here the same input dies in the
child with "memory allocation of 134217728 bytes failed" and surfaces as an
ordinary compile error.

These tests drive the REAL `_run_xray_cli_process` with substitute child
commands, because what has to be proven is that the limits actually reach
the spawned process -- not that a constant has a value.
"""

from __future__ import annotations

import json
import resource
import sys


from code_indexer.xray import rust_backend

# Child allocation sizes, in MiB.
_HEADROOM_ALLOC_MIB = 320  # > the 136 MiB a representative compile needs
_OVER_LIMIT_ALLOC_MIB = 4096  # a compile-bomb-scale allocation
_TIMEOUT_SECONDS = 120


def _run(cmd, *, acquire_compile_slot: bool = True):
    backend = rust_backend.RustNativeBackend()
    return backend._run_xray_cli_process(
        cmd,
        timeout_seconds=_TIMEOUT_SECONDS,
        on_process_spawned=None,
        acquire_compile_slot=acquire_compile_slot,
    )


def _python_child(body: str):
    return [sys.executable, "-c", body]


# ---------------------------------------------------------------------------
# The limits reach the spawned process
# ---------------------------------------------------------------------------


def test_spawned_compile_child_actually_has_the_limits_applied() -> None:
    """Read the limits back from INSIDE the child. A preexec_fn that is
    never wired into Popen, or one that silently swallows every
    setrlimit, would leave the child with the inherited limits."""
    body = (
        "import json,resource;"
        "print(json.dumps({n: resource.getrlimit(getattr(resource, n))[0]"
        " for n in ('RLIMIT_DATA','RLIMIT_FSIZE','RLIMIT_CPU','RLIMIT_CORE')}))"
    )
    stdout, error = _run(_python_child(body))

    assert error is None, f"child failed: {error}"
    limits = json.loads(stdout)
    assert limits["RLIMIT_DATA"] == rust_backend._COMPILE_RLIMIT_DATA_BYTES
    assert limits["RLIMIT_FSIZE"] == rust_backend._COMPILE_RLIMIT_FSIZE_BYTES
    assert limits["RLIMIT_CPU"] == rust_backend._COMPILE_RLIMIT_CPU_SECONDS
    assert limits["RLIMIT_CORE"] == 0


def test_limits_are_not_applied_to_the_calling_server_process() -> None:
    """The bound is per-compile. Setting it in the parent would cap the
    whole uvicorn worker -- every request, not just this compile."""
    before = resource.getrlimit(resource.RLIMIT_DATA)
    _run(_python_child("pass"))
    assert resource.getrlimit(resource.RLIMIT_DATA) == before


def test_graph_subcommand_invocations_are_not_capped() -> None:
    """--build-graph/--analyze-graph run an already-compiled dylib over a
    whole repository; their memory profile is unrelated to a compile and
    has never been measured against this bound. They pass
    acquire_compile_slot=False, and the same gate must govern the limits,
    so capping a compile can never silently cap a graph job too."""
    body = "import resource;print(resource.getrlimit(resource.RLIMIT_DATA)[0])"
    stdout, error = _run(_python_child(body), acquire_compile_slot=False)

    assert error is None, f"child failed: {error}"
    inherited = resource.getrlimit(resource.RLIMIT_DATA)[0]
    assert int(stdout.strip()) == inherited


# ---------------------------------------------------------------------------
# The limits actually bite
# ---------------------------------------------------------------------------


def test_a_compile_bomb_sized_allocation_is_killed_not_served() -> None:
    """The bound must TRIGGER, not merely be set."""
    body = f"b = bytearray({_OVER_LIMIT_ALLOC_MIB} * 1024 * 1024); print(len(b))"
    stdout, error = _run(_python_child(body))

    assert error is not None, (
        f"a {_OVER_LIMIT_ALLOC_MIB} MiB allocation must not succeed under "
        f"a {rust_backend._COMPILE_RLIMIT_DATA_BYTES // (1024 * 1024)} MiB "
        f"RLIMIT_DATA; got stdout={stdout!r}"
    )


def test_exceeding_the_bound_degrades_to_an_error_not_a_hang_or_crash() -> None:
    """A killed compile must come back as an ordinary error string from
    the same path a failed rustc uses -- never an exception out of
    _run_xray_cli_process, never a hang on the timeout."""
    body = f"b = bytearray({_OVER_LIMIT_ALLOC_MIB} * 1024 * 1024); print(len(b))"
    stdout, error = _run(_python_child(body))

    assert isinstance(error, str) and error
    assert "timed out" not in error, (
        "the limit must fail the child immediately, not leave it to the "
        "120s wall-clock timeout"
    )


def test_write_larger_than_the_file_size_bound_fails(tmp_path) -> None:
    """Scratch disk: one compile must not be able to fill the volume with
    a single unbounded output file."""
    over = rust_backend._COMPILE_RLIMIT_FSIZE_BYTES + (16 * 1024 * 1024)
    target = tmp_path / "oversized-artifact"
    body = (
        "import os;"
        f"f = open({str(target)!r}, 'wb');"
        f"f.write(b'0' * {over});"
        "f.flush();os.fsync(f.fileno());f.close();print('wrote')"
    )
    stdout, error = _run(_python_child(body))

    assert error is not None, (
        f"writing {over} bytes must fail under a "
        f"{rust_backend._COMPILE_RLIMIT_FSIZE_BYTES} byte RLIMIT_FSIZE; "
        f"got stdout={stdout!r}"
    )


# ---------------------------------------------------------------------------
# ...without breaking a legitimate compile
# ---------------------------------------------------------------------------


def test_a_legitimate_compiles_worth_of_memory_still_succeeds() -> None:
    """A representative evaluator compile peaks at 136-138 MiB RSS. A
    child allocating more than twice that must still run -- a bound tight
    enough to break real evaluators is not a bound, it is an outage."""
    body = (
        f"b = bytearray({_HEADROOM_ALLOC_MIB} * 1024 * 1024);"
        "b[0] = 1; b[-1] = 1; print(len(b))"
    )
    stdout, error = _run(_python_child(body))

    assert error is None, (
        f"{_HEADROOM_ALLOC_MIB} MiB must fit inside the per-compile bound; "
        f"got error={error!r}"
    )
    assert int(stdout.strip()) == _HEADROOM_ALLOC_MIB * 1024 * 1024


def test_a_real_so_sized_write_still_succeeds(tmp_path) -> None:
    """A compiled evaluator .so is 4.28 MiB. The file-size bound must sit
    far above it."""
    target = tmp_path / "evaluator.so"
    body = (
        "import os;"
        f"f = open({str(target)!r}, 'wb');"
        "f.write(b'0' * (5 * 1024 * 1024));f.close();"
        f"print(os.path.getsize({str(target)!r}))"
    )
    stdout, error = _run(_python_child(body))

    assert error is None, f"a 5 MiB write must succeed; got {error!r}"
    assert int(stdout.strip()) == 5 * 1024 * 1024


# ---------------------------------------------------------------------------
# The numbers themselves
# ---------------------------------------------------------------------------


def test_memory_bound_has_headroom_over_a_measured_compile() -> None:
    """Measured: 138 MiB peak RSS for a representative evaluator, and a
    640 MiB RLIMIT_DATA is the point where the real toolchain's linker
    starts failing (704 MiB succeeds). The bound must clear BOTH."""
    mib = rust_backend._COMPILE_RLIMIT_DATA_BYTES // (1024 * 1024)
    assert mib >= 704, (
        f"{mib} MiB is at or below the measured floor for a real rustc "
        f"compile of an evaluator (640 MiB fails at the link step)"
    )
    assert mib <= 2048, (
        f"{mib} MiB stops bounding anything useful on a 4Gi pod that also "
        f"admits several concurrent compiles"
    )


def test_file_size_bound_clears_a_real_artifact_by_a_wide_margin() -> None:
    mib = rust_backend._COMPILE_RLIMIT_FSIZE_BYTES // (1024 * 1024)
    assert mib >= 8 * 5, "must be far above the 4.28 MiB a real .so occupies"
    assert mib <= 512, "a per-file cap this high bounds nothing on a 10 GiB PVC"


def test_cpu_bound_sits_above_the_wall_clock_timeout() -> None:
    """RLIMIT_CPU is a backstop for the case where the wall-clock kill
    path does not fire. Below the 120s wall timeout it would instead
    become the primary timeout, failing slow-but-legitimate compiles with
    a far worse error."""
    assert rust_backend._COMPILE_RLIMIT_CPU_SECONDS > 120


def test_limits_never_raise_the_inherited_hard_limit() -> None:
    """setrlimit on a value above the inherited HARD limit raises for an
    unprivileged process; the child must clamp instead of dying, or the
    whole compile path breaks under a restrictive hard limit."""
    requested = rust_backend._COMPILE_RLIMIT_DATA_BYTES
    clamped = rust_backend._clamp_to_hard_limit(requested, hard=requested // 2)
    assert clamped == requested // 2
    assert (
        rust_backend._clamp_to_hard_limit(requested, hard=resource.RLIM_INFINITY)
        == requested
    )
