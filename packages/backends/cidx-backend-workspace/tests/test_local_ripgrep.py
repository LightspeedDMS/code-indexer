"""The local ripgrep backend: what it adds to the runner it is given.

The full ripgrep conformance suite runs against this backend as the core
resolves it, with the core's executor as the runner
(``tests/unit/seams/test_ripgrep_seam_resolution.py``); the runner is the
core's, so it is not reproduced here.
"""

from __future__ import annotations

from typing import Any, Dict, List, Sequence

import pytest

from cidx_backend_workspace.ripgrep import LocalRipgrepBackend, make_ripgrep_backend
from cidx_sdk.seams.ripgrep import BoundedProcessRunner, RipgrepBackend, RipgrepRun


class _RecordingRunner:
    def __init__(self) -> None:
        self.calls: List[Dict[str, Any]] = []

    @property
    def max_workers(self) -> int:
        return 3

    async def run_bounded(
        self,
        *,
        argv: Sequence[str],
        working_dir: str,
        output_file_path: str,
        timeout_seconds: int,
        max_output_bytes: int,
    ) -> RipgrepRun:
        self.calls.append(
            {
                "argv": list(argv),
                "working_dir": working_dir,
                "output_file_path": output_file_path,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
            }
        )
        return RipgrepRun(
            exit_code=0,
            stderr_output="",
            timed_out=False,
            timeout_seconds=float(timeout_seconds),
            output_capped=True,
        )


def test_the_factory_takes_the_runner() -> None:
    runner = _RecordingRunner()
    backend = make_ripgrep_backend(runner)
    assert isinstance(runner, BoundedProcessRunner)
    assert isinstance(backend, LocalRipgrepBackend)
    assert isinstance(backend, RipgrepBackend)
    assert backend.is_local is True and backend.runner is runner


@pytest.mark.asyncio
async def test_the_deadline_is_rounded_up_and_everything_else_forwarded() -> None:
    runner = _RecordingRunner()
    run = await make_ripgrep_backend(runner).run(
        argv=("rg", "--json", "-e", "x"),
        working_dir="/srv/example/repo",
        output_file_path="/srv/example/out.jsonl",
        timeout_seconds=12.3,
        max_output_bytes=999,
    )
    assert runner.calls == [
        {
            "argv": ["rg", "--json", "-e", "x"],
            "working_dir": "/srv/example/repo",
            "output_file_path": "/srv/example/out.jsonl",
            "timeout_seconds": 13,
            "max_output_bytes": 999,
        }
    ]
    assert run.output_capped is True
