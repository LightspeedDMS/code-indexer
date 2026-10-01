"""RED tests for Story #1593's request-scoped tool-access decision helper."""

from dataclasses import dataclass
from concurrent.futures import ThreadPoolExecutor


@dataclass
class FakeUser:
    username: str


@dataclass
class FakeGroup:
    id: int


class FakeGroupManager:
    def __init__(self, ready: bool, tools_by_group: dict[int, list[str]]):
        self.ready = ready
        self.tools_by_group = tools_by_group
        self.reads = 0

    def is_tool_access_enforcement_ready(self) -> bool:
        return self.ready

    def get_user_group(self, username: str):
        return {
            "allowed": FakeGroup(7),
            "other": FakeGroup(8),
        }.get(username)

    def get_group_tools(self, group_id: int) -> list[str]:
        self.reads += 1
        return self.tools_by_group.get(group_id, [])


def test_exception_set_is_exactly_authenticate() -> None:
    from code_indexer.server.mcp.tool_access import _ALWAYS_AVAILABLE_TOOLS

    assert _ALWAYS_AVAILABLE_TOOLS == frozenset({"authenticate"})


def test_ready_group_without_tool_is_denied() -> None:
    from code_indexer.server.mcp.tool_access import ToolAccessMemo

    manager = FakeGroupManager(True, {7: []})
    memo = ToolAccessMemo(manager)

    assert memo.is_allowed("git_push", FakeUser("allowed")) is False


def test_groupless_user_is_denied_but_authenticate_is_allowed() -> None:
    from code_indexer.server.mcp.tool_access import ToolAccessMemo

    manager = FakeGroupManager(True, {})
    memo = ToolAccessMemo(manager)

    assert memo.is_allowed("git_push", FakeUser("orphan")) is False
    assert memo.is_allowed("authenticate", FakeUser("orphan")) is True


def test_not_ready_returns_none_for_legacy_role_fallback() -> None:
    from code_indexer.server.mcp.tool_access import ToolAccessMemo

    manager = FakeGroupManager(False, {})
    memo = ToolAccessMemo(manager)

    assert memo.is_allowed("git_push", FakeUser("orphan")) is None


def test_request_memo_reads_group_tools_once() -> None:
    from code_indexer.server.mcp.tool_access import ToolAccessMemo

    manager = FakeGroupManager(True, {7: ["git_push"]})
    memo = ToolAccessMemo(manager)
    user = FakeUser("allowed")

    assert memo.is_allowed("git_push", user) is True
    assert memo.is_allowed("other_tool", user) is False
    assert manager.reads == 1


def test_effective_user_uses_impersonated_principal() -> None:
    from code_indexer.server.mcp.tool_access import resolve_effective_user

    authenticated = FakeUser("admin")
    impersonated = FakeUser("allowed")

    class Session:
        is_impersonating = True
        effective_user = impersonated

    assert resolve_effective_user(authenticated, Session()) is impersonated
    assert resolve_effective_user(authenticated, None) is authenticated


def test_revocation_is_visible_to_the_next_request() -> None:
    from code_indexer.server.mcp.tool_access import ToolAccessMemo

    manager = FakeGroupManager(True, {7: ["git_push"]})
    assert ToolAccessMemo(manager).is_allowed("git_push", FakeUser("allowed")) is True

    manager.tools_by_group[7] = []
    next_request = ToolAccessMemo(manager)
    assert next_request.is_allowed("git_push", FakeUser("allowed")) is False


def test_executor_dispatched_requests_cannot_share_memos() -> None:
    from code_indexer.server.mcp.tool_access import ToolAccessMemo

    manager = FakeGroupManager(True, {7: ["git_push"], 8: []})

    def decide(username: str) -> bool:
        memo = ToolAccessMemo(manager)
        return bool(memo.is_allowed("git_push", FakeUser(username)))

    with ThreadPoolExecutor(max_workers=2) as executor:
        allowed, denied = executor.map(decide, ["allowed", "other"])

    assert allowed is True
    assert denied is False
