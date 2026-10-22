"""MCP full-text catalog results depend only on the text the caller may see.

Real app, real handler and real indexes (test_search_meta_doors'
environment); only the embedding provider is replaced.
"""

from __future__ import annotations

import json
from typing import Any, List, Union

import pytest

from code_indexer.server.services.cidx_meta_access.fulltext_terms import (
    FULLTEXT_FORM_REFUSAL,
)
from tests.shared.cidx_meta_visibility_fixture import ALPHA, CANARY
from tests.unit.server.mcp.test_search_meta_doors import (  # noqa: F401
    _results,
    _search,
    env,
)
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    RESTRICTED,
    CatalogEnv,
)

pytestmark = pytest.mark.timeout(60)
ABSENT = "zzqqxxnotthere"
Alias = Union[str, List[str]]
DOORS = [CATALOG_ALIAS, [CATALOG_ALIAS]]


def _fts(alias: Alias, text: str) -> Any:
    return _search(
        RESTRICTED, query_text=text, repository_alias=alias, search_mode="fts"
    )


def _paths(payload: Any) -> List[str]:
    return sorted(str(row.get("file_path")) for row in _results(payload))


@pytest.mark.parametrize("alias", DOORS, ids=["single", "omni"])
def test_hidden_word_answers_like_an_absent_word(
    env: CatalogEnv,  # noqa: F811
    alias: Alias,
) -> None:
    hidden = _fts(alias, f"payments {CANARY}")
    absent = _fts(alias, f"payments {ABSENT}")
    assert hidden["success"] and absent["success"]
    assert _paths(hidden) == _paths(absent) == []
    assert _paths(_fts(alias, "payments"))


@pytest.mark.parametrize("alias", DOORS, ids=["single", "omni"])
def test_unevaluable_forms_are_refused_identically(
    env: CatalogEnv,  # noqa: F811
    alias: Alias,
) -> None:
    answers = [_fts(alias, f"payments NOT {word}") for word in (CANARY, ABSENT)]
    assert [a["success"] for a in answers] == [False, False]
    assert all(FULLTEXT_FORM_REFUSAL in json.dumps(a) for a in answers)
    assert json.dumps(answers[0]).replace(CANARY, ABSENT) == json.dumps(answers[1])


def test_ordinary_repository_boolean_query_is_not_refused(
    env: CatalogEnv,  # noqa: F811
) -> None:
    payload = _fts([f"{ALPHA}-global"], f"payments NOT {ABSENT}")
    assert payload["success"], payload
    assert _paths(payload) == ["README.md"]
