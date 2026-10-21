"""Restricted catalog full-text over a fleet of VISIBLE descriptions.

A real app over the neutral catalog fixture whose restricted caller is
granted 900 repositories, so every description is a document the caller
is shown as a projection. Real per-chunk Tantivy index; only the caller and
the embedding provider are replaced. Catalog file opens are counted with
an audit hook: a literal query reads the descriptions the engine names for
it, never every visible one. Cold and warm latency and opens per query are
reported for a rare, a common and an absent word.
"""

from __future__ import annotations

import statistics
import sys
import time
from pathlib import Path
from typing import Any, Iterator, List, Tuple

import pytest

from tests.shared.cidx_meta_visibility_fixture import RESTRICTED_USER
from tests.unit.server.query.catalog_search_env import (
    CATALOG_ALIAS,
    CatalogEnv,
    catalog_app,
    rebuild_fulltext,
)
from tests.unit.server.query.extension_filter_env_2047 import ranked_store_search

pytestmark = pytest.mark.timeout(600)
FLEET = 900
RARE = "fleet0007"
COMMON = "service"
ABSENT = "zzabsentword"
# Catalog opens of the query in progress; None while no query is counted.
_OPENS: List[Any] = [None]


def _audit(event: str, args: Tuple[Any, ...], _opens: List[Any] = _OPENS) -> None:
    # Audit hooks outlive the module: read nothing from its globals.
    opened = _opens[0]
    if opened is not None and event == "open" and isinstance(args[0], str):
        opened.append(args[0])


sys.addaudithook(_audit)


@pytest.fixture(scope="module")
def env(tmp_path_factory: pytest.TempPathFactory) -> Iterator[CatalogEnv]:
    with catalog_app(tmp_path_factory.mktemp("visible-fleet")) as catalog:
        groups = catalog.fx.group_manager
        team = groups.get_group_by_name("fixture-team")
        assert team is not None
        body = "\n".join(
            f"The {COMMON} handles request batch {k} and forwards events."
            for k in range(40)
        )
        for n in range(FLEET):
            alias = f"fleet{n:04d}"
            catalog.fx.registry.register_repo(
                alias_name=f"{alias}-global",
                repo_name=alias,
                repo_url=None,
                index_path=f"/golden/{alias}",
            )
            groups.grant_repo_access(alias, team.id, granted_by="test")
            (catalog.root / f"{alias}.md").write_text(
                f"---\nname: {alias}\ndescription: {alias} worker\n---\n\n"
                f"# {alias}\n\n{body}\n",
                encoding="utf-8",
            )
        rebuild_fulltext(catalog.root)
        yield catalog


def _query(env: CatalogEnv, text: str) -> Tuple[List[str], List[str]]:
    """The page's paths and the catalog files the query opened."""
    sqm = env.app.state.semantic_query_manager
    index_dir = str(env.root / ".code-indexer")
    opened: List[str] = []
    _OPENS[0] = opened
    try:
        with ranked_store_search():
            rows = sqm.query_user_repositories(
                username=RESTRICTED_USER,
                query_text=text,
                repository_alias=CATALOG_ALIAS,
                limit=10,
                search_mode="fts",
            )["results"]
    finally:
        _OPENS[0] = None
    catalog = [
        p for p in opened if p.startswith(str(env.root)) and not p.startswith(index_dir)
    ]
    return [str(r["file_path"]) for r in rows], catalog


def _descriptions(opened: List[str]) -> List[str]:
    return [p for p in opened if Path(p).name.startswith("fleet")]


def test_rare_word_reads_only_the_descriptions_naming_it(env: CatalogEnv) -> None:
    paths, opened = _query(env, RARE)
    assert paths == [f"{RARE}.md"]
    assert set(_descriptions(opened)) == {str(env.root / f"{RARE}.md")}


def test_absent_word_reads_no_description(env: CatalogEnv) -> None:
    paths, opened = _query(env, ABSENT)
    assert paths == []
    assert _descriptions(opened) == []


def test_common_word_reads_each_matching_description_once(env: CatalogEnv) -> None:
    paths, opened = _query(env, COMMON)
    assert len(paths) == 10
    described = _descriptions(opened)
    assert len(set(described)) == FLEET
    assert len(described) <= 2 * FLEET


@pytest.mark.parametrize("text", [RARE, COMMON, ABSENT])
def test_visible_fleet_latency(env: CatalogEnv, text: str) -> None:
    started = time.perf_counter()
    _, opened = _query(env, text)
    cold_ms = (time.perf_counter() - started) * 1000
    warm = []
    for _ in range(5):
        started = time.perf_counter()
        _query(env, text)
        warm.append((time.perf_counter() - started) * 1000)
    print(
        f"\nVISIBLE FLEET {text!r}: cold_ms={cold_ms:.0f} "
        f"warm_median_ms={statistics.median(warm):.0f} "
        f"catalog_opens_per_query={len(opened)} "
        f"description_opens={len(_descriptions(opened))}"
    )
    assert statistics.median(warm) < 10000
