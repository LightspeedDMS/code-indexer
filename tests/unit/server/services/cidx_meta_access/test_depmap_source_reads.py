"""A restricted depmap source reads only what it returns.

Classification comes from the dependency-map model already loaded (one
source of truth with the projection); ``_domains.json`` entries come from
that model; only the descriptions actually returned are projected. File
opens are traced with an audit hook; real fixture, no mocks.
"""

from __future__ import annotations

import os
import sys
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator, List, Optional, Tuple

import pytest

from code_indexer.server.services.cidx_meta_access import (
    MetaVisibility,
    load_depmap_model,
    projection_cache_stats,
)
from code_indexer.server.services.cidx_meta_access.depmap_source import (
    source_for_visibility,
)
from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CidxMetaFixture,
    add_depmap_extension,
    build_cidx_meta_fixture,
)

_tracing: List[Optional[List[str]]] = [None]


def _audit(
    event: str,
    args: Tuple[Any, ...],
    _state: List[Optional[List[str]]] = _tracing,
    _isinstance: Callable[[object, Any], bool] = isinstance,
    _path_types: Tuple[type, ...] = (str, os.PathLike),
    _fspath: Callable[[Any], Any] = os.fspath,
) -> None:
    # Installed for the whole process: everything is bound here (no global
    # lookup), so it is a no-op outside a trace and safe at shutdown.
    opened = _state[0]
    if opened is None or event != "open":
        return
    if args and _isinstance(args[0], _path_types):
        opened.append(str(_fspath(args[0])))


sys.addaudithook(_audit)


@contextmanager
def _opened_files(root: Path) -> Iterator[List[str]]:
    """Files under *root* opened inside the block (catalog-relative)."""
    opened: List[str] = []
    found: List[str] = []
    _tracing[0] = opened
    try:
        yield found
    finally:
        _tracing[0] = None
        prefix = str(root) + "/"
        # One read raises two "open" events on Python 3.9: distinct paths.
        found += sorted({p[len(prefix) :] for p in opened if p.startswith(prefix)})


@pytest.fixture(scope="module")
def fx(tmp_path_factory: pytest.TempPathFactory) -> CidxMetaFixture:
    fixture = build_cidx_meta_fixture(tmp_path_factory.mktemp("depmap-reads"))
    add_depmap_extension(fixture)
    return fixture


def _source(fx: CidxMetaFixture) -> Any:
    return source_for_visibility(
        MetaVisibility.restricted([ALPHA]), fx.dep_map_dir, fx.registry
    )


def test_a_one_domain_summary_opens_only_that_document(fx: CidxMetaFixture) -> None:
    load_depmap_model(fx.dep_map_dir)
    DepMapMCPParser(source=_source(fx)).get_domain_summary("payments")
    with _opened_files(fx.root) as opened:
        summary, _ = DepMapMCPParser(source=_source(fx)).get_domain_summary("payments")
    assert summary is not None and summary["name"] == "payments"
    assert opened == ["dependency-map/payments.md"]


def test_listing_domains_and_memberships_opens_no_file(fx: CidxMetaFixture) -> None:
    load_depmap_model(fx.dep_map_dir)
    with _opened_files(fx.root) as opened:
        source = _source(fx)
        names = source.domain_names()
        entries = source.load_domains_json()
    assert "payments" in names and any(e["name"] == "payments" for e in entries)
    assert opened == []


def test_only_returned_descriptions_are_projected(fx: CidxMetaFixture) -> None:
    load_depmap_model(fx.dep_map_dir)
    parser = DepMapMCPParser(source=_source(fx))
    before = projection_cache_stats()
    parser.get_domain_summary("payments")
    after = projection_cache_stats()
    calls = (after.projections_computed + after.projection_hits) - (
        before.projections_computed + before.projection_hits
    )
    # The payments document and its one description; never the other
    # visible domains' descriptions.
    assert calls == 2
