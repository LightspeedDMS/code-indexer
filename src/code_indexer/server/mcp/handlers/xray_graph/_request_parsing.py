"""Request parsing/validation for analyze_graph (Issue #1935 Part 2).

Moved verbatim out of the monolithic xray_graph.py -- pure relocation, zero
behaviour change. See that package's __init__.py module docstring for the
overall analyze_graph tool context.
"""

from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Tuple, Union

from .._utils import _parse_and_collapse_repo_alias

_DEFAULT_TIMEOUT_SECONDS = 120
_TIMEOUT_MIN = 10
_TIMEOUT_MAX = 600


def _validate_glob_patterns(
    value: Any, field_name: str
) -> Tuple[Optional[List[str]], Optional[Dict[str, Any]]]:
    """Validates and compiles `value` before it reaches the graph pipeline.

    A bare string (e.g. `"*.py"` instead of `["*.py"]`) would otherwise
    silently iterate per CHARACTER, producing nonsensical single-character
    glob patterns. A syntactically invalid glob would otherwise fail later
    during candidate collection, where an invalid exclude can fail open.

    Returns `(patterns, None)` on success (`patterns` is `[]` for `None`),
    or `(None, error_dict)` on a type or pattern violation.
    """
    from code_indexer.services.path_pattern_matcher import (
        InvalidPatternError,
        PathPatternMatcher,
    )

    if value is None:
        return [], None
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        return None, {
            "error": f"{field_name}_invalid",
            "message": f"{field_name} must be a list of strings",
        }
    try:
        PathPatternMatcher().compile_patterns(value)
    except InvalidPatternError as exc:
        return None, {
            "error": f"{field_name}_invalid",
            "message": str(exc),
        }
    return value, None


def _parse_timeout_seconds(timeout_raw: Any) -> Tuple[int, Optional[Dict[str, Any]]]:
    """Validates `timeout_raw` is a finite number (rejects bool, NaN,
    +/-inf, and non-numeric types) before ever calling `int(...)` on it --
    `int(float("nan"))`/`int(float("inf"))` raise `ValueError`/
    `OverflowError`, which must never surface as an unstructured failure.
    Clamps to `[_TIMEOUT_MIN, _TIMEOUT_MAX]` on success.
    """
    if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, (int, float)):
        return 0, {
            "error": "timeout_seconds_invalid",
            "message": f"timeout_seconds must be a number, got {timeout_raw!r}",
        }
    if isinstance(timeout_raw, float) and not math.isfinite(timeout_raw):
        return 0, {
            "error": "timeout_seconds_invalid",
            "message": f"timeout_seconds must be finite, got {timeout_raw!r}",
        }
    return max(_TIMEOUT_MIN, min(_TIMEOUT_MAX, int(timeout_raw))), None


def _parse_refine_flag(refine_raw: Any) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """Bug #1909: validates the new opt-in `refine` request parameter.

    Must be a real boolean (never a truthy string/int -- `bool` is itself
    a subtype of `int` in Python, so `isinstance(refine_raw, bool)` must be
    checked BEFORE any numeric check ever could accept it). Defaults to
    `False` when omitted -- `--refine` is opt-in, never the default,
    mirroring `_parse_timeout_seconds`'s fail-fast validation convention.
    """
    if not isinstance(refine_raw, bool):
        return False, {
            "error": "refine_invalid",
            "message": f"refine must be a boolean, got {refine_raw!r}",
        }
    return refine_raw, None


def _parse_analyze_graph_request(
    params: Any,
) -> Tuple[
    Union[str, List[str]],
    str,
    List[str],
    List[str],
    int,
    bool,
    Optional[Dict[str, Any]],
]:
    """Parses and validates `params` for `handle_analyze_graph` -- factored
    out to keep that handler itself short. Returns either the 5 real
    parsed values with a `None` error, or empty/zero placeholders with a
    populated error dict (which the caller must check FIRST).

    Issue #1902: `repository_alias` now accepts a bare string, a native
    list of strings, OR a JSON-encoded string array -- matching
    `xray_search`'s documented contract EXACTLY, via the SAME
    `_parse_and_collapse_repo_alias` seam `handlers/xray.py`'s
    `handle_xray_search`/`handle_xray_explore` already use (no fourth copy
    of this parsing). A single-element list collapses to a plain string
    (mirrors xray.py's v10.4.5 Defect 5 ergonomic normalization), so
    callers of a single repo see the unchanged single-repo response shape
    regardless of which form they used.

    Issue #1902 P9 review, P3: an empty string ANYWHERE inside the list
    (`[""]`, or `["real-repo", ""]`) is rejected with the SAME
    `repository_alias_required` error a bare `""` already gets, rather
    than silently entering the multi-repo path where it would otherwise
    surface much later as a per-alias `repository_not_found` -- the same
    user mistake must not produce two different error shapes depending on
    whether it was wrapped in a list.
    """
    if not isinstance(params, dict):
        return (
            "",
            "",
            [],
            [],
            0,
            False,
            {
                "error": "invalid_params",
                "message": "params must be an object",
            },
        )

    repo_alias_raw = params.get("repository_alias", "")
    evaluator_code = params.get("evaluator_code", "")
    pattern_name = params.get("pattern_name")

    repo_alias = _parse_and_collapse_repo_alias(repo_alias_raw)

    repo_alias_type_valid = isinstance(repo_alias, str) or (
        isinstance(repo_alias, list)
        and all(isinstance(item, str) for item in repo_alias)
    )
    if (
        not isinstance(evaluator_code, str)
        or not repo_alias_type_valid
        or (pattern_name is not None and not isinstance(pattern_name, str))
    ):
        return (
            "",
            "",
            [],
            [],
            0,
            False,
            {
                "error": "invalid_params",
                "message": (
                    "repository_alias must be a string, a list of strings, or a "
                    "JSON-encoded list of strings, and evaluator_code must be a "
                    "string"
                ),
            },
        )
    if not evaluator_code and not pattern_name:
        return (
            "",
            "",
            [],
            [],
            0,
            False,
            {
                "error": "evaluator_code_required",
                "message": "Either evaluator_code or pattern_name must be provided",
            },
        )
    repo_alias_empty = repo_alias == "" or (
        isinstance(repo_alias, list)
        and (len(repo_alias) == 0 or any(item == "" for item in repo_alias))
    )
    if repo_alias_empty:
        return (
            "",
            "",
            [],
            [],
            0,
            False,
            {
                "error": "repository_alias_required",
                "message": "repository_alias must be a non-empty string, or a "
                "non-empty list/JSON array of non-empty strings",
            },
        )

    include_patterns, err = _validate_glob_patterns(
        params.get("include_patterns"), "include_patterns"
    )
    if err is not None:
        return "", "", [], [], 0, False, err
    exclude_patterns, err = _validate_glob_patterns(
        params.get("exclude_patterns"), "exclude_patterns"
    )
    if err is not None:
        return "", "", [], [], 0, False, err

    timeout_seconds, err = _parse_timeout_seconds(
        params.get("timeout_seconds", _DEFAULT_TIMEOUT_SECONDS)
    )
    if err is not None:
        return "", "", [], [], 0, False, err

    refine, err = _parse_refine_flag(params.get("refine", False))
    if err is not None:
        return "", "", [], [], 0, False, err

    assert include_patterns is not None  # guaranteed by _validate_glob_patterns
    assert exclude_patterns is not None
    return (
        repo_alias,
        evaluator_code,
        include_patterns,
        exclude_patterns,
        timeout_seconds,
        refine,
        None,
    )
