"""Ripgrep backend abstraction for cidx's regex search.

The sibling of ``GitBackend``/``CloneBackend``/the SCIP indexer seam: cidx's job
is semantic search, and the ripgrep binary it shells out to is a tool it should
not have to carry. ``RegexSearchService`` builds an ``rg`` argv and hands it to
``SubprocessExecutor``; this module makes that one hop pluggable so the execution
can move to workspace-mcp, which owns content search by charter.

- ``LocalRipgrepBackend`` -- runs the argv through ``SubprocessExecutor`` exactly
  as today. The DEFAULT, so selecting nothing is a zero-behaviour-change.
- a delegated backend -- posts the same argv to workspace-mcp and returns its
  result. Supplied by a plugin, not defined here, mirroring ``GitMcpBackend``.

WHY THE SEAM SITS HERE AND NOT HIGHER. ``RegexSearchService`` is ~2,600 lines of
trigram pre-filtering, byte budgeting, ``rg --json`` event parsing, grep and
Python-multiline fallbacks, and deadline arithmetic. None of that is ripgrep; all
of it is cidx's. Delegating the *service* would move that logic across a service
boundary and duplicate it. Delegating the *exec* moves only the thing cidx should
not own, and leaves every byte cap, deadline and parser exactly where it is.

THE CONTRACT IS THE EXECUTION CONTRACT, NOT A SEARCH API. A backend receives an
argv plus the limits the caller already computed, and returns what the process
did. In particular:

* ``max_output_bytes`` must terminate the process WHILE IT RUNS, not merely
  truncate a later read -- an unbounded ``rg --json`` over a large tree is the
  failure this guards.
* ``timeout_seconds`` is the caller's remaining deadline, already apportioned.
* exit code 1 with empty stderr means "no matches" to ripgrep and MUST reach the
  caller as such, distinct from exit code 2+ which is a real error. A backend
  that collapses the two breaks ``Bug #173``.
* stdout is written to ``output_file_path`` rather than returned, because the
  caller streams it through a bounded reader.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from typing import Optional, Protocol, Sequence, runtime_checkable

logger = logging.getLogger(__name__)


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

    @property
    def found_no_matches(self) -> bool:
        """Ripgrep's "searched fine, matched nothing" (Bug #173).

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

        ``timeout_seconds`` is the caller's remaining deadline, and is REQUIRED:
        every call site already resolves one, and the underlying executor has no
        "no timeout" representation. Making it non-optional keeps a ``None`` from
        having to be invented as either 0 (an immediate kill) or infinity.
        A backend needing whole seconds rounds UP, never down -- rounding down
        would shorten a deadline the caller already apportioned.
        """
        ...

    async def supports_pcre2(self) -> bool:
        """Whether this backend's ripgrep was built with PCRE2.

        Asked at most once per process by the caller; a backend may cache.
        """
        ...


class LocalRipgrepBackend:
    """Runs ripgrep in this container via ``SubprocessExecutor``.

    Reproduces today's behaviour exactly; this is the default backend.
    """

    def __init__(self, *, max_workers: int = 2) -> None:
        self._max_workers = max_workers

    @property
    def is_local(self) -> bool:
        return True

    async def run(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ) -> RipgrepRun:
        # Imported here, not at module scope: this module is part of the seam
        # contract and is imported by consumers that must not drag in the
        # executor (and its asyncio/thread machinery) just to name the Protocol.
        import anyio

        from code_indexer.server.services.subprocess_executor import SubprocessExecutor

        executor = SubprocessExecutor(max_workers=self._max_workers)
        try:
            result = await executor.execute_with_limits(
                command=list(argv),
                working_dir=working_dir,
                # The executor takes whole seconds. Round UP so a fractional
                # remaining deadline is never silently shortened.
                timeout_seconds=int(math.ceil(timeout_seconds)),
                output_file_path=output_file_path,
                max_output_bytes=max_output_bytes,
            )
            from code_indexer.server.services.subprocess_executor import (
                ExecutionStatus,
            )

            return RipgrepRun(
                exit_code=result.exit_code,
                stderr_output=result.stderr_output or "",
                timed_out=result.timed_out,
                timeout_seconds=result.timeout_seconds,
                error_message=result.error_message or "",
                is_error=result.status == ExecutionStatus.ERROR,
                output_capped=bool(result.output_capped),
            )
        finally:
            # Issue #1601 round 5: shutdown(wait=True) is synchronous and
            # potentially blocking, so it is offloaded rather than run on the
            # event loop -- the same idiom the call sites in regex_search.py
            # use. wait=True (not False) so the worker pool is actually drained
            # before the executor is dropped.
            await anyio.to_thread.run_sync(executor.shutdown, True)

    async def supports_pcre2(self) -> bool:
        import asyncio
        import shutil

        if shutil.which("rg") is None:
            return False
        try:
            proc = await asyncio.create_subprocess_exec(
                "rg",
                "--pcre2-version",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.DEVNULL,
            )
            return await proc.wait() == 0
        except OSError:
            return False


class RipgrepBackendFactory:
    """Create a RipgrepBackend from a backend name.

    Mirrors ``GitBackendFactory``. ``local`` (the default) runs ripgrep here.
    ``delegated`` must be injected by the plugin seam and raises otherwise, so a
    misconfiguration fails loudly instead of silently running ripgrep locally --
    the whole point of delegating is that the binary may not be present.
    """

    @staticmethod
    def create(
        backend: str = "local",
        *,
        delegated_backend: Optional[RipgrepBackend] = None,
        max_workers: int = 2,
    ) -> RipgrepBackend:
        if backend == "local":
            return LocalRipgrepBackend(max_workers=max_workers)
        if backend == "delegated":
            if delegated_backend is None:
                raise RuntimeError(
                    "ripgrep_backend='delegated' selected but no delegated backend "
                    "was injected. Install the delegated backend plugin, or set "
                    "ripgrep_backend='local'. There is deliberately no fallback to "
                    "local ripgrep: an image built for delegation may not ship the "
                    "binary at all, and a silent fallback would surface as the "
                    "linear grep scan this seam exists to retire."
                )
            return delegated_backend
        raise RuntimeError(
            f"Unknown ripgrep_backend {backend!r}; expected 'local' or 'delegated'"
        )
