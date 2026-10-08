"""The shared env-var/entry-point resolution, including the branch git never ran.

``backend_plugin_loader`` is the mechanism behind every delegated seam, so a
defect here is a defect in all of them at once. Two properties matter most and
neither is obvious from reading it:

* **A configured-but-broken plugin must RAISE, never return None.** None means
  "no plugin configured", and the caller answers that by using its local
  default -- which is the exact thing a delegated deployment selected
  ``delegated`` to avoid.
* **Seams must not share resolution state.** They share the class, not the
  cache; a test that resets the content seam must not un-resolve the git seam.

Also covers the Python >= 3.10 ``entry_points(group=...)`` branch. The git
seam's own suite exercises only the 3.9 shape (it pins ``version_info``), so
deleting the 3.10 call site left that suite green -- on the interpreter CI and
production actually run.
"""

from __future__ import annotations

import sys
from typing import Any, List

import pytest

from code_indexer.server.services import backend_plugin_loader
from code_indexer.server.services.backend_plugin_loader import BackendPluginResolver


class _SeamError(RuntimeError):
    """Stand-in for a seam's own error type."""


def _make_backend() -> str:
    return "the-backend"


def _resolver(**kw: Any) -> BackendPluginResolver:
    params: dict = {
        "env_var": "CIDX_TEST_BACKEND",
        "entry_point_group": "code_indexer.test_backend",
        "error_type": _SeamError,
        "seam_name": "test",
    }
    params.update(kw)
    return BackendPluginResolver(**params)


_LOADER_MODULE = "code_indexer.server.services.backend_plugin_loader"


def _spec_for(monkeypatch: pytest.MonkeyPatch, attr: str, value: Any) -> str:
    """A 'package.module:attr' spec pointing at a STABLE module.

    Not this test module: import_module() re-imports it under its dotted name,
    producing a second module object whose functions are different objects from
    the ones pytest collected -- so an identity assertion fails even though the
    loader worked. Hanging the target on an already-imported production module
    keeps identity meaningful.
    """
    monkeypatch.setattr(backend_plugin_loader, attr, value, raising=False)
    return f"{_LOADER_MODULE}:{attr}"


class _FakeEntryPoint:
    def __init__(self, loaded: Any = _make_backend, name: str = "stub") -> None:
        self.name = name
        self._loaded = loaded

    def load(self) -> Any:
        if isinstance(self._loaded, Exception):
            raise self._loaded
        return self._loaded


@pytest.fixture(autouse=True)
def _no_ambient_plugin(monkeypatch: pytest.MonkeyPatch) -> None:
    """Deterministic regardless of what is pip-installed in the dev venv."""
    monkeypatch.delenv("CIDX_TEST_BACKEND", raising=False)
    monkeypatch.setattr(backend_plugin_loader, "entry_points", lambda **_: [])


# ── nothing configured ─────────────────────────────────────────────────────


def test_no_plugin_configured_returns_none_so_the_caller_keeps_local() -> None:
    assert _resolver().get_factory() is None
    assert _resolver().resolve() is None


# ── the env var path ───────────────────────────────────────────────────────


def test_the_env_var_resolves_a_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec_for(monkeypatch, "_a_factory", _make_backend)
    monkeypatch.setenv("CIDX_TEST_BACKEND", spec)
    assert _resolver().get_factory() is _make_backend


def test_resolve_calls_the_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec_for(monkeypatch, "_a_factory", _make_backend)
    monkeypatch.setenv("CIDX_TEST_BACKEND", spec)
    assert _resolver().resolve() == "the-backend"


@pytest.mark.parametrize(
    "spec,fragment",
    [
        ("no_colon_here", "package.module:factory"),
        ("code_indexer.nonexistent_module_xyz:f", "cannot import module"),
        (
            "code_indexer.server.services.backend_plugin_loader:not_a_real_attr",
            "has no attribute",
        ),
    ],
)
def test_a_broken_env_spec_raises_rather_than_returning_none(
    monkeypatch: pytest.MonkeyPatch, spec: str, fragment: str
) -> None:
    """None would read as "no plugin" and silently select the local default."""
    monkeypatch.setenv("CIDX_TEST_BACKEND", spec)
    with pytest.raises(_SeamError, match=fragment):
        _resolver().get_factory()


def test_a_non_callable_env_target_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    spec = _spec_for(monkeypatch, "_A_STRING", "not callable")
    monkeypatch.setenv("CIDX_TEST_BACKEND", spec)
    with pytest.raises(_SeamError, match="is not callable"):
        _resolver().get_factory()


def test_the_seams_own_error_type_is_raised() -> None:
    """Callers catch their seam's error; a bare RuntimeError would escape them."""

    class _OtherError(RuntimeError):
        pass

    import os

    os.environ["CIDX_TEST_BACKEND"] = "no_colon"
    try:
        with pytest.raises(_OtherError):
            _resolver(error_type=_OtherError).get_factory()
    finally:
        del os.environ["CIDX_TEST_BACKEND"]


# ── the entry-point path, both interpreter shapes ──────────────────────────


