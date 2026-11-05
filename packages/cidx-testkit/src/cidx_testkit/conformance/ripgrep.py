"""Conformance suite for the ripgrep seam (``cidx_sdk.seams.ripgrep``).

Subclass :class:`RipgrepBackendContract` and provide ``ripgrep_subject``: the
backend under test, the directory it is addressed with (``working_dir``) and
where that directory's files really are. Needs ``rg`` on PATH (the local
backend runs it here; a fake remote service runs it too).

The checks are the EXECUTION contract the regex search relies on, not a
search API: stdout lands in ``output_file_path``; "searched fine, matched
nothing" (exit 1, empty stderr) is distinct from an error; the byte cap is
reported as ``output_capped`` and bounds what was written.

The tests are ``async``: the suite needs an asyncio runner (pytest-asyncio).
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import List, Tuple

import pytest

from cidx_sdk.seams.ripgrep import RipgrepBackend, RipgrepRun

#: How far past ``max_output_bytes`` a capped run may have written: the local
#: executor stops at the first read crossing the cap, one 64 KiB chunk.
CAP_OVERSHOOT_BYTES = 64 * 1024

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(shutil.which("rg") is None, reason="needs ripgrep"),
]


@dataclass
class RipgrepSubject:
    """A backend under test and the repository it searches.

    Attributes:
        backend: the implementation under test.
        working_dir: the repository path as the BACKEND is addressed.
        directory: where the repository's files are on this machine.
    """

    backend: RipgrepBackend
    working_dir: str
    directory: Path

    def write(self, rel: str, data: bytes) -> None:
        target = self.directory / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)


def _matches(output: Path) -> List[str]:
    paths = []
    for line in output.read_text(encoding="utf-8").splitlines():
        event = json.loads(line)
        if event.get("type") == "match":
            text = event["data"]["path"]["text"]
            paths.append(text[2:] if text.startswith("./") else text)
    return paths


class RipgrepBackendContract:
    """The ripgrep seam's contract. Subclass and provide ``ripgrep_subject``."""

    @pytest.fixture
    def ripgrep_subject(self, tmp_path: Path) -> RipgrepSubject:
        raise NotImplementedError("provide the backend under test")

    async def _run(
        self,
        subject: RipgrepSubject,
        tmp_path: Path,
        pattern: str,
        max_output_bytes: int = 1024 * 1024,
    ) -> Tuple[RipgrepRun, Path]:
        output = tmp_path / "rg-output.jsonl"
        run = await subject.backend.run(
            argv=["rg", "--json", "-e", pattern, "."],
            working_dir=subject.working_dir,
            output_file_path=str(output),
            timeout_seconds=30.5,
            max_output_bytes=max_output_bytes,
        )
        return run, output

    async def test_satisfies_the_protocol(
        self, ripgrep_subject: RipgrepSubject
    ) -> None:
        assert isinstance(ripgrep_subject.backend, RipgrepBackend)
        assert isinstance(ripgrep_subject.backend.is_local, bool)

    async def test_matches_are_written_to_the_output_file(
        self, ripgrep_subject: RipgrepSubject, tmp_path: Path
    ) -> None:
        ripgrep_subject.write("src/app.py", b"needle = 1\nhay = 2\n")
        ripgrep_subject.write("README.md", b"no match here\n")
        run, output = await self._run(ripgrep_subject, tmp_path, "needle")
        assert run.exit_code == 0 and not run.is_error, run
        assert run.timed_out is False and run.output_capped is False
        assert _matches(output) == ["src/app.py"]

    async def test_no_match_is_not_an_error(
        self, ripgrep_subject: RipgrepSubject, tmp_path: Path
    ) -> None:
        ripgrep_subject.write("a.txt", b"hay\n")
        run, _ = await self._run(ripgrep_subject, tmp_path, "needle")
        assert run.found_no_matches is True, run

    async def test_a_bad_pattern_is_an_error_not_no_matches(
        self, ripgrep_subject: RipgrepSubject, tmp_path: Path
    ) -> None:
        ripgrep_subject.write("a.txt", b"hay\n")
        run, _ = await self._run(ripgrep_subject, tmp_path, "(unclosed")
        assert run.found_no_matches is False
        assert run.exit_code not in (0, 1), run
        assert run.stderr_output

    async def test_the_byte_cap_is_reported_and_bounds_the_output(
        self, ripgrep_subject: RipgrepSubject, tmp_path: Path
    ) -> None:
        ripgrep_subject.write("big.txt", b"needle line\n" * 40_000)
        cap = 4096
        run, output = await self._run(ripgrep_subject, tmp_path, "needle", cap)
        assert run.output_capped is True, run
        assert output.stat().st_size <= cap + CAP_OVERSHOOT_BYTES

    async def test_pcre2_support_is_a_bool(
        self, ripgrep_subject: RipgrepSubject
    ) -> None:
        assert isinstance(await ripgrep_subject.backend.supports_pcre2(), bool)
