"""A ``cidx`` executable whose ``index`` subcommand plays a scripted child
outcome, for refresh tests that need a child which FAILS with an ordinary
(non-chunk-store) error or which exits 0 having recorded failed files
(Bug #2125). Every other subcommand (``fix-config``, ``init`` ...) runs
THIS interpreter's real CLI, so the rest of the refresh is real.

The outcome is read from ``index-mode`` next to the executable at each
call:

- ``fail``: exit 1, a provider error on stderr;
- ``partial``: exit 0, three existing files recorded as failed;
- ``recover``: exit 0, the retried files succeeded (no failure recorded);
- ``noop``: exit 0 with the provider metadata untouched -- the real
  no-work path (the default);
- ``stale``: exit 0, a ``failed_files`` counter of 5 with no recorded path
  (a counter the real child does not always keep current).

Each ``index`` call appends one line to ``index-calls``.
"""

from __future__ import annotations

import sys
from pathlib import Path

PARTIAL_FAILED_FILES = ("module_1.py", "module_2.py", "module_3.py")
STALE_FAILED_FILES = 5
MODES = ("fail", "partial", "recover", "noop", "stale")

_SCRIPT = """#!{python}
import json, os, sys
from pathlib import Path

here = Path(__file__).resolve().parent
args = sys.argv[1:]
if args[:1] == ["index"]:
    mode = (here / "index-mode").read_text().strip()
    with open(here / "index-calls", "a") as calls:
        calls.write(mode + "\\n")
    if mode == "fail":
        sys.stderr.write("embedding provider rejected the request: no API key\\n")
        sys.exit(1)
    if mode == "noop":
        sys.exit(0)
    meta = Path.cwd() / ".code-indexer" / "metadata-voyage-ai.json"
    data = json.loads(meta.read_text())
    if mode == "partial":
        failed = [str(Path.cwd() / name) for name in {failed}]
        data.update(failed_files=len(failed), failed_file_paths=failed)
    elif mode == "stale":
        data.update(failed_files={stale}, failed_file_paths=[])
    else:
        data.update(failed_files=0, failed_file_paths=[])
    data["status"] = "completed"
    meta.write_text(json.dumps(data))
    sys.exit(0)
os.execv(sys.executable, [sys.executable, "-m", "code_indexer.cli", *args])
"""


class FakeIndexChild:
    """Handle on an installed fake ``cidx``: set the next outcome, count
    the ``index`` calls."""

    def __init__(self, bin_dir: Path) -> None:
        self.bin_dir = bin_dir

    def set_mode(self, mode: str) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown fake index mode: {mode}")
        (self.bin_dir / "index-mode").write_text(mode)

    def index_calls(self) -> int:
        calls = self.bin_dir / "index-calls"
        return len(calls.read_text().splitlines()) if calls.exists() else 0


def install_fake_index_child(bin_dir: Path) -> FakeIndexChild:
    """Write the fake ``cidx`` into ``bin_dir`` (put it first on PATH)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    shim = bin_dir / "cidx"
    shim.write_text(
        _SCRIPT.format(
            python=sys.executable,
            failed=list(PARTIAL_FAILED_FILES),
            stale=STALE_FAILED_FILES,
        )
    )
    shim.chmod(0o755)
    child = FakeIndexChild(bin_dir)
    child.set_mode("noop")
    return child