def test_entry_points_resolve_on_python_310_and_newer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The branch the git seam's suite never ran.

    Its only entry-point test pins version_info to 3.9, so deleting the 3.10+
    call site left that suite green -- on the interpreter CI and production
    actually use.
    """
    seen: List[Any] = []

    def fake(**kwargs: Any) -> List[Any]:
        seen.append(kwargs)
        return [_FakeEntryPoint()]

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))

    assert _resolver().get_factory() is _make_backend
    assert seen == [{"group": "code_indexer.test_backend"}], (
        "the 3.10+ path must select by group keyword"
    )


def test_entry_points_resolve_on_python_39(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake() -> dict:
        return {"code_indexer.test_backend": [_FakeEntryPoint()]}

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 9, 0))

    assert _resolver().get_factory() is _make_backend


def test_an_entry_point_that_fails_to_load_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_plugin_loader,
        "entry_points",
        lambda **_: [_FakeEntryPoint(loaded=ImportError("boom"))],
    )
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))
    with pytest.raises(_SeamError, match="failed to load"):
        _resolver().get_factory()


def test_an_entry_point_resolving_to_a_non_callable_raises(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        backend_plugin_loader,
        "entry_points",
        lambda **_: [_FakeEntryPoint(loaded="not callable")],
    )
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))
    with pytest.raises(_SeamError, match="did not resolve to a callable"):
        _resolver().get_factory()


def test_the_env_var_wins_over_an_entry_point(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _other() -> str:
        return "entry-point-backend"

    monkeypatch.setattr(
        backend_plugin_loader, "entry_points", lambda **_: [_FakeEntryPoint(_other)]
    )
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))
    spec = _spec_for(monkeypatch, "_a_factory", _make_backend)
    monkeypatch.setenv("CIDX_TEST_BACKEND", spec)
    assert _resolver().get_factory() is _make_backend


# ── caching, and independence between seams ────────────────────────────────


def test_the_lookup_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []

    def fake(**_: Any) -> List[Any]:
        calls.append(1)
        return [_FakeEntryPoint()]

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))

    resolver = _resolver()
    resolver.get_factory()
    resolver.get_factory()
    assert len(calls) == 1, "the entry-point scan must happen once, not per call"


def test_a_negative_result_is_cached_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """Otherwise every local-default startup rescans the entry points."""
    calls: List[int] = []

    def fake(**_: Any) -> List[Any]:
        calls.append(1)
        return []

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))

    resolver = _resolver()
    assert resolver.get_factory() is None
    assert resolver.get_factory() is None
    assert len(calls) == 1


def test_reset_cache_forces_a_rescan(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: List[int] = []

    def fake(**_: Any) -> List[Any]:
        calls.append(1)
        return [_FakeEntryPoint()]

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))

    resolver = _resolver()
    resolver.get_factory()
    resolver.reset_cache()
    resolver.get_factory()
    assert len(calls) == 2


def test_two_seams_do_not_share_resolution_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """They share the class, not the cache.

    A module-level cache would make resetting one seam in a test silently
    un-resolve the other, and would let a plugin registered for one group be
    handed to the wrong seam.
    """
    monkeypatch.setattr(
        backend_plugin_loader, "entry_points", lambda **_: [_FakeEntryPoint()]
    )
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))

    first = _resolver(entry_point_group="code_indexer.seam_one")
    second = _resolver(entry_point_group="code_indexer.seam_two")

    assert first.get_factory() is _make_backend
    assert second.get_factory() is _make_backend

    first.reset_cache()
    assert second._cached is not None, (
        "resetting one seam cleared another seam's resolved plugin"
    )


def test_each_seam_scans_its_own_group(monkeypatch: pytest.MonkeyPatch) -> None:
    groups: List[str] = []

    def fake(**kwargs: Any) -> List[Any]:
        groups.append(kwargs["group"])
        return []

    monkeypatch.setattr(backend_plugin_loader, "entry_points", fake)
    monkeypatch.setattr(backend_plugin_loader.sys, "version_info", (3, 12, 0))

    _resolver(entry_point_group="code_indexer.alpha").get_factory()
    _resolver(entry_point_group="code_indexer.beta").get_factory()

    assert groups == ["code_indexer.alpha", "code_indexer.beta"]


def test_the_real_interpreter_takes_the_branch_matching_its_version() -> None:
    """Guards the version gate itself, on whatever Python is running.

    Both branch tests above pin version_info, so inverting the comparison would
    leave them green. This one does not pin it.
    """
    calls: List[str] = []

    class _Probe:
        def __call__(self, **kwargs: Any) -> List[Any]:
            calls.append("kwargs")
            return []

    probe = _Probe()
    resolver = _resolver()
    original = backend_plugin_loader.entry_points
    backend_plugin_loader.entry_points = probe  # type: ignore[assignment]
    try:
        resolver.get_factory()
    finally:
        backend_plugin_loader.entry_points = original  # type: ignore[assignment]

    if sys.version_info >= (3, 10):
        assert calls == ["kwargs"], "3.10+ must call entry_points(group=...)"
    else:
        assert calls == [], "3.9 must call entry_points() with no arguments"
