"""Dependency-map membership comes from ONE model per request.

One request-scoped reader (``request_participants_reader``) serves every
catalog door that decides several paths in one request. It answers from a
single dependency-map model (the request's own when it has one, else loaded
once on first use), never by re-reading a domain file per domain, so all
decisions of that request agree even when files change mid-request; a new
request reads fresh. Unknown membership stays hidden. Real catalog fixture
and group store. Neutral names only.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.cidx_meta_access.catalog_articles import (
    catalog_decider,
)
from code_indexer.server.services.cidx_meta_access.depmap_model import EMPTY_MODEL
from code_indexer.server.services.cidx_meta_access.file_doors import CatalogFileView
from code_indexer.server.services.cidx_meta_access.membership import (
    participants_reader,
    request_participants_reader,
)
from code_indexer.server.services.cidx_meta_access.path_policy import DecisionKind
from code_indexer.server.services.cidx_meta_access.visibility import (
    resolve_meta_visibility,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ALPHA,
    CANARY,
    GAMMA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)

PAYMENTS = "dependency-map/payments.md"
LEDGER = "dependency-map/ledger.md"


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path / "data")


def _move_payments_to_gamma(fx: CidxMetaFixture) -> None:
    (fx.dep_map_dir / "payments.md").write_text(
        "---\ndomain: payments\nparticipating_repos:\n"
        f"  - {GAMMA}\n---\n\n# payments\n",
        encoding="utf-8",
    )


def _view(fx: CidxMetaFixture) -> CatalogFileView:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    return CatalogFileView(vis, fx.root, fx.access_service, fx.registry)


def test_request_reader_reads_each_domain_once(fx: CidxMetaFixture) -> None:
    request = request_participants_reader(fx.dep_map_dir)
    first = request("payments")

    _move_payments_to_gamma(fx)

    assert first == frozenset({ALPHA, CANARY})
    assert request("payments") == first
    assert participants_reader(fx.dep_map_dir)("payments") == frozenset({GAMMA})


def test_request_reader_answers_every_domain_from_one_model(
    fx: CidxMetaFixture,
) -> None:
    request = request_participants_reader(fx.dep_map_dir)
    assert request("ledger")

    _move_payments_to_gamma(fx)

    assert request("payments") == frozenset({ALPHA, CANARY})


def test_request_reader_answers_from_the_requests_own_model(
    fx: CidxMetaFixture,
) -> None:
    request = request_participants_reader(fx.dep_map_dir, lambda: EMPTY_MODEL)

    assert request("payments") is None


def test_file_door_view_decides_a_domain_once_per_request(
    fx: CidxMetaFixture,
) -> None:
    view = _view(fx)
    before = view.decide(PAYMENTS).kind

    _move_payments_to_gamma(fx)

    assert before is not DecisionKind.DENY
    assert view.decide(PAYMENTS).kind is before
    assert _view(fx).decide(PAYMENTS).kind is DecisionKind.DENY


def test_file_door_view_decides_from_the_model_its_projection_loaded(
    fx: CidxMetaFixture,
) -> None:
    view = _view(fx)
    ledger = (fx.dep_map_dir / "ledger.md").read_text(encoding="utf-8")
    assert view.project_text(LEDGER, ledger)

    _move_payments_to_gamma(fx)

    assert view.decide(PAYMENTS).kind is DecisionKind.TRANSFORM


def test_article_decider_decides_a_domain_once_per_request(
    fx: CidxMetaFixture,
) -> None:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    decide = catalog_decider(fx.root, vis, fx.access_service)
    before = decide(PAYMENTS).kind

    _move_payments_to_gamma(fx)

    assert before is not DecisionKind.DENY
    assert decide(PAYMENTS).kind is before
    fresh = catalog_decider(fx.root, vis, fx.access_service)
    assert fresh(PAYMENTS).kind is DecisionKind.DENY


def test_article_decider_answers_every_domain_from_one_model(
    fx: CidxMetaFixture,
) -> None:
    vis = resolve_meta_visibility(RESTRICTED_USER, fx.access_service)
    decide = catalog_decider(fx.root, vis, fx.access_service)
    assert decide(LEDGER).kind is DecisionKind.TRANSFORM

    _move_payments_to_gamma(fx)

    assert decide(PAYMENTS).kind is DecisionKind.TRANSFORM
