"""classify_bootstrap_disposition() -- AC11's discovery/decision classifier
(Story #1457 AC11 investigation, round 11).

AC11 (one-time proactive bootstrap for pre-existing in-repo temporal
shards) remains STRUCTURALLY BLOCKED on Story #1458: its own spec text
states THREE explicit BINDING ("MUST") dependencies on infrastructure that
does not exist yet -- (1) AC11 MUST be invoked as literal step 2 of Story
#1458's per-repo fleet-migration job, never as an independent operation;
(2) AC11 MUST run INSIDE the server process under Story #1458's per-repo
WriteLockManager write lock, with genuine QueryTracker/CleanupManager
access for synchronous in-repo-tree reclamation (unlike AC6's refresh,
which runs in a child subprocess with no such access); (3) AC11 and Story
#1458's base-clone consolidation MUST run as ONE combined fleet-migration
pass per repo, not two independent ones. None of these are inventable
safely without Story #1458 existing -- a parallel, temporary write-lock/
job-invocation mechanism now would be redundant/inconsistent once Story
#1458 actually lands.

What round 10's AC1 work DID make newly reusable: `read_legacy_shard_rows`
already IS "the AC11 step-1 scan primitive" the spec explicitly asks for
("extract only the scanning logic") -- built for AC1, directly reusable
by AC11 unchanged.

This file adds the SECOND genuinely reusable, lock-free, forward-
compatible primitive AC11's eventual sweep will need on day one:
classify_bootstrap_disposition() -- Finding 6's four-way per-namespace
disposition classifier (ALREADY_PUBLISHED / NEEDS_BOOTSTRAP /
EMPTY_ARTIFACT), pure decision logic requiring no write lock, no
in-process server access, and no coordination with Story #1458's
base-clone consolidation -- so it is safe to build and test NOW, and the
eventual full AC11 sweep can call it unchanged once Story #1458's
orchestration context exists.

Real AliasManager, real filesystem -- no mocking of the code under test.
"""

from __future__ import annotations

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_bootstrap_disposition import (
    BootstrapDisposition,
    classify_bootstrap_disposition,
)


def test_already_published_when_alias_exists(tmp_path):
    aliases_dir = tmp_path / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    version_dir = tmp_path / "sister" / "v_1700000000"
    version_dir.mkdir(parents=True)
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    legacy_shard_dir = tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
    legacy_shard_dir.mkdir(parents=True)
    (legacy_shard_dir / "vector_abc.json").write_text('{"id": "p1"}')

    disposition = classify_bootstrap_disposition(
        alias_manager, "evolution-temporal-voyage_code_3-2024Q1", legacy_shard_dir
    )

    assert disposition == BootstrapDisposition.ALREADY_PUBLISHED


def test_needs_bootstrap_when_rows_exist_and_no_pointer(tmp_path):
    aliases_dir = tmp_path / "aliases"
    alias_manager = AliasManager(str(aliases_dir))

    legacy_shard_dir = tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
    legacy_shard_dir.mkdir(parents=True)
    (legacy_shard_dir / "vector_abc.json").write_text('{"id": "p1"}')

    disposition = classify_bootstrap_disposition(
        alias_manager, "evolution-temporal-voyage_code_3-2024Q1", legacy_shard_dir
    )

    assert disposition == BootstrapDisposition.NEEDS_BOOTSTRAP


def test_empty_artifact_when_no_rows_and_no_pointer(tmp_path):
    aliases_dir = tmp_path / "aliases"
    alias_manager = AliasManager(str(aliases_dir))

    legacy_shard_dir = tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
    legacy_shard_dir.mkdir(parents=True)
    # No vector_*.json files written -- a failed/empty prior indexing
    # attempt that left directory structure behind.

    disposition = classify_bootstrap_disposition(
        alias_manager, "evolution-temporal-voyage_code_3-2024Q1", legacy_shard_dir
    )

    assert disposition == BootstrapDisposition.EMPTY_ARTIFACT


def test_already_published_takes_priority_even_with_zero_local_rows(tmp_path):
    """A prior bootstrap already succeeded and the in-repo tree was
    already reclaimed (or never had rows to begin with) -- alias_exists
    is the discriminant, checked FIRST, regardless of local row state."""
    aliases_dir = tmp_path / "aliases"
    alias_manager = AliasManager(str(aliases_dir))
    version_dir = tmp_path / "sister" / "v_1700000000"
    version_dir.mkdir(parents=True)
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    legacy_shard_dir = tmp_path / "index" / "code-indexer-temporal-voyage_code_3-2024Q1"
    # Directory does not even exist -- already fully reclaimed.

    disposition = classify_bootstrap_disposition(
        alias_manager, "evolution-temporal-voyage_code_3-2024Q1", legacy_shard_dir
    )

    assert disposition == BootstrapDisposition.ALREADY_PUBLISHED
