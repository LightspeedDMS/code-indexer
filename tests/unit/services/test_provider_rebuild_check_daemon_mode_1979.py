"""Bug #1979 (round 4): `find_providers_not_rebuilt_since` daemon-mode branch.

Round 4's coordinator ruling reverted the daemon's write path back to the
bare legacy `metadata.json` (see
`tests/unit/daemon/test_daemon_clear_metadata_path_1979.py`) instead of
propagating a per-provider filename to every reader. The daemon-mode
caller in `cli.py` (after `_index_via_daemon` returns) therefore needs
`find_providers_not_rebuilt_since` to check the daemon's OWN bare
`metadata.json` file instead of `metadata-<provider>.json`.

Since daemon-mode `--clear` with more than one configured provider is
already rejected before delegation (Bug #1979 P2, round 4 -- see
`tests/unit/cli/test_cli_daemon_multi_provider_clear_guard_1979.py`), a
daemon-mode repo is always exactly ONE configured provider, so there is no
per-provider ambiguity for the `daemon_mode=True` branch to resolve: it
always reads the single bare file.

These tests confirm ONLY the daemon_mode branch's behavior; the existing
foreground/server per-provider-file tests
(`test_provider_rebuild_status_1979.py`,
`test_provider_rebuild_physical_rows_1979.py`) are re-run unchanged
elsewhere to confirm daemon_mode=False (the default) is untouched.
"""

from __future__ import annotations

import json

from code_indexer.services.progressive_metadata import ProgressiveMetadata
from code_indexer.services.provider_rebuild_check import (
    find_providers_not_rebuilt_since,
)


def test_daemon_mode_in_progress_bare_metadata_rejected_as_not_rebuilt(tmp_path):
    """A bare metadata.json with status in_progress is not a genuine rebuild."""
    metadata_path = tmp_path / "metadata.json"
    metadata = ProgressiveMetadata(metadata_path)
    metadata.start_indexing("voyage-ai", "example-model", {})
    metadata.update_progress(files_processed=1, chunks_added=1)

    saved = json.loads(metadata_path.read_text())
    assert saved["status"] == "in_progress"

    assert find_providers_not_rebuilt_since(
        tmp_path,
        ["voyage-ai"],
        saved["last_index_timestamp"],
        daemon_mode=True,
    ) == ["voyage-ai"]


def test_daemon_mode_missing_bare_metadata_rejected_as_not_rebuilt(tmp_path):
    """No bare metadata.json at all is not a genuine rebuild."""
    assert find_providers_not_rebuilt_since(
        tmp_path,
        ["voyage-ai"],
        0.0,
        daemon_mode=True,
    ) == ["voyage-ai"]


def test_daemon_mode_completed_fresh_bare_metadata_accepted_as_rebuilt(tmp_path):
    """A bare metadata.json with status completed and a fresh timestamp
    IS a genuine rebuild."""
    metadata_path = tmp_path / "metadata.json"
    metadata = ProgressiveMetadata(metadata_path)
    metadata.start_indexing("voyage-ai", "example-model", {})

    since_timestamp = metadata.metadata["last_index_timestamp"]
    metadata.complete_indexing()

    completed = json.loads(metadata_path.read_text())
    assert completed["status"] == "completed"

    assert (
        find_providers_not_rebuilt_since(
            tmp_path,
            ["voyage-ai"],
            since_timestamp,
            daemon_mode=True,
        )
        == []
    )


def test_daemon_mode_does_not_use_per_provider_filename(tmp_path):
    """A completed PER-PROVIDER file must NOT satisfy the daemon_mode check
    -- daemon_mode reads ONLY the bare file, never metadata-<provider>.json."""
    per_provider_path = tmp_path / "metadata-voyage-ai.json"
    metadata = ProgressiveMetadata(per_provider_path)
    metadata.start_indexing("voyage-ai", "example-model", {})
    metadata.complete_indexing()

    assert find_providers_not_rebuilt_since(
        tmp_path,
        ["voyage-ai"],
        0.0,
        daemon_mode=True,
    ) == ["voyage-ai"]
