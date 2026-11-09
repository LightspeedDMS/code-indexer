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
the same contract the git seam has had since issue-64678.

The contract
------------
A plugin is a zero-argument FACTORY callable returning an object satisfying that
seam's protocol. It takes no arguments on purpose -- endpoint, token and timeout
come from the plugin's own environment, so the factory needs nothing from cidx's
config and cidx needs no knowledge of the plugin's transport.

Selection (first match wins)
----------------------------
1. The seam's env var, as ``"package.module:factory"``.
2. The entry point in the seam's group named for the DELEGATED mode
   (``delegated``, or a legacy delegate name such as ``workspacemcp``; never
   ``local``, which names the local implementation in the same group).
3. Nothing -- :meth:`BackendPluginResolver.get_factory` returns None.

The mechanism itself now lives in the SDK (``cidx_sdk.seams.SeamResolver``),
which also resolves the ``local`` mode by entry name; this class keeps the
delegated-only API every seam module and its tests were written against, and
reads entry points through THIS module's ``entry_points`` and ``sys`` so test
patches of either keep working.

Misconfiguration fails LOUD rather than silently returning None. A deployment
that selected ``delegated`` did so because it must NOT do that work locally --
often because the data is not even mounted in that pod -- so falling back to the
local implementation would deliver exactly what the operator was trying to
avoid, and would surface as empty results rather than an error.
"""

from __future__ import annotations

import logging
import sys
from importlib.metadata import entry_points
from typing import Any, Callable, Dict, Optional, Type

from cidx_sdk.entry_points import SEAM_ENTRY_POINT_GROUPS
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import DELEGATED_MODE

from code_indexer.kernel.seam_resolution import module_entry_point_source

logger = logging.getLogger(__name__)

# () -> backend instance
BackendFactory = Callable[[], Any]

#: Kept importable for callers that referenced them; resolution is the SDK's.
__all__ = ["BackendFactory", "BackendPluginResolver", "entry_points", "sys"]

_SEAM_BY_GROUP: Dict[str, str] = {
    group: seam for seam, group in SEAM_ENTRY_POINT_GROUPS.items()
}


class BackendPluginResolver:
    """Env-var-then-entry-point resolution for one seam.

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
        self._seam_name = seam_name
        self._resolver = SeamResolver(
            seam=_SEAM_BY_GROUP.get(entry_point_group, seam_name),
            entry_point_group=entry_point_group,
            env_var=env_var,
            error_type=error_type,
            display_name=seam_name,
            entry_point_source=module_entry_point_source(__name__),
        )

    @property
    def seam_resolver(self) -> SeamResolver:
        """The SDK resolver behind this one, for mode-based resolution."""
        return self._resolver

    def get_factory(self) -> Optional[BackendFactory]:
        """The delegated factory, or None when no delegate is configured.

        Raises the seam's error type when a plugin IS configured but unusable --
        never a silent None, which would read as "no plugin configured".
        """
        return self._resolver.find_factory(DELEGATED_MODE)

    def resolve(self) -> Optional[Any]:
        """Instantiate the delegated backend, or None if no plugin is configured.

        A configured-but-broken factory still raises -- from ``get_factory`` or
        from the factory call itself. Fail loud, never a silent local fallback.
        """
        factory = self.get_factory()
        if factory is None:
            return None
        return factory()

    def resolve_mode(self, mode: str, *args: Any) -> Any:
        """Instantiate the provider registered for *mode* (``local``,
        ``delegated``, ...); refuses loudly, naming the seam, when none is."""
        return self._resolver.resolve(mode, *args)

    def reset_cache(self) -> None:
        """Clear the resolved plugin. For tests."""
        self._resolver.reset_cache()
