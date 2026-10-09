"""Self-tests for the later-story check scaffolding."""

from invariant import FAIL, SKIPPED_NOT_IMPLEMENTED, Check
from later_checks import LATER_CHECKS, evaluate_later

EXPECTED_IDS = {
    "A4",
    "A6",
    "A7",
    "A8",
    "A10a",
    "A10b",
    "A10c",
    "A10d",
    "A10e",
    "A10f",
    "A11",
    "A12",
    "A13",
    "A14",
    "A15",
    "A15b",
    "A16",
    "A17",
    "A18",
    "A19",
    "A19b",
    "A20",
    "A21",
    "A21b",
    "A21c",
    "A21d",
    "A21e",
    "A21f",
    "A21g",
    "A21h",
    "A21i",
    "A22",
    "A22b",
    "A22c",
    "A22d",
    "A22e",
    "A23",
}


def _tree(tmp_path):
    src = tmp_path / "src"
    (src / "code_indexer").mkdir(parents=True)
    return src


def test_scaffold_covers_every_later_assertion():
    assert {c.check_id for c in LATER_CHECKS} == EXPECTED_IDS


def test_without_the_mechanisms_every_later_check_is_skipped(tmp_path):
    checks = evaluate_later(_tree(tmp_path), {})
    assert {c.name for c in checks} == EXPECTED_IDS
    assert all(c.status == SKIPPED_NOT_IMPLEMENTED and not c.ok for c in checks)
    a7 = next(c for c in checks if c.name == "A7")
    assert "S4" in a7.detail and "storage/pending_vectors.py" in a7.detail


def test_present_mechanism_without_an_enabled_check_fails(tmp_path):
    src = _tree(tmp_path)
    (src / "code_indexer" / "storage").mkdir()
    (src / "code_indexer" / "storage" / "pending_vectors.py").write_text(
        "# pending store\n"
    )
    a7 = next(c for c in evaluate_later(src, {}) if c.name == "A7")
    assert a7.status == FAIL
    assert "mechanism present" in a7.detail and "not enabled" in a7.detail


def test_an_enabled_evaluator_replaces_the_scaffold(tmp_path):
    enabled = {"A7": lambda: Check("A7", True, "0 inputs after store loss")}
    a7 = next(c for c in evaluate_later(_tree(tmp_path), enabled) if c.name == "A7")
    assert (a7.ok, a7.detail) == (True, "0 inputs after store loss")
