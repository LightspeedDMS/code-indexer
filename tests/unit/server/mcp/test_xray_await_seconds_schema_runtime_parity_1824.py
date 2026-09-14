"""Regression tests for Bug #1824: xray_search/xray_explore await_seconds
schema-vs-runtime drift.

Bug #1824 found that the published MCP tool schema advertised
``await_seconds`` ``maximum: 120`` ("raised from 10.0 in v10.5.0") while the
server actually enforced a 45.0 ceiling ("lowered from 30 in v10.3.2") --
two different numbers, two contradictory version histories, and a client
that validates locally against the schema would send 60 and be refused at
call time.

These tests pin the published schema's ``maximum`` to the REAL enforced
constant (``_AWAIT_SECONDS_MAX`` in ``handlers/xray.py``) by reading BOTH
values from their real sources -- never hardcoding the number twice, or the
test would just re-encode the exact drift Bug #1824 reports. They also cover
the sibling drifts found while investigating #1824:

- ``xray_explore.md``'s summary sentence (the one mentioning "inline-wait",
  right after the "Returns `{job_id}`..." line) still claimed "up to 120
  seconds (v10.5.0)" even though the doc's own ``await_seconds`` schema
  field and parameter table a few lines away had already been corrected to
  45.0 -- self-contradicting within the same file.
- ``xray_search_batch.md``'s "Key Differences" comparison table still quoted
  xray_search's OLD ``[0, 120]`` range.
- The ``await_seconds_invalid`` runtime error message AND its neighboring
  source-code comment, in both ``handle_xray_search`` and
  ``handle_xray_explore``, still narrated the stale v10.3.2 (30->10)
  transition instead of the transition that actually produced the CURRENT
  45.0 ceiling (Bug #1070, 120.0->45.0) -- contradicting the correct,
  already-fixed top-of-file comment in the very same module.
"""

from __future__ import annotations

import io
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.xray import _AWAIT_SECONDS_MAX
from code_indexer.server.mcp.handlers.xray_batch import _AWAIT_MAX as _BATCH_AWAIT_MAX

_SERVER_ROOT = (
    Path(__file__).parent.parent.parent.parent.parent
    / "src"
    / "code_indexer"
    / "server"
)
TOOL_DOCS_SEARCH_DIR = _SERVER_ROOT / "mcp" / "tool_docs" / "search"
XRAY_HANDLER_PY = _SERVER_ROOT / "mcp" / "handlers" / "xray.py"

XRAY_EXPLORE_MD = TOOL_DOCS_SEARCH_DIR / "xray_explore.md"
XRAY_SEARCH_BATCH_MD = TOOL_DOCS_SEARCH_DIR / "xray_search_batch.md"


def _load_tool_registry() -> Dict[str, Any]:
    """Import TOOL_REGISTRY exactly as the real MCP front door builds it.

    TOOL_REGISTRY is assembled DYNAMICALLY from the on-disk tool_docs
    (ToolDocLoader) -- reading it here, rather than re-parsing the YAML
    frontmatter independently, means this test observes precisely what a
    real MCP client receives. The loader emits benign chatter on stderr
    during construction; suppress it without clobbering pytest's own
    capture stream (save/restore the ACTUAL previous stream, never a
    hardcoded sys.__stderr__).
    """
    previous_stderr = sys.stderr
    sys.stderr = io.StringIO()
    try:
        from code_indexer.server.mcp.tools import TOOL_REGISTRY
    finally:
        sys.stderr = previous_stderr
    return TOOL_REGISTRY


def _inline_result() -> Dict[str, Any]:
    """Minimal successful inline xray result (search and explore share shape)."""
    return {
        "matches": [{"file_path": "a.py", "line": 1, "snippet": "x"}],
        "evaluation_errors": [],
        "files_processed": 1,
        "files_total": 1,
        "elapsed_seconds": 0.01,
        "truncated": False,
        "cache_handle": None,
    }


# ---------------------------------------------------------------------------
# Per-tool fixture: bundles the tool-specific test helpers so the boundary
# and error-message tests below can be written ONCE and parametrized over
# both xray_search and xray_explore, instead of duplicated per tool.
# ---------------------------------------------------------------------------


@dataclass
class _ToolUnderTest:
    tool_name: str
    valid_params: Dict[str, Any]
    handler: Callable[..., Any]
    make_user: Callable[[UserRole], User]
    parse_response: Callable[[Dict[str, Any]], Dict[str, Any]]
    single_repo_env: Callable[..., Any]
    make_resolved_future: Callable[[Dict[str, Any]], Any]


def _search_tool() -> _ToolUnderTest:
    from .test_xray_search_handler import (
        VALID_PARAMS,
        _import_handler,
        _make_resolved_future,
        _make_user,
        _parse_response,
        _xray_single_repo_env,
    )

    return _ToolUnderTest(
        tool_name="xray_search",
        valid_params=VALID_PARAMS,
        handler=_import_handler(),
        make_user=_make_user,
        parse_response=_parse_response,
        single_repo_env=_xray_single_repo_env,
        make_resolved_future=_make_resolved_future,
    )


