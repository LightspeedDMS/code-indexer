"""Destination confinement: region allowlist, path segments, harness gate."""

from __future__ import annotations

import dataclasses

import pytest

from code_indexer.server.services.siem_delivery.destination import (
    SECOPS_REGIONS,
    SECOPS_TOKEN_URI,
    SiemConfigInvalid,
    destination_key,
    resolve_destination,
    validate_section,
)
from code_indexer.server.utils.siem_delivery_config import SiemDeliveryConfig

KEY_PATH = "/var/lib/example/sa-key.json"


def _deployed(**overrides: object) -> SiemDeliveryConfig:
    base = SiemDeliveryConfig(
        enabled=True,
        region="us",
        project_id="example-project",
        location="us",
        instance_id="00000000-0000-0000-0000-000000000000",
        service_account_key_path=KEY_PATH,
        source_instance_label="example-label",
    )
    return dataclasses.replace(base, **overrides)  # type: ignore[arg-type]


def _harness(**overrides: object) -> SiemDeliveryConfig:
    fields = {"region": "", "harness_endpoint": "http://127.0.0.1:8902", **overrides}
    return _deployed(**fields)


def _field(exc: pytest.ExceptionInfo) -> str:
    return str(exc.value.field)


def test_default_section_is_valid_and_has_no_destination() -> None:
    cfg = SiemDeliveryConfig()
    validate_section(cfg, harness_active=False)
    assert destination_key(cfg) is None
    assert resolve_destination(cfg, harness_active=False) is None


def test_deployed_destination_uses_regional_origin_and_google_token_uri() -> None:
    dest = resolve_destination(_deployed(), harness_active=False)
    assert dest is not None
    assert dest.origin == "https://chronicle.us.rep.googleapis.com"
    assert dest.import_path == (
        "/v1/projects/example-project/locations/us/instances/"
        "00000000-0000-0000-0000-000000000000/events:import"
    )
    assert dest.token_uri == SECOPS_TOKEN_URI
    assert dest.key.startswith("gsecops:") and len(dest.key) == len("gsecops:") + 16


def test_harness_destination_key_never_collides_with_deployed() -> None:
    harness = resolve_destination(_harness(), harness_active=True)
    assert harness is not None
    assert harness.key.startswith("harness:")
    assert harness.origin == "http://127.0.0.1:8902"
    assert harness.token_uri == "http://127.0.0.1:8902/token"
    assert harness.key != destination_key(_deployed())


def test_api_version_does_not_change_the_destination_key() -> None:
    assert destination_key(_deployed(api_version="v1")) == destination_key(
        _deployed(api_version="v1alpha")
    )


@pytest.mark.parametrize(
    "overrides,field",
    [
        ({"region": "not-a-region"}, "region"),
        ({"project_id": "p/../x"}, "project_id"),
        ({"instance_id": "i?x=1"}, "instance_id"),
        ({"location": "us%2Fx"}, "location"),
        ({"location": ".hidden"}, "location"),
        ({"api_version": "v2"}, "api_version"),
        ({"max_batch_events": 0}, "max_batch_events"),
        ({"max_batch_events": 1001}, "max_batch_events"),
        ({"service_account_key_path": "relative/key.json"}, "service_account_key_path"),
        ({"source_instance_label": "bad label"}, "source_instance_label"),
        ({"project_id": ""}, "project_id"),
        ({"service_account_key_path": ""}, "service_account_key_path"),
    ],
)
def test_deployed_inputs_are_rejected_by_field(overrides: dict, field: str) -> None:
    with pytest.raises(SiemConfigInvalid) as exc:
        validate_section(_deployed(**overrides), harness_active=False)
    assert _field(exc) == field


def test_harness_endpoint_requires_the_gate() -> None:
    with pytest.raises(SiemConfigInvalid) as exc:
        validate_section(_harness(), harness_active=False)
    assert _field(exc) == "harness_endpoint"
    validate_section(_harness(), harness_active=True)


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://10.0.0.5:8902",
        "http://u:p@127.0.0.1:8902",
        "https://127.0.0.1:8902",
        "http://127.0.0.1",
        "http://127.0.0.1:8902/path",
        "http://127.0.0.1:8902?x=1",
        "http://127.0.0.1:8902#f",
        "http://127.0.0.2:8902",
    ],
)
def test_harness_endpoint_must_be_a_bare_loopback_origin(endpoint: str) -> None:
    with pytest.raises(SiemConfigInvalid) as exc:
        validate_section(_harness(harness_endpoint=endpoint), harness_active=True)
    assert _field(exc) == "harness_endpoint"


@pytest.mark.parametrize(
    "endpoint", ["http://127.0.0.1:8902", "http://[::1]:8902", "http://localhost:8902"]
)
def test_accepted_loopback_spellings(endpoint: str) -> None:
    dest = resolve_destination(_harness(harness_endpoint=endpoint), harness_active=True)
    assert dest is not None and dest.token_uri == endpoint + "/token"


def test_disabled_section_may_be_incomplete() -> None:
    validate_section(_deployed(enabled=False, project_id=""), harness_active=False)


def test_stored_harness_endpoint_is_invalid_in_a_process_without_the_gate() -> None:
    with pytest.raises(SiemConfigInvalid):
        resolve_destination(_harness(enabled=False), harness_active=False)


# Every regional endpoint (chronicle.<region>.rep.googleapis.com) listed in
# Google's "Migrate to Chronicle API" guide,
# https://docs.cloud.google.com/chronicle/docs/soar/admin-tasks/advanced/api-migration-guide
# (retrieved 2026-10-01; the legacy-to-Chronicle-API page lists the same set).
DOCUMENTED_REGIONS = frozenset(
    {
        "africa-south1",
        "asia-northeast1",
        "asia-south1",
        "asia-southeast1",
        "asia-southeast2",
        "australia-southeast1",
        "eu",
        "europe-west12",
        "europe-west2",
        "europe-west3",
        "europe-west6",
        "europe-west9",
        "me-central1",
        "me-central2",
        "me-west1",
        "northamerica-northeast2",
        "southamerica-east1",
        "us",
    }
)


def test_regions_are_the_documented_set() -> None:
    assert SECOPS_REGIONS == DOCUMENTED_REGIONS


@pytest.mark.parametrize("region", sorted(DOCUMENTED_REGIONS))
def test_every_documented_region_resolves_to_its_regional_endpoint(region: str) -> None:
    dest = resolve_destination(_deployed(region=region), harness_active=False)
    assert dest is not None
    assert dest.origin == f"https://chronicle.{region}.rep.googleapis.com"
