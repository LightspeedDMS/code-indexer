"""Test the extended DUNDER_ATTR_BLOCKLIST (Story #970 security audit follow-up).

Verifies that each newly-added dunder is rejected at AST validation time via
Attribute access, and that the blocklist has grown to the expected minimum size.

Subscript access with double-underscore prefixed strings is already blocked by
the existing ``startswith("__")`` rule in validate() — those tests assert the
behaviour continues to hold (not that it requires the new blocklist entries).
"""

from __future__ import annotations

import pytest

from code_indexer.xray.sandbox import PythonEvaluatorSandbox

# Dunders added in this extension round (live info-leak vectors):
NEWLY_BLOCKED_DUNDERS = [
    "__loader__",  # module loader access — could read source files
    "__spec__",  # module spec — origin file path leak
    "__file__",  # module path leak
    "__path__",  # package path
    "__package__",  # package name
    "__cached__",  # bytecode cache path
    "__defaults__",  # captured function defaults
    "__kwdefaults__",  # captured kwarg defaults
    "__annotations__",  # type hint leak
    "__type_params__",  # Py 3.12+ generic syntax leak (forward-compat)
    "__set_name__",  # descriptor protocol abuse
    "__instancecheck__",  # metaclass abuse
    "__subclasscheck__",  # metaclass abuse
    "__prepare__",  # metaclass
    "__weakref__",  # weakref creation
]

# Minimum size the blocklist must reach after the extension.
MIN_BLOCKLIST_SIZE = 39


class TestExtendedDunderBlocklist:
    """Each newly-added dunder must be rejected at the Attribute check."""

    @pytest.mark.parametrize("dunder", NEWLY_BLOCKED_DUNDERS)
    def test_attribute_access_blocked(self, dunder: str) -> None:
        """Attribute access to a newly-blocked dunder must fail validation."""
        sb = PythonEvaluatorSandbox()
        v = sb.validate(f"return node.{dunder}")
        assert not v.ok, f"Expected validation to reject 'node.{dunder}' but it passed"
        assert dunder in (v.reason or ""), (
            f"Expected rejection reason to mention '{dunder}', got: {v.reason!r}"
        )

    @pytest.mark.parametrize("dunder", NEWLY_BLOCKED_DUNDERS)
    def test_subscript_string_access_blocked(self, dunder: str) -> None:
        """Subscript access with a dunder string key must fail validation.

        Note: subscript blocking uses startswith("__") so ALL double-underscore
        names are covered regardless of whether they appear in DUNDER_ATTR_BLOCKLIST.
        This test documents that the protection exists.
        """
        sb = PythonEvaluatorSandbox()
        v = sb.validate(f"return node[{dunder!r}]")
        assert not v.ok, (
            f"Expected validation to reject subscript with key {dunder!r} but it passed"
        )

    def test_blocklist_size_increased(self) -> None:
        """Regression guard: DUNDER_ATTR_BLOCKLIST must not shrink below the minimum."""
        bl = PythonEvaluatorSandbox.DUNDER_ATTR_BLOCKLIST
        assert len(bl) >= MIN_BLOCKLIST_SIZE, (
            f"DUNDER_ATTR_BLOCKLIST has {len(bl)} entries — expected >= {MIN_BLOCKLIST_SIZE}. "
            "Was it accidentally trimmed?"
        )

    def test_all_newly_blocked_dunders_in_blocklist(self) -> None:
        """Every dunder in NEWLY_BLOCKED_DUNDERS must appear in the frozenset."""
        bl = PythonEvaluatorSandbox.DUNDER_ATTR_BLOCKLIST
        missing = [d for d in NEWLY_BLOCKED_DUNDERS if d not in bl]
        assert missing == [], (
            f"These dunders are missing from DUNDER_ATTR_BLOCKLIST: {missing}"
        )
