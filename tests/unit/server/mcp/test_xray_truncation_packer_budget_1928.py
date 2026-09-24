"""Bug #1928 final round (Opus P3.1): pack_entries_into_pages() undercounts
the JSON list-item separator.

json.dumps' default separators insert ", " (2 chars) between list items,
but the O(1) running-size delta only counted 1 char per separator. For
entries with a long serialized form the 1-char shortfall is negligible,
but for MANY TINY entries (e.g. plain ints in the `refine` field) the
shortfall compounds: Opus's repro showed 22/107 pages over budget at
budget_chars=5000, with one inline preview landing at 5042 chars --
pages up to ~1.5x the nominal budget.

Fix: derive the separator length from json.dumps' OWN actual output
(never hardcode "2" either) and use that value in the running-size
delta.
"""

from __future__ import annotations

import json

from code_indexer.server.mcp.handlers import xray_truncation as xt

_TINY_ENTRY_BUDGET = 5000
_MANY_TINY_ENTRIES = 2000
_RECONSTRUCTION_ENTRY_COUNT = 500


class TestPackerNeverExceedsBudgetWithTinyEntries:
    def test_every_multi_entry_page_stays_within_budget_for_tiny_int_entries(
        self,
    ) -> None:
        """The exact repro shape from Opus's finding: many plain-int
        entries in a field (mirrors `refine`), budget_chars=5000."""
        tiny_ints = list(range(_MANY_TINY_ENTRIES))
        pages = xt.pack_entries_into_pages(
            ["findings", "refine"],
            {"findings": [], "refine": tiny_ints},
            budget_chars=_TINY_ENTRY_BUDGET,
        )

        assert len(pages) > 1, "fixture must actually require multiple pages"
        multi_entry_pages = [p for p in pages if len(p["refine"]) > 1]
        assert multi_entry_pages, "fixture must produce at least one multi-entry page"
        for page in multi_entry_pages:
            serialized_len = len(json.dumps(page))
            assert serialized_len <= _TINY_ENTRY_BUDGET, (
                f"page with {len(page['refine'])} refine entries serialized to "
                f"{serialized_len} chars, exceeding budget {_TINY_ENTRY_BUDGET}"
            )

    def test_every_multi_entry_page_stays_within_budget_for_tiny_string_entries(
        self,
    ) -> None:
        """Same shortfall shape, but with short string entries (still far
        smaller than a real separator-inclusive contribution)."""
        tiny_strings = [f"f{i}.py" for i in range(_MANY_TINY_ENTRIES)]
        pages = xt.pack_entries_into_pages(
            ["matches", "evaluation_errors"],
            {"matches": tiny_strings, "evaluation_errors": []},
            budget_chars=_TINY_ENTRY_BUDGET,
        )

        multi_entry_pages = [p for p in pages if len(p["matches"]) > 1]
        assert multi_entry_pages, "fixture must produce at least one multi-entry page"
        for page in multi_entry_pages:
            serialized_len = len(json.dumps(page))
            assert serialized_len <= _TINY_ENTRY_BUDGET, (
                f"page with {len(page['matches'])} match entries serialized to "
                f"{serialized_len} chars, exceeding budget {_TINY_ENTRY_BUDGET}"
            )

    def test_reconstruction_still_exact_with_tiny_entries(self) -> None:
        tiny_ints = list(range(_RECONSTRUCTION_ENTRY_COUNT))
        pages = xt.pack_entries_into_pages(
            ["findings", "refine"],
            {"findings": [], "refine": tiny_ints},
            budget_chars=_TINY_ENTRY_BUDGET,
        )
        reconstructed = [e for p in pages for e in p["refine"]]
        assert reconstructed == tiny_ints
