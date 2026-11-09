"""Error types shared by the core and its add-ons.

Every refusal the core's feature loader can raise is a :class:`FeatureLoadError`,
so a caller (the server lifespan, a test kit) can catch the whole family. They
derive from ``RuntimeError`` because a refusal stops server startup exactly
like a delegated seam selected without its plugin does.
"""

from __future__ import annotations


class CidxSdkError(Exception):
    """Base class for every error defined by the SDK."""


class InvalidVersionError(CidxSdkError, ValueError):
    """A version string or version range could not be parsed."""


class InvalidManifestError(CidxSdkError, ValueError):
    """A feature manifest or contribution violates the contract."""


class FeatureLoadError(CidxSdkError, RuntimeError):
    """The core refused to load a feature. Stops server startup."""


class FeatureNotInstalledError(FeatureLoadError):
    """A feature is configured on but no installed distribution provides it."""


class IncompatibleSdkError(FeatureLoadError):
    """A feature's ``requires_sdk`` range excludes the running SDK."""


class FeatureDependencyError(FeatureLoadError):
    """A feature depends on a feature that is not enabled, or on itself."""


class MissingCapabilityError(FeatureLoadError):
    """A feature requires a capability the running core does not provide."""


class FeatureStartupError(FeatureLoadError):
    """A feature's ``on_startup`` hook raised."""


class CapabilityUnavailableError(CidxSdkError, LookupError):
    """``FeatureContext.capability`` was asked for a capability it lacks."""


class SeamUnavailableError(CidxSdkError, RuntimeError):
    """A seam is configured but no installed module implements it.

    Raised by :func:`cidx_sdk.seams.require_seam_provider`. Startup must stop:
    there is no built-in fallback for a seam.
    """
