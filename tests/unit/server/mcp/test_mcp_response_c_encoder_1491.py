"""Story #1491 AC7: MCP responses use the C JSON encoder (no indent=2).

Finding C3: json.dumps(data, indent=2) forces CPython's pure-Python
_iterencode fallback (5-10x slower, fully GIL-held) instead of the C
encoder. This test proves _mcp_response no longer requests indentation
while preserving semantic content.
"""

import json

from code_indexer.server.mcp.handlers._utils import _mcp_response
from tests.utils.perf_artifact_paths import perf_artifact_path


def test_mcp_response_does_not_indent_json() -> None:
    """The serialized text must not contain newline+space indentation
    artifacts that only appear when indent=2 is passed to json.dumps."""
    data = {"success": True, "results": [{"a": 1}, {"a": 2}]}
    response = _mcp_response(data)
    text = response["content"][0]["text"]

    # indent=2 always inserts "\n  " sequences for nested structures;
    # the compact C-encoder path never does.
    assert "\n" not in text


def test_mcp_response_content_is_semantically_identical() -> None:
    """Whitespace-only difference is permitted; the parsed value must be
    byte-for-byte identical to before the change."""
    data = {"success": True, "results": [1, 2, 3], "nested": {"x": "y"}}
    response = _mcp_response(data)
    text = response["content"][0]["text"]

    assert json.loads(text) == data


# Representative of a real large MCP search response: many nested result
# objects with text payloads, which is where the encoder choice actually costs.
_MEASURE_RESULT_COUNT = 4000
_MEASURE_TRIALS = 5
# Spread synthetic results over this many distinct package directories so the
# generated file paths vary the way a real repo's results do.
_MEASURE_DISTINCT_PACKAGES = 40
# Scores are spread deterministically over a small float range using this
# period, mirroring the float-heavy shape of a real ranked result set.
_MEASURE_SCORE_PERIOD = 100
_MEASURE_SCORE_DIVISOR = 200
_MEASURE_MIN_SCORE = 0.5
# The pure-Python _iterencode fallback is documented as 5-10x slower. Assert a
# deliberately conservative floor so the test measures a real effect without
# being sensitive to machine noise or a future CPython optimisation.
_MIN_SPEEDUP = 1.5


def _large_payload() -> dict:
    return {
        "success": True,
        "total_results": _MEASURE_RESULT_COUNT,
        "results": [
            {
                "file_path": (
                    f"src/pkg{index % _MEASURE_DISTINCT_PACKAGES}/module{index}.py"
                ),
                "line_number": index,
                "score": _MEASURE_MIN_SCORE
                + (index % _MEASURE_SCORE_PERIOD) / _MEASURE_SCORE_DIVISOR,
                "content": f"def handler_{index}(request):\n    return {index}\n",
                "metadata": {"language": "python", "branch": "main"},
            }
            for index in range(_MEASURE_RESULT_COUNT)
        ],
    }


def test_compact_serialization_is_measurably_faster_than_indent_two() -> None:
    """AC7: dropping indent=2 must be a real, measured improvement.

    Compares median-of-trials wall time for the indented (pure-Python
    _iterencode) encoding -- the pre-change shape -- against the REAL
    production serializer ``_mcp_response``, on the same large payload, and
    records both numbers via perf_artifact_path. Per Bug #1544, by default
    this lands in the gitignored .tmp/perf/ scratch location rather than the
    tracked reports/perf/ file, so running this test never dirties git
    status; set CIDX_WRITE_PERF_ARTIFACTS=1 to regenerate the committed
    evidence deliberately.
    """
    import statistics
    import time
    from typing import List

    artifact = perf_artifact_path("mcp_response_serialization_1491_ac7.json")
    payload = _large_payload()

    indented: List[float] = []
    production: List[float] = []
    for _ in range(_MEASURE_TRIALS):
        started = time.perf_counter()
        json.dumps(payload, indent=2)
        indented.append(time.perf_counter() - started)

        started = time.perf_counter()
        _mcp_response(payload)
        production.append(time.perf_counter() - started)

    indented_median = statistics.median(indented)
    production_median = statistics.median(production)
    speedup = indented_median / production_median

    artifact.parent.mkdir(parents=True, exist_ok=True)
    artifact.write_text(
        json.dumps(
            {
                "result_count": _MEASURE_RESULT_COUNT,
                "trials": _MEASURE_TRIALS,
                "before_indent2_median_s": indented_median,
                "after_production_mcp_response_median_s": production_median,
                "measured_speedup_x": speedup,
                "asserted_min_speedup_x": _MIN_SPEEDUP,
            },
            indent=2,
            sort_keys=True,
        )
    )

    assert speedup > _MIN_SPEEDUP, (
        f"the production serializer measured only {speedup:.2f}x faster than "
        f"indent=2 ({production_median:.4f}s vs {indented_median:.4f}s)"
    )
