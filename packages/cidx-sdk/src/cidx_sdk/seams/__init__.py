"""Seams: the swappable boundaries between the core and where work happens.

Every seam has ONE Protocol here, ONE entry-point group
(``cidx_sdk.entry_points.SEAM_ENTRY_POINT_GROUPS``) and one conformance suite
in ``cidx-testkit``. A seam is satisfied by exactly one installed module per
configured mode: a local implementation (entry name ``local``) or a delegate
(entry name ``delegated``). Nothing installed for the configured mode stops
startup, naming the seam; there is no built-in fallback.

==================  ===================================  =======================
Seam                Protocol                             Module
==================  ===================================  =======================
content             ContentBackend                       ``seams.content``
content_writer      ContentWriter                        ``seams.content_writer``
git                 GitBackend, GitCommandRunner,        ``seams.git``
                    GitProcessRunner (local's runner)
git_invoker         GitInvoker                           ``seams.git_invoker``
ripgrep             RipgrepBackend                       ``seams.ripgrep``
search              SearchBackend                        ``seams.search``
index_artifacts     IndexStore                           ``seams.index_store``
scip                ScipExecutionBackend (ScipBackend)   ``seams.scip``
xray                XrayExecutionBackend (XrayBackend)   ``seams.xray``
clone               CloneBackend                         ``seams.clone``
cli_invoker         CliInvoker                           ``seams.cli_invoker``
==================  ===================================  =======================

Import the seam modules directly for their DTOs and constants; this package
re-exports the Protocols, the resolver and the selection helpers.
"""

from __future__ import annotations

from cidx_sdk.seams.cli_invoker import CliInvoker, CliInvokerFactory
from cidx_sdk.seams.clone import (
    CloneBackend,
    CloneBackendSettings,
    CloneDestinationExistsError,
    CloneServices,
)
from cidx_sdk.seams.content import (
    ContentBackend,
    ContentEntry,
    ContentPage,
    ContentSeamSource,
    DelegatedRepositoryUnsupportedError,
    FetchedFile,
    ManifestDeadlineExceeded,
    ManifestScopeIgnored,
)
from cidx_sdk.seams.content_writer import ContentWriter
from cidx_sdk.seams.errors import (
    CliInvokerPluginError,
    CloneBackendPluginError,
    ContentBackendPluginError,
    ContentWriterPluginError,
    GitBackendPluginError,
    GitInvokerPluginError,
    IndexArtifactsPluginError,
    RipgrepBackendPluginError,
    ScipBackendPluginError,
    SearchBackendPluginError,
    SeamPluginError,
    XrayBackendPluginError,
)
from cidx_sdk.seams.git import GitBackend, GitCommandRunner, GitProcessRunner
from cidx_sdk.seams.git_invoker import GitInvoker, GitInvokerFactory
from cidx_sdk.seams.index_store import IndexStore, IndexStoreDeleteUnsupportedError
from cidx_sdk.seams.resolver import (
    EntryPointLike,
    EntryPointSource,
    SeamFactory,
    SeamResolver,
    add_entry_points,
    installed_entry_points,
    remove_entry_points,
)
from cidx_sdk.seams.ripgrep import RipgrepBackend, RipgrepRun
from cidx_sdk.seams.scip import ScipBackend, ScipExecutionBackend
from cidx_sdk.seams.search import SearchBackend
from cidx_sdk.seams.selection import (
    DELEGATED_MODE,
    LOCAL_MODE,
    entry_names_for_mode,
    is_delegate_mode,
    require_seam_provider,
    seam_entry_point_group,
    seam_modes,
)
from cidx_sdk.seams.xray import (
    XrayBackend,
    XrayCacheBackend,
    XrayExecutionBackend,
    XrayInlineExecutionBackend,
)

__all__ = [
    "DELEGATED_MODE",
    "LOCAL_MODE",
    "CliInvoker",
    "CliInvokerFactory",
    "CliInvokerPluginError",
    "CloneBackend",
    "CloneBackendSettings",
    "CloneServices",
    "CloneBackendPluginError",
    "CloneDestinationExistsError",
    "ContentBackend",
    "ContentBackendPluginError",
    "ContentEntry",
    "ContentPage",
    "ContentSeamSource",
    "ContentWriter",
    "ContentWriterPluginError",
    "DelegatedRepositoryUnsupportedError",
    "EntryPointLike",
    "EntryPointSource",
    "FetchedFile",
    "GitBackend",
    "GitBackendPluginError",
    "GitCommandRunner",
    "GitInvoker",
    "GitInvokerFactory",
    "GitInvokerPluginError",
    "GitProcessRunner",
    "IndexArtifactsPluginError",
    "IndexStore",
    "IndexStoreDeleteUnsupportedError",
    "ManifestDeadlineExceeded",
    "ManifestScopeIgnored",
    "RipgrepBackend",
    "RipgrepBackendPluginError",
    "RipgrepRun",
    "ScipBackend",
    "ScipBackendPluginError",
    "ScipExecutionBackend",
    "SeamFactory",
    "SeamPluginError",
    "SeamResolver",
    "SearchBackend",
    "SearchBackendPluginError",
    "XrayBackend",
    "XrayBackendPluginError",
    "XrayCacheBackend",
    "XrayExecutionBackend",
    "XrayInlineExecutionBackend",
    "add_entry_points",
    "entry_names_for_mode",
    "installed_entry_points",
    "is_delegate_mode",
    "remove_entry_points",
    "require_seam_provider",
    "seam_entry_point_group",
    "seam_modes",
]
