"""
Regression test for Bug #1261: the Web UI config screen must accept 0
(unlimited) as a valid value for the dependency-map pass2/delta max-turns
inputs, matching the relaxed lower bound (0 instead of 5). A stale min="5"
HTML attribute would silently block an operator from entering 0 in the
browser even though the backend accepts it.

The inputs are the cidx-depmap feature's settings, rendered by the generic
Feature Settings block from the section's declared minimum.
"""

import pytest

from tests.unit.server._depmap_feature import render_depmap_feature_settings


@pytest.fixture(scope="module")
def feature_settings_html():
    return render_depmap_feature_settings()


@pytest.mark.parametrize(
    "name", ["dependency_map_pass2_max_turns", "dependency_map_delta_max_turns"]
)
def test_max_turns_input_min_is_zero(feature_settings_html, name):
    assert (
        f'<input type="number" name="setting__{name}" value="0" min="0" step="1">'
        in feature_settings_html
    )