def _explore_tool() -> _ToolUnderTest:
    from .test_xray_explore_handler import (
        VALID_PARAMS,
        _import_handler,
        _make_resolved_future,
        _make_user,
        _parse_response,
        _xray_single_repo_env,
    )

    return _ToolUnderTest(
        tool_name="xray_explore",
        valid_params=VALID_PARAMS,
        handler=_import_handler(),
        make_user=_make_user,
        parse_response=_parse_response,
        single_repo_env=_xray_single_repo_env,
        make_resolved_future=_make_resolved_future,
    )


_TOOL_FACTORIES = [_search_tool, _explore_tool]
_TOOL_IDS = ["xray_search", "xray_explore"]


# ---------------------------------------------------------------------------
# Core regression: schema maximum pinned to the runtime constant
# ---------------------------------------------------------------------------


class TestAwaitSecondsSchemaMaximumPinnedToRuntimeCap:
    @pytest.mark.parametrize("tool_name", _TOOL_IDS)
    def test_schema_maximum_equals_runtime_cap(self, tool_name: str) -> None:
        registry = _load_tool_registry()
        schema_max = registry[tool_name]["inputSchema"]["properties"]["await_seconds"][
            "maximum"
        ]
        assert schema_max == _AWAIT_SECONDS_MAX, (
            f"{tool_name} schema advertises await_seconds maximum={schema_max!r} "
            f"but the server enforces _AWAIT_SECONDS_MAX={_AWAIT_SECONDS_MAX!r} "
            f"-- a client validating locally against the published schema "
            f"would send a value the server then refuses (Bug #1824)."
        )


# ---------------------------------------------------------------------------
# Boundary behavior, derived from the SCHEMA value (not a hardcoded number)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_factory", _TOOL_FACTORIES, ids=_TOOL_IDS)
async def test_accepts_at_schema_cap_rejects_one_above(
    tool_factory: Callable[[], _ToolUnderTest],
) -> None:
    """A call at exactly the schema-advertised cap succeeds (a genuine
    inline result, not merely "some other error"); one unit above it is
    rejected with await_seconds_invalid. The cap is read from the SCHEMA,
    not hardcoded, so this test tracks the real cap wherever it is set.
    """
    tool = tool_factory()
    registry = _load_tool_registry()
    schema_max = float(
        registry[tool.tool_name]["inputSchema"]["properties"]["await_seconds"][
            "maximum"
        ]
    )
    user = tool.make_user(UserRole.NORMAL_USER)
    resolved = tool.make_resolved_future(_inline_result())

    with tool.single_repo_env(resolved_future=resolved):
        at_cap = await tool.handler(
            {**tool.valid_params, "await_seconds": schema_max}, user
        )
    at_cap_data = tool.parse_response(at_cap)
    assert "error" not in at_cap_data, (
        f"{tool.tool_name} at await_seconds={schema_max} (the schema cap) "
        f"must succeed, got error response: {at_cap_data!r}"
    )
    assert "matches" in at_cap_data, (
        f"{tool.tool_name} at await_seconds={schema_max} must return the "
        f"real inline result (matches), got: {at_cap_data!r}"
    )

    above_cap = await tool.handler(
        {**tool.valid_params, "await_seconds": schema_max + 1.0}, user
    )
    above_cap_data = tool.parse_response(above_cap)
    assert above_cap_data.get("error") == "await_seconds_invalid", (
        f"{tool.tool_name} at await_seconds={schema_max + 1.0} (one above the "
        f"schema cap) must be rejected with await_seconds_invalid, got: "
        f"{above_cap_data!r}"
    )


# ---------------------------------------------------------------------------
# Sibling drift #1: xray_explore.md summary SENTENCE vs its own schema field
# ---------------------------------------------------------------------------


def test_xray_explore_md_prose_matches_real_handler_max() -> None:
    """xray_explore.md's summary sentence describing the inline-wait window
    must agree with the REAL enforced ceiling (_AWAIT_SECONDS_MAX), and with
    the doc's OWN await_seconds schema field/parameter table a few lines
    away -- both of which already state 45.0.

    Scoped to the ONE line in the doc that mentions "inline-wait" (the
    summary sentence right after "Returns `{job_id}`...") rather than the
    whole document, so a correct parameter-table entry elsewhere cannot
    mask a stale claim in the summary itself.
    """
    body = XRAY_EXPLORE_MD.read_text(encoding="utf-8")
    match = re.search(r"^.*inline-wait.*$", body, re.MULTILINE)
    assert match is not None, (
        "xray_explore.md must contain a summary sentence mentioning "
        "'inline-wait' that states the await_seconds ceiling"
    )
    summary_sentence = match.group(0)

    assert "up to 120" not in summary_sentence, (
        f"xray_explore.md summary sentence still claims the stale "
        f"pre-Bug-#1070 inline-wait ceiling (120s); real ceiling is "
        f"{_AWAIT_SECONDS_MAX}s. Sentence: {summary_sentence!r}"
    )
    real_ceiling_claim = f"up to {int(_AWAIT_SECONDS_MAX)} seconds"
    assert real_ceiling_claim in summary_sentence, (
        f"xray_explore.md summary sentence must state the REAL inline-wait "
        f"ceiling ({real_ceiling_claim!r} not found in {summary_sentence!r})"
    )


