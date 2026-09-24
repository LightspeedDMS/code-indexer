"""Shared typing imports for the storage protocols package (Issue #1935 Part 2).

Every per-domain protocol module imports its typing primitives from here
instead of repeating the same `from typing import ...` line 28 times, so
there is exactly one place declaring which typing constructs the Protocol
definitions rely on.

Also re-exports Protocol/runtime_checkable so per-domain modules have a
single import line for both the typing helpers and the Protocol machinery.
"""

from __future__ import annotations

from datetime import datetime
from typing import (
    Any,
    Dict,
    List,
    Optional,
    Protocol,
    Sequence,
    Tuple,
    runtime_checkable,
)

__all__ = [
    "Any",
    "Dict",
    "List",
    "Optional",
    "Protocol",
    "Sequence",
    "Tuple",
    "datetime",
    "runtime_checkable",
]
