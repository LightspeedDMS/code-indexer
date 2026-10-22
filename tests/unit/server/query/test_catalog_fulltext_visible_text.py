"""Catalog full-text results depend only on the text the caller may see.

Real app, real Tantivy index over the neutral catalog fixture
(catalog_search_env); only the embedding provider is replaced. A
restricted caller's full-text search of the catalog answers the same
whether a query word is hidden from them or absent from every document,
and query forms whose outcome cannot be decided on the shown text alone
are refused with one neutral message. Admins and ordinary repositories
are unchanged.
"""

from __future__ import annotations

from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from code_indexer.server.query.semantic_query_manager import SearchRequestError
from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    FULLTEXT_FORM_REFUSAL,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    RESTRICTED_USER,
    read_lines,
)
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    CatalogEnv,
    catalog_app,
    dump,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(90)
ABSENT = "zzqqxxnotthere"


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("fulltext-visible")) as catalog:
        yield catalog


def _search(
    env: CatalogEnv,
    user: str,
    text: str,
    alias: Optional[str] = CATALOG_ALIAS,
    mode: str = "fts",
    **extra: Any,
) -> List[Dict[str, Any]]:
    sqm = env.app.state.semantic_query_manager
    with ranked_store_search():
        out = sqm.query_user_repositories(
            username=user,
            query_text=text,
            repository_alias=alias,
            limit=10,
            search_mode=mode,
            **extra,
        )
    return list(out["results"])


def _paths(rows: List[Dict[str, Any]]) -> List[str]:
    return sorted(str(row["file_path"]) for row in rows)


@pytest.mark.parametrize("visible_word", ["payments", "alpha"])
def test_hidden_word_answers_like_an_absent_word(
    env: CatalogEnv, visible_word: str
) -> None:
    hidden = _search(env, RESTRICTED_USER, f"{visible_word} {CANARY}")
    absent = _search(env, RESTRICTED_USER, f"{visible_word} {ABSENT}")
    assert _paths(hidden) == _paths(absent) == []
    # The hidden word is in the documents: the admin finds them.
    assert _search(env, ADMIN_USER, f"{visible_word} {CANARY}")


def test_visible_words_still_find_projected_documents(env: CatalogEnv) -> None:
    rows = _search(env, RESTRICTED_USER, "payments")
    assert "dependency-map/payments.md" in _paths(rows)
    assert CANARY not in dump(rows)


def test_hit_is_anchored_on_a_line_the_caller_sees(env: CatalogEnv) -> None:
    root = env.root
    for row in _search(env, RESTRICTED_USER, "payments"):
        shown = str(row["code_snippet"])
        assert row["match_text"].lower() == "payments"
        assert "payments" in shown.lower()
        line = read_lines(root / row["file_path"])[row["line_number"] - 1]
        assert row["match_text"] in line


@pytest.mark.parametrize(
    "form",
    [
        "payments NOT {word}",
        "payments OR {word}",
        "payments AND {word}",
        "{word}*",
        "payments^2 {word}",
    ],
)
def test_unevaluable_forms_are_refused_identically(env: CatalogEnv, form: str) -> None:
    errors = []
    for word in (CANARY, ABSENT):
        with pytest.raises(SearchRequestError) as caught:
            _search(env, RESTRICTED_USER, form.format(word=word))
        errors.append(str(caught.value))
    assert errors == [FULLTEXT_FORM_REFUSAL, FULLTEXT_FORM_REFUSAL]


def test_multi_word_fuzzy_is_refused(env: CatalogEnv) -> None:
    with pytest.raises(SearchRequestError, match=FULLTEXT_FORM_REFUSAL):
        _search(env, RESTRICTED_USER, "payments ledger", fuzzy=True)


def test_single_word_fuzzy_is_judged_on_shown_text(env: CatalogEnv) -> None:
    fuzzy = _search(env, RESTRICTED_USER, "paymants", fuzzy=True)
    assert "dependency-map/payments.md" in _paths(fuzzy)
    # "betx" is one edit from a token only hidden lines hold.
    assert _search(env, ADMIN_USER, "betx", fuzzy=True)
    hidden = _search(env, RESTRICTED_USER, "betx", fuzzy=True)
    assert hidden == _search(env, RESTRICTED_USER, ABSENT, fuzzy=True) == []


def test_hybrid_refuses_unevaluable_forms(env: CatalogEnv) -> None:
    with pytest.raises(SearchRequestError, match=FULLTEXT_FORM_REFUSAL):
        _search(env, RESTRICTED_USER, f"payments NOT {CANARY}", mode="hybrid")


def _engine_paths(env: CatalogEnv, text: str) -> List[str]:
    """The raw engine's answer over the catalog's own (per-chunk) index."""
    from code_indexer.services.tantivy_index_manager import TantivyIndexManager

    index = TantivyIndexManager(env.root / ".code-indexer" / "tantivy_index")
    index.open_for_search()
    return sorted(str(row["path"]) for row in index.search(text, limit=10))


@pytest.mark.parametrize("text", [f"payments NOT {ABSENT}", f"payments NOT {CANARY}"])
def test_admin_catalog_queries_are_unchanged(env: CatalogEnv, text: str) -> None:
    rows = _search(env, ADMIN_USER, text)
    assert rows and _paths(rows) == _engine_paths(env, text)
    if CANARY in text:
        assert CANARY not in dump(rows)
    else:
        assert "dependency-map/_domains.json" in _paths(rows)


def test_catalog_locations_are_read_once_per_request(
    env: CatalogEnv, monkeypatch: pytest.MonkeyPatch
) -> None:
    from code_indexer.server.services.cidx_meta_access import served_identity

    calls: List[str] = []
    real = served_identity.catalog_locations

    def counted(golden_repos_dir: str) -> Tuple[str, ...]:
        calls.append(golden_repos_dir)
        return real(golden_repos_dir)

    gam = env.fx.group_manager
    team = gam.get_group_by_name("fixture-team")
    assert team is not None
    gam.grant_repo_access(CANARY, team.id, granted_by="test")
    try:
        monkeypatch.setattr(served_identity, "catalog_locations", counted)
        rows = _search(env, RESTRICTED_USER, "payments", alias=None)
    finally:
        gam.revoke_repo_access(CANARY, team.id)
    readmes = [r for r in rows if r["file_path"] == "README.md"]
    assert {r["repository_alias"] for r in readmes} == {
        f"{ALPHA}-global",
        f"{CANARY}-global",
    }
    assert len(calls) <= 1


def test_ordinary_repository_queries_are_unchanged(env: CatalogEnv) -> None:
    alias = f"{ALPHA}-global"
    rows = _search(env, RESTRICTED_USER, f"payments NOT {ABSENT}", alias=alias)
    assert _paths(rows) == ["README.md"]
    assert _search(env, RESTRICTED_USER, "payments ledger", alias=alias, fuzzy=True)
