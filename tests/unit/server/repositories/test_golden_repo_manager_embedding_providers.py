"""Tests for GoldenRepoManager._write_embedding_providers_to_config() (Story #620).

Includes atomicity regression tests for the Phase 3 e2e registration race
(Bug: non-atomic open("w") left config.json truncated during concurrent reads,
causing detect_current_mode() to return "uninitialized" -> cidx index failure).
"""

import json
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import MagicMock, patch


def _make_manager():
    """Create a GoldenRepoManager with minimal mocked dependencies."""
    from code_indexer.server.repositories.golden_repo_manager import GoldenRepoManager

    manager = GoldenRepoManager.__new__(GoldenRepoManager)
    manager._metadata_repo = MagicMock()
    return manager


def _setup_repo_config(tmp_dir: str, base: dict) -> Path:
    """Create .code-indexer/config.json in tmp_dir and return config file path."""
    config_dir = Path(tmp_dir) / ".code-indexer"
    config_dir.mkdir(exist_ok=True)
    config_file = config_dir / "config.json"
    config_file.write_text(json.dumps(base))
    return config_file


def _exercise_write(manager, tmp_dir: str, cohere_key) -> dict:
    """Seed config, patch get_configured_providers, invoke, return parsed config."""
    _setup_repo_config(tmp_dir, {"embedding_provider": "voyage-ai", "sentinel": 42})

    # Build expected provider list based on cohere_key
    providers = ["voyage-ai"]
    if cohere_key:
        providers.append("cohere")

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
        return_value=list(providers),
    ):
        manager._write_embedding_providers_to_config(tmp_dir)

    config_file = Path(tmp_dir) / ".code-indexer" / "config.json"
    return json.loads(config_file.read_text())  # type: ignore[no-any-return]


