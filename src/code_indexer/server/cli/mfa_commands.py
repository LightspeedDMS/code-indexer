"""Emergency MFA recovery CLI commands (Story #571).

Provides disable-mfa and list-mfa-users commands for server administrators
who need to recover locked-out user accounts. Uses direct SQLite access
so these commands work even when the server is not running.
"""

import os
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

import click


def _default_server_dir() -> str:
    """Return the default server directory path."""
    return str(Path.home() / ".cidx-server")


def _get_db_path(server_dir: str) -> str:
    """Return the database path for the given server directory."""
    return os.path.join(server_dir, "data", "cidx_server.db")


def _validate_db_exists(db_path: str) -> Optional[str]:
    """Validate that the database file exists. Returns error message or None."""
    if not os.path.exists(db_path):
        return f"Database not found at {db_path}"
    return None


def create_mfa_auth_group() -> click.Group:
    """Create the MFA auth command group for server administration."""

    @click.group("auth")
    def mfa_auth_group():
        """Emergency MFA management commands.

        Manage MFA settings directly via database access.
        Use when the server is down or a user is locked out.
        """
        pass

    @mfa_auth_group.command("disable-mfa")
    @click.option("--username", required=True, help="Username to disable MFA for")
    @click.option(
        "--force", is_flag=True, default=False, help="Skip confirmation prompt"
    )
    @click.option(
        "--server-dir",
        type=click.Path(),
        help="Server directory path (default: ~/.cidx-server)",
    )
    def disable_mfa(username: str, force: bool, server_dir: Optional[str]):
        """Disable MFA for a locked-out user.

        Removes all MFA data (secret and recovery codes) for the specified
        user. Requires confirmation unless --force is used.

        Example:
            cidx server auth disable-mfa --username alice
            cidx server auth disable-mfa --username alice --force
        """
        server_dir = server_dir or _default_server_dir()
        db_path = _get_db_path(server_dir)

        error = _validate_db_exists(db_path)
        if error:
            click.echo(f"Error: {error}", err=True)
            sys.exit(1)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            # Check user exists
            user_row = conn.execute(
                "SELECT username FROM users WHERE username = ?",
                (username,),
            ).fetchone()
            if user_row is None:
                click.echo(f"Error: User '{username}' not found.", err=True)
                sys.exit(1)

            # Check if user has MFA
            mfa_row = conn.execute(
                "SELECT mfa_enabled FROM user_mfa WHERE user_id = ?",
                (username,),
            ).fetchone()
            if mfa_row is None:
                click.echo(f"No MFA configuration found for user '{username}'.")
                return

            # Confirmation prompt unless --force
            if not force:
                click.echo(
                    f"WARNING: This will permanently remove all MFA data "
                    f"for user '{username}'."
                )
                click.echo("Type the username to confirm: ", nl=False)
                confirmation = input()
                if confirmation != username:
                    click.echo("Aborted. Username did not match.", err=True)
                    sys.exit(1)

            # Delete MFA data
            conn.execute("DELETE FROM user_mfa WHERE user_id = ?", (username,))
            conn.execute(
                "DELETE FROM user_recovery_codes WHERE user_id = ?",
                (username,),
            )
            conn.commit()

            timestamp = datetime.utcnow().isoformat()
            click.echo(f"MFA disabled for user '{username}' at {timestamp}.")
        finally:
            conn.close()

    @mfa_auth_group.command("list-mfa-users")
    @click.option(
        "--server-dir",
        type=click.Path(),
        help="Server directory path (default: ~/.cidx-server)",
    )
    def list_mfa_users(server_dir: Optional[str]):
        """List all users with their MFA status.

        Shows username, MFA enabled status, and remaining recovery codes.

        Example:
            cidx server auth list-mfa-users
        """
        server_dir = server_dir or _default_server_dir()
        db_path = _get_db_path(server_dir)

        error = _validate_db_exists(db_path)
        if error:
            click.echo(f"Error: {error}", err=True)
            sys.exit(1)

        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        try:
            rows = conn.execute(
                """
                SELECT
                    u.username,
                    CASE WHEN m.mfa_enabled = 1 THEN 'Yes' ELSE 'No' END
                        AS mfa_enabled,
                    COALESCE(
                        (SELECT COUNT(*) FROM user_recovery_codes rc
                         WHERE rc.user_id = u.username
                         AND rc.used_at IS NULL), 0
                    ) AS recovery_codes_remaining,
                    m.updated_at AS last_used
                FROM users u
                LEFT JOIN user_mfa m ON u.username = m.user_id
                ORDER BY u.username
                """,
            ).fetchall()

            if not rows:
                click.echo("No users found in the database.")
                return

            # Header
            click.echo(
                f"{'Username':<20} {'MFA Enabled':<14} "
                f"{'Recovery Codes':<16} {'Last Updated'}"
            )
            click.echo("-" * 72)

            for row in rows:
                mfa_status = row["mfa_enabled"] if row["mfa_enabled"] else "No"
                codes = row["recovery_codes_remaining"]
                last_used = row["last_used"] or "N/A"
                click.echo(
                    f"{row['username']:<20} {mfa_status:<14} {codes:<16} {last_used}"
                )
        finally:
            conn.close()

    return mfa_auth_group
