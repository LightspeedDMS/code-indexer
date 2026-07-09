"""
Shared environment builder for cidx CLI subprocess calls.

Many code paths spawn `cidx` subprocesses (init, index, scip generate,
stop/start/status, fix-config) with `cwd=<clone_or_repo_path>`: golden-repo
registration and add-index, the golden-repo refresh scheduler (source
indexing, CoW snapshot fix-config, local-repo repair, reconciliation
restore), MCP provider-index background jobs, activated-repo
activation/deactivation, cidx-meta bootstrap, the description-refresh
catch-up reindex (server-side), and the CLI/proxy layer's parallel/sequential
multi-repo command execution and watch-mode spawns (CLI-side). When the
PARENT process itself is launched with a RELATIVE `PYTHONPATH` entry (e.g.
the documented dev launch `PYTHONPATH=./src python3 -m uvicorn ...`), that
relative entry is inherited UNCHANGED by the child subprocess. Because
`PYTHONPATH` resolution is relative to the CURRENT PROCESS's cwd, the same
relative entry re-anchors to the CHILD's cwd (the clone/repo directory)
instead of the parent's own source tree.

If the cloned repository happens to contain a `src/`-layout package whose
name collides with one of cidx's own runtime dependencies (e.g. a repo with
its own `src/click/` package), the clone's package shadows the real installed
dependency on the child's `sys.path`, and the child's own `import click`
picks up the clone's code instead of site-packages. This has caused
`cidx init failed` / `cidx index failed` and golden-repo registration
hard-failures (Bug #1325), and the same class of failure applies to CLI/proxy
multi-repo command spawns (Story #1328).

Every `cidx` subprocess spawned with `cwd=<clone_or_repo_path>` MUST pass
`env=build_cidx_subprocess_env()` (or, for temporal indexing / other
already-built env dicts, run that dict through this helper as `base_env`) to
`subprocess.run` / `subprocess.Popen` so that any relative `PYTHONPATH` entry
is absolutized BEFORE the child changes its working directory.

This module lives in the shared `code_indexer.utils` package (rather than
`code_indexer.server.utils`) so that CLI/proxy code -- which must never
import from `code_indexer.server` -- can use it without a layering
violation. It depends only on the stdlib `os` module, so importing it does
not add measurable weight to CLI startup.

See Bug #1325: relative PYTHONPATH re-anchors into clone cwd, shadowing
installed dependencies in child cidx subprocesses.
See Story #1328: promoted to the shared layer for CLI/proxy reuse.
"""

import os
from typing import Dict, Optional


def build_cidx_subprocess_env(
    base_env: Optional[Dict[str, str]] = None,
) -> Dict[str, str]:
    """Return a copy of the given (or current) environment with an absolutized PYTHONPATH.

    Behavior:
    - Copies `base_env` if provided, else `os.environ`, into a NEW dict. The
      input is never mutated.
    - If `PYTHONPATH` is present, each `os.pathsep`-separated entry that is
      relative (`not os.path.isabs(entry)`) is resolved to an absolute path
      via `os.path.abspath(entry)`, resolved against THIS (the calling)
      process's cwd -- correct because this helper runs before any child
      subprocess changes its working directory. Absolute entries and empty
      entries (e.g. from a leading/trailing/doubled separator) pass through
      UNCHANGED. Entry order is preserved.
    - If `PYTHONPATH` is absent, the copy is returned unchanged -- no
      `PYTHONPATH` key is invented.
    - `PYTHONPATH` is NEVER stripped or cleared: dev-mode absolutized
      `./src` is still required so the child can `import code_indexer`.

    Callers receive a fresh dict each time; the input (`base_env` or
    `os.environ`) is never mutated.
    """
    env: Dict[str, str] = dict(base_env) if base_env is not None else dict(os.environ)

    if "PYTHONPATH" in env:
        entries = env["PYTHONPATH"].split(os.pathsep)
        resolved_entries = [
            entry if not entry or os.path.isabs(entry) else os.path.abspath(entry)
            for entry in entries
        ]
        env["PYTHONPATH"] = os.pathsep.join(resolved_entries)

    return env
