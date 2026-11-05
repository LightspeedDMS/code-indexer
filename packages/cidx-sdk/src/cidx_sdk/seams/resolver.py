"""Seam resolution: env var, then entry point by mode name, else fail loud.

One mechanism for every seam. A provider is a FACTORY registered on the seam's
entry-point group (``SEAM_ENTRY_POINT_GROUPS``) under the name of the MODE it
implements (see :mod:`cidx_sdk.seams.selection`). Most factories take no
arguments -- endpoint, token and timeout come from the provider's own
environment, so the factory needs nothing from cidx's config.

Selection for a mode (first match wins):

1. The seam's env var, as ``"package.module:factory"``, for any mode but
   ``local`` (an operator override of the delegate).
2. Exactly one entry point in the seam's group satisfying the mode.
3. Nothing: :meth:`SeamResolver.factory` raises
   :class:`~cidx_sdk.errors.SeamUnavailableError` naming the seam. There is no
   built-in default -- the kernel holds no implementation of its own.

A configured provider that cannot be used raises the seam's
:class:`~cidx_sdk.seams.errors.SeamPluginError` subclass; it never reads as
"nothing configured", because the caller would then act on a choice the
operator did not make.

Resolution only: reading WHICH mode a seam is configured with is the core's
job (its config service), and so is deciding what to do with the instance.
"""

from __future__ import annotations

import logging
import os
from importlib import import_module
from importlib.metadata import entry_points as _installed_entry_points
from typing import (
    Any,
    Callable,
    Dict,
    List,
    Mapping,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    Type,
    cast,
)

from cidx_sdk.seams.errors import SeamPluginError
from cidx_sdk.seams.selection import (
    LOCAL_MODE,
    entry_names_for_mode,
    require_seam_provider,
    seam_entry_point_group,
)

logger = logging.getLogger(__name__)

#: A provider factory. Arity is the seam's (most take none).
SeamFactory = Callable[..., Any]


class EntryPointLike(Protocol):
    """The part of ``importlib.metadata.EntryPoint`` the resolver uses."""

    @property
    def name(self) -> str: ...

    def load(self) -> Any: ...


#: group -> the entry points registered on it.
EntryPointSource = Callable[[str], Sequence[EntryPointLike]]


def installed_entry_points(group: str) -> List[EntryPointLike]:
    """The entry points installed distributions register on *group*."""
    return list(_installed_entry_points(group=group))


# Extra registrations, by group: providers that are part of the running code
# but not described by installed metadata. Only a test harness or a source-tree
# run adds them (``cidx_testkit.registry``); an installed deployment has none.
# They REPLACE an installed entry of the same name, so a stale installed copy of
# an in-tree distribution cannot add a second provider.
_extra_entry_points: Dict[str, Dict[str, EntryPointLike]] = {}


def add_entry_points(group: str, entries: Sequence[EntryPointLike]) -> None:
    """Make *entries* visible on *group* in addition to installed metadata."""
    held = _extra_entry_points.setdefault(group, {})
    for entry in entries:
        held[entry.name] = entry


def remove_entry_points(group: str, names: Sequence[str]) -> None:
    """Withdraw extra registrations added with :func:`add_entry_points`."""
    held = _extra_entry_points.get(group, {})
    for name in names:
        held.pop(name, None)
    if not held:
        _extra_entry_points.pop(group, None)


def extra_entry_points() -> Mapping[str, Mapping[str, EntryPointLike]]:
    """The extra registrations currently in force (read-only view)."""
    return {group: dict(entries) for group, entries in _extra_entry_points.items()}


def _visible(group: str, source: EntryPointSource) -> List[EntryPointLike]:
    extra = _extra_entry_points.get(group, {})
    visible = [entry for entry in source(group) if entry.name not in extra]
    visible.extend(extra.values())
    return visible


