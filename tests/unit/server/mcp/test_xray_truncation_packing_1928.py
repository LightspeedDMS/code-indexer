"""Unit tests for xray_truncation.pack_entries_into_pages() (Bug #1928
rework): whole-entry packing that NEVER shrinks or splits an entry to
make it fit -- an entry alone larger than budget_chars gets its own,
necessarily oversized, page. See _xray_truncation_test_helpers.py for
shared fixtures and the module-level Bug #1928 rationale.
"""

from __future__ import annotations

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import make_error, make_finding, make_match


class TestPackEntriesIntoPagesWholeEntries:
    def test_all_entries_fit_returns_single_page_unmodified(self) -> None:
        entries_a = [make_match(i) for i in range(3)]
        pages = xt.pack_entries_into_pages(
            ["matches", "evaluation_errors"],
            {"matches": entries_a, "evaluation_errors": []},
            budget_chars=100_000,
        )
        assert len(pages) == 1
        assert pages[0]["matches"] == entries_a

    def test_concatenated_pages_reproduce_full_input_in_order(self) -> None:
        entries_a = [make_match(i) for i in range(40)]
        entries_b = [make_error(i) for i in range(10)]
        pages = xt.pack_entries_into_pages(
            ["matches", "evaluation_errors"],
            {"matches": entries_a, "evaluation_errors": entries_b},
            budget_chars=500,
        )
        assert len(pages) > 1, "fixture must actually require multiple pages"
        reconstructed_a = [e for p in pages for e in p["matches"]]
        reconstructed_b = [e for p in pages for e in p["evaluation_errors"]]
        assert reconstructed_a == entries_a
        assert reconstructed_b == entries_b

    def test_oversized_single_entry_gets_its_own_unmodified_page(self) -> None:
        """The core P1 data-loss fix: an entry larger than budget_chars is
        NEVER shrunk when building cache pages -- it gets its own page,
        byte-for-byte intact, however large that makes the page."""
        huge_finding = {
            "pattern": "huge",
            "message": "m" * 20_000,
            "involved": list(range(2000)),
        }
        small_findings = [make_finding(i) for i in range(5)]
        pages = xt.pack_entries_into_pages(
            ["findings", "refine"],
            {"findings": [huge_finding] + small_findings, "refine": []},
            budget_chars=300,
        )
        huge_page = next(
            p for p in pages if any(f.get("pattern") == "huge" for f in p["findings"])
        )
        stored_huge = huge_page["findings"][0]
        assert stored_huge == huge_finding
        assert len(stored_huge["involved"]) == 2000, (
            "the cached page must never shrink an oversized entry -- all "
            "2000 involved ids must survive intact"
        )
