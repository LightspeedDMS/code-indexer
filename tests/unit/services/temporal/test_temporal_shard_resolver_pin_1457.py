"""TemporalShardResolver.pin() -- resolution-scope pin (Story #1457 AC8 Step 6).

Closes the in-flight-temporal-version deletion hazard: a query that resolves
a temporal shard, then re-resolves independently inside search() (via
_get_collection_path), could otherwise observe a DIFFERENT (newer) version
than the one whose refcount it never acquired -- leaving the version it
actually read unprotected against concurrent CleanupManager deletion.

pin() performs a bounded resolve-acquire-validate-retry handshake: resolve
the current shard, acquire a QueryTracker refcount on that exact path,
re-resolve to confirm it is still current (retry on race), then pin
resolution to that exact path for the duration of the block via a
per-(embedder_slug, quarter) nest-safe pin STACK that _get_collection_path
consults FIRST, before ever calling resolve() again.

This test file covers ONLY the core pin mechanism (no-op without a
QueryTracker, happy-path acquire+pin+release, retry-on-race, bounded
exhaustion) and its wiring into FilesystemVectorStore._get_collection_path.
The dispatch-loop wiring (temporal_fusion_dispatch.py), the sister-retry-on-
reclamation nested-pin mechanics, and pin-exhaustion observability counters
are Story #1457 AC8 Step 6 scope NOT YET covered here -- see
temporal_shard_resolver.py's module docstring for the honest scope
disclosure.

Real AliasManager, real QueryTracker, real filesystem -- no mocking of the
code under test.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardPinExhaustedError,
    TemporalShardResolver,
)


def _make_resolver(
    tmp_path: Path,
    repo_alias: str = "evolution",
    query_tracker: object = None,
) -> TemporalShardResolver:
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)
    alias_manager = AliasManager(str(aliases_dir))
    return TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=query_tracker,
    )


def test_pin_is_noop_without_query_tracker(tmp_path):
    """CLI/solo (no query_tracker): pin() yields a plain resolve() result --
    no refcount acquired, no pin-stack entry pushed."""
    resolver = _make_resolver(tmp_path, query_tracker=None)
    version_dir = tmp_path / "sister" / ".versioned" / "ns" / "v_1700000000"
    version_dir.mkdir(parents=True)
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    with resolver.pin("voyage_code_3", "2024Q1") as pinned:
        assert pinned is not None
        assert pinned.path == version_dir
        # True no-op: no pin-stack entry was pushed.
        assert resolver.get_pinned("voyage_code_3", "2024Q1") is None


def test_pin_acquires_refcount_and_pins_resolution_then_releases(tmp_path):
    """With a real QueryTracker: pin() increments the refcount on the
    resolved path and pushes it onto the pin stack for the duration of the
    block, then releases both (refcount -> 0, pin -> None) on exit."""
    query_tracker = QueryTracker()
    resolver = _make_resolver(tmp_path, query_tracker=query_tracker)
    version_dir = tmp_path / "sister" / ".versioned" / "ns" / "v_1700000000"
    version_dir.mkdir(parents=True)
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )
    refcount_key = str(version_dir.resolve())

    assert query_tracker.get_ref_count(refcount_key) == 0
    assert resolver.get_pinned("voyage_code_3", "2024Q1") is None

    with resolver.pin("voyage_code_3", "2024Q1") as pinned:
        assert pinned is not None
        assert pinned.path == version_dir
        assert query_tracker.get_ref_count(refcount_key) == 1
        assert resolver.get_pinned("voyage_code_3", "2024Q1") == version_dir

    assert query_tracker.get_ref_count(refcount_key) == 0
    assert resolver.get_pinned("voyage_code_3", "2024Q1") is None


class _SwapAliasOnFirstIncrement(QueryTracker):
    """Test double: performs a REAL `AliasManager.swap_alias` call on the
    FIRST `increment_ref` invocation only, then delegates to the real
    QueryTracker logic for every call (this and subsequent ones).

    Deterministically lands a race between pin()'s acquire (b) and
    validate (c) steps -- exactly where a real concurrent refresh's
    alias-swap publish would land -- without any sleep-based timing and
    without mocking the resolver's own resolve()/pin() logic at all: the
    resolver runs completely unmodified, real AliasManager, real files.
    """

    def __init__(
        self, alias_manager: AliasManager, pointer_namespace: str, new_target: str
    ) -> None:
        super().__init__()
        self._alias_manager = alias_manager
        self._pointer_namespace = pointer_namespace
        self._new_target = new_target
        self._swapped = False

    def increment_ref(self, index_path: str) -> None:
        if not self._swapped:
            self._swapped = True
            old_target = self._alias_manager.read_alias(self._pointer_namespace)
            self._alias_manager.swap_alias(
                self._pointer_namespace, self._new_target, old_target
            )
        super().increment_ref(index_path)


def test_pin_retries_when_pointer_changes_between_resolve_and_validate(tmp_path):
    """A concurrent alias swap landing between pin()'s resolve (a) and
    validate (c) steps must be detected -- the stale refcount released, and
    the handshake retried -- landing on the post-swap version, never
    silently pinning the stale one."""
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)
    alias_manager = AliasManager(str(aliases_dir))

    version_a = sister_root / ".versioned" / "ns" / "v_1700000000"
    version_a.mkdir(parents=True)
    version_b = sister_root / ".versioned" / "ns" / "v_1700000001"
    version_b.mkdir(parents=True)
    pointer_namespace = "evolution-temporal-voyage_code_3-2024Q1"
    alias_manager.create_alias(pointer_namespace, str(version_a))

    tracker = _SwapAliasOnFirstIncrement(
        alias_manager, pointer_namespace, str(version_b)
    )
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=tracker,
    )

    with resolver.pin("voyage_code_3", "2024Q1") as pinned:
        assert pinned is not None
        assert pinned.path == version_b
        assert resolver.get_pinned("voyage_code_3", "2024Q1") == version_b

    # Both the aborted A attempt and the successful B attempt released
    # their refcounts cleanly.
    assert tracker.get_ref_count(str(version_a.resolve())) == 0
    assert tracker.get_ref_count(str(version_b.resolve())) == 0


class _AlwaysSwapQueryTracker(QueryTracker):
    """Test double: performs a REAL alias swap on EVERY `increment_ref`
    call, cycling through `swap_targets` -- simulating a persistently
    racing pointer (a new swap lands between every resolve/validate pair),
    to deterministically exercise pin()'s bounded exhaustion path."""

    def __init__(
        self,
        alias_manager: AliasManager,
        pointer_namespace: str,
        swap_targets: list,
    ) -> None:
        super().__init__()
        self._alias_manager = alias_manager
        self._pointer_namespace = pointer_namespace
        self._swap_targets = iter(swap_targets)

    def increment_ref(self, index_path: str) -> None:
        new_target = next(self._swap_targets, None)
        if new_target is not None:
            old_target = self._alias_manager.read_alias(self._pointer_namespace)
            self._alias_manager.swap_alias(
                self._pointer_namespace, new_target, old_target
            )
        super().increment_ref(index_path)


