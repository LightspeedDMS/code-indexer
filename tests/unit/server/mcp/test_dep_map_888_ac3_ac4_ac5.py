"""
Story #888 — AC3, AC4, AC5: Canonical field names + input unchanged + dual-write aliases.

AC3: canonical response field names used uniformly:
     - `domain` for single-domain contexts (find_consumers consumer entries)
     - `repo` for all repo references (participating_repos, consumer entries, domain membership)
     - `source_domain`/`target_domain` for graph edge endpoints
AC4: input parameter names (repo_name, domain_name) are unchanged — only response fields change.
AC5: handler-layer dual-write helper writes both canonical AND deprecated-alias keys:
     - domain_name alongside domain (in get_repo_domains domain entries)
     - consuming_repo alongside repo (in find_consumers consumer entries)
     Centralized in _depmap_aliases.py — not duplicated across handlers.
"""

import json
from pathlib import Path
from typing import Any, Dict, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.auth.user_manager import User, UserRole


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_user() -> User:
    user = MagicMock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    return user


def _make_app_state(read_path: Path) -> MagicMock:
    state = MagicMock()
    state.dependency_map_service.cidx_meta_read_path = read_path
    return state


def _parse_response(result: Any) -> Dict[str, Any]:
    # cast needed: json.loads() returns Any; MCP handlers always return dict envelope
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