# ---------------------------------------------------------------------------
# Sibling drift #2: xray_search_batch.md comparison table
# ---------------------------------------------------------------------------


def test_xray_search_batch_md_comparison_table_matches_real_caps() -> None:
    """The 'Key Differences' table compares xray_search's await_seconds
    range against xray_search_batch's own range. Both numbers must reflect
    the REAL enforced constants (_AWAIT_SECONDS_MAX for xray_search,
    _AWAIT_MAX for xray_search_batch) -- not a stale copy of xray_search's
    pre-Bug-#1070 120s ceiling.
    """
    body = XRAY_SEARCH_BATCH_MD.read_text(encoding="utf-8")
    match = re.search(r"\|\s*await_seconds\s*\|([^|]+)\|([^|]+)\|", body)
    assert match is not None, (
        "xray_search_batch.md must contain an await_seconds row in the "
        "'Key Differences' comparison table"
    )
    xray_search_col, batch_col = match.group(1), match.group(2)

    expected_search_range = f"[0, {int(_AWAIT_SECONDS_MAX)}]"
    assert expected_search_range in xray_search_col, (
        f"xray_search_batch.md comparison table's xray_search column must "
        f"state the REAL xray_search cap {expected_search_range!r}, got "
        f"{xray_search_col!r}"
    )

    expected_batch_range = f"[0, {int(_BATCH_AWAIT_MAX)}]"
    assert expected_batch_range in batch_col, (
        f"xray_search_batch.md comparison table's batch column must state "
        f"the REAL batch cap {expected_batch_range!r}, got {batch_col!r}"
    )


# ---------------------------------------------------------------------------
# Sibling drift #3: runtime error message provenance consistency
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("tool_factory", _TOOL_FACTORIES, ids=_TOOL_IDS)
async def test_await_seconds_error_message_has_consistent_history(
    tool_factory: Callable[[], _ToolUnderTest],
) -> None:
    """The await_seconds_invalid message must narrate the transition that
    actually produced the CURRENT cap (Bug #1070, 120.0 -> 45.0), not the
    stale, superseded v10.3.2 (30 -> 10) transition -- which doesn't even
    match the current 45.0 value it is attached to.
    """
    tool = tool_factory()
    user = tool.make_user(UserRole.NORMAL_USER)
    result = await tool.handler(
        {**tool.valid_params, "await_seconds": _AWAIT_SECONDS_MAX + 1.0}, user
    )
    data = tool.parse_response(result)
    assert data.get("error") == "await_seconds_invalid"
    message = data.get("message", "")
    assert "v10.3.2" not in message and "lowered from 30" not in message, (
        f"{tool.tool_name} await_seconds_invalid message still narrates the "
        f"stale v10.3.2 (30->10) transition instead of the one that produced "
        f"the current cap: {message!r}"
    )
    assert "1070" in message, (
        f"{tool.tool_name} await_seconds_invalid message must attribute the "
        f"current cap to Bug #1070: {message!r}"
    )


# ---------------------------------------------------------------------------
# Sibling drift #3b: source-code comment provenance consistency
# ---------------------------------------------------------------------------


def test_xray_handler_source_comments_have_consistent_await_seconds_history() -> None:
    """The inline comments directly above the await_seconds validation in
    BOTH handle_xray_search and handle_xray_explore must agree with the
    top-of-file provenance comment (Bug #1070, 120.0 -> 45.0), not the
    stale, superseded v10.3.2 (30 -> 10) note left over from before the
    cap was raised to 120 and then lowered again by Bug #1070.
    """
    source = XRAY_HANDLER_PY.read_text(encoding="utf-8")
    assert "to 10 in v10.3.2" not in source, (
        "handlers/xray.py still carries the stale 'Cap lowered from 30 to "
        "10 in v10.3.2' comment next to the await_seconds validation -- "
        "that transition predates, and does not match, the CURRENT 45.0 "
        "cap (Bug #1070, 120.0 -> 45.0)."
    )
    bug_1070_mentions = source.count("Bug #1070")
    assert bug_1070_mentions >= 3, (
        f"expected the top-of-file provenance comment PLUS a consistent "
        f"'Bug #1070' reference near both the xray_search and xray_explore "
        f"await_seconds validation blocks (>= 3 total mentions), found "
        f"{bug_1070_mentions}"
    )
