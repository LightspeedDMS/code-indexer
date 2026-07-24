"""TemporalShardResolver (Story #1457 AC8): single authority for "logical
temporal namespace -> resolved physical path + source".

Publication (AC6/AC11, not yet implemented) and resolution (this module) are
two halves of ONE mechanism: a temporal version published via alias-swap is
invisible to every query without this resolver reading the same alias
pointer the publish side writes.

Owns BOTH physical roots AND the repo alias needed to bridge the two
incompatible physical naming schemes:
  - the in-repo legacy directory is
    `code-indexer-temporal-{embedder_slug}[-{quarter}]` (base-name form)
  - the sister alias pointer is
    `{repo_alias}-temporal-{embedder_slug}[-{quarter}]` (alias-prefixed form)

A single unmodified namespace string cannot look up BOTH forms unless the
alias literally equals "code-indexer" -- resolve()/catalog() therefore take
STRUCTURED (embedder_slug, quarter) identity, never a bare ambiguous
namespace string, and derive both physical forms internally.

Resolution is pointer-first, in-repo-fallback-second, decided PER
(embedder, quarter) namespace independently (round-8 N1): if a sister alias
pointer exists for that EXACT quarter namespace, resolve via the pointer
(the sister location is ALWAYS authoritative once published, even if the
old in-repo copy has not been deleted yet); if no pointer exists, fall back
to the in-repo directory for that same quarter (unbootstrapped quarters
remain served from their existing legacy location, unchanged).

NOTE (honest scope disclosure): resolve() (both the pointer-first and
in-repo-fallback branches, including the row-existence-not-queryability
is_queryable computation), catalog() (both the sister-pointer glob half and
the in-repo unbootstrapped-quarter union half), and pin() (AC8 Step 6's
core resolution-scope pin -- the bounded resolve-acquire-validate-retry
handshake, a real QueryTracker refcount, and a nest-safe per-namespace pin
stack that FilesystemVectorStore._get_collection_path consults FIRST while
a pin is active) ARE implemented and tested, including a genuinely
deterministic race test (a real concurrent AliasManager.swap_alias landing
between resolve and validate) and a genuinely deterministic exhaustion test
(a persistently racing pointer exhausting the bounded retry budget) -- both
via real infrastructure, zero mocking of this resolver's own logic.

The dispatch-loop wiring (`temporal_fusion_dispatch.py`'s `_query_shards_raw`,
Story #1457 AC8 Step 6 dispatch consumption contract items 3, 5, and 6) IS
now implemented and tested: an optional `resolver` param wraps each shard
read in `with resolver.pin(...)`, keys HNSW eviction by the resolved
`.path` (not a `base_path/shard_name` reconstruction), passes
`.physical_name` downstream, and handles `TemporalShardPinExhaustedError`
via a dedicated exception clause that calls the new
`record_temporal_pin_exhaustion()` counter (`temporal_health.py`) instead
of `record_temporal_failure()` -- keeping a lost pointer race OUT of the
provider circuit breaker. `resolver=None` (every current production
caller) is byte-identical to today (proven via the full pre-existing
`test_temporal_fusion_dispatch*.py` suite passing unchanged).

Discovery-level resolver wiring (dispatch consumption contract items 1-2)
IS now implemented and tested too: `resolve_overlapping_shards(resolver,
embedder_slug, start, end)` reuses `catalog()`/`resolve()` to build a
`List[ResolvedTemporalShard]` (item 1), `_discover_provider_shards_with_pruning`
gained a `resolver` param that routes through it and excludes any
`is_queryable=False` result BEFORE it is ever returned (item 2), and
`execute_temporal_query_with_fusion` threads a `resolver` param end-to-end
into BOTH discovery and the pin-wrapped dispatch loop. A single end-to-end
test (`test_temporal_fusion_dispatch_resolver_e2e_1457.py`) proves the FULL
chain together: a published sister quarter is discovered via the resolver's
catalog, read inside `resolver.pin(...)`, and evicted by the resolved
sister path. `resolver=None` (every current production caller) remains
byte-identical at every layer.

LIVE WIRING (Story #1457 AC1/AC2) IS now implemented and tested end-to-end:
`SemanticQueryManager._execute_temporal_query` constructs a REAL
`TemporalShardResolver` (using the `golden_repos_dir = Path(activated_repo_manager.activated_repos_dir).parent
/ "golden-repos"` convention, confirmed to match AC2's
`build_dedicated_temporal_read_store` exactly) and forwards it into
`execute_temporal_query_with_fusion`, gated on BOTH a `golden_repo_alias`
being known AND a real `query_tracker` being present on the manager
instance (constructing a resolver without a query_tracker would make
`pin()` a silent no-op). `golden_repo_alias` is threaded from
`query_user_repositories`'s loop through `_search_single_repository` ->
`_execute_temporal_query`: for an `is_global` repo it is the repo's own
alias (already resolved directly via `AliasManager`); for a regular
activated repo it is `repo_info["golden_repo_alias"]` -- CONFIRMED to exist
as a tracked column returned by `ActivatedRepoManager.list_activated_repositories`
(the underlying golden repo an activation was cloned from), contrary to an
earlier round's tentative "not found" report -- corrected after actually
checking rather than assuming. `SemanticQueryManager.query_tracker`
(default `None`, `set_query_tracker()` setter mirroring the existing
`set_shard_ownership` pattern) is wired POST-HOC at server startup via
`lifespan.py`'s `_wire_query_tracker_into_semantic_query_manager`, using
the SAME `global_lifecycle_manager.query_tracker` singleton already
assigned to `app.state.query_tracker` -- for BOTH solo and cluster server
modes (this wiring is unconditional, not gated on cluster/sharding).

UPDATE (2026-07-23 code review, stale-docstring fix, MEDIUM #13): both
items previously listed here as "not yet implemented" now ARE implemented
and tested:
  - The sister-retry-on-reclamation nested-pin mechanics (an
    `IN_REPO_LEGACY` read failing mid-flight because a different worker
    reclaimed it, retried via a nested `pin()` call) are implemented in
    `temporal_fusion_dispatch.py` and proven by
    `test_temporal_sister_retry_reclamation_1457.py` (benign-retry-
    succeeds, non-benign-reraises, and retry-also-fails-records-once
    cases).
  - AC1 itself (a golden repo actually relocating temporal shards to the
    sister location) is implemented as
    `maybe_relocate_shard_to_sister_location`
    (`temporal_relocation_trigger.py`), wired into
    `TemporalIndexer._index_one_embedder` immediately after each shard's
    normal in-repo finalize. It is gated behind an explicit opt-in safety
    flag, `CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED` (default OFF,
    mirroring Story #1456's `CIDX_CHUNKS_DB_NEW_COLLECTIONS` pattern) --
    with the gate off (the shipped default), every resolver still resolves
    every namespace to `IN_REPO_LEGACY` (unchanged in-repo behavior,
    byte-identical to pre-AC1), exactly as this paragraph originally
    described for the "not yet landed" case. Enabling the flag activates
    genuine sister-location relocation without any further live-wiring
    work, as this paragraph anticipated.
"""