def _call_find_consumers(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import depmap_find_consumers_handler

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_find_consumers_handler(params, _make_user())


def _call_get_repo_domains(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import depmap_get_repo_domains_handler

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_repo_domains_handler(params, _make_user())


def _call_get_domain_summary(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_domain_summary_handler,
    )

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_domain_summary_handler(params, _make_user())


def _call_get_cross_domain_graph(params: dict, app_state: MagicMock) -> Any:
    from code_indexer.server.mcp.handlers.depmap import (
        depmap_get_cross_domain_graph_handler,
    )

    with patch(
        "code_indexer.server.mcp.handlers.depmap._utils.app_module.app.state",
        app_state,
    ):
        return depmap_get_cross_domain_graph_handler(params, _make_user())


def _make_dep_map_with_consumer(tmp_path: Path, repo_name: str) -> Path:
    """Create a dep-map with one domain where repo_name has an incoming consumer.

    Returns tmp_path (the root, not the dep-map dir).
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    consumer_repo = "consumer-repo"
    domain = "alpha-domain"
    (dep_map_dir / "_domains.json").write_text(
        f'[{{"name":"{domain}","description":"d",'
        f'"participating_repos":["{repo_name}","{consumer_repo}"]}}]',
        encoding="utf-8",
    )
    content = (
        f"---\nname: {domain}\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        f"| {consumer_repo} | {repo_name} | {domain} | Code-level | why | ev |\n"
    )
    (dep_map_dir / f"{domain}.md").write_text(content, encoding="utf-8")
    return tmp_path


def _make_dep_map_with_repo_in_domain(
    tmp_path: Path, repo_name: str, domain_name: str
) -> Path:
    """Create a dep-map with repo_name listed in domain_name's participating_repos.

    Returns tmp_path.
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text(
        f'[{{"name":"{domain_name}","description":"d",'
        f'"participating_repos":["{repo_name}"]}}]',
        encoding="utf-8",
    )
    content = (
        f"---\nname: {domain_name}\n---\n"
        f"## Repository Roles\n\n| Repository | Language | Role |\n|---|---|---|\n"
        f"| {repo_name} | Python | Core service |\n\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
    )
    (dep_map_dir / f"{domain_name}.md").write_text(content, encoding="utf-8")
    return tmp_path


def _make_two_domain_graph(tmp_path: Path) -> Path:
    """Create a minimal two-domain graph fixture (src-dom→tgt-dom).

    Returns tmp_path.
    """
    dep_map_dir = tmp_path / "dependency-map"
    dep_map_dir.mkdir()
    (dep_map_dir / "_domains.json").write_text(
        '[{"name":"src-dom","description":"d","participating_repos":[]},'
        '{"name":"tgt-dom","description":"d","participating_repos":[]}]',
        encoding="utf-8",
    )
    (dep_map_dir / "src-dom.md").write_text(
        "---\nname: src-dom\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| repo-s | repo-t | tgt-dom | Code-level | why | ev |\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n",
        encoding="utf-8",
    )
    (dep_map_dir / "tgt-dom.md").write_text(
        "---\nname: tgt-dom\n---\n"
        "## Cross-Domain Connections\n\n"
        "### Outgoing Dependencies\n\n"
        "| This Repo | Depends On | Target Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n\n"
        "### Incoming Dependencies\n\n"
        "| External Repo | Depends On | Source Domain | Type | Why | Evidence |\n"
        "|---|---|---|---|---|---|\n"
        "| repo-s | repo-t | src-dom | Code-level | why | ev |\n",
        encoding="utf-8",
    )
    return tmp_path


# ---------------------------------------------------------------------------
# AC3: canonical response field names
# ---------------------------------------------------------------------------


class TestAC3CanonicalFieldNames:
    """AC3: canonical field names used uniformly in all depmap_* responses."""

    def test_find_consumers_uses_domain_not_domain_name_in_consumer_entry(
        self, tmp_path: Path
    ) -> None:
        """find_consumers consumer entries use 'domain' key (not 'domain_name')."""
        root = _make_dep_map_with_consumer(tmp_path, "target-repo")
        result = _call_find_consumers(
            {"repo_name": "target-repo"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["consumers"]) >= 1
        entry = data["consumers"][0]
        assert "domain" in entry, (
            f"find_consumers consumer entry must use 'domain' key; got keys: {list(entry)}"
        )

    def test_find_consumers_uses_repo_not_consuming_repo_in_consumer_entry(
        self, tmp_path: Path
    ) -> None:
        """find_consumers consumer entries use 'repo' key (not 'consuming_repo')."""
        root = _make_dep_map_with_consumer(tmp_path, "target-repo")
        result = _call_find_consumers(
            {"repo_name": "target-repo"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["consumers"]) >= 1
        entry = data["consumers"][0]
        assert "repo" in entry, (
            f"find_consumers consumer entry must use 'repo' key; got keys: {list(entry)}"
        )

    def test_get_repo_domains_uses_domain_not_domain_name_in_memberships(
        self, tmp_path: Path
    ) -> None:
        """get_repo_domains domain entries use 'domain' key (not 'domain_name')."""
        root = _make_dep_map_with_repo_in_domain(tmp_path, "my-repo", "my-domain")
        result = _call_get_repo_domains({"repo_name": "my-repo"}, _make_app_state(root))
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["domains"]) >= 1
        entry = data["domains"][0]
        assert "domain" in entry, (
            f"get_repo_domains domain entry must use 'domain' key; got keys: {list(entry)}"
        )

    def test_get_domain_summary_participating_repos_uses_repo_not_repo_name(
        self, tmp_path: Path
    ) -> None:
        """get_domain_summary participating_repos entries use 'repo' key."""
        root = _make_dep_map_with_repo_in_domain(tmp_path, "my-repo", "my-domain")
        result = _call_get_domain_summary(
            {"domain_name": "my-domain"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert data["success"] is True
        summary = data["summary"]
        assert summary is not None
        assert len(summary["participating_repos"]) >= 1
        entry = summary["participating_repos"][0]
        assert "repo" in entry, (
            f"participating_repos entry must use 'repo' key; got keys: {list(entry)}"
        )

    def test_cross_domain_graph_uses_source_domain_and_target_domain_in_edges(
        self, tmp_path: Path
    ) -> None:
        """cross_domain_graph edge entries use 'source_domain' and 'target_domain' keys."""
        root = _make_two_domain_graph(tmp_path)
        result = _call_get_cross_domain_graph({}, _make_app_state(root))
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["edges"]) >= 1
        edge = data["edges"][0]
        assert "source_domain" in edge, (
            f"graph edge must use 'source_domain' key; got keys: {list(edge)}"
        )
        assert "target_domain" in edge, (
            f"graph edge must use 'target_domain' key; got keys: {list(edge)}"
        )


# ---------------------------------------------------------------------------
# AC4: input parameter names unchanged
# ---------------------------------------------------------------------------


class TestAC4InputParameterNamesUnchanged:
    """AC4: input parameters repo_name and domain_name must still work unchanged."""

    def test_find_consumers_accepts_repo_name_input_param(self, tmp_path: Path) -> None:
        """find_consumers still accepts 'repo_name' as input parameter (unchanged)."""
        root = _make_dep_map_with_consumer(tmp_path, "my-target")
        result = _call_find_consumers({"repo_name": "my-target"}, _make_app_state(root))
        data = _parse_response(result)
        # Must not be invalid_input (which would indicate the param name changed)
        assert data.get("resolution") != "invalid_input", (
            "repo_name input parameter must still be accepted (unchanged)"
        )

    def test_get_repo_domains_accepts_repo_name_input_param(
        self, tmp_path: Path
    ) -> None:
        """get_repo_domains still accepts 'repo_name' as input parameter (unchanged)."""
        root = _make_dep_map_with_repo_in_domain(tmp_path, "my-repo", "d")
        result = _call_get_repo_domains({"repo_name": "my-repo"}, _make_app_state(root))
        data = _parse_response(result)
        assert data.get("resolution") != "invalid_input", (
            "repo_name input parameter must still be accepted"
        )

    def test_get_domain_summary_accepts_domain_name_input_param(
        self, tmp_path: Path
    ) -> None:
        """get_domain_summary still accepts 'domain_name' as input parameter (unchanged)."""
        root = _make_dep_map_with_repo_in_domain(tmp_path, "r", "target-domain")
        result = _call_get_domain_summary(
            {"domain_name": "target-domain"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert data.get("resolution") != "invalid_input", (
            "domain_name input parameter must still be accepted"
        )

    def test_find_consumers_with_repo_name_finds_matching_consumers(
        self, tmp_path: Path
    ) -> None:
        """find_consumers with repo_name='target-repo' returns consumers for that repo."""
        root = _make_dep_map_with_consumer(tmp_path, "target-repo")
        result = _call_find_consumers(
            {"repo_name": "target-repo"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["consumers"]) == 1, (
            "Input parameter name 'repo_name' must still route to correct consumers"
        )


# ---------------------------------------------------------------------------
# AC5: dual-write helper — both canonical and deprecated aliases present
# ---------------------------------------------------------------------------


class TestAC5DualWriteAliases:
    """AC5: dual-write helper writes both canonical and deprecated-alias keys."""

    def test_find_consumers_entry_has_both_repo_and_consuming_repo(
        self, tmp_path: Path
    ) -> None:
        """find_consumers consumer entry has both 'repo' (canonical) and 'consuming_repo' (deprecated alias)."""
        root = _make_dep_map_with_consumer(tmp_path, "target-repo")
        result = _call_find_consumers(
            {"repo_name": "target-repo"}, _make_app_state(root)
        )
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["consumers"]) >= 1
        entry = data["consumers"][0]
        assert "repo" in entry, (
            f"find_consumers entry missing canonical 'repo' key; got: {list(entry)}"
        )
        assert "consuming_repo" in entry, (
            f"find_consumers entry missing deprecated alias 'consuming_repo' key; got: {list(entry)}"
        )
        # Both must carry the same value
        assert entry["repo"] == entry["consuming_repo"], (
            f"Canonical 'repo' and deprecated 'consuming_repo' must be equal; "
            f"repo={entry['repo']!r}, consuming_repo={entry['consuming_repo']!r}"
        )

    def test_get_repo_domains_entry_has_both_domain_and_domain_name(
        self, tmp_path: Path
    ) -> None:
        """get_repo_domains domain entry has both 'domain' (canonical) and 'domain_name' (deprecated alias)."""
        root = _make_dep_map_with_repo_in_domain(tmp_path, "my-repo", "my-domain")
        result = _call_get_repo_domains({"repo_name": "my-repo"}, _make_app_state(root))
        data = _parse_response(result)
        assert data["success"] is True
        assert len(data["domains"]) >= 1
        entry = data["domains"][0]
        assert "domain" in entry, (
            f"get_repo_domains entry missing canonical 'domain' key; got: {list(entry)}"
        )
        assert "domain_name" in entry, (
            f"get_repo_domains entry missing deprecated alias 'domain_name' key; got: {list(entry)}"
        )
        # Both must carry the same value
        assert entry["domain"] == entry["domain_name"], (
            f"Canonical 'domain' and deprecated 'domain_name' must be equal; "
            f"domain={entry['domain']!r}, domain_name={entry['domain_name']!r}"
        )

    def test_depmap_aliases_module_exists(self) -> None:
        """_depmap_aliases.py module must exist and be importable."""
        from code_indexer.server.mcp.handlers import _depmap_aliases  # noqa: F401

        assert _depmap_aliases is not None

    def test_dual_write_helper_function_is_centralized(self) -> None:
        """Dual-write helper must be importable from _depmap_aliases module."""
        from code_indexer.server.mcp.handlers._depmap_aliases import (
            apply_consumer_aliases,
            apply_domain_membership_aliases,
        )

        assert callable(apply_consumer_aliases)
        assert callable(apply_domain_membership_aliases)

    def test_apply_consumer_aliases_adds_both_keys(self) -> None:
        """apply_consumer_aliases adds canonical 'repo' and deprecated 'consuming_repo'."""
        from code_indexer.server.mcp.handlers._depmap_aliases import (
            apply_consumer_aliases,
        )

        entry = {
            "consuming_repo": "consumer-x",
            "domain": "d",
            "dependency_type": "Code-level",
            "evidence": "ev",
        }
        result = apply_consumer_aliases(entry)
        assert "repo" in result, (
            f"apply_consumer_aliases must add 'repo' key; got: {list(result)}"
        )
        assert "consuming_repo" in result, (
            f"apply_consumer_aliases must preserve 'consuming_repo' deprecated alias; got: {list(result)}"
        )
        assert result["repo"] == result["consuming_repo"] == "consumer-x"

    def test_apply_consumer_aliases_dual_writes_both_repo_and_domain(self) -> None:
        """apply_consumer_aliases must dual-write BOTH repo<->consuming_repo AND domain<->domain_name.

        Blocker 1: consumer entries have both canonical domain and deprecated domain_name.
        """
        from code_indexer.server.mcp.handlers._depmap_aliases import (
            apply_consumer_aliases,
        )

        result = apply_consumer_aliases(
            {
                "repo": "repo-x",
                "domain": "alpha-domain",
                "evidence": "ev",
                "dependency_type": "Code-level",
            }
        )
        assert result["repo"] == "repo-x"
        assert result["consuming_repo"] == "repo-x"
        assert result["domain"] == "alpha-domain"
        assert result["domain_name"] == "alpha-domain", (
            f"apply_consumer_aliases must dual-write domain_name alongside domain; "
            f"got keys: {list(result)}"
        )

    def test_apply_domain_membership_aliases_adds_both_keys(self) -> None:
        """apply_domain_membership_aliases adds canonical 'domain' and deprecated 'domain_name'."""
        from code_indexer.server.mcp.handlers._depmap_aliases import (
            apply_domain_membership_aliases,
        )

        entry = {"domain_name": "alpha-domain", "role": "Core service"}
        result = apply_domain_membership_aliases(entry)
        assert "domain" in result, (
            f"apply_domain_membership_aliases must add 'domain' key; got: {list(result)}"
        )
        assert "domain_name" in result, (
            f"apply_domain_membership_aliases must preserve 'domain_name' deprecated alias; got: {list(result)}"
        )
        assert result["domain"] == result["domain_name"] == "alpha-domain"
