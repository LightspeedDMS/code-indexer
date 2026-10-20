"""AccessFilteringService.filter_query_results projects catalog rows: the
safety net for doors that do not go through _perform_search (REST FTS
branch, omni fan-out, temporal post-processing). Real app state, real
groups, real catalog files (catalog_search_env); no mocks."""

from __future__ import annotations

from typing import Any, Dict, Iterator, List

import pytest

from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    read_lines,
)
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    CatalogEnv,
    catalog_app,
    dump,
)

pytestmark = pytest.mark.timeout(60)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("catalog-net")) as catalog:
        yield catalog


def _rows(env: CatalogEnv) -> List[Dict[str, Any]]:
    alpha = read_lines(env.root / f"{ALPHA}.md")
    return [
        {  # REST FTS shape
            "path": f"{ALPHA}.md",
            "line_start": 0,
            "line_end": 0,
            "snippet": "\n".join(alpha[:4]),
            "language": "md",
            "repository_alias": CATALOG_ALIAS,
        },
        {  # omni shape
            "file_path": f"{CANARY}.md",
            "line_start": 1,
            "content": f"# {CANARY}",
            "score": 0.5,
            "source_repo": CATALOG_ALIAS,
        },
        {
            "file_path": "dependency-map/_journal.json",
            "line_number": 1,
            "code_snippet": CANARY,
            "repository_alias": CATALOG_ALIAS,
        },
    ]


def _filter(env: CatalogEnv, rows: List[Dict[str, Any]], user: str) -> Any:
    return env.fx.access_service.filter_query_results(rows, user)


def test_restricted_catalog_rows_are_projected(env: CatalogEnv) -> None:
    out = _filter(env, _rows(env), RESTRICTED_USER)
    dumped = dump(out)
    assert CANARY not in dumped and GAMMA not in dumped
    assert [row["path"] for row in out] == [f"{ALPHA}.md"]
    assert f"{ALPHA} talks to [restricted repository]" in out[0]["snippet"]
    assert _filter(env, out, RESTRICTED_USER) == out


def test_admin_rows_are_unchanged(env: CatalogEnv) -> None:
    rows = _rows(env)
    assert _filter(env, rows, ADMIN_USER) == rows


def test_rest_rows_refuse_without_the_access_service(env: CatalogEnv) -> None:
    from types import SimpleNamespace

    from code_indexer.server.services.cidx_meta_access.catalog_search import (
        rest_rows_for_caller,
    )
    from code_indexer.server.services.repo_access_guard import (
        AccessFilteringServiceUnavailableError,
    )

    with pytest.raises(AccessFilteringServiceUnavailableError):
        rest_rows_for_caller(None, RESTRICTED_USER, _rows(env))
    ordinary = [{"file_path": "README.md", "repository_alias": f"{ALPHA}-global"}]
    assert rest_rows_for_caller(None, RESTRICTED_USER, ordinary) == ordinary
    service = env.fx.access_service
    fts = SimpleNamespace(
        query_text=CANARY,
        case_sensitive=False,
        regex=False,
        fuzzy=False,
        edit_distance=0,
    )
    assert rest_rows_for_caller(service, RESTRICTED_USER, _rows(env), fts) == []
    plain = rest_rows_for_caller(service, RESTRICTED_USER, _rows(env))
    assert plain == _filter(env, _rows(env), RESTRICTED_USER)