from __future__ import annotations

import glob
import re
import threading
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_collection_naming import (
    TEMPORAL_COLLECTION_PREFIX,
    get_quarter_range,
)
from code_indexer.services.temporal.temporal_row_existence import (
    temporal_shard_has_committed_rows,
)


class TemporalShardPinExhaustedError(Exception):
    """Raised by TemporalShardResolver.pin() when the bounded
    resolve-acquire-validate-retry handshake lost the race against a
    concurrent alias swap on every attempt (Story #1457 AC8 Step 6).

    A TRANSIENT failure, distinct from a durable "shard does not exist"
    result -- deliberately kept OUT of the provider circuit breaker (a lost
    pointer race is not a provider-health signal).
    """


_QUARTER_SUFFIX_RE = re.compile(r"-(\d{4}Q[1-4])$")


def parse_physical_temporal_name(
    collection_name: str,
) -> Optional[Tuple[str, Optional[str]]]:
    """Parse a bare physical collection name into (embedder_slug, quarter).

    Story #1457 AC8's REQUIRED parser fallback: `_get_collection_path`
    receives ONLY a bare `collection_name: str` with no embedder/quarter
    context (the entire read path -- `search()`/`collection_exists()` --
    and the entire write/index path reach it this way). Strips the
    `code-indexer-temporal-` prefix (physical/base-name form; the
    alias-prefixed pointer form is not handled here -- dispatch always
    passes `.physical_name`, the base-name form, per the story's Dispatch
    Consumption Contract), then applies the SAME quarter regex
    `temporal_collection_naming.py` already uses to recover the quarter
    suffix (quarter=None for the quarter-less monolith form).

    Returns None if collection_name is not a temporal collection name at
    all (no `code-indexer-temporal-` prefix), or is not a non-empty string.
    """
    if not isinstance(collection_name, str) or not collection_name:
        return None
    if not collection_name.startswith(TEMPORAL_COLLECTION_PREFIX):
        return None

    remainder = collection_name[len(TEMPORAL_COLLECTION_PREFIX) :]
    if not remainder:
        return None

    match = _QUARTER_SUFFIX_RE.search(remainder)
    if match:
        quarter = match.group(1)
        embedder_slug = remainder[: -len(quarter) - 1]
        return (embedder_slug, quarter)

    return (remainder, None)


