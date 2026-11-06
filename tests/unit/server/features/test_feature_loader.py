"""Feature loader: discovery, refusals, ordering (modular plan phase 3)."""

from __future__ import annotations

from functools import partial
from pathlib import Path
from typing import Any, Callable, Dict, List, Sequence

import pytest
from cidx_sdk import (
    BaseFeature,
    FeatureDependencyError,
    FeatureLoadError,
    FeatureManifest,
    FeatureNotInstalledError,
    IncompatibleSdkError,
    MissingCapabilityError,
    ToolContribution,
)
from cidx_sdk.capabilities import CONFIG

from code_indexer.server.features.loader import (
    FeatureCandidate,
    discover_installed_features,
    load_features,
    normalize_enabled_features,
)

from .conftest import write_distribution


def _candidate(name: str, factory: Callable[[], object]) -> FeatureCandidate:
    return FeatureCandidate(name=name, load=lambda: factory, source=f"inline:{name}")


def _feature(name: str, **manifest: Any) -> BaseFeature:
    return BaseFeature(
        FeatureManifest(name=name, version="1.0.0", requires_sdk=">=0.1", **manifest)
    )


def test_discovers_installed_entry_points(installed_sample_features: Dict[str, str]):
    found = discover_installed_features()
    for name, target in installed_sample_features.items():
        assert found[name].source == target


def test_loads_an_installed_feature_through_its_entry_point(
    installed_sample_features: Dict[str, str],
):
    loaded = load_features(["sample"], candidates=discover_installed_features())
    assert [item.manifest.name for item in loaded] == ["sample"]
    item = loaded[0]
    assert item.manifest.version == "1.2.0"
    assert item.source == "cidx_sample_feature:make_sample"
    assert [t.name for t in item.contributions.tools] == ["sample_ping"]
    assert [m.version for m in item.contributions.migrations] == [1]


def test_enabled_but_not_installed_refuses(installed_sample_features: Dict[str, str]):
    with pytest.raises(FeatureNotInstalledError, match="'cidx-absent'"):
        load_features(
            ["sample", "cidx-absent"], candidates=discover_installed_features()
        )


def test_incompatible_sdk_refuses(installed_sample_features: Dict[str, str]):
    with pytest.raises(IncompatibleSdkError, match=">=99.0"):
        load_features(["sample-future-sdk"], candidates=discover_installed_features())


def test_dependency_order_overrides_configured_order(
    installed_sample_features: Dict[str, str],
):
    loaded = load_features(
        ["sample-dependent", "sample"], candidates=discover_installed_features()
    )
    assert [item.manifest.name for item in loaded] == ["sample", "sample-dependent"]


def test_dependency_not_enabled_refuses(installed_sample_features: Dict[str, str]):
    with pytest.raises(FeatureDependencyError, match="'sample-dependent'"):
        load_features(["sample-dependent"], candidates=discover_installed_features())


def test_installed_but_not_enabled_is_never_imported() -> None:
    def explode() -> object:
        raise AssertionError("a feature that is not enabled was imported")

    candidates = {
        "quiet": FeatureCandidate(name="quiet", load=explode, source="x:y"),
    }
    assert load_features([], candidates=candidates) == []


def test_dependency_cycle_refuses() -> None:
    candidates = {
        "a": _candidate("a", lambda: _feature("a", depends_on=("b",))),
        "b": _candidate("b", lambda: _feature("b", depends_on=("a",))),
    }
    with pytest.raises(FeatureDependencyError, match="cycle"):
        load_features(["a", "b"], candidates=candidates)


def test_ties_keep_configured_order() -> None:
    candidates = {n: _candidate(n, partial(_feature, n)) for n in ("c", "a", "b")}
    loaded = load_features(["c", "a", "b"], candidates=candidates)
    assert [item.manifest.name for item in loaded] == ["c", "a", "b"]


def test_required_capability_must_be_provided() -> None:
    candidates = {
        "cap": _candidate(
            "cap", lambda: _feature("cap", required_capabilities=(CONFIG,))
        )
    }
    with pytest.raises(MissingCapabilityError, match="config"):
        load_features(["cap"], candidates=candidates)
    loaded = load_features(
        ["cap"], candidates=candidates, provided_capabilities=frozenset({CONFIG})
    )
    assert loaded[0].manifest.name == "cap"


class _ToolFeature(BaseFeature):
    def __init__(self, name: str, tool: str) -> None:
        super().__init__(
            FeatureManifest(name=name, version="1.0.0", requires_sdk=">=0.1")
        )
        self._tool = tool

    def tools(self) -> Sequence[ToolContribution]:
        return (ToolContribution(self._tool, lambda p, u: {}, ""),)


class _BadHookFeature(BaseFeature):
    def routers(self) -> Any:
        return ["not-a-router-contribution"]


class _RaisingHookFeature(BaseFeature):
    def ui_pages(self) -> Any:
        raise ValueError("boom")


def _bad_hook() -> BaseFeature:
    return _BadHookFeature(
        FeatureManifest(name="bad", version="1.0.0", requires_sdk=">=0.1")
    )


def _raising_hook() -> BaseFeature:
    return _RaisingHookFeature(
        FeatureManifest(name="bad", version="1.0.0", requires_sdk=">=0.1")
    )


def _raising_factory() -> object:
    raise RuntimeError("cannot build")


@pytest.mark.parametrize(
    "factory, message",
    [
        (lambda: _feature("other"), "must match"),
        (lambda: object(), "does not implement"),
        (_raising_factory, "factory raised RuntimeError"),
        (_bad_hook, "routers\\(\\) returned a str"),
        (_raising_hook, "ui_pages\\(\\) raised ValueError"),
    ],
)
def test_malformed_features_refuse(factory: Callable[[], object], message: str):
    with pytest.raises(FeatureLoadError, match=message):
        load_features(["bad"], candidates={"bad": _candidate("bad", factory)})


def test_non_callable_entry_point_refuses() -> None:
    candidate = FeatureCandidate(name="bad", load=lambda: 42, source="m:CONST")
    with pytest.raises(FeatureLoadError, match="not a callable factory"):
        load_features(["bad"], candidates={"bad": candidate})


def test_duplicate_tool_names_refuse() -> None:
    candidates = {
        "one": _candidate("one", lambda: _ToolFeature("one", "same_tool")),
        "two": _candidate("two", lambda: _ToolFeature("two", "same_tool")),
    }
    with pytest.raises(FeatureLoadError, match="same_tool"):
        load_features(["one", "two"], candidates=candidates)


def test_same_name_registered_by_two_distributions_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    site = tmp_path / "site"
    write_distribution(site, "dist-one", {"twice": "pkg_one:make"})
    write_distribution(site, "dist-two", {"twice": "pkg_two:make"})
    monkeypatch.syspath_prepend(str(site))
    with pytest.raises(FeatureLoadError, match="registered twice"):
        discover_installed_features()


@pytest.mark.parametrize("value", ["sample", [""], ["Bad"], [3], {"a": 1}])
def test_enabled_features_value_is_validated(value: object) -> None:
    with pytest.raises(FeatureLoadError):
        normalize_enabled_features(value)


def test_enabled_features_dedupes_keeping_order() -> None:
    names: List[str] = normalize_enabled_features(["b", "a", "b"])
    assert names == ["b", "a"]
    assert normalize_enabled_features(None) == []
