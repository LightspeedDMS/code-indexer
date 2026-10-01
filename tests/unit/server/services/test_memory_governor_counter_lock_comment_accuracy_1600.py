"""B3 (code review remediation, Story #1600): 3 comment/docstring sites in
memory_governor.py falsely claim query_admissions_denied is "the first
[GovernorCounters] counter mutated from concurrent request-handling
threads". This is factually false: src/code_indexer/services/temporal/
temporal_fusion_dispatch.py:614-615 already mutates
counters.shards_evicted_after_use and calls maybe_trim() (which increments
counters.trim_calls) from a request thread (the temporal dispatch path),
neither lock-protected -- BEFORE query_admissions_denied (Story #1600)
existed.

The correct, narrower claim is that query_admissions_denied is the first
counter given EXPLICIT LOCK PROTECTION for its cross-thread mutation --
not the first one mutated cross-thread at all. This is a comment-only fix
(no behavioral/functional change): shards_evicted_after_use/trim_calls
remain deliberately unprotected (a plain `+= 1` on an int is GIL-atomic
enough that this is a metrics-accuracy issue, not a correctness one) --
out of scope to add locking there.

Covers all 3 sites: the GovernorCounters field comment, the __init__
_counters_lock comment, and the increment_query_admissions_denied()
docstring. All 3 must name BOTH pre-existing unprotected counters
(shards_evicted_after_use and trim_calls), not just one, so the reader
understands the full extent of the pre-existing condition.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_GOVERNOR_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "services" / "memory_governor.py"
)

_FALSE_CLAIM_PHRASE = "first counter mutated from concurrent"
_FALSE_CLAIM_PHRASE_ALT = "first GovernorCounters field mutated from concurrent"
_CROSS_THREAD_UNPROTECTED_MARKER = "shards_evicted_after_use"
_UNPROTECTED_SECOND_MARKER = "trim_calls"


def _source() -> str:
    return _GOVERNOR_PATH.read_text()


class TestGovernorCountersFieldCommentAccuracy:
    def test_query_admissions_denied_field_comment_does_not_falsely_claim_first_mutated(
        self,
    ):
        source = _source()
        field_start = source.find("query_admissions_denied: int = 0")
        assert field_start != -1, "query_admissions_denied field not found"
        comment_start = source.rfind("\n\n", 0, field_start)
        comment = source[comment_start:field_start]

        assert _FALSE_CLAIM_PHRASE not in comment, (
            "B3: the query_admissions_denied field comment must not claim "
            "it is 'the first counter mutated from concurrent' threads -- "
            "temporal_fusion_dispatch.py already mutates "
            "shards_evicted_after_use/trim_calls from a request thread, "
            "unprotected, before this field existed."
        )
        assert _FALSE_CLAIM_PHRASE_ALT not in comment
        assert _CROSS_THREAD_UNPROTECTED_MARKER in comment, (
            "B3: the corrected comment must note shards_evicted_after_use "
            "is also cross-thread-mutated (unprotected, out of scope)."
        )
        assert _UNPROTECTED_SECOND_MARKER in comment, (
            "B3: the corrected comment must note trim_calls is also "
            "cross-thread-mutated (unprotected, out of scope)."
        )


class TestInitCountersLockCommentAccuracy:
    def test_counters_lock_comment_does_not_falsely_claim_first_mutated(self):
        source = _source()
        assign_start = source.find("self._counters_lock = threading.Lock()")
        assert assign_start != -1, "_counters_lock assignment not found"
        comment_start = source.rfind("\n\n", 0, assign_start)
        comment = source[comment_start:assign_start]

        assert _FALSE_CLAIM_PHRASE not in comment, (
            "B3: the __init__ _counters_lock comment must not claim "
            "query_admissions_denied is 'the first counter mutated from "
            "concurrent' threads."
        )
        assert _CROSS_THREAD_UNPROTECTED_MARKER in comment, (
            "B3: the corrected comment must note shards_evicted_after_use "
            "is also cross-thread-mutated (unprotected, out of scope)."
        )
        assert _UNPROTECTED_SECOND_MARKER in comment, (
            "B3: the corrected comment must note trim_calls is also "
            "cross-thread-mutated (unprotected, out of scope)."
        )


class TestIncrementQueryAdmissionsDeniedDocstringAccuracy:
    def test_docstring_does_not_falsely_claim_unlike_every_other_counter(self):
        source = _source()
        def_start = source.find("def increment_query_admissions_denied(self) -> None:")
        assert def_start != -1, "increment_query_admissions_denied not found"
        docstring_start = source.find('"""', def_start)
        docstring_end = source.find('"""', docstring_start + 3)
        docstring = source[docstring_start:docstring_end]

        assert "Unlike every other GovernorCounters field" not in docstring, (
            "B3: the docstring must not claim query_admissions_denied is "
            "the ONLY cross-thread-mutated counter -- "
            "shards_evicted_after_use/trim_calls are also mutated from a "
            "request thread (temporal_fusion_dispatch.py), unprotected."
        )
        assert _CROSS_THREAD_UNPROTECTED_MARKER in docstring, (
            "B3: the corrected docstring must note shards_evicted_after_use "
            "is also cross-thread-mutated (unprotected, out of scope)."
        )
        assert _UNPROTECTED_SECOND_MARKER in docstring, (
            "B3: the corrected docstring must note trim_calls is also "
            "cross-thread-mutated (unprotected, out of scope)."
        )
        assert "temporal_fusion_dispatch" in docstring, (
            "B3: the corrected docstring must cite the actual "
            "cross-thread call site (temporal_fusion_dispatch.py) that "
            "predates and contradicts the false 'first' claim."
        )
