"""Where SIEM delivery may send: a documented SecOps region, or (only behind
the non-production fault-injection gate) a loopback test receiver.

There is no free-form endpoint or URL field: the origin is DERIVED from a
region in :data:`SECOPS_REGIONS`, and the parent path is built from three
validated single path segments.  Validation errors name the FIELD only,
never the value.
"""

from __future__ import annotations

import hashlib
import ipaddress
import re
from dataclasses import dataclass
from typing import FrozenSet, Optional, Tuple
from urllib.parse import quote, urlsplit

from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

# Chronicle API regional endpoints (https://chronicle.<region>.rep.googleapis.com):
# EVERY region in the regional-endpoint table of Google's "Migrate to
# Chronicle API" guide,
# https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide
# (retrieved 2026-10-01; the "Migrate from legacy SIEM API to Chronicle API"
# page lists the same set).  A region missing here cannot be configured.
SECOPS_REGIONS: FrozenSet[str] = frozenset(
    {
        "us",
        "eu",
        "africa-south1",
        "asia-northeast1",
        "asia-south1",
        "asia-southeast1",
        "asia-southeast2",
        "australia-southeast1",
        "europe-west2",
        "europe-west3",
        "europe-west6",
        "europe-west9",
        "europe-west12",
        "me-central1",
        "me-central2",
        "me-west1",
        "northamerica-northeast2",
        "southamerica-east1",
    }
)

# The token_uri Google writes into service-account key files.
SECOPS_TOKEN_URI = "https://oauth2.googleapis.com/token"
SECOPS_SCOPE = "https://www.googleapis.com/auth/chronicle"
API_VERSIONS: FrozenSet[str] = frozenset({"v1", "v1beta", "v1alpha"})
MAX_BATCH_EVENTS_LIMIT = 1000
HARNESS_HOSTS: FrozenSet[str] = frozenset({"127.0.0.1", "::1", "localhost"})

_SEGMENT_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_LABEL_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_KEY_HASH_CHARS = 16


class SiemConfigInvalid(ValueError):
    """A SIEM delivery setting is invalid; carries the field NAME only."""

    def __init__(self, field: str, reason: str) -> None:
        super().__init__(f"siem_delivery.{field}: {reason}")
        self.field = field
        self.reason = reason


@dataclass(frozen=True)
class Destination:
    """A validated destination: everything a request is built from."""

    key: str
    origin: str
    import_path: str
    token_uri: str
    harness: bool
    key_path: str
    api_version: str


def parse_harness_endpoint(value: str) -> Tuple[str, str, int]:
    """(scheme, host literal, port) of a bare loopback origin, else raise."""
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError as exc:
        raise SiemConfigInvalid("harness_endpoint", "not a URL") from exc
    host = parts.hostname or ""
    if (
        parts.scheme != "http"
        or host not in HARNESS_HOSTS
        or port is None
        or parts.username is not None
        or parts.password is not None
        or parts.path not in ("",)
        or parts.query
        or parts.fragment
        or "?" in value
        or "#" in value
    ):
        raise SiemConfigInvalid(
            "harness_endpoint", "must be http://<loopback host>:<port> only"
        )
    if host != "localhost" and not ipaddress.ip_address(host).is_loopback:
        raise SiemConfigInvalid("harness_endpoint", "not a loopback address")
    return parts.scheme, host, port


def _check_formats(cfg: SiemDeliveryConfig, harness_active: bool) -> None:
    if cfg.api_version not in API_VERSIONS:
        raise SiemConfigInvalid("api_version", "unsupported API version")
    if (
        isinstance(cfg.max_batch_events, bool)
        or not isinstance(cfg.max_batch_events, int)
        or not 1 <= cfg.max_batch_events <= MAX_BATCH_EVENTS_LIMIT
    ):
        raise SiemConfigInvalid("max_batch_events", "must be 1 to 1000")
    if cfg.harness_endpoint:
        if not harness_active:
            raise SiemConfigInvalid(
                "harness_endpoint", "allowed only with the fault-injection gate"
            )
        parse_harness_endpoint(cfg.harness_endpoint)
    if cfg.region and cfg.region not in SECOPS_REGIONS:
        raise SiemConfigInvalid("region", "not a documented SecOps region")
    for name in ("project_id", "location", "instance_id"):
        value = getattr(cfg, name)
        if value and not _SEGMENT_RE.match(value):
            raise SiemConfigInvalid(name, "must be a single path segment")
    path = cfg.service_account_key_path
    if path and not path.startswith("/"):
        raise SiemConfigInvalid("service_account_key_path", "must be absolute")
    label = cfg.source_instance_label
    if label and not _LABEL_RE.match(label):
        raise SiemConfigInvalid("source_instance_label", "invalid characters")


def _check_required(cfg: SiemDeliveryConfig) -> None:
    if not cfg.harness_endpoint and not cfg.region:
        raise SiemConfigInvalid("region", "required when enabled")
    for name in (
        "project_id",
        "location",
        "instance_id",
        "service_account_key_path",
        "source_instance_label",
    ):
        if not getattr(cfg, name):
            raise SiemConfigInvalid(name, "required when enabled")


def validate_section(cfg: SiemDeliveryConfig, *, harness_active: bool) -> None:
    """Raise :class:`SiemConfigInvalid` for the first invalid field."""
    _check_formats(cfg, harness_active)
    if cfg.enabled:
        _check_required(cfg)


def destination_complete(cfg: SiemDeliveryConfig) -> bool:
    return bool(
        (cfg.harness_endpoint or cfg.region)
        and cfg.project_id
        and cfg.location
        and cfg.instance_id
        and cfg.service_account_key_path
    )


def destination_key(cfg: SiemDeliveryConfig) -> Optional[str]:
    """Stable tenant identity of a complete destination (api_version excluded)."""
    if not destination_complete(cfg):
        return None
    if cfg.harness_endpoint:
        prefix, origin = "harness:", cfg.harness_endpoint
    else:
        prefix, origin = "gsecops:", cfg.region
    material = "|".join((origin, cfg.project_id, cfg.location, cfg.instance_id))
    digest = hashlib.sha256(material.encode("utf-8")).hexdigest()
    return prefix + digest[:_KEY_HASH_CHARS]


def import_path(cfg: SiemDeliveryConfig) -> str:
    segments = [
        quote(s, safe="") for s in (cfg.project_id, cfg.location, cfg.instance_id)
    ]
    return (
        f"/{cfg.api_version}/projects/{segments[0]}/locations/{segments[1]}"
        f"/instances/{segments[2]}/events:import"
    )


def resolve_destination(
    cfg: SiemDeliveryConfig, *, harness_active: bool
) -> Optional[Destination]:
    """The validated destination, None when none is configured; raises
    :class:`SiemConfigInvalid` for a stored value this process must not use."""
    validate_section(cfg, harness_active=harness_active)
    key = destination_key(cfg)
    if key is None:
        return None
    if cfg.harness_endpoint:
        origin, token_uri = cfg.harness_endpoint, cfg.harness_endpoint + "/token"
    else:
        origin = f"https://chronicle.{cfg.region}.rep.googleapis.com"
        token_uri = SECOPS_TOKEN_URI
    return Destination(
        key=key,
        origin=origin,
        import_path=import_path(cfg),
        token_uri=token_uri,
        harness=bool(cfg.harness_endpoint),
        key_path=cfg.service_account_key_path,
        api_version=cfg.api_version,
    )
