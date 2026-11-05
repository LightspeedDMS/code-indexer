"""The ONE module of ``cidx_wiki`` that imports the code-indexer core.

The wiki is meant to import ``cidx_sdk`` and third-party packages only. A few
pure helpers and the server pieces it still needs are not in the SDK yet, so
this adapter reaches them in ``code_indexer`` and every other module of the
package imports them from here. When the SDK provides them, this file
switches to the SDK and nothing else changes. The import contract
``wiki-feature-no-core`` (``.importlinter``) counts these imports in its
ratchet baseline; no other module of the package may add one.

The distribution does not declare ``code-indexer`` as a dependency (the core
depends on ``cidx-wiki``, not the reverse), so until this adapter is gone
``cidx_wiki`` runs only where the core is installed, which is everywhere the
core loads it.

=============================  ============================================  ===========================
Name                           Used for                                      Replaced by
=============================  ============================================  ===========================
``capabilities``               content seam reads (``content_seam_source``,  ``content_seam`` capability
                               ``content_seam_delegated``) and the           (and SDK capabilities for
                               ``SERVER_PLATFORM_HOST`` slot                 each host method group)
``server_platform()``          the backend registry's wiki cache store, the  SDK capabilities as each
                               SQLite connection manager, alias reads, the   is added (see
                               activated-repository query hold and access    ``ServerPlatformHost``)
                               check, the search-event recorder, the
                               article HTML sanitiser, the Web UI admin
                               session/CSRF/elevation and golden-repos page
path confinement               ``is_readable_within_root``,                  SDK utilities
                               ``confine_relative_path``, ``has_git_segment``
``plan_fetch_batches``         batching content-seam fetches by size         an SDK utility
=============================  ============================================  ===========================

The catalog access projection is NOT reached here: the routes take it from
the ``access_projection`` capability (``server/catalog_view.py``).
"""

from __future__ import annotations

import importlib

from code_indexer.indexing.fetch_planner import (
    plan_fetch_batches as plan_fetch_batches,
)
from code_indexer.kernel import capabilities as capabilities
from code_indexer.utils.path_confinement import (
    confine_relative_path as confine_relative_path,
    has_git_segment as has_git_segment,
    is_readable_within_root as is_readable_within_root,
)

#: What :func:`server_platform` returns.
ServerPlatformHost = capabilities.ServerPlatformHost


#: The core package whose import installs the platform host.
_SERVER_PACKAGE = "code_indexer.server"


def server_platform() -> ServerPlatformHost:
    """The server's platform pieces (see the table above).

    The slot is filled when the core's server package is imported; a process
    that rendered an article before importing it (a library test) loads it
    here, once.

    Raises:
        RuntimeError: the server package could not install the host.
    """
    host = capabilities.SERVER_PLATFORM_HOST.get()
    if host is None:
        importlib.import_module(_SERVER_PACKAGE)
        host = capabilities.SERVER_PLATFORM_HOST.get()
    if host is None:
        raise RuntimeError(
            "the code-indexer server platform is not loaded in this process"
        )
    return host


def content_seam_delegated() -> bool:
    """True when repository files are read through a non-local backend."""
    return bool(capabilities.content_seam_delegated())
