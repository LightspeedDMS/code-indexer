"""History, temporal and X-Ray of the internal catalog are admin-only.

Real group store (SQLite) from the shared neutral fixture; the activation
store is a small in-memory stand-in of ActivatedRepoManager's lookup.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services.cidx_meta_access import acting_scope
from code_indexer.server.services.cidx_meta_access.history_policy import (
    CatalogSurface,
    catalog_surface_refusal,
    golden_catalog_surface_refusal,
    refuse_catalog_temporal,
)
from tests.shared.cidx_meta_visibility_fixture import (
    ADMIN_USER,
    ALPHA,
    RESTRICTED_USER,
    CidxMetaFixture,
    build_cidx_meta_fixture,
)


class _Activations:
    def __init__(self, records: List[Dict[str, Any]]):
        self._records = records

    def get_repository(
        self, username: str, user_alias: str, *, touch: bool = True
    ) -> Optional[Dict[str, Any]]:
        return next((r for r in self._records if r["user_alias"] == user_alias), None)


CATALOG_CLONE = {"user_alias": "team-notes", "golden_repo_alias": "cidx-meta"}
ALPHA_CLONE = {"user_alias": "my-alpha", "golden_repo_alias": ALPHA}


@pytest.fixture
def fx(tmp_path: Path) -> CidxMetaFixture:
    return build_cidx_meta_fixture(tmp_path)


@pytest.fixture
def activations() -> _Activations:
    return _Activations([CATALOG_CLONE, ALPHA_CLONE])


@pytest.mark.parametrize("surface", list(CatalogSurface))
@pytest.mark.parametrize("alias", ["cidx-meta-global", "cidx-meta", "team-notes"])
def test_restricted_caller_is_refused_on_the_catalog(
    fx: CidxMetaFixture, activations: _Activations, surface: CatalogSurface, alias: str
) -> None:
    refusal = catalog_surface_refusal(
        surface, alias, RESTRICTED_USER, fx.access_service, activations
    )
    assert refusal == surface.refusal
    assert "cidx-meta" not in refusal and "team-notes" not in refusal


def test_refusal_texts_name_the_surface() -> None:
    assert CatalogSurface.GIT_HISTORY.refusal == (
        "Git history is not available for this repository."
    )
    assert CatalogSurface.TEMPORAL.refusal == (
        "Temporal search is not available for this repository."
    )
    assert CatalogSurface.XRAY.refusal == "X-Ray is not available for this repository."


@pytest.mark.parametrize("alias", ["cidx-meta-global", "team-notes"])
def test_admin_keeps_access_to_the_catalog(
    fx: CidxMetaFixture, activations: _Activations, alias: str
) -> None:
    for surface in CatalogSurface:
        assert (
            catalog_surface_refusal(
                surface, alias, ADMIN_USER, fx.access_service, activations
            )
            is None
        )


def test_admin_acting_for_a_user_is_restricted(
    fx: CidxMetaFixture, activations: _Activations
) -> None:
    with acting_scope([ALPHA]):
        refusal = catalog_surface_refusal(
            CatalogSurface.GIT_HISTORY,
            "cidx-meta-global",
            ADMIN_USER,
            fx.access_service,
            activations,
        )
    assert refusal == CatalogSurface.GIT_HISTORY.refusal


@pytest.mark.parametrize("alias", [f"{ALPHA}-global", "my-alpha", "cidx-metadata"])
def test_other_repositories_are_not_refused(
    fx: CidxMetaFixture, activations: _Activations, alias: str
) -> None:
    assert (
        catalog_surface_refusal(
            CatalogSurface.GIT_HISTORY,
            alias,
            RESTRICTED_USER,
            fx.access_service,
            activations,
        )
        is None
    )


def test_unresolvable_activation_identity_counts_as_the_catalog(
    fx: CidxMetaFixture,
) -> None:
    refusal = catalog_surface_refusal(
        CatalogSurface.GIT_HISTORY, "my-alpha", RESTRICTED_USER, fx.access_service, None
    )
    assert refusal == CatalogSurface.GIT_HISTORY.refusal


def test_missing_access_service_fails_closed(activations: _Activations) -> None:
    assert (
        catalog_surface_refusal(
            CatalogSurface.TEMPORAL, "cidx-meta-global", ADMIN_USER, None, activations
        )
        == CatalogSurface.TEMPORAL.refusal
    )


def test_golden_door_judges_the_golden_name_only(fx: CidxMetaFixture) -> None:
    for alias in ("cidx-meta-global", "cidx-meta"):
        assert (
            golden_catalog_surface_refusal(
                CatalogSurface.XRAY, alias, RESTRICTED_USER, fx.access_service
            )
            == CatalogSurface.XRAY.refusal
        )
        assert (
            golden_catalog_surface_refusal(
                CatalogSurface.XRAY, alias, ADMIN_USER, fx.access_service
            )
            is None
        )
    for other in (f"{ALPHA}-global", ALPHA, "team-notes"):
        assert (
            golden_catalog_surface_refusal(
                CatalogSurface.XRAY, other, RESTRICTED_USER, fx.access_service
            )
            is None
        )
    unjudgeable: List[Any] = ["", None, ["cidx-meta"]]
    for value in unjudgeable:
        assert (
            golden_catalog_surface_refusal(
                CatalogSurface.XRAY, value, RESTRICTED_USER, fx.access_service
            )
            == CatalogSurface.XRAY.refusal
        )
    assert (
        golden_catalog_surface_refusal(
            CatalogSurface.XRAY, "cidx-meta-global", ADMIN_USER, None
        )
        == CatalogSurface.XRAY.refusal
    )


def test_temporal_refusal_is_a_request_rejection(
    fx: CidxMetaFixture, activations: _Activations
) -> None:
    from code_indexer.server.query.semantic_query_manager import SearchRequestError

    with pytest.raises(SearchRequestError) as raised:
        refuse_catalog_temporal(
            "team-notes", RESTRICTED_USER, fx.access_service, activations
        )
    assert str(raised.value) == CatalogSurface.TEMPORAL.refusal
    refuse_catalog_temporal("team-notes", ADMIN_USER, fx.access_service, activations)
    refuse_catalog_temporal("my-alpha", RESTRICTED_USER, fx.access_service, activations)


# Shapes the gate cannot judge as one concrete repository: a collection is
# refused when any element is; a pattern could expand to the catalog; any
# other non-string or empty value is unjudgeable. All fail closed.
_REFUSED_SHAPES: Dict[str, Any] = {
    "list-with-catalog": [f"{ALPHA}-global", "cidx-meta-global"],
    "tuple-with-catalog": (f"{ALPHA}-global", "cidx-meta"),
    "nested-list-with-catalog": [[f"{ALPHA}-global", "cidx-meta-global"]],
    "star": "*",
    "prefix-glob": "cidx-*",
    "question-glob": f"{ALPHA}?global",
    "bracket-glob": "[ab]lpha-global",
    "none": None,
    "integer": 7,
    "mapping": {},
    "empty-string": "",
    "empty-list": [],
}
_GRANTED_SHAPES: Dict[str, Any] = {
    "list-of-granted": [f"{ALPHA}-global"],
    "tuple-of-granted": (f"{ALPHA}-global",),
}


@pytest.mark.parametrize("surface", list(CatalogSurface))
@pytest.mark.parametrize("shape", sorted(_REFUSED_SHAPES))
def test_unjudgeable_shapes_fail_closed_for_restricted_caller(
    fx: CidxMetaFixture, activations: _Activations, surface: CatalogSurface, shape: str
) -> None:
    alias = _REFUSED_SHAPES[shape]
    assert (
        catalog_surface_refusal(
            surface, alias, RESTRICTED_USER, fx.access_service, activations
        )
        == surface.refusal
    )
    assert (
        golden_catalog_surface_refusal(
            surface, alias, RESTRICTED_USER, fx.access_service
        )
        == surface.refusal
    )


@pytest.mark.parametrize("surface", list(CatalogSurface))
@pytest.mark.parametrize("shape", sorted(_GRANTED_SHAPES))
def test_collections_of_granted_repositories_are_not_refused(
    fx: CidxMetaFixture, activations: _Activations, surface: CatalogSurface, shape: str
) -> None:
    alias = _GRANTED_SHAPES[shape]
    assert (
        catalog_surface_refusal(
            surface, alias, RESTRICTED_USER, fx.access_service, activations
        )
        is None
    )
    assert (
        golden_catalog_surface_refusal(
            surface, alias, RESTRICTED_USER, fx.access_service
        )
        is None
    )


@pytest.mark.parametrize("surface", list(CatalogSurface))
@pytest.mark.parametrize("shape", sorted(_REFUSED_SHAPES))
def test_admin_is_never_refused_whatever_the_shape(
    fx: CidxMetaFixture, activations: _Activations, surface: CatalogSurface, shape: str
) -> None:
    alias = _REFUSED_SHAPES[shape]
    assert (
        catalog_surface_refusal(
            surface, alias, ADMIN_USER, fx.access_service, activations
        )
        is None
    )
    assert (
        golden_catalog_surface_refusal(surface, alias, ADMIN_USER, fx.access_service)
        is None
    )


@pytest.mark.parametrize("alias", ["*", None])
def test_temporal_rejects_unjudgeable_shapes(
    fx: CidxMetaFixture, activations: _Activations, alias: Any
) -> None:
    from code_indexer.server.query.semantic_query_manager import SearchRequestError

    with pytest.raises(SearchRequestError):
        refuse_catalog_temporal(alias, RESTRICTED_USER, fx.access_service, activations)


@pytest.mark.parametrize("alias", ["*", None, [f"{ALPHA}-global", "cidx-*"]])
def test_xray_async_gate_refuses_unjudgeable_shapes(
    fx: CidxMetaFixture, monkeypatch: pytest.MonkeyPatch, alias: Any
) -> None:
    import asyncio

    from code_indexer.server.mcp.handlers.xray import _catalog_guard

    monkeypatch.setattr(
        _catalog_guard, "xray_access_service", lambda: fx.access_service
    )
    refusal = asyncio.run(
        _catalog_guard.catalog_xray_refusal_async(alias, RESTRICTED_USER)
    )
    assert refusal is not None
    assert refusal["message"] == CatalogSurface.XRAY.refusal
    admin = asyncio.run(_catalog_guard.catalog_xray_refusal_async(alias, ADMIN_USER))
    assert admin is None