def test_pin_raises_pin_exhausted_error_after_max_attempts(tmp_path):
    """A persistently racing pointer (a new swap lands between EVERY
    resolve/validate pair) exhausts pin()'s bounded retry budget and
    raises TemporalShardPinExhaustedError -- never silently pins a stale
    or partially-validated path, and never leaks a refcount along the
    way."""
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)
    alias_manager = AliasManager(str(aliases_dir))

    versions = []
    for i in range(4):
        v = sister_root / ".versioned" / "ns" / f"v_170000000{i}"
        v.mkdir(parents=True)
        versions.append(v)
    pointer_namespace = "evolution-temporal-voyage_code_3-2024Q1"
    alias_manager.create_alias(pointer_namespace, str(versions[0]))

    tracker = _AlwaysSwapQueryTracker(
        alias_manager, pointer_namespace, [str(v) for v in versions[1:]]
    )
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=tracker,
    )

    with pytest.raises(TemporalShardPinExhaustedError):
        with resolver.pin("voyage_code_3", "2024Q1"):
            pass

    for v in versions:
        assert tracker.get_ref_count(str(v.resolve())) == 0


class _RaisingOnSecondReadAliasManager(AliasManager):
    """Test double: a REAL AliasManager whose read_alias() raises on the
    SECOND call only (delegating normally on every other call). Lands the
    failure during pin()'s revalidation resolve() call (the 2nd of 2
    resolve() calls, each invoking read_alias() once) -- simulating a
    real external failure (e.g. a transient filesystem error) WITHOUT
    patching the resolver under test at all."""

    def __init__(self, aliases_dir: str) -> None:
        super().__init__(aliases_dir)
        self._call_count = 0

    def read_alias(self, alias_name: str):
        self._call_count += 1
        if self._call_count == 2:
            raise RuntimeError("simulated alias read failure during revalidation")
        return super().read_alias(alias_name)


def test_pin_releases_refcount_when_revalidate_raises(tmp_path):
    """Story #1457 HIGH #5 (2026-07-23 code review): an exception raised
    AFTER increment_ref() (e.g. during the revalidate resolve() call)
    must not leak the refcount -- exactly one decrement per increment,
    no matter what path is taken."""
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "clone" / ".code-indexer" / "index"
    legacy_index_path.mkdir(parents=True)

    alias_manager = _RaisingOnSecondReadAliasManager(str(aliases_dir))
    version_dir = sister_root / ".versioned" / "ns" / "v_1700000000"
    version_dir.mkdir(parents=True)
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    query_tracker = QueryTracker()
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=query_tracker,
    )

    with pytest.raises(RuntimeError, match="simulated alias read failure"):
        with resolver.pin("voyage_code_3", "2024Q1"):
            pass  # never reached

    refcount_key = str(version_dir.resolve())
    assert query_tracker.get_ref_count(refcount_key) == 0, (
        "refcount must be released even when an exception is raised "
        "between increment_ref() and the pin being pushed"
    )
