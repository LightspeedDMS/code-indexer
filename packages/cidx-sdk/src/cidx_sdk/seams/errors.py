"""Error types of the seam resolvers.

:class:`cidx_sdk.errors.SeamUnavailableError` is the refusal when no module
satisfies a configured seam. :class:`SeamPluginError` is its sibling for a
provider that IS configured but unusable (cannot be imported, is not callable,
or its factory fails). Each seam has its own subclass, so a caller can catch
one seam's misconfiguration; all of them stop startup.
"""

from __future__ import annotations

from cidx_sdk.errors import SeamUnavailableError


class SeamPluginError(SeamUnavailableError):
    """A seam provider was configured but could not be loaded or called."""


class ContentBackendPluginError(SeamPluginError):
    """The content seam's provider is configured but unusable."""


class ContentWriterPluginError(SeamPluginError):
    """The content writer seam's provider is configured but unusable."""


class GitBackendPluginError(SeamPluginError):
    """The git seam's provider is configured but unusable."""


class GitInvokerPluginError(SeamPluginError):
    """The git invoker seam's provider is configured but unusable."""


class RipgrepBackendPluginError(SeamPluginError):
    """The ripgrep seam's provider is configured but unusable."""


class SearchBackendPluginError(SeamPluginError):
    """The search seam's provider is configured but unusable."""


class IndexArtifactsPluginError(SeamPluginError):
    """The index artifacts seam's provider is configured but unusable."""


class ScipBackendPluginError(SeamPluginError):
    """The SCIP seam's provider is configured but unusable."""


class XrayBackendPluginError(SeamPluginError):
    """The X-Ray seam's provider is configured but unusable."""


class CloneBackendPluginError(SeamPluginError):
    """The clone seam's provider is configured but unusable."""


class CliInvokerPluginError(SeamPluginError):
    """The CLI invoker seam's provider is configured but unusable."""
