"""The CI token JSON store (CITokenManager without a database backend)
never deletes a stored value: an unreadable token is kept and marked as
needing re-entry; a readable legacy token is rewritten in the v2 format."""

from __future__ import annotations

import json
import os
import stat
from pathlib import Path

from code_indexer.server.services.ci_token_manager import CITokenManager
from code_indexer.server.services.token_encryption import (
    derive_key_from_salt,
    encrypt_token,
)

GH_TOKEN = "ghp_" + "j" * 36


def _write_store(server_dir: Path, token_value: str) -> Path:
    path = server_dir / "ci_tokens.json"
    path.write_text(json.dumps({"github": {"token": token_value, "base_url": None}}))
    return path


def test_unreadable_json_token_is_kept_and_marked(tmp_path: Path) -> None:
    foreign = encrypt_token(GH_TOKEN, derive_key_from_salt("another-node.example"))
    path = _write_store(tmp_path, foreign)
    mgr = CITokenManager(server_dir_path=str(tmp_path))
    assert mgr.get_token("github") is None
    stored = json.loads(path.read_text())["github"]
    assert stored["token"] == foreign
    assert stored["needs_reentry"] is True
    again = CITokenManager(server_dir_path=str(tmp_path))
    assert again.list_tokens()["github"].needs_reentry is True
    assert again.list_tokens()["github"].configured is True


def test_readable_legacy_json_token_is_rewritten_as_v2(tmp_path: Path) -> None:
    legacy = encrypt_token(GH_TOKEN, derive_key_from_salt(os.uname().nodename))
    path = _write_store(tmp_path, legacy)
    data = CITokenManager(server_dir_path=str(tmp_path)).get_token("github")
    assert data is not None and data.token == GH_TOKEN
    assert json.loads(path.read_text())["github"]["token"].startswith("v2:")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert not [p for p in tmp_path.iterdir() if p.name.endswith(".tmp")]


def test_saving_a_new_token_clears_the_mark(tmp_path: Path) -> None:
    foreign = encrypt_token(GH_TOKEN, derive_key_from_salt("another-node.example"))
    _write_store(tmp_path, foreign)
    mgr = CITokenManager(server_dir_path=str(tmp_path))
    mgr.get_token("github")
    mgr.save_token("github", GH_TOKEN)
    assert mgr.list_tokens()["github"].needs_reentry is False
    data = mgr.get_token("github")
    assert data is not None and data.token == GH_TOKEN