class TemporalShardSource(Enum):
    """Where a resolved temporal shard's data currently lives."""

    #: Resolved via alias pointer under sister_root (immutable v_* version).
    SISTER_POINTER = "sister_pointer"
    #: Resolved to the un-bootstrapped in-repo directory (mutable).
    IN_REPO_LEGACY = "in_repo_legacy"


@dataclass(frozen=True)
class ResolvedTemporalShard:
    """A single temporal namespace's resolved physical location.

    Attributes:
        pointer_namespace: Alias-prefixed pointer name, e.g.
            "{repo_alias}-temporal-{embedder_slug}-{quarter}" -- what AC6
            publishes and this resolver reads.
        physical_name: Base-name in-repo directory name, e.g.
            "code-indexer-temporal-{embedder_slug}-{quarter}".
        path: The resolved v_* directory (sister) OR the in-repo shard
            directory (legacy).
        source: Which of the two physical roots this shard currently
            resolves to.
        is_queryable: True if this shard is currently searchable (has a
            working HNSW). Distinct from data-existence (the row-existence
            scan) -- a SISTER_POINTER result is always queryable
            (publish-last ordering guarantees hnsw_index.bin exists before
            the pointer is created); an IN_REPO_LEGACY result is queryable
            iff its hnsw_index.bin exists.
    """

    pointer_namespace: str
    physical_name: str
    path: Path
    source: TemporalShardSource
    is_queryable: bool


