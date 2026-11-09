"""SeamResolver: env var, then entry point by MODE name, else fail loud."""

from __future__ import annotations

import sys
import types
from typing import Any, Dict, Iterator, List, Sequence

import pytest

from cidx_sdk.errors import SeamUnavailableError
from cidx_sdk.seams import (
    ContentBackendPluginError,
    SeamPluginError,
    SeamResolver,
    add_entry_points,
    entry_names_for_mode,
    remove_entry_points,
)
from cidx_sdk.seams.resolver import extra_entry_points

_GROUP = "code_indexer.content_backend"


class _Entry:
    def __init__(self, name: str, loaded: Any) -> None:
        self.name = name
        self._loaded = loaded

    def load(self) -> Any:
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


@pytest.fixture(autouse=True)
def _no_extra_registrations() -> Iterator[None]:
    """Only what each test registers: a host test session (the core's) may
    have registered its in-tree providers on the same groups."""
    held = extra_entry_points()
    for group, entries in held.items():
        remove_entry_points(group, list(entries))
    try:
        yield
    finally:
        for group, entries in held.items():
            add_entry_points(group, list(entries.values()))


def _local() -> str:
    return "local-impl"


def _delegate() -> str:
    return "delegate-impl"


class _Source:
    """An entry-point source that records the groups it was asked for."""

    def __init__(self, entries: Sequence[_Entry]) -> None:
        self.entries = list(entries)
        self.calls: List[str] = []

    def __call__(self, group: str) -> List[_Entry]:
        self.calls.append(group)
        return list(self.entries) if group == _GROUP else []


def _source(entries: Sequence[_Entry]) -> _Source:
    return _Source(entries)


def _resolver(entries: Sequence[_Entry], **kw: Any) -> SeamResolver:
    params: Dict[str, Any] = {
        "seam": "content",
        "env_var": "CIDX_EXAMPLE_BACKEND",
        "error_type": ContentBackendPluginError,
        "entry_point_source": _source(entries),
        "environ": {},
    }
    params.update(kw)
    return SeamResolver(**params)


@pytest.fixture
def module_factory() -> Iterator[str]:
    """A ``package.module:attr`` spec for the env-var path."""
    module = types.ModuleType("cidx_sdk_resolver_probe")
    setattr(module, "make", _delegate)
    setattr(module, "not_callable", 3)
    sys.modules[module.__name__] = module
    try:
        yield module.__name__
    finally:
        sys.modules.pop(module.__name__, None)


def test_local_mode_selects_the_entry_named_local() -> None:
    resolver = _resolver([_Entry("local", _local), _Entry("delegated", _delegate)])
    assert resolver.resolve("local") == "local-impl"
    assert resolver.resolve("delegated") == "delegate-impl"


def test_a_legacy_delegate_name_satisfies_delegated_but_never_local() -> None:
    resolver = _resolver([_Entry("local", _local), _Entry("workspacemcp", _delegate)])
    assert resolver.resolve("delegated") == "delegate-impl"
    assert resolver.providers("local") == ["local"]


def test_another_modes_name_is_not_a_legacy_delegate() -> None:
    names = ["local", "ontap", "cow-daemon", "workspacemcp"]
    assert entry_names_for_mode("clone", "delegated", names) == ["workspacemcp"]
    assert entry_names_for_mode("clone", "ontap", names) == ["ontap"]


def test_no_provider_for_the_mode_fails_loud_naming_the_seam() -> None:
    resolver = _resolver([_Entry("delegated", _delegate)])
    with pytest.raises(SeamUnavailableError, match="seam 'content'.*'local'"):
        resolver.factory("local")
    assert resolver.find_factory("local") is None


def test_two_providers_for_one_mode_fail_loud() -> None:
    resolver = _resolver([_Entry("gitmcp", _delegate), _Entry("other", _delegate)])
    with pytest.raises(SeamUnavailableError, match="exactly one"):
        resolver.find_factory("delegated")


def test_a_provider_that_fails_to_load_raises_the_seam_error() -> None:
    resolver = _resolver([_Entry("local", ImportError("boom"))])
    with pytest.raises(ContentBackendPluginError, match="failed to load: boom"):
        resolver.factory("local")


def test_a_provider_that_is_not_callable_raises_the_seam_error() -> None:
    resolver = _resolver([_Entry("local", 42)])
    with pytest.raises(ContentBackendPluginError, match="did not resolve"):
        resolver.factory("local")


def test_the_env_var_overrides_a_delegate_but_not_local(module_factory: str) -> None:
    resolver = _resolver(
        [_Entry("local", _local), _Entry("delegated", _local)],
        environ={"CIDX_EXAMPLE_BACKEND": f"{module_factory}:make"},
    )
    assert resolver.resolve("delegated") == "delegate-impl"
    assert resolver.resolve("local") == "local-impl"
    assert resolver.provenance("delegated") == (
        f"CIDX_EXAMPLE_BACKEND={module_factory}:make"
    )


@pytest.mark.parametrize(
    "attr, fragment",
    [
        ("", "package.module:factory"),
        (":missing", "has no attribute"),
        (":not_callable", "is not callable"),
    ],
)
def test_a_broken_env_spec_raises(
    module_factory: str, attr: str, fragment: str
) -> None:
    spec = module_factory + attr if attr else "no_colon"
    resolver = _resolver([], environ={"CIDX_EXAMPLE_BACKEND": spec})
    with pytest.raises(ContentBackendPluginError, match=fragment):
        resolver.find_factory("delegated")


def test_an_unimportable_env_module_raises() -> None:
    resolver = _resolver(
        [], environ={"CIDX_EXAMPLE_BACKEND": "cidx_sdk_no_such_module_xyz:f"}
    )
    with pytest.raises(ContentBackendPluginError, match="cannot import module"):
        resolver.find_factory("delegated")


def test_lookups_are_cached_per_mode_and_reset_rescans() -> None:
    source = _source([_Entry("local", _local)])
    resolver = _resolver([], entry_point_source=source)
    resolver.find_factory("local")
    resolver.find_factory("local")
    resolver.find_factory("delegated")
    resolver.find_factory("delegated")
    assert source.calls == [_GROUP, _GROUP]
    resolver.reset_cache()
    resolver.find_factory("local")
    assert len(source.calls) == 3


def test_extra_registrations_add_to_and_replace_installed_entries() -> None:
    resolver = _resolver([_Entry("local", _delegate)])
    add_entry_points(_GROUP, [_Entry("local", _local)])
    try:
        assert resolver.resolve("local") == "local-impl"
        assert "local" in extra_entry_points()[_GROUP]
    finally:
        remove_entry_points(_GROUP, ["local"])
    assert _GROUP not in extra_entry_points()


def test_the_seam_errors_are_runtime_errors_and_seam_unavailable() -> None:
    assert issubclass(ContentBackendPluginError, SeamPluginError)
    assert issubclass(SeamPluginError, SeamUnavailableError)
    assert issubclass(ContentBackendPluginError, RuntimeError)


def test_an_unknown_seam_is_refused_at_construction() -> None:
    with pytest.raises(SeamUnavailableError, match="unknown seam"):
        SeamResolver(seam="no-such-seam")
