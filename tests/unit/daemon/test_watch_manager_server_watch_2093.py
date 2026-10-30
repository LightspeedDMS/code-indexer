"""#2093: the server's watch indexes every configured provider, each with
the per-provider metadata file the server's own `cidx index` children
write; the daemon's watch keeps its single provider and bare metadata.json
(Bug #1979)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import List

import pytest

from code_indexer.config import ConfigManager
from code_indexer.daemon.watch_manager import DaemonWatchManager


def _repo(tmp_path: Path, providers: List[str]) -> Path:
    config_manager = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    config_manager.create_default_config(codebase_dir=tmp_path)
    config = config_manager.load()
    config.embedding_providers = providers
    config_manager.save(config)
    return tmp_path


def _metadata_names(handler) -> List[str]:
    return [i.progressive_metadata.metadata_path.name for i in handler.indexers]


def _collections(handler) -> List[str]:
    return [
        i.vector_store_client.resolve_collection_name(i.config, i.embedding_provider)
        for i in handler.indexers
    ]


@pytest.fixture
def provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VOYAGE_API_KEY", "sentinel-voyage-2093")
    monkeypatch.setenv("CO_API_KEY", "sentinel-cohere-2093")


def test_server_watch_indexes_every_configured_provider(
    tmp_path: Path, provider_keys: None
) -> None:
    repo = _repo(tmp_path, ["voyage-ai", "cohere"])

    handler = DaemonWatchManager()._create_watch_handler(
        str(repo), None, server_watch=True
    )

    assert [i.embedding_provider.get_provider_name() for i in handler.indexers] == [
        "voyage-ai",
        "cohere",
    ]
    assert handler.smart_indexer is handler.indexers[0]
    assert _metadata_names(handler) == [
        "metadata-voyage-ai.json",
        "metadata-cohere.json",
    ]
    assert len(set(_collections(handler))) == 2


def test_server_watch_skips_a_provider_without_key_loudly(
    tmp_path: Path,
    provider_keys: None,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.delenv("CO_API_KEY")
    repo = _repo(tmp_path, ["voyage-ai", "cohere"])

    with caplog.at_level(logging.WARNING):
        handler = DaemonWatchManager()._create_watch_handler(
            str(repo), None, server_watch=True
        )

    assert _metadata_names(handler) == ["metadata-voyage-ai.json"]
    assert any("cohere" in r.getMessage() for r in caplog.records)


def test_daemon_watch_keeps_single_provider_and_bare_metadata(
    tmp_path: Path, provider_keys: None
) -> None:
    repo = _repo(tmp_path, ["voyage-ai", "cohere"])

    handler = DaemonWatchManager()._create_watch_handler(str(repo), None)

    assert _metadata_names(handler) == ["metadata.json"]
