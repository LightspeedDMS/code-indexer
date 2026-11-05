"""Ripgrep backend abstraction for cidx's regex search.

The sibling of ``GitBackend``/``CloneBackend``/the SCIP indexer seam: cidx's job
is semantic search, and the ripgrep binary it shells out to is a tool it should
not have to carry. ``RegexSearchService`` builds an ``rg`` argv and hands it to
``SubprocessExecutor``; this module makes that one hop pluggable so the execution
can move to workspace-mcp, which owns content search by charter.

- ``LocalRipgrepBackend`` -- runs the argv through ``SubprocessExecutor`` exactly
  as today. The DEFAULT, so selecting nothing is a zero-behaviour-change. It
  lives in ``cidx-backend-workspace`` (entry ``local``) and is reached through
  the seam resolver (:func:`local_ripgrep_backend`); the executor reaches it
  as the injected :class:`SubprocessExecutorRunner`.
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
* ``stop_after_matches`` is OPTIONAL (see ``RipgrepBackend.run``). The caller
  passes it only to a backend whose ``run`` names the parameter
  (:func:`accepts_stop_after_matches`), so a backend predating it keeps working.
* CANCELLATION is the other way a delegated run is stopped early, and the one
  the workspace-mcp plugin implements: the caller awaits ``run`` as a task,
  tails ``output_file_path``, and cancels the task once it has enough complete
  match lines. A delegated backend must treat cancellation as "stop now":
  close its connection so the server kills ripgrep, and leave the lines it
  already wrote. The caller cuts the file back to the lines it counted and
  reports a successful, truncated run (``stopped_early``).
"""

from __future__ import annotations

import logging
from typing import Optional, Sequence

# The local implementation lives in its own distribution
# (packages/backends/cidx-backend-workspace) and is selected by the seam
# resolver under the entry name ``local``; re-exported for existing imports.
from cidx_backend_workspace.ripgrep import (
    LocalRipgrepBackend as LocalRipgrepBackend,
)

# Moved to the kernel (Phase 2 of plans/designs/modular-packaging-addons.md);
# re-exported so existing imports keep resolving to the same objects.
from code_indexer.kernel.seam_types import (
    RipgrepBackend as RipgrepBackend,
    RipgrepRun as RipgrepRun,
)

logger = logging.getLogger(__name__)


def accepts_stop_after_matches(backend: object) -> bool:
    """Whether *backend*'s ``run`` takes the optional ``stop_after_matches``.

    Read from the signature, so a backend (or a test double) predating the
    parameter is simply never sent it.
    """
    import inspect

    run = getattr(backend, "run", None)
    if run is None:
        return False
    try:
        parameters = inspect.signature(run).parameters
    except (TypeError, ValueError):
        return False
    return "stop_after_matches" in parameters


class SubprocessExecutorRunner:
    """The core's :class:`~cidx_sdk.seams.ripgrep.BoundedProcessRunner`: one
    ``SubprocessExecutor`` run, with its byte cap and deadline.

    Handed to the local ripgrep backend (``cidx-backend-workspace``), which
    owns the ripgrep-specific part (whole-second rounding, the PCRE2 probe)
    and spawns nothing itself. The executor is looked up on its module at
    call time, so a test patching
    ``server.services.subprocess_executor.SubprocessExecutor`` reaches it.
    """

    def __init__(self, *, max_workers: int = 2) -> None:
        self._max_workers = max_workers

    @property
    def max_workers(self) -> int:
        return self._max_workers

    async def run_bounded(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> RipgrepRun:
        # Imported here, not at module scope: this module is part of the seam
        # contract and is imported by consumers that must not drag in the
        # executor (and its asyncio/thread machinery) just to name the Protocol.
        import anyio

        from code_indexer.server.services import subprocess_executor

        executor = subprocess_executor.SubprocessExecutor(max_workers=self._max_workers)
        try:
            result = await executor.execute_with_limits(
                command=list(argv),
                working_dir=working_dir,
                timeout_seconds=timeout_seconds,
                output_file_path=output_file_path,
                max_output_bytes=max_output_bytes,
            )
            return RipgrepRun(
                exit_code=result.exit_code,
                stderr_output=result.stderr_output or "",
                timed_out=result.timed_out,
                timeout_seconds=result.timeout_seconds,
                error_message=result.error_message or "",
                is_error=result.status == subprocess_executor.ExecutionStatus.ERROR,
                output_capped=bool(result.output_capped),
            )
        finally:
            # Issue #1601 round 5: shutdown(wait=True) is synchronous and
            # potentially blocking, so it is offloaded rather than run on the
            # event loop -- the same idiom the call sites in regex_search.py
            # use. wait=True (not False) so the worker pool is actually drained
            # before the executor is dropped.
            await anyio.to_thread.run_sync(executor.shutdown, True)


def local_ripgrep_backend(*, max_workers: int = 2) -> RipgrepBackend:
    """The module installed for the ripgrep seam's ``local`` mode, running
    through this process's :class:`SubprocessExecutorRunner`.

    Reached through the seam resolver, never constructed directly: with no
    local module installed this raises ``SeamUnavailableError`` naming the
    seam (there is no built-in implementation to fall back to).
    """
    from code_indexer.server.services.ripgrep_backend_plugin import (
        resolve_ripgrep_backend,
    )

    backend: RipgrepBackend = resolve_ripgrep_backend(
        "local", SubprocessExecutorRunner(max_workers=max_workers)
    )
    return backend


class RipgrepBackendFactory:
    """Create a RipgrepBackend from a backend name.

    Mirrors ``GitBackendFactory``. ``local`` (the default) runs ripgrep here,
    through the module installed for the local mode (:func:`local_ripgrep_backend`).
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
            return local_ripgrep_backend(max_workers=max_workers)
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
