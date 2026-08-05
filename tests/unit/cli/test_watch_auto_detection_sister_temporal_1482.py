"""GitHub Issue #1482 (extension, site 4 -- LOW priority/future-proofing):
detect_existing_indexes() (cli_watch_helpers.py) only ever scans the local
`.code-indexer/index/` directory for a temporal collection. When a
standalone `cidx watch` process happens to be running directly inside a
golden repo's own clone (the one genuine standalone case with a real
sister root -- see temporal_sister_root_detection.py), a temporal
collection relocated to the golden-owned sister location (Story #1457
AC1) is invisible, so watch mode never starts the temporal handler even
though temporal data genuinely exists for this repo.

Real infra: a real AliasManager pointer + a real committed-row shard
directory at the sister location (using
temporal_row_reader/temporal_shard_has_committed_rows's own on-disk
contract -- a shard dir with at least one non-metadata point file counts
as "has committed rows"). Ordinary standalone repos (no golden-repos
ancestor) must remain entirely unaffected -- covered by the pre-existing
test_watch_auto_detection.py suite, which must keep passing unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.cli_watch_helpers import detect_existing_indexes
from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)

REPO_ALIAS = "myrepo"
POINTER_NAMESPACE = "myrepo-temporal-voyage_code_3-2024Q1"
FIXTURE_VERSION_TIMESTAMP = "1785164318"


def _build_sister_only_golden_clone(tmp_path: Path) -> Path:
    golden_repos_dir = tmp_path / "golden-repos"
    project_root = golden_repos_dir / REPO_ALIAS
    index_base = project_root / ".code-indexer" / "index"
    index_base.mkdir(parents=True)

    # Bug #1529: temporal data for a golden repo lives at the FIXED
    # server-owned root ({golden_repos_dir}/.temporal/{alias}/), not a
    # .versioned snapshot behind an alias pointer. The behavior under test is
    # unchanged: detection must find temporal data OUTSIDE the repo tree.
    version_dir = (
        server_temporal_index_root(golden_repos_dir, REPO_ALIAS)
        / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    version_dir.mkdir(parents=True)
    # A single committed-row marker file -- temporal_shard_has_committed_rows
    # (temporal_row_existence.py) treats any non-metadata point file in the
    # shard dir as evidence of committed rows.
    (version_dir / "vector_0.json").write_text(json.dumps({"id": "commit:abc:0"}))

    return project_root


def test_detect_existing_indexes_sees_sister_relocated_temporal_data(tmp_path):
    project_root = _build_sister_only_golden_clone(tmp_path)

    result = detect_existing_indexes(project_root)

    assert result["temporal"] is True, (
        "detect_existing_indexes must recognize temporal data relocated to "
        "the golden-owned sister location when project_root structurally "
        "IS a golden repo's own clone (Bug #1482 extension); got: "
        f"{result!r}"
    )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
