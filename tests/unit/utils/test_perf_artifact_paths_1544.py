"""Bug #1544: perf-evidence tests must not dirty tracked reports/perf/*.json.

`tests/unit/server/test_event_loop_concurrency_1491.py` and
`tests/unit/server/mcp/test_mcp_response_c_encoder_1491.py` unconditionally
wrote fresh timing measurements into TRACKED files under `reports/perf/` on
every run. The numbers are non-deterministic (real wall-clock measurements),
so every gate run left the working tree dirty with unrelated churn.

`tests/utils/perf_artifact_paths.py`'s `perf_artifact_path(filename)` is the
shared fix: by default it resolves to a gitignored scratch location
(`.tmp/perf/`), and only resolves to the real tracked `reports/perf/`
location when the operator explicitly opts in via the
`CIDX_WRITE_PERF_ARTIFACTS` environment variable -- mirroring how Story
#1168's standalone benchmark script is invoked deliberately rather than as
part of the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.utils.perf_artifact_paths import PERF_ARTIFACT_ENV_VAR, perf_artifact_path


def _repo_root() -> Path:
    # tests/unit/utils/test_perf_artifact_paths_1544.py -> repo root
    return Path(__file__).resolve().parents[3]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Never let ambient CI/dev-shell env leak into these assertions."""
    monkeypatch.delenv(PERF_ARTIFACT_ENV_VAR, raising=False)


def test_default_resolves_to_gitignored_scratch_location() -> None:
    """With no opt-in env var, the path must NOT be under tracked reports/perf/."""
    path = perf_artifact_path("some_measurement_1544.json")

    tracked_dir = _repo_root() / "reports" / "perf"
    assert tracked_dir not in path.parents, (
        f"default perf artifact path {path} resolved under the tracked "
        f"{tracked_dir} directory -- a plain test run would dirty git status"
    )
    assert path.name == "some_measurement_1544.json"


def test_default_scratch_location_is_exactly_tmp_perf() -> None:
    """The default scratch location is exactly .tmp/perf/, not merely some
    other subdirectory of .tmp/ that a looser check would also accept."""
    path = perf_artifact_path("another_measurement_1544.json")

    expected = _repo_root() / ".tmp" / "perf" / "another_measurement_1544.json"
    assert path == expected


def test_opt_in_env_var_resolves_to_tracked_reports_perf(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Explicit opt-in must resolve to the real, tracked reports/perf/ path."""
    monkeypatch.setenv(PERF_ARTIFACT_ENV_VAR, "1")

    path = perf_artifact_path("opt_in_measurement_1544.json")

    expected = _repo_root() / "reports" / "perf" / "opt_in_measurement_1544.json"
    assert path == expected


@pytest.mark.parametrize("truthy_value", ["true", "TRUE", "yes", "on"])
def test_opt_in_env_var_accepts_common_truthy_spellings(
    monkeypatch: pytest.MonkeyPatch, truthy_value: str
) -> None:
    monkeypatch.setenv(PERF_ARTIFACT_ENV_VAR, truthy_value)

    path = perf_artifact_path("opt_in_measurement_1544.json")

    expected = _repo_root() / "reports" / "perf" / "opt_in_measurement_1544.json"
    assert path == expected


@pytest.mark.parametrize("falsy_value", ["0", "false", "", "no"])
def test_falsy_env_var_values_stay_on_the_default_scratch_path(
    monkeypatch: pytest.MonkeyPatch, falsy_value: str
) -> None:
    """A falsy/empty env var value must behave identically to it being unset."""
    monkeypatch.setenv(PERF_ARTIFACT_ENV_VAR, falsy_value)

    path = perf_artifact_path("falsy_measurement_1544.json")

    tracked_dir = _repo_root() / "reports" / "perf"
    assert tracked_dir not in path.parents


def test_parent_directory_is_created() -> None:
    """Callers must be able to write immediately -- no separate mkdir needed."""
    path = perf_artifact_path("mkdir_check_1544.json")

    assert path.parent.is_dir()