class TemporalShardResolver:
    """Single authority for "logical temporal namespace -> resolved
    physical path + source" (Story #1457 AC8).

    Args:
        alias_manager: AliasManager instance scoped to the aliases
            directory used for reading/publishing per-quarter alias
            pointer files.
        repo_alias: The golden repo's alias (e.g. "evolution"); required
            to construct the alias-prefixed pointer name.
        sister_root: Where published `.versioned/{ns}/v_*/` directories
            live (AC6 publish target / AC8 catalog source).
        legacy_index_path: The ORIGINAL in-repo `.code-indexer/index/`
            (AC8 in-repo fallback source). Physical dir names there are
            `code-indexer-temporal-{embedder_slug}[-{quarter}]`
            (base-name form -- NEVER alias-prefixed).
        query_tracker: Optional QueryTracker for AC8 Step 6's
            resolution-scope pin -- refcounts the resolved path for the
            duration of a `pin()` block. None (CLI/solo -- no server
            process) makes `pin()` a true no-op: it yields a plain
            `resolve()` result with no refcount and no pin-stack entry.
    """

    def __init__(
        self,
        alias_manager: AliasManager,
        repo_alias: str,
        sister_root: Path,
        legacy_index_path: Path,
        query_tracker: Optional[Any] = None,
    ) -> None:
        self._alias_manager = alias_manager
        self._repo_alias = repo_alias
        self._sister_root = Path(sister_root)
        self._legacy_index_path = Path(legacy_index_path)
        self._query_tracker = query_tracker
        # 2026-07-23 code review CRITICAL #2: a single resolver-wide dict
        # let concurrent threads pinning the SAME (embedder_slug, quarter)
        # namespace observe/pop EACH OTHER's pin-stack entries, breaking
        # the refcount-matches-path-read invariant this pin mechanism
        # exists to guarantee. threading.local() gives each thread its
        # OWN stacks dict -- no cross-thread visibility is possible, and
        # no lock is needed (only the owning thread ever touches its own
        # thread-local storage).
        self._pin_stack_local = threading.local()

    def _thread_local_stacks(self) -> Dict[Tuple[str, Optional[str]], List[Path]]:
        """Return the CALLING thread's own pin-stack dict, lazily
        initialized on first access from that thread."""
        stacks = getattr(self._pin_stack_local, "stacks", None)
        if stacks is None:
            stacks = {}
            self._pin_stack_local.stacks = stacks
        return stacks

    def resolve(
        self, embedder_slug: str, quarter: Optional[str]
    ) -> Optional[ResolvedTemporalShard]:
        """Resolve (embedder_slug, quarter) to its current physical location.

        Pointer-first: a sister alias pointer, once created, is a durable
        guarantee the sister version is authoritative for this namespace --
        checked BEFORE any in-repo fallback, regardless of whether the old
        in-repo copy has been deleted yet.

        Returns None when neither a sister pointer nor in-repo legacy data
        exists for this namespace.
        """
        suffix = f"-{quarter}" if quarter else ""
        pointer_namespace = f"{self._repo_alias}-temporal-{embedder_slug}{suffix}"
        physical_name = f"code-indexer-temporal-{embedder_slug}{suffix}"

        target_path = self._alias_manager.read_alias(pointer_namespace)
        if target_path is not None:
            return ResolvedTemporalShard(
                pointer_namespace=pointer_namespace,
                physical_name=physical_name,
                path=Path(target_path),
                source=TemporalShardSource.SISTER_POINTER,
                is_queryable=True,
            )

        legacy_path = self._legacy_index_path / physical_name
        if temporal_shard_has_committed_rows(legacy_path):
            # Row-existence-not-queryability principle: hnsw_index.bin
            # presence is a QUERYABILITY signal, never the DATA-EXISTENCE
            # discriminator (which remains the row-existence scan above).
            is_queryable = (legacy_path / "hnsw_index.bin").exists()
            return ResolvedTemporalShard(
                pointer_namespace=pointer_namespace,
                physical_name=physical_name,
                path=legacy_path,
                source=TemporalShardSource.IN_REPO_LEGACY,
                is_queryable=is_queryable,
            )

        return None

    def catalog(self, embedder_slug: str) -> List[Optional[str]]:
        """Return the finite, authoritative (quarter or None-for-monolith)
        set for open-ended-range discovery (AC8 Fix 4), for ONE
        embedder_slug: UNION of (1) sister pointer files matching
        `{repo_alias}-temporal-{embedder_slug}-*` (alias-form; plus the
        quarter-less `{repo_alias}-temporal-{embedder_slug}` monolith
        pointer if present) and (2) in-repo
        `code-indexer-temporal-{embedder_slug}-YYYYQN` dirs (base-name
        form; plus the quarter-less monolith dir) that have real committed
        rows and NO corresponding pointer yet -- excluding any in-repo dir
        whose pointer ALREADY exists, to avoid double-counting a namespace
        as both sister-published and in-repo-present (round-8 N1: handles
        the partial-bootstrap window where some quarters are relocated and
        others are still in-repo).
        """
        quarter_prefix = f"{self._repo_alias}-temporal-{embedder_slug}"
        quarters: set = set()

        if self._alias_manager.aliases_dir.is_dir():
            escaped_prefix = glob.escape(quarter_prefix)
            for alias_file in self._alias_manager.aliases_dir.glob(
                f"{escaped_prefix}*.json"
            ):
                name = alias_file.stem
                if name == quarter_prefix:
                    quarters.add(None)
                elif name.startswith(f"{quarter_prefix}-"):
                    quarters.add(name[len(quarter_prefix) + 1 :])

        physical_prefix = f"code-indexer-temporal-{embedder_slug}"
        if self._legacy_index_path.is_dir():
            for entry in self._legacy_index_path.iterdir():
                if not entry.is_dir():
                    continue
                name = entry.name
                if name == physical_prefix:
                    quarter: Optional[str] = None
                elif name.startswith(f"{physical_prefix}-"):
                    quarter = name[len(physical_prefix) + 1 :]
                else:
                    continue
                if quarter in quarters:
                    continue
                if temporal_shard_has_committed_rows(entry):
                    quarters.add(quarter)

        return sorted(quarters, key=lambda q: (q is None, q))

    #: AC8 Step 6 bounded retry count for the resolve-acquire-validate
    #: handshake (Messi #14 -- no time.sleep; concurrent alias swaps are
    #: minutes apart vs a microsecond retry loop).
    _PIN_MAX_ATTEMPTS = 3

    def get_pinned(self, embedder_slug: str, quarter: Optional[str]) -> Optional[Path]:
        """Return the currently-pinned path for (embedder_slug, quarter),
        or None if no `pin()` block is active for this namespace.

        `_get_collection_path` MUST consult this FIRST, before calling
        `resolve()` again, while a pin is active (Story #1457 AC8 Step 6).
        """
        key = (embedder_slug, quarter)
        stack = self._thread_local_stacks().get(key)
        if stack:
            return stack[-1]
        return None

    def _push_pin(self, key: Tuple[str, Optional[str]], path: Path) -> None:
        self._thread_local_stacks().setdefault(key, []).append(path)

    def _pop_pin(self, key: Tuple[str, Optional[str]]) -> None:
        stacks = self._thread_local_stacks()
        stack = stacks.get(key)
        if stack:
            stack.pop()
            if not stack:
                del stacks[key]

    @contextmanager
    def pin(
        self, embedder_slug: str, quarter: Optional[str]
    ) -> Iterator[Optional[ResolvedTemporalShard]]:
        """Resolution-scope pin (Story #1457 AC8 Step 6).

        Performs a bounded resolve-acquire-validate-retry handshake against
        the current alias pointer, holds a QueryTracker refcount on the
        resolved v_* path, and pins `_get_collection_path` resolution to
        that exact path for the duration of the block -- closing the
        in-flight-temporal-version deletion hazard: `search()` re-resolves
        independently (via `_get_collection_path`), so a bare discovery-time
        refcount does not protect the version actually read if a concurrent
        alias swap races between discovery and the read.

        Yields the ResolvedTemporalShard. No-op (yields a plain `resolve()`
        result, no refcount, no pin-stack entry) when this resolver has no
        `query_tracker` (CLI/solo -- no server process at all).

        Raises:
            TemporalShardPinExhaustedError: after `_PIN_MAX_ATTEMPTS`
                consecutive lost resolve/validate races.
        """
        if self._query_tracker is None:
            yield self.resolve(embedder_slug, quarter)
            return

        key = (embedder_slug, quarter)
        for _attempt in range(self._PIN_MAX_ATTEMPTS):
            resolved = self.resolve(embedder_slug, quarter)
            if resolved is None:
                yield None
                return

            resolved_path = resolved.path.resolve()
            refcount_key = str(resolved_path)
            self._query_tracker.increment_ref(refcount_key)
            # Story #1457 HIGH #5 (2026-07-23 code review): everything
            # from here to the pin being pushed is wrapped in ONE
            # try/finally so an exception ANYWHERE in this window (the
            # revalidation resolve() call, push_pin, or the caller's own
            # code raising between the yield and its pop) still releases
            # EXACTLY the one refcount just acquired -- never a leak, and
            # never a double-decrement (the `decremented` flag makes the
            # lost-race path and the normal yield-then-pop path both
            # visible to this outer finally).
            decremented = False
            try:
                revalidated = self.resolve(embedder_slug, quarter)
                if revalidated is None or revalidated.path.resolve() != resolved_path:
                    # Lost the race: a concurrent swap moved the pointer
                    # between resolve and acquire. Release and retry.
                    self._query_tracker.decrement_ref(refcount_key)
                    decremented = True
                    continue

                self._push_pin(key, resolved.path)
                try:
                    yield revalidated
                finally:
                    self._pop_pin(key)
                    self._query_tracker.decrement_ref(refcount_key)
                    decremented = True
                return
            finally:
                if not decremented:
                    self._query_tracker.decrement_ref(refcount_key)

        raise TemporalShardPinExhaustedError(
            f"Lost {self._PIN_MAX_ATTEMPTS} consecutive resolve/validate "
            f"races for temporal namespace (embedder_slug={embedder_slug!r}, "
            f"quarter={quarter!r}) -- suspect persistent resolver "
            f"instability, not a one-off race."
        )


