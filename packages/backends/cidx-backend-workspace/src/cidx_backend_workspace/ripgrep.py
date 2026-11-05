"""The local ripgrep backend: ``rg`` runs in this pod.

Registered on ``code_indexer.ripgrep_backend`` as ``local``. The process is
run by the :class:`~cidx_sdk.seams.ripgrep.BoundedProcessRunner` the core
passes to :func:`make_ripgrep_backend` (its size-capped executor): the byte
cap has to kill ripgrep while it runs, and the deadline has to be enforced
where the process is spawned, so neither is reimplemented here.

It does NOT take ``stop_after_matches``: ripgrep here writes to a local file
the caller's parser already stops reading at ``max_results``. The saving that
option exists for -- not streaming a whole repository's matches across the
network -- is the delegated one.
"""

from __future__ import annotations

import asyncio
import math
import shutil
from typing import Sequence

from cidx_sdk.seams.ripgrep import BoundedProcessRunner, RipgrepRun


class LocalRipgrepBackend:
    """Runs ripgrep in this container through the core's bounded runner."""

    def __init__(self, runner: BoundedProcessRunner) -> None:
        self._runner = runner

    @property
    def runner(self) -> BoundedProcessRunner:
        """The runner ripgrep is executed with."""
        return self._runner

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
        return await self._runner.run_bounded(
            argv=list(argv),
            working_dir=working_dir,
            output_file_path=output_file_path,
            # The runner takes whole seconds. Round UP so a fractional
            # remaining deadline is never silently shortened.
            timeout_seconds=int(math.ceil(timeout_seconds)),
            max_output_bytes=max_output_bytes,
        )

    async def supports_pcre2(self) -> bool:
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


def make_ripgrep_backend(runner: BoundedProcessRunner) -> LocalRipgrepBackend:
    """Entry point ``local`` on ``code_indexer.ripgrep_backend``.

    Takes the core's bounded runner -- the one seam factory with an argument
    in the local mode; a delegate's factory takes none.
    """
    return LocalRipgrepBackend(runner)
