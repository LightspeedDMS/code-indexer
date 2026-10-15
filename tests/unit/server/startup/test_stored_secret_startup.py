"""Startup creates or validates the stored-secret key off the event loop and
runs the stored-secret sweep in a worker thread."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from code_indexer.server.services.stored_secret_key import (
    KEY_FILENAME,
    StoredSecretKeyError,
)
from code_indexer.server.services.token_encryption import (
    derive_key_from_salt,
    encrypt_token,
)
from code_indexer.server.startup.stored_secret_startup import (
    prepare_stored_secret_key,
    start_stored_secret_sweep,
)
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends.ci_tokens_backend import (
    CITokensSqliteBackend,
)
from code_indexer.server.storage.sqlite_backends.git_credentials_backend import (
    GitCredentialsSqliteBackend,
)

_LIFESPAN = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "startup"
    / "lifespan.py"
)


@pytest.mark.asyncio
async def test_solo_prepare_creates_key_file_and_no_salt_file(tmp_path: Path) -> None:
    await prepare_stored_secret_key(tmp_path, "sqlite", SimpleNamespace())
    assert (tmp_path / KEY_FILENAME).is_file()
    assert not (tmp_path / ".encryption_key_salt").exists()


@pytest.mark.asyncio
async def test_solo_prepare_fails_loudly_on_corrupt_key(tmp_path: Path) -> None:
    (tmp_path / KEY_FILENAME).write_text("corrupt")
    with pytest.raises(StoredSecretKeyError):
        await prepare_stored_secret_key(tmp_path, "sqlite", SimpleNamespace())


def _registry(tmp_path: Path) -> SimpleNamespace:
    db = str(tmp_path / "server.db")
    DatabaseSchema(db).initialize_database()
    return SimpleNamespace(
        ci_tokens=CITokensSqliteBackend(db),
        git_credentials=GitCredentialsSqliteBackend(db),
    )


@pytest.mark.asyncio
async def test_sweep_task_rewrites_legacy_rows(tmp_path: Path) -> None:
    registry = _registry(tmp_path)
    legacy = encrypt_token(
        "glpat-" + "d" * 24, derive_key_from_salt(os.uname().nodename)
    )
    registry.ci_tokens.save_token("gitlab", legacy, None)
    app = SimpleNamespace(state=SimpleNamespace())
    start_stored_secret_sweep(app, tmp_path, registry)
    await app.state.stored_secret_sweep_task
    stored = registry.ci_tokens.get_token("gitlab")["encrypted_token"]
    assert stored.startswith("v2:")


@pytest.mark.asyncio
async def test_sweep_failure_is_logged_not_raised(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    app = SimpleNamespace(state=SimpleNamespace())
    caplog.set_level(logging.ERROR)
    start_stored_secret_sweep(app, tmp_path, SimpleNamespace())  # no backends
    await app.state.stored_secret_sweep_task
    assert any("sweep failed" in r.getMessage() for r in caplog.records)


@pytest.mark.asyncio
async def test_stop_cancels_and_awaits_the_sweep_task(tmp_path: Path) -> None:
    from code_indexer.server.startup.stored_secret_startup import (
        stop_stored_secret_sweep,
    )

    app = SimpleNamespace(state=SimpleNamespace())
    await stop_stored_secret_sweep(app)  # no task yet: no-op
    start_stored_secret_sweep(app, tmp_path, _registry(tmp_path))
    await stop_stored_secret_sweep(app)
    assert app.state.stored_secret_sweep_task.done()


def test_lifespan_shutdown_stops_the_sweep() -> None:
    source = _LIFESPAN.read_text()
    shutdown = source[source.find("\n        yield") :]
    assert "await stop_stored_secret_sweep(app)" in shutdown


def test_lifespan_reads_ci_token_off_the_event_loop() -> None:
    source = _LIFESPAN.read_text()
    startup = source[: source.find("\n        yield")]
    assert "anyio.to_thread.run_sync(\n" in startup
    assert "_read_github_token\n" in startup
    assert "github_token_data = token_manager.get_token(" not in startup


def test_lifespan_startup_wires_key_preparation_and_sweep() -> None:
    source = _LIFESPAN.read_text()
    startup = source[: source.find("\n        yield")]
    assert "await prepare_stored_secret_key(" in startup
    assert "start_stored_secret_sweep(" in startup