class SeamResolver:
    """Env-var-then-entry-point resolution for one seam, by mode.

    ``seam`` is a key of ``SEAM_ENTRY_POINT_GROUPS`` (its group is looked up),
    unless ``entry_point_group`` is given. ``env_var`` names the operator
    override for delegate modes; ``error_type`` is raised for a provider that
    is configured but unusable. Holds its own cache, so seams never share resolution state: a test that
    resets one seam must not silently un-resolve another.
    """

    def __init__(
        self,
        *,
        seam: str,
        entry_point_group: Optional[str] = None,
        env_var: Optional[str] = None,
        error_type: Type[Exception] = SeamPluginError,
        display_name: Optional[str] = None,
        entry_point_source: Optional[EntryPointSource] = None,
        environ: Optional[Mapping[str, str]] = None,
    ) -> None:
        self.seam = seam
        # A seam outside SEAM_ENTRY_POINT_GROUPS is accepted only with an
        # explicit group (a test seam, or a core resolver predating its SDK
        # registration); it then has the local and delegated modes only.
        self.entry_point_group = entry_point_group or seam_entry_point_group(seam)
        self.env_var = env_var
        self._error_type = error_type
        self._display_name = display_name or seam
        self._source = entry_point_source or installed_entry_points
        self._environ = environ
        self._cache: Dict[str, Tuple[Optional[SeamFactory], Optional[str]]] = {}

    # -- helpers ---------------------------------------------------------------

    def _fail(self, message: str, cause: Optional[BaseException] = None) -> None:
        error = self._error_type(message)
        if cause is not None:
            raise error from cause
        raise error

    def _env_spec(self, mode: str) -> str:
        if self.env_var is None or mode == LOCAL_MODE:
            return ""
        environ = self._environ if self._environ is not None else os.environ
        return environ.get(self.env_var, "").strip()

    def _load_from_path(self, spec: str) -> SeamFactory:
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
        return cast(SeamFactory, factory)

    def _load_entry_point(self, selected: EntryPointLike) -> SeamFactory:
        try:
            factory = selected.load()
        except Exception as exc:  # surfaced as a configuration error
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
        return cast(SeamFactory, factory)

    def _candidates(self, mode: str) -> List[EntryPointLike]:
        visible = _visible(self.entry_point_group, self._source)
        wanted = entry_names_for_mode(
            self.seam, mode, [entry.name for entry in visible]
        )
        return [entry for entry in visible if entry.name in wanted]

    def _lookup(self, mode: str) -> Tuple[Optional[SeamFactory], Optional[str]]:
        cached = self._cache.get(mode)
        if cached is not None:
            return cached
        spec = self._env_spec(mode)
        result: Tuple[Optional[SeamFactory], Optional[str]]
        if spec:
            result = (self._load_from_path(spec), f"{self.env_var}={spec}")
            logger.info(
                "%s backend plugin loaded from %s=%s",
                self._display_name,
                self.env_var,
                spec,
            )
        else:
            candidates = self._candidates(mode)
            if not candidates:
                result = (None, None)
            else:
                name = require_seam_provider(
                    self.seam,
                    mode,
                    [entry.name for entry in candidates],
                    self.entry_point_group,
                )
                chosen = next(entry for entry in candidates if entry.name == name)
                result = (self._load_entry_point(chosen), f"entry point {name!r}")
                logger.info(
                    "%s backend plugin loaded from entry-point group %r (%s)",
                    self._display_name,
                    self.entry_point_group,
                    name,
                )
        self._cache[mode] = result
        return result

    # -- public API ------------------------------------------------------------

    def providers(self, mode: str) -> List[str]:
        """Names of the installed entries that satisfy *mode* (uncached)."""
        return [entry.name for entry in self._candidates(mode)]

    def find_factory(self, mode: str) -> Optional[SeamFactory]:
        """The provider factory for *mode*, or None when nothing provides it.

        Raises the seam's error type when a provider IS configured but
        unusable, and :class:`~cidx_sdk.errors.SeamUnavailableError` when more
        than one entry satisfies *mode*.
        """
        return self._lookup(mode)[0]

    def factory(self, mode: str) -> SeamFactory:
        """The provider factory for *mode*; refuses loudly when there is none.

        Raises:
            SeamUnavailableError: nothing provides *mode* (naming the seam),
                or more than one entry does.
            SeamPluginError: (the seam's subclass) a provider is configured
                but unusable.
        """
        factory = self.find_factory(mode)
        if factory is None:
            require_seam_provider(self.seam, mode, [], self.entry_point_group)
        return cast(SeamFactory, factory)

    def provenance(self, mode: str) -> Optional[str]:
        """Where *mode*'s provider came from (env var or entry name), if any."""
        return self._lookup(mode)[1]

    def resolve(self, mode: str, *args: Any) -> Any:
        """Instantiate *mode*'s provider: ``factory(mode)(*args)``."""
        return self.factory(mode)(*args)

    def reset_cache(self) -> None:
        """Forget every resolved provider. For tests."""
        self._cache.clear()
