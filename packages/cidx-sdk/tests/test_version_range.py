"""The SDK version-range check the core uses to accept or refuse a feature."""

from __future__ import annotations

import pytest

from cidx_sdk import SDK_VERSION, VersionRange, parse_version, sdk_satisfies
from cidx_sdk.errors import InvalidVersionError
from cidx_sdk.version import version_satisfies


def test_sdk_version_is_semver_triplet() -> None:
    assert len(parse_version(SDK_VERSION)) == 3


@pytest.mark.parametrize(
    "version, spec, expected",
    [
        ("0.1.0", ">=0.1,<0.2", True),
        ("0.2.0", ">=0.1,<0.2", False),
        ("0.0.9", ">=0.1,<0.2", False),
        ("1.0", "==1.0.0", True),
        ("1.0.1", "==1.0", False),
        ("1.0.1", "!=1.0.0", True),
        ("1.0.0", "!=1.0", False),
        ("2.0.0", ">1.9.9", True),
        ("1.9.9", ">1.9.9", False),
        ("1.9.9", "<=1.9.9", True),
        ("1.4.5", "~=1.4", True),
        ("1.9", "~=1.4", True),
        ("2.0", "~=1.4", False),
        ("1.3.9", "~=1.4", False),
        ("1.4.2", "~=1.4.2", True),
        ("1.4.9", "~=1.4.2", True),
        ("1.5.0", "~=1.4.2", False),
        ("10.0.0", ">=9", True),
        ("0.1.0", " >= 0.1 , < 0.2 ", True),
    ],
)
def test_version_satisfies(version: str, spec: str, expected: bool) -> None:
    assert version_satisfies(version, spec) is expected


@pytest.mark.parametrize(
    "spec", ["", "   ", "0.1", ">=0.1,", "=>0.1", ">=0.1a1", "~=1", ">=1.*", "^1.0"]
)
def test_invalid_ranges_raise(spec: str) -> None:
    with pytest.raises(InvalidVersionError):
        VersionRange.parse(spec)


@pytest.mark.parametrize("version", ["", "1.", "v1.0", "1.0.0rc1", "1..0", "-1"])
def test_invalid_versions_raise(version: str) -> None:
    with pytest.raises(InvalidVersionError):
        parse_version(version)


def test_range_round_trips_to_string() -> None:
    assert str(VersionRange.parse(">=0.1, <0.2")) == ">=0.1,<0.2"


def test_sdk_satisfies_uses_running_version_by_default() -> None:
    assert sdk_satisfies(f"=={SDK_VERSION}")
    assert not sdk_satisfies(f"!={SDK_VERSION}")
    assert sdk_satisfies(">=1.0", sdk_version="1.2.0")
