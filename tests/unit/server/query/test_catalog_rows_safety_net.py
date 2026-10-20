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
