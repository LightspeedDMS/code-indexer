"""Loaded features at runtime: startup/shutdown hooks and health.

One :class:`FeatureRegistry` per server process, built by the lifespan
(``server/startup/feature_wiring.py``). The process-wide holder below exists
for the health surfaces, which have no ``app`` handle. It holds what THIS
process has installed and loaded -- deliberately per node, like the audit
counter's node id: each node reports its own add-ons, so a pod built from the
wrong profile shows up as a named gap on that node. It holds no
cross-request state.

The registry also answers what the contribution consumers need at runtime:
each feature's context (one per feature, capabilities resolved on first use),
config section and background jobs, and the health reports a feature pushes
through the ``health`` capability. The contributions themselves are installed
by ``server/startup/feature_wiring.py`` (tools, routers, pages, config,
migrations, jobs).
"""

from __future__ import annotations

import logging
import threading
from typing import (
    Any,
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Tuple,
)

from cidx_sdk import (
    SDK_VERSION,
    BackgroundJob,
    CapabilityUnavailableError,
    ConfigSection,
    FeatureStartupError,
    HealthReport,
)
from cidx_sdk.feature import HEALTHY, UNHEALTHY

from code_indexer.server.features.jobs import describe_jobs
from code_indexer.server.features.loader import LoadedFeature

logger = logging.getLogger(__name__)

_JSON_SCALARS = (str, int, float, bool, type(None))


#: ``provider(feature_name) -> capability object`` (see ``capabilities.py``).
CapabilityProvider = Callable[[str], object]


class CoreFeatureContext:
    """The core's :class:`cidx_sdk.FeatureContext` for one feature.

    Each capability object is built on first request and then reused, so a
    feature gets the same object every time it asks.
    """

    def __init__(
        self,
        feature_name: str,
        capabilities: Mapping[str, CapabilityProvider],
        core_version: str,
    ) -> None:
        self._feature_name = feature_name
        self._providers = dict(capabilities)
        self._core_version = core_version
        self._resolved: Dict[str, object] = {}
        self._lock = threading.Lock()

    @property
    def feature_name(self) -> str:
        return self._feature_name

    @property
    def sdk_version(self) -> str:
        return SDK_VERSION

    @property
    def core_version(self) -> str:
        return self._core_version

    def provided_capabilities(self) -> FrozenSet[str]:
        return frozenset(self._providers)

    def has_capability(self, name: str) -> bool:
        return name in self._providers

    def capability(self, name: str) -> object:
        provider = self._providers.get(name)
        if provider is None:
            raise CapabilityUnavailableError(
                f"capability {name!r} is not provided to feature {self._feature_name!r}"
            )
        with self._lock:
            if name not in self._resolved:
                self._resolved[name] = provider(self._feature_name)
            return self._resolved[name]


def _json_detail(detail: Mapping[str, object]) -> Dict[str, Any]:
    return {
        str(key): value if isinstance(value, _JSON_SCALARS) else str(value)
        for key, value in detail.items()
    }


def _report_dict(report: HealthReport) -> Dict[str, Any]:
    return {
        "component": report.component,
        "status": report.status,
        "detail": _json_detail(report.detail),
        "reasons": [str(reason) for reason in report.reasons],
    }


