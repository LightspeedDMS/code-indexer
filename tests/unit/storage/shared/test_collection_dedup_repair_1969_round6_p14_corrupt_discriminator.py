"""Unit tests for Bug #1969 Round 6, finding P1-4: direct coverage of
`is_self_heal_reprocess_sidecar_corrupt` -- the discriminator that lets a
caller distinguish "the sidecar is genuinely absent/empty" (safe to treat
as no pending work) from "a per-event marker exists but is corrupt/
unreadable" (its replay record is lost and MUST force a broader detection
strategy, per `test_smart_indexer_1969_round6_p14_corrupt_sidecar_reconcile.py`).

Amendment 8: the single-file `.self-heal-reprocess-pending` sidecar this
file's tests once targeted was NEVER SHIPPED (verified absent from every
remote branch and every real index on this machine); the marker-directory
design below is the only sidecar storage format.
"""

import hashlib
from pathlib import Path

from code_indexer.storage.shared.collection_dedup_repair import (
    SELF_HEAL_REPROCESS_PENDING_DIR_NAME,
    is_self_heal_reprocess_sidecar_corrupt,
    quarantine_corrupt_self_heal_reprocess_sidecar,
)


def _marker_dir(collection_dir: Path) -> Path:
    return collection_dir / SELF_HEAL_REPROCESS_PENDING_DIR_NAME


def _write_marker(collection_dir: Path, name: str, content: str) -> Path:
    marker_dir = _marker_dir(collection_dir)
    marker_dir.mkdir(parents=True, exist_ok=True)
    marker_path = marker_dir / name
    marker_path.write_text(content)
    return marker_path


def _write_valid_marker(collection_dir: Path, rel_path: str, nonce: str) -> Path:
    """A marker whose filename digest matches its content, per Amendment 9's
    validation -- the production writer's naming convention."""
    digest = hashlib.sha256(rel_path.encode()).hexdigest()
    return _write_marker(collection_dir, f"{digest}.{nonce}", rel_path)


def test_absent_sidecar_is_not_corrupt(tmp_path: Path) -> None:
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is False


def test_empty_marker_directory_is_not_corrupt(tmp_path: Path) -> None:
    _marker_dir(tmp_path).mkdir()
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is False


def test_valid_populated_markers_are_not_corrupt(tmp_path: Path) -> None:
    _write_valid_marker(tmp_path, "a.py", "nonce-a")
    _write_valid_marker(tmp_path, "b.py", "nonce-b")
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is False


def test_empty_marker_is_corrupt(tmp_path: Path) -> None:
    _write_marker(tmp_path, "marker-empty.nonce", "")
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is True


def test_digest_mismatched_marker_is_corrupt(tmp_path: Path) -> None:
    """A marker whose content does not match its own filename digest is
    corrupt, per Amendment 9 -- proof the bytes were damaged after the
    fact, not a legitimate marker for a different path."""
    _write_marker(tmp_path, "marker-mismatched.nonce", "a.py")
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is True


def test_one_corrupt_marker_among_valid_ones_is_corrupt(tmp_path: Path) -> None:
    _write_valid_marker(tmp_path, "a.py", "nonce-a")
    _write_marker(tmp_path, "marker-bad.nonce", "")
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is True


def test_quarantine_preserves_valid_markers_while_moving_corrupt_one(
    tmp_path: Path,
) -> None:
    valid_marker = _write_valid_marker(tmp_path, "a.py", "nonce-a")
    bad_marker = _write_marker(tmp_path, "marker-bad.nonce", "")

    quarantine_corrupt_self_heal_reprocess_sidecar(tmp_path)

    assert not bad_marker.exists()
    marker_dir = _marker_dir(tmp_path)
    quarantined = list(marker_dir.glob(f"{bad_marker.name}.corrupt.*"))
    assert len(quarantined) == 1
    assert valid_marker.read_text() == "a.py"
    assert is_self_heal_reprocess_sidecar_corrupt(tmp_path) is False
