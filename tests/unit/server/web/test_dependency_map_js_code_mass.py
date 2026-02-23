"""Tests for Story #273: Log-Compressed Code Mass Bubble Sizing - Frontend JS constants.

Validates that dependency_map.js contains the correct constants and algorithm
for log-compressed bubble sizing by file count (AC5, AC6).

These are structural content tests of the JavaScript source, which is the
appropriate testing approach for JS in a Python test suite.
"""
from pathlib import Path


def _read_js() -> str:
    """Read the dependency_map.js file content.

    Path traversal from tests/unit/server/web/:
      .parent       -> tests/unit/server/web/
      .parent.parent -> tests/unit/server/
      .parent x3    -> tests/unit/
      .parent x4    -> tests/
      .parent x5    -> project root
    """
    js_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "static"
        / "js"
        / "dependency_map.js"
    )
    return js_path.read_text()


class TestDependencyMapJsCodeMassConstants:
    """AC5: Frontend contains correct Story #273 constants."""

    def test_code_mass_scale_constant_defined(self):
        """AC5: CODE_MASS_SCALE=7 constant must be present."""
        js = _read_js()
        assert "CODE_MASS_SCALE" in js, "dependency_map.js must define CODE_MASS_SCALE"
        assert "CODE_MASS_SCALE = 7" in js, (
            "CODE_MASS_SCALE must equal 7 (log10 multiplier for file count)"
        )

    def test_code_mass_max_constant_defined(self):
        """AC5: CODE_MASS_MAX=35 constant must be present."""
        js = _read_js()
        assert "CODE_MASS_MAX" in js, "dependency_map.js must define CODE_MASS_MAX"
        assert "CODE_MASS_MAX = 35" in js, (
            "CODE_MASS_MAX must equal 35 (cap on code mass radius contribution)"
        )

    def test_max_radius_updated_to_140(self):
        """AC5: MAX_RADIUS must be updated to 140 (was 105) to accommodate code mass factor."""
        js = _read_js()
        assert "MAX_RADIUS = 140" in js, (
            "MAX_RADIUS must be 140 (increased from 105 to allow code mass contribution). "
            f"Found: {[line for line in js.splitlines() if 'MAX_RADIUS' in line]}"
        )
        assert "MAX_RADIUS = 105" not in js, (
            "MAX_RADIUS = 105 (old value) must not appear in dependency_map.js"
        )


class TestDependencyMapJsCodeMassAlgorithm:
    """AC5, AC6: Frontend _nodeRadius() uses log10 compression for codeMassFactor."""

    def test_code_mass_factor_uses_log10(self):
        """AC5: codeMassFactor must use Math.log10 for compression."""
        js = _read_js()
        assert "codeMassFactor" in js, (
            "_nodeRadius() must compute codeMassFactor variable"
        )
        assert "Math.log10" in js, (
            "codeMassFactor must use Math.log10() for log-compressed scaling"
        )

    def test_code_mass_factor_uses_total_file_count(self):
        """AC5: codeMassFactor must reference total_file_count from the node data."""
        js = _read_js()
        assert "total_file_count" in js, (
            "_nodeRadius() must read total_file_count from node data"
        )

    def test_code_mass_factor_adds_one_before_log(self):
        """AC6: log10(totalFileCount + 1) prevents log(0) for domains with zero files."""
        js = _read_js()
        # The formula must use +1 inside Math.log10() to avoid log10(0) = -Infinity
        assert "Math.log10(" in js and ("+ 1)" in js or "+1)" in js), (
            "Must use Math.log10(totalFileCount + 1) pattern"
        )

    def test_code_mass_factor_capped_with_min(self):
        """AC5: codeMassFactor must be capped at CODE_MASS_MAX using Math.min."""
        js = _read_js()
        # The formula caps codeMassFactor: Math.min(..., CODE_MASS_MAX)
        assert "CODE_MASS_MAX" in js, "codeMassFactor cap must reference CODE_MASS_MAX"

    def test_radius_includes_code_mass_factor(self):
        """AC5: The final radius sum must include codeMassFactor."""
        js = _read_js()
        assert "codeMassFactor" in js, (
            "radius computation must include codeMassFactor in the sum"
        )
