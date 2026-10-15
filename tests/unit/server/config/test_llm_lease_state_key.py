"""LLM lease state is encrypted under the random stored-secret key; a state
written with the legacy host-derived key stays loadable and is rewritten."""

from __future__ import annotations

import binascii
import json
from pathlib import Path
from typing import Any

import pytest

from code_indexer.server.config.llm_lease_state import (
    LlmLeaseState,
    LlmLeaseStateManager,
)
from code_indexer.server.services.token_encryption import decrypt_single

STATE = LlmLeaseState(lease_id="lease-1", credential_id="cred-1")


def _stored_blob(server_dir: Path) -> str:
    envelope = json.loads((server_dir / "llm_lease_state.json").read_text())
    return str(envelope["encrypted_state"])


def test_saved_state_is_v2_and_not_readable_with_host_key(tmp_path: Path) -> None:
    mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    mgr.save_state(STATE)
    blob = _stored_blob(tmp_path)
    assert blob.startswith("v2:")
    with pytest.raises((ValueError, binascii.Error)):
        decrypt_single(blob, mgr._encryption_key)
    assert LlmLeaseStateManager(server_dir_path=str(tmp_path)).load_state() == STATE


def test_legacy_host_key_state_is_loaded_and_rewritten(tmp_path: Path) -> None:
    mgr = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    legacy = mgr._encrypt(
        json.dumps(
            {
                "lease_id": "lease-1",
                "credential_id": "cred-1",
                "credential_type": "oauth",
            }
        )
    )
    (tmp_path / "llm_lease_state.json").write_text(
        json.dumps({"encrypted_state": legacy})
    )
    assert not legacy.startswith("v2:")
    assert LlmLeaseStateManager(server_dir_path=str(tmp_path)).load_state() == STATE
    assert _stored_blob(tmp_path).startswith("v2:")


def test_concurrent_save_during_legacy_rewrite_is_not_lost(tmp_path: Path) -> None:
    import stat
    import threading

    newer = LlmLeaseState(lease_id="lease-2", credential_id="cred-2")
    writer = LlmLeaseStateManager(server_dir_path=str(tmp_path))
    legacy = writer._encrypt(
        json.dumps(
            {
                "lease_id": "lease-1",
                "credential_id": "cred-1",
                "credential_type": "oauth",
            }
        )
    )
    (tmp_path / "llm_lease_state.json").write_text(
        json.dumps({"encrypted_state": legacy})
    )
    saver = threading.Thread(target=writer.save_state, args=(newer,))

    class _RacingManager(LlmLeaseStateManager):
        def _rewritten_blob(self, encrypted_blob: str) -> Any:
            result = super()._rewritten_blob(encrypted_blob)
            saver.start()  # a concurrent save arrives mid-rewrite
            saver.join(timeout=0.5)
            return result

    loaded = _RacingManager(server_dir_path=str(tmp_path)).load_state()
    saver.join(timeout=10)
    assert loaded == STATE
    assert LlmLeaseStateManager(server_dir_path=str(tmp_path)).load_state() == newer
    path = tmp_path / "llm_lease_state.json"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_state_from_another_server_dir_is_unreadable(tmp_path: Path) -> None:
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    LlmLeaseStateManager(server_dir_path=str(a)).save_state(STATE)
    (b / "llm_lease_state.json").write_text((a / "llm_lease_state.json").read_text())
    with pytest.raises(ValueError):
        LlmLeaseStateManager(server_dir_path=str(b)).load_state()
