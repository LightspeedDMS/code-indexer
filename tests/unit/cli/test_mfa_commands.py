"""
Tests for Story #571: Emergency MFA Recovery CLI commands.

Tests disable-mfa and list-mfa-users commands using real SQLite databases.
"""

import os
import sqlite3
import tempfile

import pytest
from click.testing import CliRunner

from code_indexer.server.cli.mfa_commands import (
    create_mfa_auth_group,
    _default_server_dir,
)


def test_default_server_dir():
    """Default server dir points to ~/.cidx-server."""
    result = _default_server_dir()
    assert result.endswith(".cidx-server")
    assert ".cidx-server" in result


@pytest.fixture
def temp_server_dir():
    """Create a temporary server directory with database."""
    with tempfile.TemporaryDirectory() as tmpdir:
        data_dir = os.path.join(tmpdir, "data")
        os.makedirs(data_dir)
        db_path = os.path.join(data_dir, "cidx_server.db")

        conn = sqlite3.connect(db_path)
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS users (
                username TEXT PRIMARY KEY,
                password_hash TEXT,
                role TEXT DEFAULT 'user',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_mfa (
                user_id TEXT UNIQUE NOT NULL,
                encrypted_secret TEXT NOT NULL,
                key_id INTEGER DEFAULT 1,
                mfa_enabled BOOLEAN DEFAULT 0,
                last_used_counter INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );

            CREATE TABLE IF NOT EXISTS user_recovery_codes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                code_hash TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                used_at TIMESTAMP,
                used_ip TEXT
            );
            """
        )
        conn.commit()
        conn.close()
        yield tmpdir


@pytest.fixture
def populated_server_dir(temp_server_dir):
    """Server dir with users and MFA data."""
    db_path = os.path.join(temp_server_dir, "data", "cidx_server.db")
    conn = sqlite3.connect(db_path)

    # Insert users
    conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        ("alice", "hash1", "admin"),
    )
    conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        ("bob", "hash2", "user"),
    )
    conn.execute(
        "INSERT INTO users (username, password_hash, role) VALUES (?, ?, ?)",
        ("charlie", "hash3", "user"),
    )

    # alice has MFA enabled
    conn.execute(
        "INSERT INTO user_mfa (user_id, encrypted_secret, mfa_enabled) VALUES (?, ?, ?)",
        ("alice", "enc_secret_1", 1),
    )
    # bob has MFA row but not enabled
    conn.execute(
        "INSERT INTO user_mfa (user_id, encrypted_secret, mfa_enabled) VALUES (?, ?, ?)",
        ("bob", "enc_secret_2", 0),
    )
    # alice has recovery codes
    conn.execute(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
        ("alice", "code_hash_1"),
    )
    conn.execute(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
        ("alice", "code_hash_2"),
    )
    conn.execute(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
        ("alice", "code_hash_3"),
    )
    # bob has one unused recovery code
    conn.execute(
        "INSERT INTO user_recovery_codes (user_id, code_hash) VALUES (?, ?)",
        ("bob", "code_hash_4"),
    )

    conn.commit()
    conn.close()
    yield temp_server_dir


def _make_cli(server_dir):
    """Create a click group with the MFA auth subgroup for testing."""
    import click

    @click.group()
    @click.pass_context
    def cli(ctx):
        ctx.ensure_object(dict)

    mfa_group = create_mfa_auth_group()
    cli.add_command(mfa_group, "auth")
    return cli


class TestDisableMfa:
    """Tests for the disable-mfa command."""

    def test_disable_mfa_with_force_removes_mfa_data(self, populated_server_dir):
        """Force flag skips confirmation and removes all MFA data."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "alice",
                "--force",
                "--server-dir",
                populated_server_dir,
            ],
        )
        assert result.exit_code == 0
        assert "disabled" in result.output.lower()

        # Verify MFA data removed from database
        db_path = os.path.join(populated_server_dir, "data", "cidx_server.db")
        conn = sqlite3.connect(db_path)
        mfa_row = conn.execute(
            "SELECT * FROM user_mfa WHERE user_id = 'alice'"
        ).fetchone()
        recovery_rows = conn.execute(
            "SELECT * FROM user_recovery_codes WHERE user_id = 'alice'"
        ).fetchall()
        conn.close()
        assert mfa_row is None
        assert len(recovery_rows) == 0

    def test_disable_mfa_with_confirmation_prompt(self, populated_server_dir):
        """Without --force, prompts for username confirmation."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "alice",
                "--server-dir",
                populated_server_dir,
            ],
            input="alice\n",
        )
        assert result.exit_code == 0
        assert "type the username to confirm" in result.output.lower()
        assert "disabled" in result.output.lower()

    def test_disable_mfa_wrong_confirmation_aborts(self, populated_server_dir):
        """Wrong confirmation input aborts without deleting."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "alice",
                "--server-dir",
                populated_server_dir,
            ],
            input="wrong_name\n",
        )
        assert result.exit_code == 1
        assert "aborted" in result.output.lower()

        # Verify MFA data NOT removed
        db_path = os.path.join(populated_server_dir, "data", "cidx_server.db")
        conn = sqlite3.connect(db_path)
        mfa_row = conn.execute(
            "SELECT * FROM user_mfa WHERE user_id = 'alice'"
        ).fetchone()
        conn.close()
        assert mfa_row is not None

    def test_disable_mfa_nonexistent_user(self, populated_server_dir):
        """Disabling MFA for user without MFA reports no MFA found."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "charlie",
                "--force",
                "--server-dir",
                populated_server_dir,
            ],
        )
        assert result.exit_code == 0
        assert "no mfa" in result.output.lower()

    def test_disable_mfa_unknown_user(self, populated_server_dir):
        """Disabling MFA for completely unknown user reports error."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "nonexistent",
                "--force",
                "--server-dir",
                populated_server_dir,
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower()

    def test_disable_mfa_missing_database(self, temp_server_dir):
        """Reports error when database file doesn't exist."""
        cli = _make_cli(temp_server_dir)
        runner = CliRunner()
        # Remove the database
        db_path = os.path.join(temp_server_dir, "data", "cidx_server.db")
        os.unlink(db_path)
        result = runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "alice",
                "--force",
                "--server-dir",
                temp_server_dir,
            ],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()

    def test_disable_mfa_does_not_affect_other_users(self, populated_server_dir):
        """Disabling MFA for alice does not affect bob's data."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        runner.invoke(
            cli,
            [
                "auth",
                "disable-mfa",
                "--username",
                "alice",
                "--force",
                "--server-dir",
                populated_server_dir,
            ],
        )

        db_path = os.path.join(populated_server_dir, "data", "cidx_server.db")
        conn = sqlite3.connect(db_path)
        bob_mfa = conn.execute(
            "SELECT * FROM user_mfa WHERE user_id = 'bob'"
        ).fetchone()
        bob_codes = conn.execute(
            "SELECT * FROM user_recovery_codes WHERE user_id = 'bob'"
        ).fetchall()
        conn.close()
        assert bob_mfa is not None
        assert len(bob_codes) == 1


class TestListMfaUsers:
    """Tests for the list-mfa-users command."""

    def test_list_mfa_users_shows_all_users(self, populated_server_dir):
        """Lists all users with their MFA status."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["auth", "list-mfa-users", "--server-dir", populated_server_dir],
        )
        assert result.exit_code == 0
        assert "alice" in result.output
        assert "bob" in result.output
        assert "charlie" in result.output

    def test_list_mfa_users_shows_enabled_status(self, populated_server_dir):
        """Shows correct MFA enabled/disabled status."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["auth", "list-mfa-users", "--server-dir", populated_server_dir],
        )
        assert result.exit_code == 0
        # alice has MFA enabled, bob has MFA but not enabled, charlie has no MFA
        output_lines = result.output.strip().split("\n")
        # Find alice's line - should show enabled
        alice_line = [line for line in output_lines if "alice" in line]
        assert len(alice_line) == 1
        assert "yes" in alice_line[0].lower() or "enabled" in alice_line[0].lower()

    def test_list_mfa_users_shows_recovery_code_count(self, populated_server_dir):
        """Shows count of unused recovery codes."""
        cli = _make_cli(populated_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["auth", "list-mfa-users", "--server-dir", populated_server_dir],
        )
        assert result.exit_code == 0
        # alice has 3 unused recovery codes
        alice_line = [
            line for line in result.output.strip().split("\n") if "alice" in line
        ]
        assert "3" in alice_line[0]

    def test_list_mfa_users_empty_database(self, temp_server_dir):
        """Shows message when no users exist."""
        cli = _make_cli(temp_server_dir)
        runner = CliRunner()
        result = runner.invoke(
            cli,
            ["auth", "list-mfa-users", "--server-dir", temp_server_dir],
        )
        assert result.exit_code == 0
        assert "no users" in result.output.lower()

    def test_list_mfa_users_missing_database(self, temp_server_dir):
        """Reports error when database doesn't exist."""
        cli = _make_cli(temp_server_dir)
        runner = CliRunner()
        db_path = os.path.join(temp_server_dir, "data", "cidx_server.db")
        os.unlink(db_path)
        result = runner.invoke(
            cli,
            ["auth", "list-mfa-users", "--server-dir", temp_server_dir],
        )
        assert result.exit_code == 1
        assert "not found" in result.output.lower() or "error" in result.output.lower()
