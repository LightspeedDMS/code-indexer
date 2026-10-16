"""Scaffolding for the design 15.1 assertions whose mechanisms do not exist yet.

Each entry names the story that builds the mechanism and a probe: a file
(optionally containing a needle) in the tree under test's ``code_indexer``
package that exists only once the mechanism lands. The verdict is:

* SKIPPED-NOT-IMPLEMENTED while the probe is absent (12.83.0 today);
* FAIL as soon as the probe is present but no evaluator is enabled -- the
  story that lands the mechanism must also turn its check on, so a later
  check can never pass silently;
* the enabled evaluator's own result once a story registers one in
  ``ENABLED`` (``check_id -> callable returning a Check``).

The version-agnostic cores R-a..R-12 and A1, A2, A3, A5 live in
``invariant.py`` and run today.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Mapping, Optional

from invariant import SKIPPED_NOT_IMPLEMENTED, Check

Evaluator = Callable[[], Check]


@dataclass(frozen=True)
class LaterCheck:
    check_id: str
    title: str
    story: str
    probe: str  # path relative to src/code_indexer
    needle: Optional[str] = None


_REGISTRY = "storage/paths_registry.py"
_PENDING = "storage/pending_vectors.py"
_WRITER = "storage/index_writer.py"
_LEASE = "server/services/indexer_lease.py"
_STORE = "storage/sqlite_chunk_store.py"
_HNSW = "storage/hnsw_index_manager.py"
_JOBS = "server/repositories/background_jobs.py"

LATER_CHECKS: List[LaterCheck] = [
    LaterCheck(
        "A4",
        "recovery plans no more than unsatisfied files (registry)",
        "S5/S6",
        _REGISTRY,
    ),
    LaterCheck(
        "A6", "registry, HNSW labels and FTS counts consistent", "S5/S14/S16", _REGISTRY
    ),
    LaterCheck("A7", "store loss recovers with 0 inputs", "S4", _PENDING),
    LaterCheck("A8", "upgrade inputs equal census Q2", "S0b/S5", _REGISTRY),
    LaterCheck("A10a", "frozen child exits 89 after takeover", "S15", _LEASE),
    LaterCheck("A10b", "node losing the DB fences its children", "S15", _LEASE),
    LaterCheck("A10c", "two-process takeover under the soak", "S15", _LEASE),
    LaterCheck("A10d", "corruption at takeover restores from snapshot", "S15", _LEASE),
    LaterCheck("A10e", "pre-spawn cut points leave no stranded lease", "S15", _LEASE),
    LaterCheck("A10f", "takeover ordering and parent publish", "S15", _LEASE),
    LaterCheck("A11", "parent kill: PDEATHSIG, start ticks verified", "S15", _LEASE),
    LaterCheck("A12", "kill at every durable boundary", "S2", _WRITER),
    LaterCheck(
        "A13",
        "provider faults within the spend meter",
        "S8",
        "server/services/embedding_call_instrumentation.py",
        "SpendMeter",
    ),
    LaterCheck("A14", "mid-run growth deferred, never exit 88", "S4", _WRITER, "defer"),
    LaterCheck("A15", "rename, listing loss, branches", "S3/S5", _REGISTRY),
    LaterCheck(
        "A15b", "visibility round trips", "S5/S16", _REGISTRY, "hidden_branches"
    ),
    LaterCheck("A16", "multimodal identity", "S17", _REGISTRY, "image_refs"),
    LaterCheck(
        "A17",
        "old shapes (SHARDED_JSON exit 92, keyless rows)",
        "S19",
        "services/index_failure_exit_codes.py",
        "SHARDED_JSON",
    ),
    LaterCheck("A18", "recovery memory bound", "S16/S13", _HNSW, "apply_journal"),
    LaterCheck(
        "A19", "restart end to end yields and resumes", "S20", _JOBS, "admit_work"
    ),
    LaterCheck(
        "A19b", "staged maintenance and resubmission", "S20", _JOBS, "resubmit_spec"
    ),
    LaterCheck("A20", "queries during maintenance", "S20", _JOBS, "QUERY_JOB_TYPES"),
    LaterCheck("A21", "FTS completeness (#2056)", "S14", _STORE, "fts_journal"),
    LaterCheck("A21b", "FTS source collection", "S14", _STORE, "fts_journal"),
    LaterCheck(
        "A21c",
        "crash between every pair of 7.4 steps",
        "S14/S16",
        _HNSW,
        "open_active_generation",
    ),
    LaterCheck("A21d", "FTS gate correctness", "S14", _STORE, "fts_journal"),
    LaterCheck(
        "A21e",
        "live reads of golden base clones",
        "S14/S16",
        _HNSW,
        "open_active_generation",
    ),
    LaterCheck(
        "A21f", "merges never touch shared FTS files", "S14", _STORE, "fts_journal"
    ),
    LaterCheck("A21g", "FTS generation costs no bytes", "S14", _STORE, "fts_journal"),
    LaterCheck(
        "A21h", "no-change refresh writes nothing", "S16", _HNSW, "full_build_triggers"
    ),
    LaterCheck(
        "A21i",
        "every full-build trigger publishes",
        "S16",
        _HNSW,
        "full_build_triggers",
    ),
    LaterCheck(
        "A22", "generations on the real hnswlib fork", "S16", _STORE, "hnsw_journal"
    ),
    LaterCheck("A22b", "HNSW journal and binding", "S16", _STORE, "hnsw_journal"),
    LaterCheck(
        "A22c",
        "pairing under a concurrent switch",
        "S16",
        _HNSW,
        "open_active_generation",
    ),
    LaterCheck(
        "A22d",
        "downgrade reads current data",
        "S16/S14",
        _HNSW,
        "open_active_generation",
    ),
    LaterCheck(
        "A22e",
        "new readers never touch legacy paths",
        "S16/S15",
        _HNSW,
        "open_active_generation",
    ),
    LaterCheck("A23", "fleet pacing on 900 repos", "S15/S2", _LEASE),
]

#: Stories register their evaluators here when they turn a check on.
ENABLED: Dict[str, Evaluator] = {}


def _mechanism_present(src: Path, check: LaterCheck) -> bool:
    path = src / "code_indexer" / check.probe
    if not path.is_file():
        return False
    return check.needle is None or check.needle in path.read_text(errors="replace")


def evaluate_later(src: Path, enabled: Mapping[str, Evaluator]) -> List[Check]:
    """One Check per later assertion, for the tree under test ``src``."""
    results = []
    for check in LATER_CHECKS:
        probe = check.probe + (f" containing {check.needle!r}" if check.needle else "")
        if check.check_id in enabled:
            results.append(enabled[check.check_id]())
        elif not _mechanism_present(src, check):
            results.append(
                Check(
                    check.check_id,
                    False,
                    f"needs {check.story}: {probe} absent",
                    skipped=SKIPPED_NOT_IMPLEMENTED,
                    title=check.title,
                )
            )
        else:
            results.append(
                Check(
                    check.check_id,
                    False,
                    f"mechanism present ({probe}) but its harness check is not "
                    f"enabled; {check.story} must register it in later_checks.ENABLED",
                    title=check.title,
                )
            )
    return results
