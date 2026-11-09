"""The core's consumer of ``search_augmenters()`` and the memory visibility
the ``access_projection`` capability answers (SDK 0.7, modular phase 4f)."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Iterator, List, Optional, Sequence

import pytest
from cidx_sdk import (
    BaseFeature,
    FeatureLoadError,
    FeatureManifest,
    SearchAugmenterContribution,
    SearchAugmentRequest,
)

from code_indexer.server.features.access_projection import (
    CoreAccessProjectionCapability,
)
from code_indexer.server.features.loader import FeatureCandidate, load_features
from code_indexer.server.features.search_augmenters import (
    active_search_augmenters,
    install_feature_search_augmenters,
    run_search_augmenters,
    wants_query_vector,
)


def _augmenter(
    key: str,
    *,
    active: Any = True,
    answer: Optional[List[dict]] = None,
    raises: bool = False,
    wants_vector: bool = True,
) -> SearchAugmenterContribution:
    def is_active() -> bool:
        if isinstance(active, Exception):
            raise active
        return bool(active)

    def augment(request: SearchAugmentRequest) -> Optional[Sequence[dict]]:
        if raises:
            raise RuntimeError("augmenter failure")
        return answer

    return SearchAugmenterContribution(
        key=key,
        search_modes=("semantic",),
        is_active=is_active,
        augment=augment,
        wants_query_vector=wants_vector,
    )


def _loaded(name: str, *augmenters: SearchAugmenterContribution) -> Any:
    class _Feature(BaseFeature):
        def search_augmenters(self) -> Sequence[SearchAugmenterContribution]:
            return augmenters

    feature = _Feature(
        FeatureManifest(name=name, version="1.0.0", requires_sdk=">=0.7")
    )
    (loaded,) = load_features(
        [name],
        candidates={
            name: FeatureCandidate(name=name, load=lambda: lambda: feature, source="t")
        },
        provided_capabilities=frozenset(),
    )
    return loaded


@pytest.fixture(autouse=True)
def _nothing_installed() -> Iterator[None]:
    install_feature_search_augmenters([])
    yield
    install_feature_search_augmenters([])


def _request(mode: str = "semantic") -> SearchAugmentRequest:
    return SearchAugmentRequest(
        username="u", query_text="q", search_mode=mode, limit=5, query_vector=[0.1]
    )


def test_nothing_installed_nothing_active() -> None:
    assert active_search_augmenters("semantic") == ()


def test_active_follows_the_mode_and_the_switch() -> None:
    on = _augmenter("on_section", answer=[{"a": 1}])
    off = _augmenter("off_section", active=False)
    broken = _augmenter("broken_section", active=RuntimeError("no settings"))
    install_feature_search_augmenters([_loaded("example", on, off, broken)])

    assert active_search_augmenters("semantic") == (on,)
    assert active_search_augmenters("fts") == ()


def test_sections_are_answered_by_key_and_failures_add_none() -> None:
    answers = _augmenter("answers", answer=[{"a": 1}])
    silent = _augmenter("silent", answer=None)
    failing = _augmenter("failing", raises=True)

    sections = run_search_augmenters([answers, silent, failing], _request())

    assert sections == {"answers": [{"a": 1}]}


def test_the_vector_is_wanted_only_when_an_augmenter_wants_it() -> None:
    assert wants_query_vector([_augmenter("x", wants_vector=False)]) is False
    assert wants_query_vector([_augmenter("x"), _augmenter("y", wants_vector=False)])


def test_two_features_answering_under_one_key_are_refused() -> None:
    with pytest.raises(FeatureLoadError, match="'same'"):
        install_feature_search_augmenters(
            [
                _loaded("first", _augmenter("same")),
                _loaded("second", _augmenter("same")),
            ]
        )
    assert active_search_augmenters("semantic") == ()


class _Access:
    def __init__(self, visible: Sequence[str]) -> None:
        self._visible = list(visible)

    def filter_visible_memory_ids(self, ids: List[str], username: str) -> List[str]:
        return [i for i in self._visible if i in ids]


def test_memory_visibility_is_the_access_filtering_rule_in_input_order() -> None:
    app = SimpleNamespace(
        state=SimpleNamespace(access_filtering_service=_Access(["b", "a"]))
    )
    projection = CoreAccessProjectionCapability(app)

    assert projection.visible_memory_ids("u", ["a", "c", "b"]) == ["a", "b"]


def test_without_an_access_service_no_memory_is_visible() -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    assert CoreAccessProjectionCapability(app).visible_memory_ids("u", ["a"]) == []