def _to_utc(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=timezone.utc)


def resolve_overlapping_shards(
    resolver: TemporalShardResolver,
    embedder_slug: str,
    start: Optional[datetime],
    end: Optional[datetime],
) -> List[ResolvedTemporalShard]:
    """Resolver-aware discovery (Story #1457 AC8 dispatch consumption
    contract items 1-2): return the List[ResolvedTemporalShard] whose date
    range overlaps [start, end], reusing resolver.catalog() (the
    authoritative pointer+in-repo union) and resolver.resolve() (pointer-
    first resolution) -- NEVER a bare index_path.iterdir() scan.

    None start or end means open-ended (all time on that side), matching
    get_overlapping_shards()'s existing semantics exactly. The quarter-less
    monolith namespace (quarter=None) is always included regardless of
    date range, appended last -- catalog()'s established None-last
    ordering is preserved verbatim (no re-sort here).

    This is a SEPARATE, additive function -- get_overlapping_shards()
    itself is left completely untouched for the CLI/solo path and any
    caller that has not been given a resolver.
    """
    norm_start = _to_utc(start)
    norm_end = _to_utc(end)

    resolved_shards: List[ResolvedTemporalShard] = []
    for quarter in resolver.catalog(embedder_slug):
        if quarter is not None:
            match = _QUARTER_SUFFIX_RE.match(f"-{quarter}")
            if match is None:
                continue
            year, q = int(quarter[:4]), int(quarter[5])
            shard_start, shard_end = get_quarter_range(year, q)
            overlaps = True
            if norm_end is not None and norm_end <= shard_start:
                overlaps = False
            if norm_start is not None and norm_start >= shard_end:
                overlaps = False
            if not overlaps:
                continue

        resolved = resolver.resolve(embedder_slug, quarter)
        if resolved is not None:
            resolved_shards.append(resolved)

    return resolved_shards
