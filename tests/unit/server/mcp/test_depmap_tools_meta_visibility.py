"""The six depmap tools answer from the caller's view of the dependency map.

Real fixture directory, real group store, real registry, real parsers and
handlers. A restricted caller (granted ``alpha`` only) never sees the
canary or ``gamma``; edges, counts, consumers and memberships are computed
from rows whose repositories they may see; a hidden domain answers exactly
like a missing one; anomalies are never returned to them. An admin's
answers are the recorded answers of the unchanged code.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Iterator

import pytest

from code_indexer.server.mcp.protocol import handle_tools_call
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    AUDIT_DOMAIN,
    CANARY,
    RESTRICTED_USER,
    CidxMetaFixture,
    add_depmap_extension,
    build_cidx_meta_fixture,
)
from tests.unit.server.mcp.conftest import extract_mcp_data
from tests.unit.server.mcp.depmap_visibility_support import (
    SIX_CALLS,
    assert_hidden_absent,
    build_depmap_server,
    call_tool,
    make_user,
    normalize,
)

GOLDEN = Path(__file__).with_name("depmap_admin_golden_meta_visibility.json")
RESTRICTED_EMAIL = "restricted@example.com"


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path_factory.mktemp("depmap-vis"))
    add_depmap_extension(fixture)
    return fixture


@pytest.fixture
def server(fx: CidxMetaFixture) -> Iterator[SimpleNamespace]:
    with pytest.MonkeyPatch.context() as monkeypatch:
        yield build_depmap_server(monkeypatch, fx)


def _restricted(key: str) -> Dict[str, Any]:
    tool, args = SIX_CALLS[key]
    return call_tool(tool, args, RESTRICTED_USER)


def test_admin_answers_equal_the_recorded_answers(
    server: SimpleNamespace, fx: CidxMetaFixture
) -> None:
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    for key, (tool, args) in SIX_CALLS.items():
        assert normalize(call_tool(tool, args, ADMIN_USER), fx.root) == golden[key], key


def test_graph_counts_only_rows_with_visible_repositories(
    server: SimpleNamespace,
) -> None:
    data = _restricted("graph")
    assert_hidden_absent(data)
    assert data["success"] is True
    # payments -> ledger has one visible and one hidden row (admin: 2);
    # audit -> payments exists only through the hidden repository.
    assert data["edges"] == [
        {
            "source_domain": "payments",
            "target_domain": "ledger",
            "dependency_count": 1,
            "types": ["Code-level"],
        }
    ]


def test_hub_domains_rank_only_the_projected_graph(server: SimpleNamespace) -> None:
    data = _restricted("hubs")
    assert_hidden_absent(data)
    assert data["hubs"] == [
        {"domain": "payments", "in_degree": 0, "out_degree": 1, "total": 1},
        {"domain": "ledger", "in_degree": 1, "out_degree": 0, "total": 1},
    ]


def test_stale_domains_list_only_visible_domains(server: SimpleNamespace) -> None:
    data = _restricted("stale")
    assert_hidden_absent(data)
    names = [d["domain_name"] for d in data["stale_domains"]]
    assert sorted(names) == sorted(["payments", "ledger", AUDIT_DOMAIN])
    payments = next(d for d in data["stale_domains"] if d["domain_name"] == "payments")
    assert payments["last_analyzed"] == "2026-01-01T00:00:00+00:00"


@pytest.mark.parametrize("hidden", [CANARY, "inventory", "legacy"])
def test_hidden_domain_summary_matches_missing_domain(
    server: SimpleNamespace, hidden: str
) -> None:
    missing = call_tool(
        "depmap_get_domain_summary", {"domain_name": "no-such-domain"}, RESTRICTED_USER
    )
    answer = call_tool(
        "depmap_get_domain_summary", {"domain_name": hidden}, RESTRICTED_USER
    )
    assert json.dumps(answer, sort_keys=True) == json.dumps(missing, sort_keys=True)
    assert answer["resolution"] == "domain_not_indexed"


def test_visible_domain_summary_lists_only_visible_participants(
    server: SimpleNamespace,
) -> None:
    payments = _restricted("summary_payments")
    assert_hidden_absent(payments)
    summary = payments["summary"]
    assert summary["participating_repos"] == [{"repo": ALPHA, "role": "Payment API"}]
    assert summary["cross_domain_connections"] == [
        {"target_domain": "ledger", "dependency_count": 1}
    ]
    audit = _restricted("summary_audit")
    assert_hidden_absent(audit)
    assert audit["summary"]["participating_repos"] == [
        {"repo": ALPHA, "role": "Audit reader"}
    ]
    assert audit["summary"]["cross_domain_connections"] == []


def test_consumers_whose_repository_is_hidden_are_removed(
    server: SimpleNamespace,
) -> None:
    data = _restricted("consumers_alpha")
    assert_hidden_absent(data)
    assert [(c["repo"], c["domain"]) for c in data["consumers"]] == [
        (ALPHA, AUDIT_DOMAIN)
    ]


def test_repo_domains_list_no_hidden_domain(server: SimpleNamespace) -> None:
    data = _restricted("repo_domains_alpha")
    assert_hidden_absent(data)
    domains = [d["domain"] for d in data["domains"]]
    assert "payments" in domains and AUDIT_DOMAIN in domains
    assert CANARY not in domains


@pytest.mark.parametrize("key", sorted(SIX_CALLS))
def test_restricted_answers_carry_no_anomalies_and_no_server_path(
    server: SimpleNamespace, fx: CidxMetaFixture, key: str
) -> None:
    data = _restricted(key)
    text = assert_hidden_absent(data)
    for channel in ("anomalies", "parser_anomalies", "data_anomalies"):
        assert data.get(channel, []) == [], (channel, data)
    assert str(fx.root) not in text
    assert str(fx.root.resolve()) not in text


def _dispatch(
    server: SimpleNamespace, tool: str, arguments: Dict[str, Any], username: str
) -> Dict[str, Any]:
    server.user_manager = SimpleNamespace(
        get_user_by_email=lambda email: (
            make_user(RESTRICTED_USER) if email == RESTRICTED_EMAIL else None
        )
    )
    result = asyncio.run(
        handle_tools_call({"name": tool, "arguments": arguments}, make_user(username))
    )
    data: Dict[str, Any] = extract_mcp_data(result)
    return data


@pytest.mark.parametrize("tool", ["depmap_get_repo_domains", "depmap_find_consumers"])
def test_dispatcher_still_refuses_a_hidden_repo_name(
    server: SimpleNamespace, tool: str
) -> None:
    with pytest.raises(ValueError):
        _dispatch(server, tool, {"repo_name": CANARY}, RESTRICTED_USER)


def test_admin_acting_for_restricted_user_gets_restricted_graph(
    server: SimpleNamespace,
) -> None:
    acting = _dispatch(
        server,
        "depmap_get_cross_domain_graph",
        {"acting_users": [RESTRICTED_EMAIL]},
        ADMIN_USER,
    )
    assert acting == _restricted("graph")
    summary = _dispatch(
        server,
        "depmap_get_domain_summary",
        {"domain_name": CANARY, "acting_users": [RESTRICTED_EMAIL]},
        ADMIN_USER,
    )
    assert summary["resolution"] == "domain_not_indexed"
