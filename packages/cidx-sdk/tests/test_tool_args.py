"""MCP tool argument coercion shared by the core's and the features' handlers."""

from __future__ import annotations

from typing import Any

import pytest
from cidx_sdk.tool_args import coerce_float, coerce_int


@pytest.mark.parametrize(
    "value, expected",
    [(None, 7), ("12", 12), (3, 3), (4.9, 4), ("x", 7), ([], 7), ("1.5", 7)],
)
def test_coerce_int(value: Any, expected: int) -> None:
    assert coerce_int(value, 7) == expected


@pytest.mark.parametrize(
    "value, expected",
    [(None, 0.5), ("1.5", 1.5), (2, 2.0), ("x", 0.5), ({}, 0.5)],
)
def test_coerce_float(value: Any, expected: float) -> None:
    assert coerce_float(value, 0.5) == expected
