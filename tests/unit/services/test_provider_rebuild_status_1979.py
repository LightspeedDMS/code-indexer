"""A recent progress timestamp alone cannot establish a completed rebuild."""

import json

from code_indexer.services.progressive_metadata import ProgressiveMetadata
from code_indexer.services.provider_rebuild_check import (
    find_providers_not_rebuilt_since,
)


def test_recent_in_progress_metadata_does_not_count_as_rebuilt(tmp_path):
    metadata_path = tmp_path / "metadata-voyage-ai.json"
    metadata = ProgressiveMetadata(metadata_path)
    metadata.start_indexing("voyage-ai", "example-model", {})
    metadata.update_progress(files_processed=1, chunks_added=1)

    saved = json.loads(metadata_path.read_text())
    assert saved["status"] == "in_progress"
    assert saved["last_index_timestamp"] > 0
    assert find_providers_not_rebuilt_since(
        tmp_path, ["voyage-ai"], saved["last_index_timestamp"]
    ) == ["voyage-ai"]

    metadata.complete_indexing()
    completed = json.loads(metadata_path.read_text())
    assert completed["status"] == "completed"
    assert (
        find_providers_not_rebuilt_since(
            tmp_path, ["voyage-ai"], saved["last_index_timestamp"]
        )
        == []
    )
