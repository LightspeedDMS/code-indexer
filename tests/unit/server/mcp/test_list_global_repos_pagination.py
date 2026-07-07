"""C12: list_global_repos must page its response (was unbounded ~533K tokens)."""

from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import UserRole
from code_indexer.server.mcp.handlers import repos as R


def _admin():
    u = MagicMock()
    u.role = UserRole.ADMIN  # bypass access filtering -> isolate pagination
    u.username = "admin"
    return u


def _fake_repos(n):
    return [{"repo_name": f"r{i}", "user_alias": f"r{i}-global"} for i in range(n)]


def _call(args, repos):
    with (
        patch.object(R, "_list_global_repos", return_value=repos),
        patch.object(R, "_mcp_response", side_effect=lambda x: x),
    ):
        return R.handle_list_global_repos(args, _admin())


def test_default_limit_caps_and_reports_pagination():
    out = _call({}, _fake_repos(120))
    assert out["success"] is True
    assert out["total"] == 120
    assert out["limit"] == 50 and out["offset"] == 0
    assert len(out["repos"]) == 50 and out["returned"] == 50
    assert out["has_more"] is True


def test_offset_pages_through():
    out = _call({"limit": 40, "offset": 100}, _fake_repos(120))
    assert out["offset"] == 100 and out["limit"] == 40
    assert out["returned"] == 20  # only 20 left past offset 100
    assert out["has_more"] is False


def test_limit_clamped_to_max():
    out = _call({"limit": 99999}, _fake_repos(500))
    assert out["limit"] == 200 and out["returned"] == 200


def test_non_positive_limit_falls_back_to_default():
    assert _call({"limit": 0}, _fake_repos(60))["limit"] == 50
    assert _call({"limit": -5}, _fake_repos(60))["limit"] == 50
    assert _call({"limit": "junk"}, _fake_repos(60))["limit"] == 50


def test_name_filter_narrows_before_paging():
    repos = _fake_repos(30) + [
        {"repo_name": "auth-service", "user_alias": "auth-global"}
    ]
    out = _call({"name_filter": "AUTH"}, repos)
    assert out["total"] == 1
    assert out["repos"][0]["repo_name"] == "auth-service"


def test_has_more_false_when_page_covers_all():
    out = _call({"limit": 200}, _fake_repos(10))
    assert out["total"] == 10 and out["returned"] == 10 and out["has_more"] is False
