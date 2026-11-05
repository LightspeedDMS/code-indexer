"""Feature config sections: where their values live and how they change.

A feature's ``config_section()`` declares typed keys, each bootstrap or
runtime (``cidx_sdk.ConfigKey.bootstrap``), keyed in storage by FEATURE name:

* runtime keys -> ``ServerConfig.feature_settings[feature][key]``, a runtime
  setting stored by the config service in the database (every node reads the
  committed row) and edited from the Web UI config screen or the admin API;
* bootstrap keys -> ``ServerConfig.feature_bootstrap_settings[feature][key]``,
  a ``BOOTSTRAP_KEYS`` entry kept in ``config.json`` and read before the
  database exists. Never edited by the server; an operator edits the file.

Neither is ever an environment variable. A key with no stored value reads as
its declared default.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from cidx_sdk import ConfigKey, ConfigSection

#: ServerConfig field holding runtime values: {feature: {key: value}}.
RUNTIME_FIELD = "feature_settings"
#: ServerConfig field (a bootstrap key) holding bootstrap values.
BOOTSTRAP_FIELD = "feature_bootstrap_settings"


class FeatureConfigError(ValueError):
    """An update names an unknown key, a bootstrap key, or a wrong type."""


def _stored(config: Any, field_name: str, feature: str) -> Mapping[str, Any]:
    by_feature = getattr(config, field_name, None) or {}
    values = by_feature.get(feature) if isinstance(by_feature, Mapping) else None
    return values if isinstance(values, Mapping) else {}


def section_values(feature: str, section: ConfigSection, config: Any) -> Dict[str, Any]:
    """Every key of *section*: its stored value, or its default."""
    runtime = _stored(config, RUNTIME_FIELD, feature)
    bootstrap = _stored(config, BOOTSTRAP_FIELD, feature)
    values: Dict[str, Any] = {}
    for key in section.keys:
        stored = bootstrap if key.bootstrap else runtime
        values[key.name] = stored[key.name] if key.name in stored else key.default
    return values


def _type_matches(key: ConfigKey, value: Any) -> bool:
    default = key.default
    if default is None:
        return value is None or isinstance(value, (str, int, float, bool))
    if isinstance(default, bool):
        return isinstance(value, bool)
    if isinstance(default, int):
        return isinstance(value, int) and not isinstance(value, bool)
    if isinstance(default, float):
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    return isinstance(value, type(default))


def _check_bounds(feature: str, key: ConfigKey, value: Any) -> None:
    """Refuse a numeric *value* outside the key's ``minimum``/``maximum``."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return
    if (key.minimum is not None and value < key.minimum) or (
        key.maximum is not None and value > key.maximum
    ):
        raise FeatureConfigError(
            f"{feature}.{key.name} must be between {key.minimum} and "
            f"{key.maximum}, got {value}"
        )


def validate_runtime_update(
    feature: str, section: ConfigSection, updates: Mapping[str, Any]
) -> Dict[str, Any]:
    """*updates* checked against *section* (key, kind, type, bounds); float
    keys given an int get a float. Raises :class:`FeatureConfigError` naming
    the first bad key."""
    by_name = {key.name: key for key in section.keys}
    checked: Dict[str, Any] = {}
    for name, value in updates.items():
        key = by_name.get(name)
        if key is None:
            raise FeatureConfigError(f"feature {feature!r} has no setting {name!r}")
        if key.bootstrap:
            raise FeatureConfigError(
                f"{feature}.{name} is a bootstrap setting: set it in config.json "
                f"({BOOTSTRAP_FIELD}) and restart"
            )
        if not _type_matches(key, value):
            raise FeatureConfigError(
                f"{feature}.{name} must be a {type(key.default).__name__}"
            )
        if isinstance(key.default, float) and isinstance(value, int):
            value = float(value)
        _check_bounds(feature, key, value)
        checked[name] = value
    return checked


def runtime_update_mutator(
    feature: str, section: ConfigSection, updates: Mapping[str, Any]
) -> Callable[[Any], None]:
    """Validate *updates* and return the config-service mutator that merges
    them into ``feature_settings[feature]``. The caller commits it with
    ``ConfigService.apply_audited_change`` (one compare-and-set on the
    committed row, one ``config_changed`` audit row).

    Raises:
        FeatureConfigError: an unknown key, a bootstrap key or a wrong type.
    """
    checked = validate_runtime_update(feature, section, updates)

    def _mutate(candidate: Any) -> None:
        current = dict(getattr(candidate, RUNTIME_FIELD) or {})
        merged = dict(current.get(feature) or {})
        merged.update(checked)
        current[feature] = merged
        setattr(candidate, RUNTIME_FIELD, current)

    return _mutate


def commit_runtime_update(
    service: Any,
    feature: str,
    section: ConfigSection,
    updates: Mapping[str, Any],
    actor: str,
) -> None:
    """Validate *updates* and commit them through *service* (the config
    service) as one audited runtime change: one ``config_changed`` row on
    ``feature_settings.<feature>`` for *actor*. The one write path of a
    feature's settings (the config API and the Web UI config screen).

    Raises:
        FeatureConfigError: an unknown key, a bootstrap key, a wrong type or
            a value out of bounds; nothing is committed.
    """
    service.apply_audited_change(
        runtime_update_mutator(feature, section, updates),
        actor=actor,
        target_id=f"{RUNTIME_FIELD}.{feature}",
    )


def describe_sections(
    sections: Sequence[Tuple[str, ConfigSection]], config: Any
) -> List[Dict[str, Any]]:
    """The schema and current value of every registered section, for the
    config API and the Web UI config screen."""
    described: List[Dict[str, Any]] = []
    for feature, section in sections:
        values = section_values(feature, section, config)
        described.append(
            {
                "feature": feature,
                "section": section.name,
                "title": section.title or section.name,
                "description": section.description,
                "keys": [
                    {
                        "name": key.name,
                        "label": key.label or key.name,
                        "kind": "bootstrap" if key.bootstrap else "runtime",
                        "type": type(key.default).__name__,
                        "default": key.default,
                        "value": values[key.name],
                        "description": key.description,
                        "minimum": key.minimum,
                        "maximum": key.maximum,
                        "restart_required": key.restart_required,
                    }
                    for key in section.keys
                ],
            }
        )
    return described


def find_section(
    sections: Sequence[Tuple[str, ConfigSection]], feature: str
) -> Optional[ConfigSection]:
    """The section *feature* registered, or None."""
    for name, section in sections:
        if name == feature:
            return section
    return None
