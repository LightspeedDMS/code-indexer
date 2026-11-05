"""Make the test-only ``cidx_sample_feature`` add-on discoverable.

Writes a real ``*.dist-info`` (METADATA + entry_points.txt) into a temp
directory and puts it, plus the fixture's source directory, on ``sys.path``.
``importlib.metadata`` then finds the entry points exactly as it would for a
pip-installed add-on, so the tests exercise real discovery rather than a mock.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterator, Mapping

import pytest

from code_indexer.server.features.registry import set_active_registry

_FIXTURES = Path(__file__).resolve().parents[3] / "feature_fixtures"
FEATURE_GROUP = "code_indexer.features"


def write_distribution(
    site_dir: Path, dist_name: str, entry_points: Mapping[str, str]
) -> None:
    dist_info = site_dir / f"{dist_name}-0.0.0.dist-info"
    dist_info.mkdir(parents=True)
    (dist_info / "METADATA").write_text(
        f"Metadata-Version: 2.1\nName: {dist_name}\nVersion: 0.0.0\n",
        encoding="utf-8",
    )
    lines = [f"[{FEATURE_GROUP}]"] + [
        f"{name} = {target}" for name, target in entry_points.items()
    ]
    (dist_info / "entry_points.txt").write_text(
        "\n".join(lines) + "\n", encoding="utf-8"
    )


@pytest.fixture
def installed_sample_features(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> Iterator[Dict[str, str]]:
    """Install the sample features' entry points; yields name -> target."""
    from_fixture = _sample_entry_points()
    write_distribution(tmp_path / "site", "cidx-sample-feature", from_fixture)
    monkeypatch.syspath_prepend(str(_FIXTURES))
    monkeypatch.syspath_prepend(str(tmp_path / "site"))
    yield from_fixture


@pytest.fixture(scope="module")
def sample_feature_site(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Dict[str, str]]:
    """Module-scoped :func:`installed_sample_features` (for tests sharing
    one real app): the entry points stay discoverable for the module."""
    from_fixture = _sample_entry_points()
    site = tmp_path_factory.mktemp("feature-site")
    write_distribution(site, "cidx-sample-feature", from_fixture)
    added = [str(_FIXTURES), str(site)]
    for entry in added:
        sys.path.insert(0, entry)
    try:
        yield from_fixture
    finally:
        for entry in added:
            sys.path.remove(entry)


def _sample_entry_points() -> Dict[str, str]:
    sys.path.insert(0, str(_FIXTURES))
    try:
        from cidx_sample_feature import ENTRY_POINTS
    finally:
        sys.path.remove(str(_FIXTURES))
    return dict(ENTRY_POINTS)


@pytest.fixture(autouse=True)
def no_active_registry() -> Iterator[None]:
    """No test leaks a published registry into another test's health output."""
    set_active_registry(None)
    yield
    set_active_registry(None)
