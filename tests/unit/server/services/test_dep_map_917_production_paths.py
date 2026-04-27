"""
Story #917 — Production-path lock acquisition tests.

Verifies that the domain lock is acquired in the PRODUCTION helpers:

  _repair_one_self_loop:    lock observed at atomic_write_text call
  rewrite_malformed_yaml_file: lock observed at build_and_append_malformed_yaml_journal_entry
    call (called immediately after write_bytes succeeds — same lock scope)
  _execute_unique_rewrite:  lock observed at atomic_write_text call

Each test uses monkeypatch to wrap the real function with a lock-state observer
(make_lock_observer). The observer records whether the domain lock was locked at
the moment the real write (or immediate post-write journal append) was called,
then delegates to the real function unchanged.

Tests FAIL until production code acquires the domain lock inside the helpers.
"""

import re

from tests.unit.server.services.test_dep_map_917_builders import (
    make_anomaly,
    make_lock_observer,
    write_domain_fixtures,
)


def test_self_loop_production_path_acquires_lock(tmp_path, monkeypatch):
    """_repair_one_self_loop holds the domain lock at the atomic_write_text call site."""
    import code_indexer.server.services.dep_map_repair_phase37 as phase37_mod
    from code_indexer.server.services.dep_map_repair_phase37 import (
        _repair_one_self_loop,
    )

    output_dir = tmp_path / "dep-map"
    output_dir.mkdir()
    write_domain_fixtures(output_dir, "self_loop", domain_name="domain-a")
    anomaly = make_anomaly("SELF_LOOP", "domain-a")

    observer, lock_was_held = make_lock_observer(
        "domain-a", phase37_mod.atomic_write_text
    )
    monkeypatch.setattr(phase37_mod, "atomic_write_text", observer)

    fixed: list[str] = []
    errors: list[str] = []
    _repair_one_self_loop(output_dir, anomaly, fixed, errors, journal=None)

    assert not errors, f"Unexpected errors: {errors}"
    assert lock_was_held, "atomic_write_text was never called — no write occurred"
    assert lock_was_held[0], (
        "domain lock was NOT held at atomic_write_text in _repair_one_self_loop"
    )


def test_malformed_yaml_production_path_acquires_lock(tmp_path, monkeypatch):
    """rewrite_malformed_yaml_file holds the domain lock at the write_bytes call site.

    build_and_append_malformed_yaml_journal_entry is called immediately after
    file_path.write_bytes inside rewrite_malformed_yaml_file — both calls occur
    within the same lock scope, so observing lock state at journal-append entry
    correctly reflects whether the lock was held during the write.
    """
    import code_indexer.server.services.dep_map_repair_malformed_yaml as malformed_mod
    from code_indexer.server.services.dep_map_repair_malformed_yaml import (
        repair_single_malformed_yaml_anomaly,
    )
    from code_indexer.server.services.dep_map_repair_phase37 import (
        build_and_append_malformed_yaml_journal_entry as real_journal_fn,
    )

    output_dir = tmp_path / "dep-map"
    output_dir.mkdir()
    write_domain_fixtures(output_dir, "malformed_yaml", domain_name="domain-z")
    anomaly = make_anomaly("MALFORMED_YAML", "domain-z", "malformed yaml frontmatter")
    domain_list = [
        {
            "name": "domain-z",
            "last_analyzed": "2024-06-01T12:00:00",
            "participating_repos": ["repo-x", "repo-y"],
        }
    ]

    observer, lock_was_held = make_lock_observer("domain-z", real_journal_fn)
    monkeypatch.setattr(
        malformed_mod, "build_and_append_malformed_yaml_journal_entry", observer
    )

    def _bounds(content):
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return None
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                return (0, i)
        return None

    fixed: list[str] = []
    errors: list[str] = []
    repair_single_malformed_yaml_anomaly(
        output_dir,
        anomaly,
        domain_list,
        fixed,
        errors,
        domain_analyzer=None,
        log_fn=lambda msg: None,
        locate_frontmatter_bounds_fn=_bounds,
        is_safe_domain_name_fn=lambda n: bool(re.match(r"^[a-z0-9][a-z0-9-]*$", n)),
    )

    assert lock_was_held, "build_and_append_malformed_yaml_journal_entry never called"
    assert lock_was_held[0], (
        "domain lock was NOT held at write time in rewrite_malformed_yaml_file"
    )


def test_garbage_domain_production_path_acquires_lock(tmp_path, monkeypatch):
    """_execute_unique_rewrite holds the source domain lock at the atomic_write_text call."""
    import code_indexer.server.services.dep_map_repair_phase37 as phase37_mod
    from code_indexer.server.services.dep_map_repair_garbage_domain import (
        build_inverted_repo_index,
        repair_one_garbage_domain_anomaly,
    )
    from code_indexer.server.services.dep_map_repair_phase37 import (
        Action,
        JournalEntry,
        RepairJournal,
    )

    _PROSE = "the order-service repo handles order events"
    _SRC = "domain-src"
    _TGT = "order-fulfillment"
    output_dir = tmp_path / "dep-map"
    output_dir.mkdir()
    write_domain_fixtures(output_dir, "garbage_source", domain_name=_SRC, prose=_PROSE)
    write_domain_fixtures(output_dir, "garbage_target", domain_name=_TGT)
    domain_list = [
        {"name": _SRC, "participating_repos": ["service-a"]},
        {"name": _TGT, "participating_repos": ["order-service"]},
    ]
    journal = RepairJournal(journal_path=tmp_path / "journal.jsonl")
    repo_to_domains = build_inverted_repo_index(domain_list)
    anomaly = make_anomaly(
        "GARBAGE_DOMAIN_REJECTED",
        _SRC,
        f"prose-fragment target domain rejected: '{_PROSE}'",
    )

    # _execute_unique_rewrite imports atomic_write_text from phase37 inside the function
    # body — patching phase37_mod.atomic_write_text is picked up at import time.
    observer, lock_was_held = make_lock_observer(_SRC, phase37_mod.atomic_write_text)
    monkeypatch.setattr(phase37_mod, "atomic_write_text", observer)

    def _append(jnl, src, tgt, action, citations, file_writes=None, errors=None):
        entry = JournalEntry(
            anomaly_type="GARBAGE_DOMAIN_REJECTED",
            source_domain=src,
            target_domain=tgt or "",
            source_repos=[],
            target_repos=[],
            verdict="N_A",
            action=action.value,
            citations=citations,
            file_writes=file_writes or [],
            claude_response_raw="",
            effective_mode="deterministic",
        )
        jnl.append(entry)

    def _backfill(
        jnl,
        stem,
        tgt,
        src_path,
        tgt_path,
        cells,
        fixed,
        errors,
        dry_run=False,
        journal_disabled=False,
        effective_mode="enabled",
        would_be_writes=None,
    ):
        _append(jnl, stem, tgt, Action.garbage_domain_remapped, [], errors=errors)

    fixed: list[str] = []
    errors: list[str] = []
    repair_one_garbage_domain_anomaly(
        output_dir,
        anomaly,
        repo_to_domains,
        journal,
        fixed,
        errors,
        is_safe_domain_name_fn=lambda n: bool(re.match(r"^[a-z0-9][a-z0-9-]*$", n)),
        append_journal_fn=_append,
        journal_and_backfill_fn=_backfill,
        extract_prose_fn=lambda msg: msg.split("'")[1] if "'" in msg else msg,
    )

    assert lock_was_held, "atomic_write_text was never called in garbage-domain repair"
    assert lock_was_held[0], (
        "source domain lock was NOT held at atomic_write_text in _execute_unique_rewrite"
    )
