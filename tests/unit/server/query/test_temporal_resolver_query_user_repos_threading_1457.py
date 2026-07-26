"""Story #1457 AC1/AC2 live wiring: golden_repo_alias threaded end-to-end
through query_user_repositories -> _search_single_repository ->
_execute_temporal_query.

Follows the exact established pattern from
test_temporal_embedder_override_server_wiring_1291.py's
TestQueryUserRepositoriesThreadsEmbedderOverride: _execute_temporal_query
is monkeypatched at the class boundary (its own resolver-construction
behavior is proven separately in
test_temporal_resolver_server_wiring_1457.py); this file proves the
DISCRIMINATOR -- which alias is passed as golden_repo_alias -- is computed
correctly for each repo shape query_user_repositories's loop already
distinguishes:
  - is_global repo: golden_repo_alias = the alias itself (it already IS
    the golden repo's own alias, resolved directly via AliasManager).
  - regular activated repo: golden_repo_alias = repo_info["golden_repo_alias"]
    (the underlying golden repo this activation was cloned from -- NEVER
    the activation's own possibly-renamed user_alias).
  - a repo_info with no golden-repo lineage info at all (e.g. explicit
    repo_path, no is_global/golden_repo_alias key): golden_repo_alias=None
    -- no resolver constructed, byte-identical fallback.
"""

from __future__ import annotations

import logging

from unittest.mock import MagicMock


def test_golden_repo_alias_threaded_for_is_global_and_activated_repos(
    monkeypatch, tmp_path
):
    from code_indexer.server.query.semantic_query_manager import (
        SemanticQueryManager,
    )
    from code_indexer.global_repos.alias_manager import AliasManager

    captured: dict = {}

    def _fake_execute_temporal_query(self, **kwargs):
        captured[kwargs["repository_alias"]] = kwargs.get("golden_repo_alias")
        result = MagicMock()
        result.results = []
        return []

    monkeypatch.setattr(
        SemanticQueryManager,
        "_execute_temporal_query",
        _fake_execute_temporal_query,
    )

    manager = SemanticQueryManager.__new__(SemanticQueryManager)
    manager.max_results_per_query = 50
    manager.logger = logging.getLogger("test.semantic_query_manager")
    manager.query_tracker = None

    # A real alias pointer so the is_global branch's AliasManager.read_alias
    # resolves successfully (that branch is untouched production logic,
    # not part of what this test targets -- it must keep working).
    aliases_dir = tmp_path / "golden-repos" / "aliases"
    global_target = tmp_path / "golden-repos" / "global-repo"
    global_target.mkdir(parents=True)
    AliasManager(str(aliases_dir)).create_alias("global-repo", str(global_target))

    class _FakeARM:
        activated_repos_dir = str(tmp_path / "activated-repos")

        def list_activated_repositories(self, _u):
            return [
                {
                    "user_alias": "global-repo",
                    "is_global": True,
                },
                {
                    "user_alias": "my-activation",
                    "golden_repo_alias": "the-real-golden-repo",
                },
                {
                    "user_alias": "no-lineage-repo",
                    "repo_path": str(tmp_path / "no-lineage-repo"),
                },
            ]

        def get_activated_repo_path(self, _u, _a):
            return str(tmp_path / "activated" / _a)

        def get_activation_id(self, _u, _a):
            return None

    manager.activated_repo_manager = _FakeARM()

    import code_indexer.server.app as _app_mod

    class _FakeState:
        backend_registry = None
        http_client_factory = None

    class _FakeApp:
        state = _FakeState()

    monkeypatch.setattr(_app_mod, "app", _FakeApp(), raising=False)

    manager.query_user_repositories(
        username="alice",
        query_text="find auth",
        repository_alias=None,
        time_range_all=True,
        query_strategy="primary_only",
    )

    assert captured, "_execute_temporal_query was never called"
    assert captured.get("global-repo") == "global-repo", (
        "is_global repo must forward its OWN alias as golden_repo_alias; "
        f"captured={captured}"
    )
    assert captured.get("my-activation") == "the-real-golden-repo", (
        "regular activated repo must forward repo_info['golden_repo_alias'] "
        f"(the UNDERLYING golden repo), never its own user_alias; captured={captured}"
    )
    assert captured.get("no-lineage-repo") is None, (
        "a repo with no golden-repo lineage info must forward "
        f"golden_repo_alias=None (byte-identical fallback); captured={captured}"
    )
