"""The ripgrep seam: run one ripgrep invocation somewhere.

Entry-point group ``code_indexer.ripgrep_backend``. ``local`` runs ``rg`` in
this pod; ``delegated`` runs it in workspace-mcp (``POST /content/rg``), which
validates the argv against an allowlist. What cidx sends there is pinned by a
Pact (``tests/contracts/workspace-mcp/``) owned by the delegate distribution.

Moved from ``code_indexer.kernel.seam_types`` (originally
``server/storage/shared/ripgrep_backend.py``); the old modules re-export.

The local implementation (``cidx-backend-workspace``) does not spawn the
process itself: the core's size-capped executor does, handed to the local
factory as a :class:`BoundedProcessRunner` (the factory takes it as its one
argument). The byte cap must terminate the process WHILE IT RUNS, which is the
executor's job, not the seam's.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable


@dataclass(frozen=True)
class RipgrepRun:
    """What the process did. Mirrors the fields of ``ExecutionResult`` the
    existing call sites already branch on, so migrating a call site is a
    substitution rather than a rewrite."""

    exit_code: Optional[int]
    stderr_output: str
    timed_out: bool
    timeout_seconds: Optional[float]
    error_message: str = ""
    #: The executor classified the run as an error (its ``ExecutionStatus.ERROR``).
    #: Kept as its own flag rather than re-derived from ``exit_code``: the
    #: executor raises it for spawn failures and byte-cap kills too, where the
    #: exit code alone would be misleading. Callers must still separate
    #: ``found_no_matches`` from a genuine failure before reporting one.
    is_error: bool = False
    #: The byte ceiling KILLED the process mid-run, so the scan is incomplete.
    #: The caller reports this to users as a truncated search, so a backend that
    #: silently drops it turns a partial result into an apparently complete one.
    output_capped: bool = False
    #: The backend stopped reading -- and so killed ripgrep -- after
    #: ``stop_after_matches`` match events, as asked. A SUCCESSFUL run whose
    #: output holds at least that many matches: the caller reports it as
    #: truncated at ``max_results`` (it is never read-capped, never an error).
    stopped_early: bool = False

    @property
    def found_no_matches(self) -> bool:
        """Ripgrep's "searched fine, matched nothing".

        Exit 1 with empty stderr. Anything else with a non-zero code is an
        error, and callers must keep treating it as one.
        """
        return self.exit_code == 1 and not self.stderr_output


@runtime_checkable
class RipgrepBackend(Protocol):
    """Structural interface for running one ripgrep invocation."""

    @property
    def is_local(self) -> bool:
        """True when the binary runs in this process's container.

        Drives the engine probe: a delegated backend's ripgrep is not on this
        PATH, so ``shutil.which("rg")`` says nothing about whether ripgrep is
        available and must not be consulted.
        """
        ...

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        """Run ``argv``, writing stdout to ``output_file_path``.

        OPTIONAL keyword ``stop_after_matches: Optional[int]`` -- not part of
        this signature, so existing backends still satisfy the protocol. A
        backend that declares it in ``run`` promises: once it has written
        ``stop_after_matches`` ``rg --json`` events of ``"type": "match"`` to
        ``output_file_path`` it stops reading and terminates ripgrep, keeps
        every complete line written so far, and returns a run with
        ``stopped_early=True``, ``is_error=False``. ``None`` means "no limit".

        ``timeout_seconds`` is the caller's remaining deadline, and is REQUIRED.
        A backend needing whole seconds rounds UP, never down.
        """
        ...

    async def supports_pcre2(self) -> bool:
        """Whether this backend's ripgrep was built with PCRE2.

        Asked at most once per process by the caller; a backend may cache.
        """
        ...


@runtime_checkable
class BoundedProcessRunner(Protocol):
    """Runs one process with a wall-clock deadline and an output byte cap.

    What the local ripgrep backend is given to run ``rg`` with. The core
    supplies it (its size-capped subprocess executor); a backend never spawns
    an uncapped process of its own.
    """

    @property
    def max_workers(self) -> int:
        """How many runs the runner executes concurrently."""
        ...

    async def run_bounded(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> RipgrepRun:
        """Run ``argv`` in ``working_dir`` writing stdout to
        ``output_file_path``; kill it at ``timeout_seconds`` (whole seconds)
        or once stdout exceeds ``max_output_bytes``. Never raises for a
        failing process: the outcome is the returned record."""
        ...
