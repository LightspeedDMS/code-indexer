"""Shared helpers for the Bug #1575 PathIndex/delete_points regression
tests (test_filesystem_vector_store_1575_regression_delete_pathindex_gap.py
and test_filesystem_vector_store_1575_regression_missing_path_index.py).

Not itself a test module (no ``test_*`` functions) -- pure setup/reading
utilities reused by both, to avoid triplicating the dynamic-module-loading
boilerplate and metadata-reading logic.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, List, cast

import numpy as np

_PROJECT_ROOT = Path(__file__).parents[3]
_SCRIPT_PATH = (
    _PROJECT_ROOT / "scripts" / "analysis" / "measure_incremental_refresh_cost_1575.py"
)

VECTOR_DIM = 8


def load_measurement_module() -> Any:
    """Dynamically load ``measure_incremental_refresh_cost_1575.py`` --
    same technique the permanent Finding-1 scaling test uses (mypy cannot
    resolve a dotted import for a scripts/analysis/ path that only exists
    via a test-time sys.path mutation)."""
    spec = importlib.util.spec_from_file_location(
        "measure_incremental_refresh_cost_1575_pathindex_gap_helpers",
        _SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def make_vector(seed: int) -> List[float]:
    rng = np.random.default_rng(seed)
    return cast(
        List[float], rng.standard_normal(VECTOR_DIM).astype(np.float32).tolist()
    )


def read_unique_file_count(base_path: Path, collection_name: str) -> int:
    collection_path = base_path / collection_name
    meta = json.loads((collection_path / "collection_meta.json").read_text())
    return int(meta["unique_file_count"])
