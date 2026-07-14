"""
Bug #1370: module-level `rich.console.Console()` singletons cache
terminal/color detection at construction time (first import into the
pytest process) and never re-evaluate it afterward. When a single pytest
process runs hundreds of Click `CliRunner`-based CLI tests together (as
`fast-automation.sh` does), whichever test (or import) happens to run
first "freezes" the color/highlight behavior for every subsequent test
that touches the shared singleton -- producing ANSI escape codes that
split substrings plain-text assertions expect intact, and, for
JSON-emitting commands, stray control sequences ahead of the payload.

Root-cause investigation for #1370 found this defect in FOURTEEN
module-level `console = Console()` singletons (not just
`code_indexer.cli`): cli.py:639, cli_keys.py, cli_watch_helpers.py,
cli_cicd.py, cli_scip.py, cli_files.py, cli_index.py,
proxy/cli_integration.py, cli_help.py, cli_daemon_lifecycle.py,
cli_git.py, cli_daemon_delegation.py, mode_specific_handlers.py, and
utils/temporal_display.py. All other `Console()` construction sites in
cli.py are function-local (rebuilt fresh per Click invocation, after
CliRunner has already redirected sys.stdout) and are NOT exposed to this
bug.

TWO independent import paths load these modules as SEPARATE objects:
some test files do `from code_indexer.cli import ...` while others do
`from src.code_indexer.cli import ...` (both resolve to the same source
file but are distinct entries in sys.modules with their OWN `console`
singleton). Resetting only `code_indexer.*` leaves the `src.code_indexer.*`
copies polluted.

Root-cause investigation ALSO found that Rich's color/highlight
detection additionally honors the `FORCE_COLOR` environment variable
(https://force-color.org/), read live off the process's `os.environ` on
every `is_terminal`/`_detect_color_system()` call -- NOT just at
construction. When `FORCE_COLOR` is set in the ambient shell (as it is
in some sandboxed tool-invocation environments), it forces color on for
EVERY freshly-constructed `Console()` too, including ones built inside
Click command bodies after `CliRunner` has already redirected stdout,
and ones built by a CLI process spawned via `subprocess.run([sys.executable,
"-m", "code_indexer.cli", ...])` (which inherits `os.environ` from the
pytest process). This is a distinct mechanism from the singleton-caching
issue above -- it does not depend on test order -- but produces the
identical ANSI-splitting symptom and is part of the same bug's
reproducible failure list.

Fix direction chosen: a session-wide autouse pytest fixture
(`tests/conftest.py::_deterministic_cli_console_singletons`) that (1)
resets every known module-level console singleton, under BOTH import
paths, to a fixed, deterministic `Console(force_terminal=False,
no_color=True)` before each test and restores the pre-test object
afterward, and (2) strips `FORCE_COLOR` / forces `NO_COLOR=1` in
`os.environ` for the duration of each test, restoring the previous
values afterward. Production runtime behavior (`cli.py` itself) is
untouched -- this is purely a test-isolation fix.

These tests are ORDER-DEPENDENT BY DESIGN: `test_a_*` intentionally
pollutes several console singletons the way a leaky prior test (or a
"first import in a TTY" scenario) would, WITHOUT restoring them.
`test_b_*`/`test_c_*` then prove that regardless of that pollution, the
next test in the same pytest process gets clean, deterministic,
ANSI-free output. Pytest preserves source-order for functions defined in
the same module, so `test_a_*` reliably runs before the others here
without needing pytest-order plugins. `test_d_*`/`test_e_*` do not
depend on ordering -- they prove the environment-variable and dual
import-path handling directly.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

from click.testing import CliRunner
from rich.console import Console

from code_indexer.cli import cli
from code_indexer.services.hnsw_health_service import HealthCheckResult


def _make_healthy_result() -> HealthCheckResult:
    return HealthCheckResult(
        valid=True,
        file_exists=True,
        readable=True,
        loadable=True,
        element_count=1000,
        connections_checked=5000,
        min_inbound=2,
        max_inbound=10,
        orphan_count=0,
        index_path="/path/to/.code-indexer/index/hnsw.bin",
        file_size_bytes=1024,
        last_modified=datetime(2024, 1, 15, 10, 30, 45, tzinfo=timezone.utc),
        errors=[],
        check_duration_ms=42.0,
        from_cache=False,
    )


class TestConsoleSingletonPollutionIsolation:
    """Reproduces and locks in the fix for Bug #1370."""

    def test_a_pollutes_console_singletons_simulating_leaky_prior_test(self):
        """Simulate a prior test (or the process's first cli.py import
        while sys.stdout looked like a colorful TTY) leaving module-level
        console singletons in a colorful, auto-highlighting state and
        NOT cleaning up after itself. This mirrors the real defect: the
        pollution is process-lifetime, not scoped to one test.
        """
        import code_indexer.cli as cli_module
        import code_indexer.cli_index as cli_index_module

        cli_module.console = Console(force_terminal=True, color_system="standard")
        cli_index_module.console = Console(force_terminal=True, color_system="standard")

        # Sanity check: the pollution actually took effect on this object.
        assert cli_module.console.color_system == "standard"
        assert cli_index_module.console.color_system == "standard"

    def test_b_health_command_output_is_plain_regardless_of_prior_pollution(self):
        """Regression test for Bug #1370.

        Must pass no matter what `test_a_*` (or any other test earlier
        in the pytest process) left in `code_indexer.cli.console`. Before
        the fix, this fails because the polluted singleton survives
        across tests and Rich's automatic ReprHighlighter wraps numbers
        (e.g. the orphan count) in ANSI SGR codes, splitting
        "Orphan Count: 0" into "Orphan Count: \\x1b[1;36m0\\x1b[0m".
        """
        runner = CliRunner()
        with patch(
            "code_indexer.services.hnsw_health_service.HNSWHealthService"
        ) as mock_service_class:
            mock_service = MagicMock()
            mock_service.check_health.return_value = _make_healthy_result()
            mock_service_class.return_value = mock_service

            result = runner.invoke(cli, ["health"])

        assert "\x1b[" not in result.output, (
            "ANSI escape codes leaked into plain CLI output -- the "
            f"console singleton was not reset between tests: {result.output!r}"
        )
        assert "Orphan Count: 0" in result.output

    def test_c_sibling_module_console_singletons_are_also_reset(self):
        """Bug #1370's root-cause investigation found the SAME defect in
        thirteen sibling modules beyond code_indexer.cli. Spot-check a
        representative subset (including the one polluted by test_a) to
        prove the fixture resets all registered singletons, not just the
        one literally named in the original bug report.
        """
        import code_indexer.cli_index as cli_index_module
        import code_indexer.cli_help as cli_help_module
        import code_indexer.utils.temporal_display as temporal_display_module

        for mod in (cli_index_module, cli_help_module, temporal_display_module):
            assert mod.console.no_color is True, (
                f"{mod.__name__}.console was not reset to no_color=True"
            )
            assert mod.console.is_terminal is False, (
                f"{mod.__name__}.console was not reset to force_terminal=False"
            )

    def test_d_subprocess_cli_invocation_ignores_ambient_force_color(self):
        """Regression test for Bug #1370's second mechanism.

        Several tests (e.g. test_cli_diff_context_flag.py,
        test_remote_initialization.py) invoke the CLI via
        `subprocess.run([sys.executable, "-m", "code_indexer.cli", ...])`.
        A freshly-spawned subprocess inherits `os.environ` from the
        pytest process, so if `FORCE_COLOR` is set in the ambient
        sandbox/shell environment, the subprocess's own fresh
        `Console()` construction (Rich reads `FORCE_COLOR` live off
        `os.environ`, not just at import) forces color on regardless of
        the non-tty output pipe -- splitting plain-text assertions with
        ANSI codes. This does not depend on the singleton at all and is
        not order-dependent; it must be neutralized for every test via
        environment normalization.
        """
        import subprocess
        import sys
        import uuid

        # Generated at runtime (never a literal) -- this CLI invocation is
        # rejected by argument validation before any credential is used.
        non_secret_cli_arg_value = uuid.uuid4().hex
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "code_indexer.cli",
                "init",
                "--remote",
                "https://cidx.example.com",
                "--password",
                non_secret_cli_arg_value,
            ],
            capture_output=True,
            text=True,
            timeout=30,
        )

        combined_output = result.stdout + result.stderr
        assert "\x1b[" not in combined_output, (
            "ANSI escape codes leaked into subprocess CLI output -- "
            f"ambient FORCE_COLOR pollution was not neutralized: {combined_output!r}"
        )
        assert (
            "Usage: cidx init --remote <server-url> --username <user> --password <pass>"
            in combined_output
        )

    def test_e_dual_import_path_console_singleton_is_also_reset(self):
        """Regression test for Bug #1370's third mechanism.

        Some test files import `from src.code_indexer.cli import
        admin_jobs_group` (e.g. test_admin_jobs_cleanup_implementation.py)
        instead of `from code_indexer.cli import ...`. Both spellings
        resolve to the same source file but load as TWO SEPARATE module
        objects in sys.modules, each with its OWN `console` singleton.
        Resetting only `code_indexer.cli` leaves `src.code_indexer.cli`
        polluted -- the fixture must reset both.
        """
        import src.code_indexer.cli as src_cli_module

        assert src_cli_module.console.no_color is True, (
            "src.code_indexer.cli.console was not reset to no_color=True -- "
            "the dual import path was not covered"
        )
        assert src_cli_module.console.is_terminal is False, (
            "src.code_indexer.cli.console was not reset to force_terminal=False"
        )

    # NOTE: the sibling E2E-suite mechanism (tests/e2e/conftest.py's
    # session-scoped `e2e_cli_env` fixture leaking ambient FORCE_COLOR into
    # real `cidx` subprocess invocations via `run_cidx(..., env=e2e_cli_env)`)
    # is the SAME underlying root cause but was filed and fixed separately as
    # Bug #1372 (`tests/e2e/helpers.py::sanitize_cli_subprocess_env`, wired
    # into `e2e_cli_env`, covered by
    # tests/unit/test_e2e_cli_env_color_sanitization_1372.py). It is
    # intentionally NOT duplicated here -- see #1372 for that fix.