class TestWriteEmbeddingProvidersToConfig:
    """Test _write_embedding_providers_to_config writes the correct providers list."""

    def test_writes_voyage_and_cohere_when_both_configured(self):
        """Writes both providers (no duplicates) when cohere API key is present."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key="cohere-key-123")

        providers = result["embedding_providers"]
        assert set(providers) == {"voyage-ai", "cohere"}
        assert len(providers) == 2

    def test_writes_only_voyage_when_no_cohere_key(self):
        """Writes only voyage-ai when cohere API key is None."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key=None)

        assert result["embedding_providers"] == ["voyage-ai"]

    def test_writes_only_voyage_when_cohere_key_is_empty_string(self):
        """Guard: empty string cohere key is treated as not configured."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key="")

        assert result["embedding_providers"] == ["voyage-ai"]

    def test_preserves_existing_config_keys(self):
        """Writing embedding_providers preserves all other keys in config.json."""
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            result = _exercise_write(manager, tmp_dir, cohere_key=None)

        assert result["embedding_provider"] == "voyage-ai"
        assert result["sentinel"] == 42


class TestWriteEmbeddingProvidersAtomicity:
    """Regression tests for the Phase 3 registration config-init race.

    The bug: _write_embedding_providers_to_config used open("w") + json.dump()
    which truncates the file first, leaving a window where concurrent readers
    (e.g. cidx index subprocess calling detect_current_mode -> json.load) see
    an empty file, get JSONDecodeError, return "uninitialized", and fail with
    "Command 'index' is not available in no configuration found".

    Fix: use atomic write (tempfile + os.replace) like seed_provider_config.
    These tests prove that no reader ever observes config.json in a truncated
    or corrupt state while _write_embedding_providers_to_config is running.
    """

    def test_write_embedding_providers_is_atomic(self):
        """config.json is never visible as empty/truncated during write.

        Spawns a reader thread that continuously attempts json.load(config.json)
        while _write_embedding_providers_to_config runs in the main thread.
        Any JSONDecodeError or empty-file read during the window is a failure.
        """
        manager = _make_manager()
        errors: list[str] = []
        stop_event = threading.Event()

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = _setup_repo_config(
                tmp_dir, {"embedding_provider": "voyage-ai", "sentinel": 99}
            )

            def reader():
                """Continuously read config.json; record any parse failure."""
                while not stop_event.is_set():
                    try:
                        raw = config_file.read_bytes()
                        if raw:  # non-empty: must be valid JSON
                            json.loads(raw)
                    except json.JSONDecodeError as exc:
                        errors.append(f"JSONDecodeError: {exc} raw={raw!r}")
                    except Exception:
                        pass  # file may not exist momentarily — that's fine
                    time.sleep(0.0001)

            t = threading.Thread(target=reader, daemon=True)
            t.start()

            # Run the write 50 times to maximise race window exposure
            with patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ):
                for _ in range(50):
                    manager._write_embedding_providers_to_config(tmp_dir)

            stop_event.set()
            t.join(timeout=2.0)

        assert not errors, (
            f"Atomic write violated: reader saw corrupt config.json "
            f"({len(errors)} occurrence(s)):\n" + "\n".join(errors[:5])
        )

    def test_concurrent_write_embedding_providers_config_stays_valid(self):
        """Multiple concurrent _write_embedding_providers_to_config calls on the
        same path never leave config.json in an unparseable state.

        This mirrors the Phase 3 e2e scenario where concurrent background jobs
        might theoretically race on the same clone (or where a reader such as
        seed_provider_config reads config.json between truncation and completion).
        """
        manager = _make_manager()
        parse_errors: list[str] = []
        stop_event = threading.Event()

        with tempfile.TemporaryDirectory() as tmp_dir:
            config_file = _setup_repo_config(
                tmp_dir, {"embedding_provider": "voyage-ai", "x": 1}
            )

            def reader():
                while not stop_event.is_set():
                    try:
                        raw = config_file.read_bytes()
                        if raw:
                            json.loads(raw)
                    except json.JSONDecodeError as exc:
                        parse_errors.append(str(exc))
                    except Exception:
                        pass
                    time.sleep(0.00005)

            def writer():
                with patch(
                    "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                    ".get_configured_providers",
                    return_value=["voyage-ai"],
                ):
                    for _ in range(30):
                        manager._write_embedding_providers_to_config(tmp_dir)

            reader_thread = threading.Thread(target=reader, daemon=True)
            writer_threads = [threading.Thread(target=writer) for _ in range(3)]

            reader_thread.start()
            for wt in writer_threads:
                wt.start()
            for wt in writer_threads:
                wt.join(timeout=10.0)
            stop_event.set()
            reader_thread.join(timeout=2.0)

            # Final state must be valid JSON — assertion inside context block
            # so config_file is read before TemporaryDirectory cleanup deletes it.
            final = json.loads(config_file.read_text())
            assert "embedding_providers" in final

        assert not parse_errors, (
            f"Atomicity violated: {len(parse_errors)} corrupt read(s): "
            + "; ".join(parse_errors[:3])
        )

    def test_write_embedding_providers_produces_valid_json_after_write(self):
        """After _write_embedding_providers_to_config completes, config.json
        is valid JSON and contains the expected embedding_providers key.
        Atomic write must leave no *.tmp files behind in the .code-indexer dir.
        """
        manager = _make_manager()
        with tempfile.TemporaryDirectory() as tmp_dir:
            _setup_repo_config(tmp_dir, {"embedding_provider": "voyage-ai"})
            with patch(
                "code_indexer.services.embedding_factory.EmbeddingProviderFactory"
                ".get_configured_providers",
                return_value=["voyage-ai"],
            ):
                manager._write_embedding_providers_to_config(tmp_dir)

            config_file = Path(tmp_dir) / ".code-indexer" / "config.json"
            raw = config_file.read_text()
            parsed = json.loads(raw)  # must not raise
            assert parsed["embedding_providers"] == ["voyage-ai"]
            # Atomic write: no temp files left behind
            leftover = list((Path(tmp_dir) / ".code-indexer").glob("*.tmp"))
            assert leftover == [], f"Temp files left behind: {leftover}"
