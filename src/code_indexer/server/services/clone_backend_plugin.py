"""
The clone seam's resolver: which installed module provides ``clone_backend``.

``clone_backend`` names a MODE (``local``, ``ontap``, ``cow-daemon``,
``delegated``); each mode is a provider on ``code_indexer.clone_backend``
registered under that name:

==============  ================================================  =====================
Mode            Provider                                          Distribution
==============  ================================================  =====================
``local``       ``cidx_backend_workspace.clone:make_local_...``   cidx-backend-workspace
``ontap``       ``cidx_backend_workspace.clone:make_ontap_...``   cidx-backend-workspace
``cow-daemon``  ``cidx_backend_workspace.clone:make_cow_daemon_`` cidx-backend-workspace
``delegated``   ``code_indexer...clone_backend:make_delegated_``  the core, until
                                                                  cidx-delegate-workspace
==============  ================================================  =====================

A provider is a factory taking ONE argument, ``CloneBackendSettings`` (the
mode's config section plus the core runtime, ``core_clone_services()``).
Nothing installed for the mode -- or two modules for it -- refuses with
``SeamUnavailableError`` naming the seam; a provider that cannot be loaded
raises :class:`CloneBackendPluginError`. There is no built-in fallback.

No environment override: unlike the other seams the clone resolver reads no
``package.module:factory`` env var, because ``CIDX_CLONE_BACKEND`` already
carries a MODE NAME in ``scripts/cluster-migrate.sh``; read as a factory path
it would refuse every non-local mode.
"""

from __future__ import annotations

import sys
from importlib.metadata import entry_points
from typing import List

from cidx_sdk.entry_points import CLONE_BACKEND_ENTRY_POINT_GROUP
from cidx_sdk.seams.clone import CloneBackend, CloneBackendSettings
from cidx_sdk.seams.errors import CloneBackendPluginError as CloneBackendPluginError
from cidx_sdk.seams.resolver import SeamResolver
from cidx_sdk.seams.selection import LOCAL_MODE

from code_indexer.kernel.seam_resolution import module_entry_point_source

ENTRY_POINT_GROUP = CLONE_BACKEND_ENTRY_POINT_GROUP

#: Kept importable: tests patch ``entry_points`` here to control resolution.
__all__ = [
    "ENTRY_POINT_GROUP",
    "CloneBackendPluginError",
    "entry_points",
    "installed_clone_providers",
    "local_clone_backend",
    "reset_cache",
    "resolve_clone_backend",
    "sys",
]

_resolver = SeamResolver(
    seam="clone",
    entry_point_group=ENTRY_POINT_GROUP,
    error_type=CloneBackendPluginError,
    display_name="clone",
    entry_point_source=module_entry_point_source(__name__),
)


def resolve_clone_backend(mode: str, settings: CloneBackendSettings) -> CloneBackend:
    """Instantiate the clone backend installed for *mode* with *settings*.

    Raises:
        SeamUnavailableError: nothing (or more than one module) provides *mode*.
        CloneBackendPluginError: the provider is installed but unusable.
        ValueError: *settings* lacks the config section *mode* needs.
    """
    backend: CloneBackend = _resolver.resolve(mode, settings)
    return backend


def local_clone_backend(versioned_base: str = "") -> CloneBackend:
    """The ``local`` mode's backend on the core runtime, whatever mode the
    server is configured with: for copies that are pod-local by nature (the
    shared ``.versioned`` registry a delegated backend composes, an index
    copied between two directories of this pod)."""
    from code_indexer.server.storage.shared.clone_backend import core_clone_services

    return resolve_clone_backend(
        LOCAL_MODE,
        CloneBackendSettings(
            services=core_clone_services(), versioned_base=versioned_base
        ),
    )


def installed_clone_providers(mode: str) -> List[str]:
    """Names of the installed entries that provide clone mode *mode*."""
    return _resolver.providers(mode)


def reset_cache() -> None:
    """Clear the resolved providers. For tests."""
    _resolver.reset_cache()
