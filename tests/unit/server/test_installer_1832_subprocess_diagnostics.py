"""Bug #1832: the four `installer.py` subprocess-diagnostic sites
(Claude CLI / SCIP indexers / scip-dotnet / scip-go installation) used only
`result.stderr` -- when the failing install subprocess writes its real
diagnostic to stdout instead (installers routinely narrate progress and
errors on stdout), the logged ERROR degrades to
"<tool> installation failed: " with an empty tail.

Discriminating case (AC5): stderr EMPTY, stdout NON-EMPTY. A test using a
non-empty stderr would pass before the fix and prove nothing.

`ServerInstaller.__init__` hardcodes `Path.home() / ".cidx-server"` with no
constructor seam to redirect it, so this test builds the instance via
`ServerInstaller.__new__(ServerInstaller)` (bypassing `__init__` outright,
a plain Python idiom -- no `unittest.mock.patch` involved) and sets only
the one attribute (`server_dir`) the four methods under test actually
read. The ONLY thing mocked is the genuine external boundary --
`subprocess.run` -- via one dispatcher that answers every
`npm`/`dotnet`/`go`/tool `--version` probe and every actual install
command by matching its command prefix, so all four sites share ONE
parametrized runner instead of four near-duplicate test bodies, and no
internal `_is_*_installed`/`_is_*_available` method is mocked.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.installer import ServerInstaller

_DISCRIMINATING_STDOUT = "npm ERR! could not resolve registry.npmjs.org (DNS failure)"

# Command prefixes (first two argv tokens) that represent a genuine
# availability probe for npm/dotnet/go -- these must report "available".
_AVAILABILITY_OK_PREFIXES = {
    ("npm", "--version"),
    ("dotnet", "--version"),
    ("go", "version"),
}

# Command prefixes for a tool's own "am I installed" probe -- these must
# report "not installed" so each install_* method proceeds to install.
_NOT_INSTALLED_PREFIXES = {
    ("claude", "--version"),
    ("scip-python", "--version"),
    ("scip-typescript", "--version"),
    ("scip-dotnet", "--version"),
    ("scip-go", "--version"),
}


def _dispatch_subprocess_run(cmd, **kwargs) -> Mock:
    prefix = tuple(cmd[:2])
    if prefix in _AVAILABILITY_OK_PREFIXES:
        return Mock(args=cmd, returncode=0, stdout="", stderr="")
    if prefix in _NOT_INSTALLED_PREFIXES:
        return Mock(args=cmd, returncode=1, stdout="", stderr="")
    # Anything else is the actual install command (npm install / dotnet
    # tool install / go install) -- fail it with the discriminating shape.
    return Mock(args=cmd, returncode=1, stdout=_DISCRIMINATING_STDOUT, stderr="")


@pytest.fixture
def installer(tmp_path) -> ServerInstaller:
    """Bypasses __init__ via __new__ (plain Python, no mock.patch) since
    the real constructor hardcodes Path.home() with no seam to redirect
    it; sets only the one attribute the methods under test read."""
    inst = ServerInstaller.__new__(ServerInstaller)
    inst.server_dir = tmp_path / ".cidx-server"
    return inst


@pytest.mark.parametrize(
    "install_method",
    [
        "install_claude_cli",
        "install_scip_indexers",
        "install_scip_dotnet",
        "install_scip_go",
    ],
)
def test_empty_stderr_nonempty_stdout_surfaces_in_error_log(
    install_method: str, installer: ServerInstaller, caplog
) -> None:
    with (
        patch("subprocess.run", side_effect=_dispatch_subprocess_run),
        caplog.at_level(logging.ERROR),
    ):
        result = getattr(installer, install_method)()

    assert result is False

    errors = [
        record.message
        for record in caplog.records
        if "installation failed" in record.message
    ]
    assert errors, (
        f"[{install_method}] expected an installation-failure error log, "
        f"got: {caplog.records}"
    )
    assert _DISCRIMINATING_STDOUT in errors[0], (
        f"[{install_method}] stdout diagnostic missing from error: {errors[0]!r}"
    )
