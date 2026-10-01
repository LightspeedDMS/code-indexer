"""Story #1494 AC2: FTS `--regex` post-filter loop is bounded.

Finding A4 (GIL-blocking analysis report): `tantivy_index_manager.py`'s
per-result `compiled_regex_pattern.search(content_raw)` loop runs in the
server threadpool, GIL-held, scaling with result count x content size. An
unlimited (`limit=0`) regex query can iterate up to 100,000 candidates.
Fixed by capping the number of candidates that get the expensive Python
regex-extraction step via a named module-level constant
(`_MAX_REGEX_EXTRACTION_CANDIDATES`) -- candidates beyond the cap are still
returned (never silently dropped, per the project's anti-silent-failure
rule) with a `regex_extraction_capped: True` marker and a sentinel match
position instead of a real extracted position/snippet.

Real TantivyIndexManager, real on-disk index, real regex matching -- no
mocking of tantivy itself. The cap constant is monkeypatched down to a
small value purely so the test completes quickly; production behavior for
normal-sized (under-cap) result sets is verified field-by-field (except
`score`, a real BM25-derived float this test does not attempt to predict)
against the pre-#1494 contract, and covered additionally by the
pre-existing `test_tantivy_regex_1497.py`/`test_tantivy_regex_snippet_
extraction.py` suites, which this change must not regress.
"""

from __future__ import annotations

import pytest

from code_indexer.services import tantivy_index_manager as tim
from code_indexer.services.tantivy_index_manager import TantivyIndexManager

_TEST_CAP = 2
_DOC_COUNT = 5
_REGEX_PATTERN = "cancel_job_\\d+"


@pytest.fixture
def tantivy_manager(tmp_path):
    manager = TantivyIndexManager(tmp_path / "tantivy_index")
    manager.initialize_index(create_new=True)
    yield manager
    manager.close()


def _doc_content(i: int) -> str:
    return f"def cancel_job_{i}(self):\n    pass\n"


def _make_doc(i: int) -> dict:
    content = _doc_content(i)
    return {
        "path": f"src/module_{i}.py",
        "content": content,
        "content_raw": content,
        "identifiers": [f"cancel_job_{i}"],
        "line_start": 1,
        "line_end": 2,
        "language": "python",
    }


@pytest.fixture
def indexed_manager(tantivy_manager):
    for i in range(_DOC_COUNT):
        tantivy_manager.add_document(_make_doc(i))
    tantivy_manager.commit()
    return tantivy_manager


def _index_from_path(path: str) -> str:
    return path.split("module_")[1].split(".py")[0]


def _expected_uncapped_fields(result: dict) -> dict:
    """The complete, exactly-predictable field set (everything but `score`,
    a real BM25-derived float) a genuine per-document regex extraction
    produces for this fixture -- match is always on line 1 of a
    single-line-match document, so the column is fully determined by the
    document's own known content string."""
    i = _index_from_path(result["path"])
    content = _doc_content(int(i))
    match_text = f"cancel_job_{i}"
    return {
        "path": f"src/module_{i}.py",
        "line": 1,
        "column": content.index(match_text) + 1,
        "match_text": match_text,
        "snippet": "",
        "snippet_start_line": 1,
        "language": "python",
    }


def _without_score(result: dict) -> dict:
    return {k: v for k, v in result.items() if k != "score"}


class TestRegexExtractionCandidateCap:
    def test_cap_constant_is_a_named_module_level_constant(self) -> None:
        """Must be an explicit named constant, never a magic number inline
        in the loop (per AC2's explicit technical requirement)."""
        assert hasattr(tim, "_MAX_REGEX_EXTRACTION_CANDIDATES")
        assert isinstance(tim._MAX_REGEX_EXTRACTION_CANDIDATES, int)
        assert tim._MAX_REGEX_EXTRACTION_CANDIDATES > 0

    def test_candidates_beyond_cap_are_marked_capped_with_sentinel_values(
        self, indexed_manager, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """All DOC_COUNT documents must still come back (never silently
        omitted), but only the first _TEST_CAP get real regex extraction --
        the rest are marked regex_extraction_capped=True with the sentinel
        match_text (the raw query pattern, the same fallback the existing
        module-unavailable branch already uses) rather than a real
        extracted identifier."""
        monkeypatch.setattr(tim, "_MAX_REGEX_EXTRACTION_CANDIDATES", _TEST_CAP)

        results = indexed_manager.search(
            query_text=_REGEX_PATTERN,
            use_regex=True,
            snippet_lines=0,
            limit=0,
        )

        assert len(results) == _DOC_COUNT, (
            "capping regex extraction must never drop results from the returned set"
        )

        capped = [r for r in results if r.get("regex_extraction_capped") is True]
        uncapped = [r for r in results if r.get("regex_extraction_capped") is not True]
        assert len(uncapped) == _TEST_CAP
        assert len(capped) == _DOC_COUNT - _TEST_CAP

        for r in capped:
            assert r["match_text"] == _REGEX_PATTERN, (
                "capped candidates must use the sentinel match_text, "
                "proving real per-result extraction was skipped for them"
            )
        for r in uncapped:
            assert "regex_extraction_capped" not in r or (
                r["regex_extraction_capped"] is False
            )
            expected = _expected_uncapped_fields(r)
            assert _without_score(r) == expected

    def test_under_cap_regex_search_matches_pre_1494_field_contract(
        self, indexed_manager
    ) -> None:
        """With the real (large) production cap, a small result set must
        never carry the capped marker, and every field except `score`
        (path, line, column, match_text, snippet, language) must exactly
        match what a genuine, uncapped per-document regex extraction
        produces -- the same contract that existed before this story."""
        results = indexed_manager.search(
            query_text=_REGEX_PATTERN,
            use_regex=True,
            snippet_lines=0,
            limit=0,
        )

        assert len(results) == _DOC_COUNT
        assert not any(r.get("regex_extraction_capped") for r in results)
        for r in results:
            assert _without_score(r) == _expected_uncapped_fields(r)


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
