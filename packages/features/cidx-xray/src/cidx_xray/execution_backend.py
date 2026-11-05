"""Where an X-Ray evaluator is compiled and run.

X-Ray compiles caller-supplied Rust and runs it over a repository through
``xray-cli``. That EXECUTION is the part that may move out of the cidx pod (to
a plugin backend, e.g. evo-sandbox's, which runs it in sandbox-mcp); everything
above it -- auth, validation for
UX, candidate selection, job tracking, truncation -- stays in cidx and never
learns where the evaluator ran. This module is the seam between the two:

* :class:`XrayExecutionBackend` -- "execute a scan" and "execute a graph
  analysis", carrying exactly the inputs ``xray-cli`` needs today and
  returning today's result shapes.
* ``LocalXrayExecutionBackend`` (in ``rust_backend``) -- the in-pod
  implementation: compile semaphore, rlimits, env allowlist and the local +
  Postgres compile caches all live there.
* a delegated implementation -- a plugin on the ``code_indexer.xray_backend``
  entry point, shaping its results with ``xray/delegation_api.py``.

``RustNativeBackend`` (what the search engine and handlers construct) keeps
its ``run_batch`` / ``run_graph_analysis`` signatures and dispatches to the
backend chosen by ``xray_backend`` (see
``server/startup/xray_backend_wiring.py``), so callers do not change.
"""

from __future__ import annotations

# The seam's types now live in the kernel (Phase 2 of
# plans/designs/modular-packaging-addons.md), so the platform and the backends
# can share them without importing X-Ray; re-exported here unchanged, reached
# through the feature's one core adapter (cidx_xray._platform).
from cidx_xray._platform import (
    BatchResult as BatchResult,
    XrayExecutionBackend as XrayExecutionBackend,
    XrayGraphRequest as XrayGraphRequest,
    XrayScanOutcome as XrayScanOutcome,
    XrayScanRequest as XrayScanRequest,
)
