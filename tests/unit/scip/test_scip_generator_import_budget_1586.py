"""Story #1586 Finding 6: scip/generator.py must not pull server-layer
telemetry modules into the CLI import path at module scope.

`code_indexer.scip.generator` is imported on the CLI's `cidx scip generate`
path. Story #1586 AC5 added a `create_span()`-wrapped span around whole-repo
SCIP generation, but the import was placed at MODULE scope
(`from code_indexer.server.telemetry.spans import create_span`), which
transitively pulls in ~11 server-layer modules (Story #1586 code-review
Finding 6, measured ~+0.1s on `cidx scip generate`) -- a CLI-layer/server-
layer boundary violation matching this project's documented Bug #1468
precedent (`storage.shared.chunk_layout`'s eager psycopg/fastapi pull).

Verified via a real subprocess import (no mocking): importing
code_indexer.scip.generator alone must never place
code_indexer.server.telemetry.spans (or the code_indexer.server package
generally) into sys.modules. Mirrors the existing subprocess-proof pattern
in tests/unit/xray/test_lazy_load.py.
"""

from __future__ import annotations

import os
import subprocess
import sys


def _run_import_probe(import_statement: str) -> str:
    """Run `import_statement` in a fresh subprocess with PYTHONPATH=./src
    and print which sys.modules keys start with 'code_indexer.server'.
    Returns the subprocess's stdout.
    """
    env = dict(os.environ)
    env["PYTHONPATH"] = "src"
    probe = (
        f"import sys; {import_statement}; "
        "server_mods = sorted("
        "m for m in sys.modules if m.startswith('code_indexer.server')"
        "); "
        "print('SERVER_MODULES:', server_mods)"
    )
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        env=env,
        cwd=os.getcwd(),
        timeout=60,
    )
    assert result.returncode == 0, (
        f"import probe subprocess failed: stdout={result.stdout!r} "
        f"stderr={result.stderr!r}"
    )
    return result.stdout


class TestScipGeneratorImportBudget:
    def test_scip_generator_import_does_not_pull_in_server_telemetry_modules(self):
        stdout = _run_import_probe("import code_indexer.scip.generator")

        assert "SERVER_MODULES: []" in stdout, (
            "importing code_indexer.scip.generator alone must not pull any "
            "code_indexer.server.* module into sys.modules -- "
            f"got: {stdout!r}"
        )
