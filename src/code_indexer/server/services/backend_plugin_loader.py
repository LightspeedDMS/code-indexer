"""
Resolve a delegated backend's factory from the environment or an entry point.

One mechanism, shared by every delegated seam
---------------------------------------------
``git_backend`` was the first seam to need this: a deployment supplies the
delegated implementation out of tree, and cidx finds it by env var or entry
point. ``content_backend`` needs exactly the same resolution, and
``ripgrep_backend`` will be the third. The rule in this repo is that logic
living in two places gets extracted, so the mechanism lives here once and each
seam supplies only its own names.

What a seam supplies: the env var it reads, the entry-point group it scans, and
the exception type its callers already catch. What it gets back: a resolver with
the same contract the git seam has had since EVO-64678.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object satisfying that
seam's protocol. It takes no arguments on purpose -- endpoint, token and timeout
come from the plugin's own environment, so the factory needs nothing from cidx's
config and cidx needs no knowledge of the plugin's transport.

Selection (first match wins)
----------------------------
1. The seam's env var, as ``"package.module:factory"``.
2. An entry point in the seam's group.
3. Nothing -- the caller keeps its built-in local default.

Misconfiguration fails LOUD rather than silently returning None. A deployment
that selected ``delegated`` did so because it must NOT do that work locally --
often because the data is not even mounted in that pod -- so falling back to the
local implementation would deliver exactly what the operator was trying to
avoid, and would surface as empty results rather than an error.
"""

from __future__ import annotations

import logging
import os
import sys
from importlib import import_module
from importlib.metadata import entry_points
from typing import Any, Callable, List, Optional, Type, cast

logger = logging.getLogger(__name__)

# () -> backend instance
BackendFactory = Callable[[], Any]

_NO_PLUGIN = object()


class BackendPluginResolver:
    """Env-var-then-entry-point resolution for one delegated seam.

    Holds its own cache, so seams never share resolution state: a test that
    resets the content seam must not silently un-resolve the git seam.
    """

    def __init__(
        self,
        *,
        env_var: str,
        entry_point_group: str,
        error_type: Type[Exception],
        seam_name: str,
    ) -> None:
        self.env_var = env_var
        self.entry_point_group = entry_point_group
        self._error_type = error_type
        self._seam_name = seam_name
        self._cached: object = None

    def _fail(self, message: str, cause: Optional[BaseException] = None) -> None:
        error = self._error_type(message)
        if cause is not None:
            raise error from cause
        raise error

    def _load_from_path(self, spec: str) -> BackendFactory:
        if ":" not in spec:
            self._fail(
                f"{self.env_var}={spec!r} is not in 'package.module:factory' form"
            )
        module_name, _, attr = spec.partition(":")
        try:
            module = import_module(module_name)
        except ImportError as exc:
            self._fail(
                f"{self.env_var}={spec!r}: cannot import module {module_name!r}: {exc}",
                exc,
            )
        factory = getattr(module, attr, None)
        if factory is None:
            self._fail(
                f"{self.env_var}={spec!r}: module {module_name!r} "
                f"has no attribute {attr!r}"
            )
        if not callable(factory):
            self._fail(f"{self.env_var}={spec!r}: {attr!r} is not callable")
        return cast(BackendFactory, factory)

    def _load_from_entry_points(self) -> Optional[BackendFactory]:
        # importlib.metadata.entry_points() only accepts the `group=` keyword
        # starting in Python 3.10 (SelectableGroups). On 3.9 the function takes
        # NO parameters at all and returns a plain dict of group ->
        # List[EntryPoint]; `.get(group, [])` is the pre-3.10 equivalent lookup.
        # This repo's CI matrix still targets 3.9, so both call shapes must be
        # supported.
        candidates: List[Any]
        if sys.version_info >= (3, 10):
            candidates = list(entry_points(group=self.entry_point_group))
        else:
            candidates = list(entry_points().get(self.entry_point_group, []))
        selected = next(iter(candidates), None)
        if selected is None:
            return None
        try:
            factory = selected.load()
        except Exception as exc:  # noqa: BLE001 - surfaced as a config error
            self._fail(
                f"entry point {selected.name!r} in group "
                f"{self.entry_point_group!r} failed to load: {exc}",
                exc,
            )
        if not callable(factory):
            self._fail(
                f"entry point {selected.name!r} in group "
                f"{self.entry_point_group!r} did not resolve to a callable"
            )
        return cast(BackendFactory, factory)

    def get_factory(self) -> Optional[BackendFactory]:
        """The configured factory, or None when the caller keeps its default.

        Raises the seam's error type when a plugin IS configured but unusable --
        never a silent None, which would read as "no plugin configured".
        """
        if self._cached is not None:
            if self._cached is _NO_PLUGIN:
                return None
            return cast(BackendFactory, self._cached)

        spec = os.environ.get(self.env_var, "").strip()
        factory: Optional[BackendFactory]
        if spec:
            factory = self._load_from_path(spec)
            logger.info(
                "%s backend plugin loaded from %s=%s",
                self._seam_name,
                self.env_var,
                spec,
            )
        else:
            factory = self._load_from_entry_points()
            if factory is not None:
                logger.info(
                    "%s backend plugin loaded from entry-point group %r",
                    self._seam_name,
                    self.entry_point_group,
                )

        self._cached = factory if factory is not None else _NO_PLUGIN
        return factory

    def resolve(self) -> Optional[Any]:
        """Instantiate the delegated backend, or None if no plugin is configured.

        A configured-but-broken factory still raises -- from ``get_factory`` or
        from the factory call itself. Fail loud, never a silent local fallback.
        """
        factory = self.get_factory()
        if factory is None:
            return None
        return factory()

    def reset_cache(self) -> None:
        """Clear the resolved plugin. For tests."""
        self._cached = None
