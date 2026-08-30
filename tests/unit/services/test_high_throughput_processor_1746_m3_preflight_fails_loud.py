"""Bug #1746 M3 (code review finding): the hasattr() guard on the
preflight_chunk_store_writable() call silently disables Change 4's ENTIRE
protection if the method is ever renamed on FilesystemVectorStore (the real
production vector_store_client type). A future rename must fail LOUD
(AttributeError) instead of the hasattr() guard silently skipping the
safety check -- consistent with this project's anti-silent-failure
invariant (Foundation #13).

This is a unit-level contract test using a real FilesystemVectorStore
subclass with a declared __getattribute__ override (not a runtime
monkeypatch of the shared production class) to faithfully simulate the
method having been renamed/removed. embedding_provider is a plain
MagicMock (files=[] below means no real embedding logic ever executes) --
matching the established precedent in
tests/unit/services/test_high_throughput_processor_1746_finalize_or_abort.py's
_make_processor helper.
"""

from unittest.mock import MagicMock

import pytest

from code_indexer.services.high_throughput_processor import HighThroughputProcessor
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


class _RenamedPreflightStore(FilesystemVectorStore):
    """Real FilesystemVectorStore subclass with a declared
    __getattribute__ override that raises AttributeError for
    preflight_chunk_store_writable -- faithfully simulates the method
    having been renamed/removed from the class, WITHOUT mutating the
    shared production FilesystemVectorStore class itself."""

    def __getattribute__(self, name: str):
        if name == "preflight_chunk_store_writable":
            raise AttributeError(
                "'FilesystemVectorStore' object has no attribute "
                "'preflight_chunk_store_writable' (simulated rename)"
            )
        return super().__getattribute__(name)


class TestBug1746M3PreflightFailsLoudOnRename:
    """M3: a future rename of preflight_chunk_store_writable on the real
    FilesystemVectorStore class must fail LOUD, not silently skip."""

    def test_missing_preflight_method_on_real_store_raises_attribute_error(
        self, tmp_path
    ) -> None:
        codebase_dir = tmp_path / "repo"
        codebase_dir.mkdir()

        index_dir = codebase_dir / ".code-indexer" / "index"
        store = _RenamedPreflightStore(
            base_path=index_dir, use_chunks_db_for_new_collections=True
        )

        config = MagicMock()
        config.codebase_dir = codebase_dir
        config.exclude_dirs = []
        config.exclude_patterns = []

        embedding_provider = MagicMock()
        embedding_provider.get_current_model.return_value = "voyage-code-3"
        embedding_provider.get_provider_name.return_value = "voyage-ai"

        processor = HighThroughputProcessor(
            config=config,
            embedding_provider=embedding_provider,
            vector_store_client=store,
        )

        with pytest.raises(AttributeError):
            processor.process_files_high_throughput(
                files=[],
                vector_thread_count=2,
                batch_size=50,
            )
