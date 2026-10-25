from __future__ import annotations

import json
from pathlib import Path
from typing import List, Optional, Set

import pytest

from code_indexer.storage.hnsw_index_manager import HNSWIndexManager

_DIM = 8


def _vector_file(root: Path, point_id: str, path: str, hidden: List[str]) -> Path:
    target = root / f"vector_{point_id}.json"
    target.write_text(
        json.dumps(
            {
                "id": point_id,
                "vector": [0.1] * _DIM,
                "payload": {"path": path, "hidden_branches": hidden},
            }
        )
    )
    return target


@pytest.mark.parametrize(
    "visible, branch, expected",
    [
        ({"a.py", "b.py"}, "feature-x", ["b"]),
        ({"a.py", "b.py"}, None, ["a", "b"]),
        (None, "feature-x", ["b"]),
    ],
    ids=["visible-and-branch", "visible-only", "branch-only"],
)
def test_json_loader_never_loads_a_point_hidden_on_the_branch(
    tmp_path: Path,
    visible: Optional[Set[str]],
    branch: Optional[str],
    expected: List[str],
) -> None:
    files = [
        _vector_file(tmp_path, "a", "a.py", ["feature-x"]),
        _vector_file(tmp_path, "b", "b.py", []),
    ]

    _, ids = HNSWIndexManager(vector_dim=_DIM)._load_vectors_from_json_files(
        files, _DIM, visible, branch
    )

    assert sorted(ids) == expected


@pytest.mark.parametrize(
    "visible, branch, expected",
    [
        ({"a.py", "b.py"}, "feature-x", ["b"]),
        ({"a.py", "b.py"}, None, ["a", "b"]),
        (None, "feature-x", ["b"]),
    ],
    ids=["visible-and-branch", "visible-only", "branch-only"],
)
def test_chunks_db_loader_never_loads_a_point_hidden_on_the_branch(
    tmp_path: Path,
    visible: Optional[Set[str]],
    branch: Optional[str],
    expected: List[str],
) -> None:
    from tests.unit.storage.test_hnsw_index_manager_1461_lazy_payload_perf import (
        _make_chunks_db_collection,
        _record,
    )

    collection = tmp_path / "coll"
    _make_chunks_db_collection(
        collection,
        [
            _record("a", "a.py", _DIM, hidden_branches=["feature-x"]),
            _record("b", "b.py", _DIM),
        ],
        vector_dim=_DIM,
    )

    _, ids = HNSWIndexManager(vector_dim=_DIM)._load_vectors_from_chunks_db(
        collection, _DIM, visible, branch
    )

    assert sorted(ids) == expected