class FeatureRegistry:
    """The features this process loaded, in dependency order."""

    def __init__(
        self,
        loaded: Sequence[LoadedFeature],
        *,
        installed: Sequence[str],
        capabilities: Optional[Mapping[str, CapabilityProvider]] = None,
        core_version: str,
    ) -> None:
        self._loaded = tuple(loaded)
        self._installed = tuple(sorted(installed))
        self._capabilities: Dict[str, CapabilityProvider] = dict(capabilities or {})
        self._core_version = core_version
        self._lock = threading.Lock()
        self._start_attempted = False
        self._stopped = False
        self._started: List[LoadedFeature] = []
        self._contexts = {
            item.manifest.name: CoreFeatureContext(
                item.manifest.name, self._capabilities, core_version
            )
            for item in self._loaded
        }
        # Reports pushed through the ``health`` capability. Per node by
        # design, like every health input: /health describes this node.
        self._reports_lock = threading.Lock()
        self._published: Dict[str, Dict[str, HealthReport]] = {}

    @property
    def loaded(self) -> Sequence[LoadedFeature]:
        return self._loaded

    @property
    def provided_capabilities(self) -> FrozenSet[str]:
        return frozenset(self._capabilities)

    def context_for(self, name: str) -> CoreFeatureContext:
        """The one context of loaded feature *name* (a fresh one otherwise)."""
        ctx = self._contexts.get(name)
        if ctx is None:
            return CoreFeatureContext(name, self._capabilities, self._core_version)
        return ctx

    def get(self, name: str) -> Optional[LoadedFeature]:
        for item in self._loaded:
            if item.manifest.name == name:
                return item
        return None

    def config_sections(self) -> List[Tuple[str, ConfigSection]]:
        """``(feature name, section)`` for every loaded feature with one:
        the sections the config screen and the config API show."""
        return [
            (item.manifest.name, item.contributions.config_section)
            for item in self._loaded
            if item.contributions.config_section is not None
        ]

    def config_section_of(self, name: str) -> Optional[ConfigSection]:
        item = self.get(name)
        return None if item is None else item.contributions.config_section

    def background_job_of(
        self, feature: str, job_name: str
    ) -> Tuple[BackgroundJob, CoreFeatureContext]:
        """Feature *feature*'s registered job *job_name* and its context.

        Raises:
            KeyError: no such feature or job.
        """
        item = self.get(feature)
        if item is not None:
            for job in item.contributions.background_jobs:
                if job.name == job_name:
                    return job, self.context_for(feature)
        raise KeyError(f"feature {feature!r} registers no background job {job_name!r}")

    def publish_report(self, feature: str, report: HealthReport) -> None:
        with self._reports_lock:
            self._published.setdefault(feature, {})[report.component] = report

    def withdraw_report(self, feature: str, component: str) -> None:
        with self._reports_lock:
            self._published.get(feature, {}).pop(component, None)

    def _published_reports(self, feature: str) -> List[HealthReport]:
        with self._reports_lock:
            return list(self._published.get(feature, {}).values())

    def start(self) -> None:
        """Call each feature's ``on_startup`` once, in dependency order.

        Blocking: call from a worker thread. A second call is a no-op. If a
        hook raises, the features already started are stopped in reverse
        order and :class:`FeatureStartupError` is raised (startup must stop).
        """
        with self._lock:
            if self._start_attempted:
                return
            self._start_attempted = True
            for item in self._loaded:
                name = item.manifest.name
                try:
                    item.feature.on_startup(self.context_for(name))
                except Exception as exc:
                    logger.error(
                        "feature %s on_startup failed (%s); stopping %d started "
                        "feature(s)",
                        name,
                        type(exc).__name__,
                        len(self._started),
                    )
                    self._stop_started()
                    raise FeatureStartupError(
                        f"feature {name!r} on_startup raised "
                        f"{type(exc).__name__}: {exc}"
                    ) from exc
                self._started.append(item)
                logger.info("feature %s %s started", name, item.manifest.version)

    def stop(self) -> None:
        """Call ``on_shutdown`` once per started feature, in reverse order.

        Blocking: call from a worker thread. Never raises: a failing hook is
        logged and the rest still run.
        """
        with self._lock:
            self._stop_started()

    def _stop_started(self) -> None:
        if self._stopped:
            return
        self._stopped = True
        while self._started:
            item = self._started.pop()
            name = item.manifest.name
            try:
                item.feature.on_shutdown(self.context_for(name))
            except Exception as exc:
                logger.warning(
                    "feature %s on_shutdown failed (%s)", name, type(exc).__name__
                )

    def is_started(self, name: str) -> bool:
        return any(item.manifest.name == name for item in self._started)

    def _feature_health(self, item: LoadedFeature) -> List[Dict[str, Any]]:
        published = [
            _report_dict(report)
            for report in self._published_reports(item.manifest.name)
        ]
        return self._hook_health(item) + published

    def _hook_health(self, item: LoadedFeature) -> List[Dict[str, Any]]:
        name = item.manifest.name
        try:
            reports = item.feature.health()
            if isinstance(reports, (str, bytes)) or not isinstance(
                reports, (list, tuple)
            ):
                raise TypeError("health() must return a list or tuple")
            return [
                _report_dict(report)
                if isinstance(report, HealthReport)
                else _report_dict(
                    HealthReport(
                        component=name,
                        status=UNHEALTHY,
                        reasons=(
                            f"health() returned {type(report).__name__}, "
                            "not a HealthReport",
                        ),
                    )
                )
                for report in reports
            ]
        except Exception as exc:
            return [
                _report_dict(
                    HealthReport(
                        component=name,
                        status=UNHEALTHY,
                        reasons=(f"health() raised {type(exc).__name__}",),
                    )
                )
            ]

    def health_payload(self) -> Dict[str, Any]:
        """The ``features`` block of the health responses.

        A report that is not healthy makes the overall status DEGRADED, never
        UNHEALTHY (:func:`degraded_reasons`): an add-on's trouble must not
        take the node out of the load balancer.
        """
        loaded_names = {item.manifest.name for item in self._loaded}
        return {
            "sdk_version": SDK_VERSION,
            "loaded": [
                {
                    "name": item.manifest.name,
                    "version": item.manifest.version,
                    "requires_sdk": item.manifest.requires_sdk,
                    "source": item.source,
                    "started": self.is_started(item.manifest.name),
                    "background_jobs": describe_jobs(
                        item.contributions.background_jobs
                    ),
                    "health": self._feature_health(item),
                }
                for item in self._loaded
            ],
            "installed_not_enabled": [
                name for name in self._installed if name not in loaded_names
            ],
        }


_active_lock = threading.Lock()
_active_registry: Optional[FeatureRegistry] = None


def set_active_registry(registry: Optional[FeatureRegistry]) -> None:
    """Publish (or clear, with None) this process's registry for health."""
    global _active_registry
    with _active_lock:
        _active_registry = registry


def get_active_registry() -> Optional[FeatureRegistry]:
    with _active_lock:
        return _active_registry


def feature_offered(name: str) -> bool:
    """Whether the core should offer what feature *name* serves (a link, a
    button): False only when this process's registry is active and *name* is
    not loaded in it. With no active registry (a process that never loaded
    features, e.g. a test app) nothing is known and the answer is True."""
    registry = get_active_registry()
    return registry is None or registry.get(name) is not None


def feature_health_payload() -> Optional[Dict[str, Any]]:
    """The health ``features`` block, or None when no registry is active.

    None means the lifespan has not loaded features in this process (for
    example a test app without a lifespan), not "no features".
    """
    registry = get_active_registry()
    if registry is None:
        return None
    return registry.health_payload()


def degraded_reasons(payload: Optional[Mapping[str, Any]]) -> List[str]:
    """One failure reason per feature report that is not healthy in the
    ``features`` health block *payload* (None: no registry, no reasons)."""
    if not payload:
        return []
    reasons: List[str] = []
    for entry in payload.get("loaded", ()):
        for report in entry.get("health", ()):
            if report.get("status") == HEALTHY:
                continue
            detail = "; ".join(report.get("reasons") or ()) or report.get("status")
            reasons.append(
                f"Feature {entry.get('name')} ({report.get('component')}): {detail}"
            )
    return reasons
