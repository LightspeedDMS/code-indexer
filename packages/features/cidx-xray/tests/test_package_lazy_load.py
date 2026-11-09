"""Importing cidx_xray (and its feature object and CLI) must not load tree-sitter.

The lazy-load invariant: ``tree_sitter`` / ``tree_sitter_languages`` are
imported only inside ``AstSearchEngine.__init__``. The core's own gate
(``tests/unit/xray/test_lazy_load.py``) proves it for ``cidx --help``; this one
proves it for the feature's modules themselves, which the core's feature
loader imports at server startup. Fresh interpreter, so no earlier test can
have loaded tree-sitter already.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys

_MODULES = (
    "cidx_xray",
    "cidx_xray.feature",
    "cidx_xray.cli",
    "cidx_xray.search_engine",
    "cidx_xray.rust_backend",
    "cidx_xray.sandbox",
    "cidx_xray.postgres_cache",
)


def test_importing_the_package_does_not_load_tree_sitter() -> None:
    code = (
        "import importlib, json, sys\n"
        f"for name in {_MODULES!r}:\n"
        "    importlib.import_module(name)\n"
        "print(json.dumps([m for m in sys.modules if m.startswith('tree_sitter')]))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=60,
        env=dict(os.environ),
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout.strip().splitlines()[-1]) == []
