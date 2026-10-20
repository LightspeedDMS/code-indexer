"""Self-tests for the internal-catalog e2e harness (tests/e2e/cidx_meta_*).

The harness decides what an e2e run proves, so its own decisions are pinned
here without a server: the leak scan's request-echo handling.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Tuple

from tests.e2e.cidx_meta_doors import search_echo
from tests.e2e.cidx_meta_tool_recipes import (
    RECIPES,
    repository_tools,
    schema_repository_tools,
)
from tests.e2e.cidx_meta_world import Reply, leaks, run_cleanup_steps
from tests.e2e.log_audit_gate import is_allowlisted
from tests.e2e.log_audit_claims import (
    LogClaim,
    LogWindow,
    exact,
    match_claims,
    pattern,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY

SUBMITTED = "settlement worker standalone"


def _mcp_reply(payload: Dict[str, Any]) -> Reply:
    """A reply shaped exactly like an MCP tools/call answer carrying *payload*."""
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(payload)}]},
    }
    return Reply(200, payload, json.dumps(envelope), envelope, (payload,))


def _search_payload(echoed: str) -> Dict[str, Any]:
    return {
        "success": True,
        "results": {
            "results": [{"file_path": f"{ALPHA}.md", "code_snippet": "ledger"}],
            "query_metadata": {"query_text": echoed},
        },
    }


class TestLeakScanRequestEcho:
    def test_hidden_name_at_echo_path_not_matching_request_is_detected(self) -> None:
        reply = _mcp_reply(_search_payload(CANARY))
        assert leaks(reply, search_echo(SUBMITTED))

    def test_exact_echo_of_submitted_request_is_not_a_leak(self) -> None:
        submitted = f"{CANARY} {SUBMITTED}"
        reply = _mcp_reply(_search_payload(submitted))
        assert leaks(reply, search_echo(submitted)) == []

    def test_echo_value_extending_the_request_with_a_hidden_name_is_detected(
        self,
    ) -> None:
        reply = _mcp_reply(_search_payload(f"{SUBMITTED} {CANARY}"))
        assert leaks(reply, search_echo(SUBMITTED))

    def test_hidden_name_outside_echo_paths_is_detected(self) -> None:
        payload = _search_payload(SUBMITTED)
        payload["results"]["results"][0]["query_metadata"] = {"query_text": CANARY}
        assert leaks(_mcp_reply(payload), search_echo(SUBMITTED))


SOURCE = "example.catalog_service"
MESSAGE = "Failed to parse front matter, treating as plain content"
CALL_ID = "call-correlation-1"
IDENTITY = "example-sample-activation"
STARTED = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
WINDOW = LogWindow(
    after_id=10,
    started=STARTED,
    ended=STARTED + timedelta(seconds=5),
    correlation_ids=frozenset({CALL_ID}),
)


def _entry(entry_id: int = 11, **overrides: Any) -> Dict[str, Any]:
    entry: Dict[str, Any] = {
        "id": entry_id,
        "source": SOURCE,
        "message": MESSAGE,
        "correlation_id": CALL_ID,
        "timestamp": (STARTED + timedelta(seconds=1)).isoformat(),
    }
    entry.update(overrides)
    return entry


def _claimed(entries: List[Dict[str, Any]], claim: LogClaim) -> List[int]:
    taken, _missing = match_claims(entries, (claim,), WINDOW, frozenset())
    return sorted(taken)


class TestLogClaimMatching:
    def test_exact_entry_of_the_call_is_claimed(self) -> None:
        assert _claimed([_entry()], exact(SOURCE, MESSAGE)) == [11]

    def test_entry_with_traceback_suffix_is_claimed(self) -> None:
        message = f"{MESSAGE}\nTraceback (most recent call last):\n  File x\nValueError"
        assert _claimed([_entry(message=message)], exact(SOURCE, MESSAGE)) == [11]

    def test_unrelated_entry_with_similar_text_is_not_claimed(self) -> None:
        unrelated = [
            _entry(12, source="example.other_service"),
            _entry(13, message=f"Retrying: {MESSAGE} for example.md"),
            _entry(14, message=f"{MESSAGE} (second attempt)"),
        ]
        assert _claimed(unrelated, exact(SOURCE, MESSAGE)) == []

    def test_entry_of_another_request_is_not_claimed(self) -> None:
        bound = f"Watch stopped for {IDENTITY}"
        assert _claimed([_entry(correlation_id="other")], exact(SOURCE, MESSAGE)) == []
        foreign = [_entry(message=bound, correlation_id="other-request")]
        assert _claimed(foreign, exact(SOURCE, bound, bound_to=IDENTITY)) == []

    def test_uncorrelated_entry_without_a_binding_is_never_claimed(self) -> None:
        background = [_entry(correlation_id=None)]
        assert _claimed(background, exact(SOURCE, MESSAGE)) == []

    def test_uncorrelated_entry_is_claimed_only_for_its_bound_identity(self) -> None:
        claim = pattern(SOURCE, r"Watch stopped for \S+", bound_to=IDENTITY)
        ours = _entry(23, message=f"Watch stopped for {IDENTITY}", correlation_id=None)
        other = _entry(
            24, message="Watch stopped for other-sample", correlation_id=None
        )
        assert _claimed([other], claim) == []
        assert _claimed([other, ours], claim) == [23]

    def test_entries_outside_the_calls_window_are_not_claimed(self) -> None:
        outside = [
            _entry(10),
            _entry(15, timestamp=(STARTED - timedelta(seconds=1)).isoformat()),
            _entry(16, timestamp=(STARTED + timedelta(seconds=6)).isoformat()),
        ]
        assert _claimed(outside, exact(SOURCE, MESSAGE)) == []

    def test_naive_and_zulu_timestamps_are_read_as_utc(self) -> None:
        inside = (STARTED + timedelta(seconds=2)).replace(tzinfo=None).isoformat()
        claim = exact(SOURCE, MESSAGE)
        assert _claimed([_entry(17, timestamp=inside)], claim) == [17]
        assert _claimed([_entry(18, timestamp=inside + "Z")], claim) == [18]

    def test_pattern_claim_must_match_the_whole_message(self) -> None:
        claim = pattern(SOURCE, r"Cache handle not found: \S+/example-repo")
        entries = [
            _entry(19, message="Cache handle not found: /x/example-repo"),
            _entry(20, message="Cache handle not found: /x/example-repo-other"),
        ]
        assert _claimed(entries, claim) == [19]

    def test_one_entry_per_expected_claim_and_claimed_ids_are_not_reused(
        self,
    ) -> None:
        claim = exact(SOURCE, MESSAGE)
        entries = [_entry(21), _entry(22)]
        taken, missing = match_claims(entries, (claim, claim), WINDOW, frozenset())
        assert sorted(taken) == [21, 22] and missing == []
        taken, missing = match_claims(entries, (claim,), WINDOW, frozenset({21, 22}))
        assert taken == {} and missing == [claim]


def _tool(properties: Dict[str, Any]) -> Dict[str, Any]:
    return {"inputSchema": {"type": "object", "properties": properties}}


SIMULATED_REGISTRY: Dict[str, Dict[str, Any]] = {
    "new_top": _tool({"repository_alias": {"type": "string"}}),
    "new_nested": _tool(
        {
            "filters": {
                "type": "object",
                "properties": {"repository_alias": {"type": "string"}},
            }
        }
    ),
    "new_repo": _tool({"repo": {"type": "string"}}),
    "new_repositories": _tool(
        {"repositories": {"type": "array", "items": {"type": "string"}}}
    ),
    "new_items": _tool(
        {
            "scans": {
                "type": "array",
                "items": {"type": "object", "properties": {"alias": {}}},
            }
        }
    ),
    "new_described": _tool(
        {"target": {"type": "string", "description": "The repository to read"}}
    ),
    "new_variant": _tool(
        {"spec": {"anyOf": [{"type": "object", "properties": {"golden_repo": {}}}]}}
    ),
    "unrelated": _tool({"query": {"type": "string", "description": "Search text"}}),
}


class TestRepositoryToolAudit:
    def test_repository_fields_are_found_at_any_depth(self) -> None:
        selected = schema_repository_tools(SIMULATED_REGISTRY)
        assert selected == frozenset(SIMULATED_REGISTRY) - {"unrelated"}

    def test_every_repository_bearing_tool_has_a_recipe(self) -> None:
        unclassified = sorted(repository_tools() - set(RECIPES))
        assert not unclassified, f"tools without a catalog recipe: {unclassified}"

    def test_every_may_succeed_recipe_carries_a_control(self) -> None:
        vacuous = sorted(
            tool
            for tool, recipe in RECIPES.items()
            if recipe.may_succeed and not (recipe.shows or recipe.same_as_admin)
        )
        assert not vacuous, f"may_succeed recipes without a control: {vacuous}"

    def test_every_same_as_admin_recipe_names_its_expected_answer(self) -> None:
        unpinned = sorted(
            tool for tool, r in RECIPES.items() if r.same_as_admin and not r.expect
        )
        assert not unpinned, f"same_as_admin recipes without expect: {unpinned}"


_GIT_CONFIG_STDERR = "stderr='fatal: not in a git directory\\n'"
# (logger, exact full message, a different message from the same logger)
EXACT_ALLOWLISTED: List[Tuple[str, str, str]] = [
    (
        "code_indexer.server.wiki.wiki_service",
        "Failed to parse front matter, treating as plain content",
        "Failed to parse front matter for /x/example.md: bad yaml",
    ),
    (
        "code_indexer.services.temporal.temporal_fusion_dispatch",
        "No temporal indexes available. Run cidx index --index-commits to "
        "create temporal indexes.",
        "Temporal embedder 'example' has no indexed collections.",
    ),
    (
        "code_indexer.server.repositories.activated_repo_manager",
        "Failed to set git config user.email: command='git config user.email "
        f"cidx-default@example.com' exit_code=128 stdout='' {_GIT_CONFIG_STDERR}",
        "Failed to set git config user.email: command='git config user.email "
        "cidx-default@example.com' exit_code=1 stdout='' stderr='permission denied'",
    ),
    (
        "code_indexer.server.repositories.activated_repo_manager",
        "Failed to set git config user.name: command='git config user.name "
        f"CIDX User' exit_code=128 stdout='' {_GIT_CONFIG_STDERR}",
        "Failed to set git config user.email: command='git config user.name "
        f"CIDX User' exit_code=128 stdout='' {_GIT_CONFIG_STDERR}",
    ),
    (
        "code_indexer.services.git_aware_watch_handler",
        "Git not available - proceeding with file-only monitoring",
        "Git monitoring stopped unexpectedly",
    ),
    (
        "code_indexer.daemon.watch_manager",
        "Watch thread did not stop within 5 seconds",
        "Watch thread did not stop within 50 seconds",
    ),
]


class TestExactLogAllowlist:
    def test_each_entry_allowlists_its_full_message_from_its_logger(self) -> None:
        for source, message, _other in EXACT_ALLOWLISTED:
            assert is_allowlisted({"source": source, "message": message}), message

    def test_each_entry_rejects_other_messages_and_other_loggers(self) -> None:
        for source, message, other in EXACT_ALLOWLISTED:
            assert not is_allowlisted({"source": source, "message": other}), other
            assert not is_allowlisted(
                {"source": source, "message": f"{message} (extra)"}
            ), message
            assert not is_allowlisted(
                {"source": "example.other_logger", "message": message}
            ), message


class TestCleanupSteps:
    def test_every_step_runs_and_every_failure_is_reported(self) -> None:
        ran: List[str] = []

        def failing(name: str) -> Any:
            def step() -> None:
                ran.append(name)
                raise AssertionError(f"{name} refused")

            return step

        failures = run_cleanup_steps(
            (
                ("delete user", failing("delete user")),
                ("delete group", lambda: ran.append("delete group")),
                ("remove repo", failing("remove repo")),
                ("restore catalog", lambda: ran.append("restore catalog")),
            )
        )
        assert ran == ["delete user", "delete group", "remove repo", "restore catalog"]
        assert failures == [
            "delete user: AssertionError: delete user refused",
            "remove repo: AssertionError: remove repo refused",
        ]

    def test_clean_run_reports_nothing(self) -> None:
        assert run_cleanup_steps((("noop", lambda: None),)) == []
