"""
Story #911 AC5: Path Safety And Missing-File Handling.

Tests that _repair_garbage_domain_rejected fails safely when:
- The anomaly file contains a path traversal sequence (..)
- The source file does not exist on disk
- The target domain .md file is absent when a unique match is found
"""

from tests.unit.server.services.test_dep_map_911_builders import (
    make_domains_json_911,
    make_executor_911,
    make_garbage_domain_anomaly,
    make_source_domain_file,
)

_PROSE = "order-service lookup token"
_SOURCE_STEM = "domain-a"
_ANOMALY_FILE = f"{_SOURCE_STEM}.md"
_DOMAINS = [
    {"name": "order-fulfillment", "participating_repos": ["order-service"]},
]


def test_path_traversal_appends_unsafe_error(tmp_path):
    """Anomaly file containing '..' is rejected; error appended and no file written."""
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly("../evil.md", _PROSE)
    executor = make_executor_911()
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    assert any("unsafe path" in e for e in errors)
    assert {f.name for f in tmp_path.iterdir()} == {"_domains.json"}


def test_missing_source_file_appends_not_found_error(tmp_path):
    """Anomaly pointing to a nonexistent source file appends a file-not-found error."""
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly("nonexistent.md", _PROSE)
    executor = make_executor_911()
    errors: list = []

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    assert any("file not found" in e for e in errors)


def test_missing_target_domain_file_appends_error(tmp_path):
    """Unique match found but target .md absent → error; source file is unchanged."""
    make_source_domain_file(tmp_path, _SOURCE_STEM, _PROSE)
    make_domains_json_911(tmp_path, _DOMAINS)
    anomaly = make_garbage_domain_anomaly(_ANOMALY_FILE, _PROSE)
    executor = make_executor_911()
    errors: list = []
    original_bytes = (tmp_path / _ANOMALY_FILE).read_bytes()

    executor._repair_garbage_domain_rejected(tmp_path, anomaly, [], errors)

    assert any("missing for mirror backfill" in e for e in errors)
    assert (tmp_path / _ANOMALY_FILE).read_bytes() == original_bytes


def test_append_garbage_journal_failure_surfaces_to_errors():
    """When journal.append() raises, the error goes to errors[] not silently to logger.

    Regression for M1 (Codex review): _append_garbage_journal must propagate journal
    write failures to the caller's errors[] list when errors is provided, so that
    callers on the ambiguous, no-candidate, and unsafe-target paths are informed
    of journal failures (MESSI Rule 13 — no silent failures).
    """
    from code_indexer.server.services.dep_map_repair_phase37 import Action
    from tests.unit.server.services.test_dep_map_911_builders import make_executor_911

    class FailJournal:
        def append(self, _entry):
            raise RuntimeError("disk full")

    executor = make_executor_911()
    errors: list = []
    executor._append_garbage_journal(
        FailJournal(),
        "domain-a",
        "order-fulfillment",
        Action.garbage_domain_remapped,
        [],
        errors=errors,
    )
    assert any("journal write failed" in e for e in errors)
