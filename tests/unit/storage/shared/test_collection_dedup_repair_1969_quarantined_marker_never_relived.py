"""Unit test for Bug #1969 (post-Round-6 defect found during E2E
validation): a quarantined corrupt self-heal-reprocess marker
(`<name>.corrupt.<timestamp>`, produced by
``quarantine_corrupt_self_heal_reprocess_sidecar``) must NEVER be treated
as a live marker again.

Before this fix, ``_read_self_heal_reprocess_markers`` skipped only
``*.tmp`` entries -- a quarantined ``*.corrupt.<timestamp>`` file was still
handed to ``Path.read_text()`` on every subsequent read. For the real-world
corruption (invalid UTF-8 bytes), that raised ``UnicodeDecodeError`` on
every single call, so every later `cidx index`/reconcile run logged
`WARNING self-heal-reprocess marker ...corrupt.<ts> could not be read
('utf-8' codec ...)` forever -- permanent WARNING noise that would trip
the E2E log-audit gate (`tests/e2e/log_audit_gate.py`).

This test proves the read path ignores an already-quarantined marker
entirely (no WARNING, not surfaced in the pending set) while still
returning every genuinely live marker, and that the corrupt-detection
discriminator does not re-flag the quarantined file (so the P1-4 forced
reconcile fires exactly once per corruption event, not on every run).
"""

import hashlib
import logging
from pathlib import Path

from code_indexer.storage.shared.collection_dedup_repair import (
    SELF_HEAL_REPROCESS_PENDING_DIR_NAME,
    is_self_heal_reprocess_sidecar_corrupt,
    quarantine_corrupt_self_heal_reprocess_sidecar,
    read_pending_self_heal_reprocess_paths,
)


def _marker_dir(collection_dir: Path) -> Path:
    return collection_dir / SELF_HEAL_REPROCESS_PENDING_DIR_NAME


def _write_valid_marker(collection_dir: Path, rel_path: str, nonce: str) -> Path:
    digest = hashlib.sha256(rel_path.encode()).hexdigest()
    marker_dir = _marker_dir(collection_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / f"{digest}.{nonce}"
    marker_path.write_text(rel_path)
    return marker_path


def test_quarantined_marker_is_never_reread_or_rewarned(tmp_path: Path, caplog) -> None:
    valid_marker = _write_valid_marker(tmp_path, "a.py", "nonce-a")

    marker_dir = _marker_dir(tmp_path)
    bad_marker = marker_dir / "deadbeefcafe.nonce-bad"
    # Invalid UTF-8 bytes -- the exact real-world corruption from the
    # #1969 E2E report (`'utf-8' codec ...` decode failure).
    bad_marker.write_bytes(b"\xff\xfe\x00bad-not-utf8")

    # Sanity: the sidecar IS corrupt before quarantine.
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is True

    quarantine_corrupt_self_heal_reprocess_sidecar(tmp_path)
    assert not bad_marker.exists()
    quarantined = list(marker_dir.glob(f"{bad_marker.name}.corrupt.*"))
    assert len(quarantined) == 1, "corrupt marker must be preserved, not deleted"

    caplog.clear()
    with caplog.at_level(logging.WARNING):
        pending = read_pending_self_heal_reprocess_paths(tmp_path)

    # The live marker is still read correctly.
    assert pending == frozenset({"a.py"})
    assert valid_marker.exists()

    # No WARNING about the quarantined marker on this or any subsequent read.
    assert not any(
        "could not be read" in record.message for record in caplog.records
    ), f"unexpected WARNING(s) about quarantined marker: {caplog.records}"

    # The forced-reconcile discriminator must not re-trip for the SAME
    # already-quarantined corruption event (it already fired once, via the
    # quarantine call above).
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is False
