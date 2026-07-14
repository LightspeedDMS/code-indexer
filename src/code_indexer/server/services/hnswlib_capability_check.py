"""Server-side hnswlib fork-capability startup check (Bug #1392).

A drifted (stock PyPI) hnswlib install on the server's own Python
environment would cause every finalize-time orphan detect+repair call to
fail with AttributeError. Unlike the CLI side (storage/hnsw_index_manager.py,
which fails LOUD via HNSWCapabilityError at build/finalize entry points),
hard-failing SERVER STARTUP over this would take down ALL query serving --
a wildly disproportionate blast-radius increase for a defect that (per the
originating bug report) leaves query serving unaffected, and a violation of
this project's "Query Is Everything" invariant. So this check logs ERROR
loudly but NEVER raises or blocks startup; see wiring in
`server/startup/lifespan.py`.
"""

import logging
import sys
from typing import Tuple

from code_indexer.storage.hnsw_index_manager import EXPECTED_HNSWLIB_FORK_COMMIT
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

_DOCS_POINTER = "docs/hnswlib-custom-build.md"


def check_hnswlib_capability() -> Tuple[bool, str]:
    """Check whether this Python environment's hnswlib has the custom fork.

    Returns:
        (True, "ok") if hnswlib.Index has both check_integrity and
        repair_orphans. Otherwise (False, <actionable message>) naming the
        interpreter, the expected fork commit, and the docs rebuild pointer.
    """
    try:
        import hnswlib
    except ImportError as e:
        return (
            False,
            f"hnswlib is not installed on this Python environment "
            f"(interpreter: {sys.executable}): {e}. Expected the custom "
            f"fork at commit {EXPECTED_HNSWLIB_FORK_COMMIT}. See "
            f"{_DOCS_POINTER} for the rebuild procedure.",
        )

    if hasattr(hnswlib.Index, "check_integrity") and hasattr(
        hnswlib.Index, "repair_orphans"
    ):
        return (True, "ok")

    return (
        False,
        "Installed hnswlib is missing check_integrity()/repair_orphans() -- "
        f"this Python environment (interpreter: {sys.executable}) does not "
        f"have the custom hnswlib fork (expected commit "
        f"{EXPECTED_HNSWLIB_FORK_COMMIT}) installed. See {_DOCS_POINTER} for "
        "the rebuild procedure.",
    )


def run_hnswlib_capability_startup_check() -> None:
    """Run check_hnswlib_capability() and log the result -- NEVER raises.

    Bug #1392: the small, independently-testable helper `server/startup/
    lifespan.py`'s startup sequence calls. Wrapped in its own try/except so
    it is safe to call directly from startup without an additional
    surrounding try/except (though lifespan.py's own idiom wraps every
    startup step for defense-in-depth regardless).
    """
    try:
        ok, message = check_hnswlib_capability()
        if ok:
            logger.info("hnswlib capability check: %s", message)
        else:
            logger.error(format_error_log("APP-GENERAL-1392", message))
    except Exception as e:
        logger.error(
            format_error_log(
                "APP-GENERAL-1392",
                f"hnswlib capability startup check itself raised: {e}",
            )
        )
