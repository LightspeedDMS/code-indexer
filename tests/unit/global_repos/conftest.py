import contextlib
from typing import Generator
from unittest.mock import MagicMock, patch

import pytest


def _patch_research_assistant_service() -> contextlib.AbstractContextManager:  # type: ignore[type-arg]
    """Return a context manager that patches enforce_pace_maker_config in
    research_assistant_service when that module is importable (Python 3.9+
    with fastapi/bleach installed).  Falls back to a no-op context manager
    when the module cannot be imported (Python 3.11 without fastapi/bleach).
    """
    try:
        import code_indexer.server.services.research_assistant_service as _ras

        return patch.object(_ras, "enforce_pace_maker_config", MagicMock())
    except (ImportError, ModuleNotFoundError):
        return contextlib.nullcontext()


@pytest.fixture(autouse=True)
def _disable_pace_maker_guard() -> Generator[None, None, None]:
    import code_indexer.server.services.claude_invoker as _ci

    with patch.object(_ci, "enforce_pace_maker_config", MagicMock()):
        with _patch_research_assistant_service():
            yield


# ---------------------------------------------------------------------------
# Ripgrep seam test double
# ---------------------------------------------------------------------------
#
# The ripgrep argv is executed through RipgrepBackend, so a test scripts the
# backend instead of substituting SubprocessExecutor inside regex_search's
# namespace and hand-building the executor's result type.
#
# The important property is that this double WRITES its stdout to the
# output_file_path it is handed, exactly as the real backend does. Tests that
# previously had to patch ``builtins.open`` globally to fake the read-back can
# let the service read a real file instead -- a global open() patch reaches
# every unrelated open in the call, which is why those tests needed careful
# scoping and were fragile.


class FakeRipgrepBackend:
    """Scripted RipgrepBackend that records what it was asked to run.

    Args mirror the fields the service branches on. ``stdout`` is written to the
    output file so the service's real reader and parser run unchanged.
    """

    def __init__(
        self,
        *,
        stdout: str = "",
        exit_code: int = 0,
        stderr_output: str = "",
        timed_out: bool = False,
        is_error: bool = False,
        output_capped: bool = False,
        supports_pcre2: bool = True,
        on_run=None,
        argv_log=None,
    ) -> None:
        self.stdout = stdout
        self.exit_code = exit_code
        self.stderr_output = stderr_output
        self.timed_out = timed_out
        self.is_error = is_error
        self.output_capped = output_capped
        self._supports_pcre2 = supports_pcre2
        self._on_run = on_run
        self.argv_log = argv_log if argv_log is not None else []
        self.calls: list = []

    @property
    def is_local(self) -> bool:
        return True

    @property
    def argv(self) -> list:
        """The argv of the most recent run, for flag assertions."""
        if not self.calls:
            raise AssertionError("the backend was never invoked")
        return self.calls[-1]["argv"]

    async def run(
        self,
        *,
        argv,
        working_dir: str,
        output_file_path: str,
        timeout_seconds: float,
        max_output_bytes: int,
    ):
        from code_indexer.server.storage.shared.ripgrep_backend import RipgrepRun

        self.argv_log.append(list(argv))
        self.calls.append(
            {
                "argv": list(argv),
                "working_dir": working_dir,
                "output_file_path": output_file_path,
                "timeout_seconds": timeout_seconds,
                "max_output_bytes": max_output_bytes,
            }
        )
        if self._on_run is not None:
            self._on_run(self, output_file_path)
        else:
            with open(output_file_path, "w", encoding="utf-8") as handle:
                handle.write(self.stdout)
        return RipgrepRun(
            exit_code=self.exit_code,
            stderr_output=self.stderr_output,
            timed_out=self.timed_out,
            timeout_seconds=timeout_seconds,
            is_error=self.is_error,
            output_capped=self.output_capped,
        )

    async def supports_pcre2(self) -> bool:
        return self._supports_pcre2


@pytest.fixture
def fake_ripgrep_backend():
    """Factory for a FakeRipgrepBackend; pass it to RegexSearchService."""

    def _make(**kwargs) -> FakeRipgrepBackend:
        return FakeRipgrepBackend(**kwargs)

    return _make


# The ripgrep argv is driven by LocalRipgrepBackend, which imports
# SubprocessExecutor INSIDE run(); the grep and glob paths still import it at
# regex_search's module scope. `from X import Y` binds Y at import time, so the
# two patch targets are independent -- patching one never reaches the other.
# Tests that assert on EXECUTOR MECHANICS (shutdown offload, byte-cap kills,
# attribute access on its result) keep mocking the executor and simply aim at
# the right one; tests that assert on PARSING or BRANCHING use
# FakeRipgrepBackend instead and mock nothing.
RIPGREP_EXECUTOR_TARGET = (
    "code_indexer.server.services.subprocess_executor.SubprocessExecutor"
)
LEGACY_EXECUTOR_TARGET = "code_indexer.global_repos.regex_search.SubprocessExecutor"


def executor_target(engine: str) -> str:
    """Where SubprocessExecutor must be patched for ``engine``."""
    return RIPGREP_EXECUTOR_TARGET if engine == "ripgrep" else LEGACY_EXECUTOR_TARGET
