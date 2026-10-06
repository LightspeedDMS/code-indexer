"""``token_suffix`` in a git credential listing follows the stored-secret
display rule: nothing for a token under 20 characters, the
last 4 for a longer one. Real GitCredentialManager over a real SQLite DB;
the Web list partial is rendered with the real template environment.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.git_credential_manager import GitCredentialManager
from code_indexer.server.storage.database_manager import DatabaseSchema
from code_indexer.server.storage.sqlite_backends import GitCredentialsSqliteBackend

# Neutral sample tokens (never real credentials).
SHORT_TOKENS = ["q7Z", "op-short-pat-0019ch"]
LONG_TOKEN = "example-sample-pat-0000" + "Tk9w"


def _listed_suffix(tmp_path: Path, token: str) -> str:
    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    manager = GitCredentialManager(db_path)
    GitCredentialsSqliteBackend(db_path).upsert_credential(
        credential_id="cred-example-001",
        username="example-user",
        forge_type="github",
        forge_host="github.example.com",
        encrypted_token=manager._encrypt_token(token),
        git_user_name="Example User",
        git_user_email="user@example.com",
        forge_username="example-forge-user",
    )
    (cred,) = manager.list_credentials("example-user")
    assert "encrypted_token" not in cred
    return str(cred["token_suffix"])


@pytest.mark.parametrize("token", SHORT_TOKENS, ids=["3-chars", "19-chars"])
def test_short_token_reveals_no_suffix(tmp_path, token) -> None:
    assert len(token) < 20
    assert _listed_suffix(tmp_path, token) == ""


def test_long_token_reveals_last_four_only(tmp_path) -> None:
    assert len(LONG_TOKEN) >= 20
    assert _listed_suffix(tmp_path, LONG_TOKEN) == "Tk9w"


@pytest.mark.parametrize(
    "suffix,expected", [("", "<code>****</code>"), ("Tk9w", "<code>****Tk9w</code>")]
)
def test_list_partial_renders_suffix_sensibly(suffix, expected) -> None:
    from code_indexer.server.web.routes import templates

    html = templates.get_template("partials/git_credentials_list.html").render(
        git_credentials=[
            {
                "credential_id": "cred-example-001",
                "forge_type": "github",
                "forge_host": "github.example.com",
                "token_suffix": suffix,
            }
        ]
    )
    assert expected in html
