"""Discover, validate and order feature add-ons.

Loading rules (``plans/designs/modular-endstate.md`` section 6):

* A feature is loaded only when the bootstrap config key ``enabled_features``
  names it. Installed-but-not-enabled features are discovered (their entry
  point names are listed in health) but never imported.
* A name that is enabled but not installed refuses startup
  (:class:`FeatureNotInstalledError`). No fallback, same as a delegated seam
  selected without its plugin.
* ``requires_sdk`` must include the running SDK (:class:`IncompatibleSdkError`).
* Every ``required_capabilities`` entry must be provided by the core
  (:class:`MissingCapabilityError`).
* ``depends_on`` must name enabled features; features are returned in
  dependency order, ties kept in configured order. A cycle refuses startup.
* Every contribution hook is called once here and its result type-checked, so
  a malformed add-on fails at boot rather than when a later phase first
  consumes the hook. Only ``on_startup``/``on_shutdown`` and ``health`` are
  consumed today (see ``registry``).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from importlib.metadata import distributions
from typing import (
    Callable,
    Dict,
    FrozenSet,
    List,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Type,
    TypeVar,
)

from cidx_sdk import (
    SDK_VERSION,
    BackgroundJob,
    IndexBuilderContribution,
    JobTypeContribution,
    CliCommandContribution,
    ConfigSection,
    Feature,
    FeatureDependencyError,
    FeatureLoadError,
    FeatureManifest,
    FeatureNotInstalledError,
    IncompatibleSdkError,
    Migration,
    MissingCapabilityError,
    RouterContribution,
    SearchAugmenterContribution,
    ToolContribution,
    UiPageContribution,
    VersionRange,
    WebQueryModeContribution,
)
from cidx_sdk.entry_points import FEATURES_ENTRY_POINT_GROUP
from cidx_sdk.errors import CidxSdkError
from cidx_sdk.feature import is_valid_feature_name
from cidx_sdk.seams.resolver import extra_entry_points

_T = TypeVar("_T")


@dataclass(frozen=True)
class FeatureCandidate:
    """An installed feature, not yet imported.

    Attributes:
        name: entry point name (the feature name).
        load: returns the entry point's object -- a zero-argument factory.
        source: where it came from, for messages (``module:attr``).
    """

    name: str
    load: Callable[[], object]
    source: str


@dataclass(frozen=True)
class FeatureContributions:
    """Validated hook results. Held for later phases; not consumed yet."""

    tools: Tuple[ToolContribution, ...]
    routers: Tuple[RouterContribution, ...]
    ui_pages: Tuple[UiPageContribution, ...]
    cli_commands: Tuple[CliCommandContribution, ...]
    config_section: Optional[ConfigSection]
    migrations: Tuple[Migration, ...]
    background_jobs: Tuple[BackgroundJob, ...]
    job_types: Tuple[JobTypeContribution, ...] = ()
    index_builders: Tuple[IndexBuilderContribution, ...] = ()
    web_query_modes: Tuple[WebQueryModeContribution, ...] = ()
    search_augmenters: Tuple[SearchAugmenterContribution, ...] = ()


@dataclass(frozen=True)
class LoadedFeature:
    feature: Feature
    manifest: FeatureManifest
    source: str
    contributions: FeatureContributions


def _normalized_dist_name(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def discover_installed_features() -> Dict[str, FeatureCandidate]:
    """Every entry point in ``code_indexer.features``, keyed by name.

    Walks the distributions rather than calling ``entry_points(group=...)``:
    on Python 3.10+ that call keeps only one entry point per name, so a second
    distribution registering the same feature name would silently shadow the
    first. A distribution found twice on ``sys.path`` counts once (first wins,
    as for imports).

    Reads distribution metadata from disk: call it off the event loop.

    Raises:
        FeatureLoadError: two installed distributions register the same name.
    """
    found: Dict[str, FeatureCandidate] = {}
    seen: Set[str] = set()
    for dist in distributions():
        dist_name = _normalized_dist_name(dist.metadata["Name"] or "")
        if dist_name in seen:
            continue
        seen.add(dist_name)
        for ep in dist.entry_points:
            if ep.group != FEATURES_ENTRY_POINT_GROUP:
                continue
            if ep.name in found:
                raise FeatureLoadError(
                    f"feature {ep.name!r} is registered twice in entry-point "
                    f"group {FEATURES_ENTRY_POINT_GROUP!r} "
                    f"({found[ep.name].source} and {ep.value}); uninstall one"
                )
            found[ep.name] = FeatureCandidate(
                name=ep.name, load=ep.load, source=ep.value
            )
    # A source-tree run (no installed metadata) registers what the in-tree
    # distributions declare with the SDK, as for seams
    # (``cidx_testkit.registry``); such an entry replaces an installed one of
    # the same name, so a stale installed copy never shadows the tree.
    for name, entry in extra_entry_points().get(FEATURES_ENTRY_POINT_GROUP, {}).items():
        found[name] = FeatureCandidate(
            name=name,
            load=entry.load,
            source=str(getattr(entry, "value", name)),
        )
    return found


def normalize_enabled_features(value: object) -> List[str]:
    """Validate the ``enabled_features`` config value; dedupe, keep order.

    Raises:
        FeatureLoadError: not a list of valid feature names.
    """
    if value is None:
        return []
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise FeatureLoadError(
            f"enabled_features must be a list of feature names, got {value!r}"
        )
    names: List[str] = []
    for item in value:
        if not is_valid_feature_name(item):
            raise FeatureLoadError(f"enabled_features has an invalid name {item!r}")
        if item not in names:
            names.append(item)
    return names


def _instantiate(candidate: FeatureCandidate) -> Feature:
    try:
        factory = candidate.load()
    except Exception as exc:
        raise FeatureLoadError(
            f"feature {candidate.name!r} ({candidate.source}) failed to import: "
            f"{type(exc).__name__}: {exc}"
        ) from exc
    if not callable(factory):
        raise FeatureLoadError(
            f"feature {candidate.name!r} ({candidate.source}) is not a callable factory"
        )
    try:
        feature = factory()
    except Exception as exc:
        raise FeatureLoadError(
            f"feature {candidate.name!r} factory raised {type(exc).__name__}: {exc}"
        ) from exc
    if not isinstance(feature, Feature):
        raise FeatureLoadError(
            f"feature {candidate.name!r} factory returned {type(feature).__name__}, "
            "which does not implement the cidx_sdk Feature protocol"
        )
    return feature


def _check_manifest(
    name: str,
    feature: Feature,
    sdk_version: str,
    provided_capabilities: FrozenSet[str],
) -> FeatureManifest:
    manifest = feature.manifest
    if not isinstance(manifest, FeatureManifest):
        raise FeatureLoadError(
            f"feature {name!r} manifest is not a cidx_sdk FeatureManifest"
        )
    if manifest.name != name:
        raise FeatureLoadError(
            f"feature entry point {name!r} returned a manifest named "
            f"{manifest.name!r}; they must match"
        )
    if not VersionRange.parse(manifest.requires_sdk).contains(sdk_version):
        raise IncompatibleSdkError(
            f"feature {name!r} {manifest.version} requires cidx-sdk "
            f"{manifest.requires_sdk}, but this server runs cidx-sdk {sdk_version}"
        )
    missing = sorted(set(manifest.required_capabilities) - provided_capabilities)
    if missing:
        raise MissingCapabilityError(
            f"feature {name!r} requires capabilities {missing} that this server "
            f"does not provide (provided: {sorted(provided_capabilities)})"
        )
    return manifest


def _typed_tuple(
    name: str, hook: str, value: object, item_type: Type[_T]
) -> Tuple[_T, ...]:
    if isinstance(value, (str, bytes)) or not isinstance(value, (list, tuple)):
        raise FeatureLoadError(
            f"feature {name!r} {hook}() must return a list or tuple, "
            f"got {type(value).__name__}"
        )
    items: List[_T] = []
    for item in value:
        if not isinstance(item, item_type):
            raise FeatureLoadError(
                f"feature {name!r} {hook}() returned a {type(item).__name__}; "
                f"expected {item_type.__name__}"
            )
        items.append(item)
    return tuple(items)


def _call_hook(name: str, hook: str, fn: Callable[[], object]) -> object:
    try:
        return fn()
    except CidxSdkError as exc:
        raise FeatureLoadError(f"feature {name!r} {hook}() invalid: {exc}") from exc
    except Exception as exc:
        raise FeatureLoadError(
            f"feature {name!r} {hook}() raised {type(exc).__name__}: {exc}"
        ) from exc


def _hook_items(
    name: str, feature: Feature, hook: str, item_type: Type[_T]
) -> Tuple[_T, ...]:
    value = _call_hook(name, hook, getattr(feature, hook))
    return _typed_tuple(name, hook, value, item_type)


def _collect_contributions(name: str, feature: Feature) -> FeatureContributions:
    migrations = _hook_items(name, feature, "migrations", Migration)
    versions = [migration.version for migration in migrations]
    if versions != sorted(set(versions)):
        raise FeatureLoadError(
            f"feature {name!r} migrations() versions must be strictly increasing, "
            f"got {versions}"
        )
    section = _call_hook(name, "config_section", feature.config_section)
    if section is not None and not isinstance(section, ConfigSection):
        raise FeatureLoadError(
            f"feature {name!r} config_section() must return a ConfigSection or None"
        )
    return FeatureContributions(
        tools=_hook_items(name, feature, "tools", ToolContribution),
        routers=_hook_items(name, feature, "routers", RouterContribution),
        ui_pages=_hook_items(name, feature, "ui_pages", UiPageContribution),
        cli_commands=_hook_items(name, feature, "cli_commands", CliCommandContribution),
        config_section=section,
        migrations=migrations,
        background_jobs=_hook_items(name, feature, "background_jobs", BackgroundJob),
        job_types=_hook_items(name, feature, "job_types", JobTypeContribution),
        index_builders=_hook_items(
            name, feature, "index_builders", IndexBuilderContribution
        ),
        web_query_modes=_hook_items(
            name, feature, "web_query_modes", WebQueryModeContribution
        ),
        search_augmenters=_hook_items(
            name, feature, "search_augmenters", SearchAugmenterContribution
        ),
    )


def _dependency_order(
    enabled: Sequence[str], manifests: Mapping[str, FeatureManifest]
) -> List[str]:
    for name in enabled:
        absent = [dep for dep in manifests[name].depends_on if dep not in manifests]
        if absent:
            raise FeatureDependencyError(
                f"feature {name!r} depends on {absent}, which "
                f"{'is' if len(absent) == 1 else 'are'} not enabled"
            )
    ordered: List[str] = []
    remaining = list(enabled)
    while remaining:
        ready = [
            name
            for name in remaining
            if all(dep in ordered for dep in manifests[name].depends_on)
        ]
        if not ready:
            raise FeatureDependencyError(
                f"features {sorted(remaining)} have a dependency cycle"
            )
        ordered.append(ready[0])
        remaining.remove(ready[0])
    return ordered


def _check_unique_tool_names(loaded: Sequence[LoadedFeature]) -> None:
    owner: Dict[str, str] = {}
    for item in loaded:
        for tool in item.contributions.tools:
            if tool.name in owner:
                raise FeatureLoadError(
                    f"MCP tool {tool.name!r} is contributed by both "
                    f"{owner[tool.name]!r} and {item.manifest.name!r}"
                )
            owner[tool.name] = item.manifest.name


def load_features(
    enabled: Sequence[str],
    *,
    candidates: Mapping[str, FeatureCandidate],
    sdk_version: str = SDK_VERSION,
    provided_capabilities: FrozenSet[str] = frozenset(),
) -> List[LoadedFeature]:
    """Load the *enabled* features from *candidates*, in dependency order.

    *candidates* is normally :func:`discover_installed_features`; tests pass
    their own mapping (the loader's injection seam).

    Raises:
        FeatureLoadError: any refusal; see the module docstring.
    """
    names = normalize_enabled_features(list(enabled))
    missing = [name for name in names if name not in candidates]
    if missing:
        raise FeatureNotInstalledError(
            f"enabled_features lists {missing}, but no installed distribution "
            f"provides {'it' if len(missing) == 1 else 'them'} (entry-point group "
            f"{FEATURES_ENTRY_POINT_GROUP!r}; installed: {sorted(candidates)}). "
            "Install the add-on or remove it from enabled_features."
        )
    features: Dict[str, Feature] = {}
    manifests: Dict[str, FeatureManifest] = {}
    for name in names:
        feature = _instantiate(candidates[name])
        manifests[name] = _check_manifest(
            name, feature, sdk_version, provided_capabilities
        )
        features[name] = feature
    loaded = [
        LoadedFeature(
            feature=features[name],
            manifest=manifests[name],
            source=candidates[name].source,
            contributions=_collect_contributions(name, features[name]),
        )
        for name in _dependency_order(names, manifests)
    ]
    _check_unique_tool_names(loaded)
    return loaded


def load_cli_commands(
    candidates: Mapping[str, FeatureCandidate],
    *,
    sdk_version: str = SDK_VERSION,
    provided_capabilities: FrozenSet[str] = frozenset(),
) -> Tuple[Dict[str, Tuple[CliCommandContribution, ...]], Dict[str, str]]:
    """Every installed feature's ``cli_commands()``, feature by feature.

    The CLI's view of the add-ons: a feature's commands need no server, so
    every installed one counts (not only the server's ``enabled_features``),
    and one refused add-on does not hide the others. Only the manifest and
    ``cli_commands()`` are consulted -- the server hooks (tools, routers,
    pages, migrations) are never called, so a command imports nothing of the
    server side. A feature whose ``depends_on`` names a feature that is not
    installed is refused like any other.

    Returns:
        ``(feature name -> its commands, feature name -> refusal message)``.
    """
    commands: Dict[str, Tuple[CliCommandContribution, ...]] = {}
    refusals: Dict[str, str] = {}
    for name in sorted(candidates):
        try:
            feature = _instantiate(candidates[name])
            manifest = _check_manifest(
                name, feature, sdk_version, provided_capabilities
            )
            absent = [dep for dep in manifest.depends_on if dep not in candidates]
            if absent:
                raise FeatureDependencyError(
                    f"feature {name!r} depends on {absent}, which "
                    f"{'is' if len(absent) == 1 else 'are'} not installed"
                )
            commands[name] = _hook_items(
                name, feature, "cli_commands", CliCommandContribution
            )
        except CidxSdkError as exc:
            refusals[name] = str(exc)
    return commands, refusals
