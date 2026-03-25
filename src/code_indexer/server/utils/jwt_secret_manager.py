"""
JWT Secret Key Management for CIDX Server.

Handles persistent storage of JWT secret keys to ensure tokens remain valid
across server restarts.

In SQLite (standalone) mode: stores secret in ~/.cidx-server/.jwt_secret with
secure file permissions.

In PostgreSQL (cluster) mode: stores secret in the cluster_secrets table so
that all nodes in the cluster share the same signing key (Story #528).
"""

import secrets
from contextlib import contextmanager
from pathlib import Path
from typing import Optional


class JWTSecretManager:
    """
    Manages persistent JWT secret keys.

    Ensures JWT secret keys are stored securely and persist across server
    restarts. In standalone (SQLite) mode, uses file system storage with
    appropriate security permissions. In cluster (PostgreSQL) mode, uses a
    shared cluster_secrets table so every node signs and verifies tokens with
    the same key.
    """

    def __init__(
        self,
        server_dir_path: Optional[str] = None,
        pg_dsn: Optional[str] = None,
    ):
        """
        Initialize JWT secret manager.

        Args:
            server_dir_path: Path to server directory (defaults to ~/.cidx-server)
            pg_dsn: PostgreSQL DSN string.  When provided, secrets are stored in
                    and read from the cluster_secrets PostgreSQL table instead of
                    a local file.  When None, file-based storage is used (AC5).
        """
        if server_dir_path:
            self.server_dir = Path(server_dir_path)
        else:
            self.server_dir = Path.home() / ".cidx-server"

        self.secret_file_path = self.server_dir / ".jwt_secret"
        self._ensure_server_directory_exists()

        self._pg_dsn: Optional[str] = pg_dsn
        self._pg_schema_ensured: bool = False

    # ------------------------------------------------------------------
    # Internal helpers — server directory
    # ------------------------------------------------------------------

    def _ensure_server_directory_exists(self):
        """Ensure server directory exists."""
        self.server_dir.mkdir(exist_ok=True)

    # ------------------------------------------------------------------
    # PostgreSQL helpers
    # ------------------------------------------------------------------

    @contextmanager
    def _pg_connect(self):
        """
        Context manager that yields an open psycopg v3 connection.

        The caller must call conn.commit() after any mutations.
        Overrideable in tests without touching psycopg globals.
        """
        try:
            import psycopg  # type: ignore[import]
        except ImportError as exc:
            raise ImportError(
                "psycopg (v3) is required for PostgreSQL JWT secret storage. "
                "Install it with: pip install psycopg"
            ) from exc

        conn = psycopg.connect(self._pg_dsn)
        try:
            yield conn
        finally:
            conn.close()

    def _ensure_pg_schema(self) -> None:
        """Create cluster_secrets table if it does not already exist (AC6)."""
        with self._pg_connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS cluster_secrets (
                    key_name   TEXT        PRIMARY KEY,
                    key_value  TEXT        NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
                """
            )
            conn.commit()

    def _get_or_create_secret_pg(self) -> str:
        """
        Get or create the JWT secret in PostgreSQL — race-condition safe (AC4).

        Pattern:
          1. Try to read existing secret.
          2. If absent, generate a new one and INSERT … ON CONFLICT DO NOTHING.
          3. Re-read so the *winning* value (which may have been inserted by
             another node) is always returned.
        """
        if not self._pg_schema_ensured:
            self._ensure_pg_schema()
            self._pg_schema_ensured = True
        with self._pg_connect() as conn:
            # 1. Try to read existing secret
            row = conn.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = 'jwt_secret'"
            ).fetchone()
            if row:
                return row[0]  # type: ignore[no-any-return]

            # 2. Generate + insert (race-safe with ON CONFLICT DO NOTHING)
            new_secret = secrets.token_urlsafe(32)
            conn.execute(
                """
                INSERT INTO cluster_secrets (key_name, key_value)
                VALUES ('jwt_secret', %s)
                ON CONFLICT (key_name) DO NOTHING
                """,
                (new_secret,),
            )
            conn.commit()

            # 3. Re-read the winner (another node may have beaten us)
            row = conn.execute(
                "SELECT key_value FROM cluster_secrets WHERE key_name = 'jwt_secret'"
            ).fetchone()
            # row must exist at this point — we either inserted or ON CONFLICT
            # preserved an existing row
            return row[0]  # type: ignore[index, no-any-return]

    def _rotate_secret_pg(self) -> str:
        """Update the JWT secret in PostgreSQL (cluster mode rotate).

        Uses UPSERT so the rotation succeeds even if the row was
        removed (e.g., DB recovery).  Without this, a bare UPDATE on a
        missing row would silently match zero rows and the new secret
        would never be persisted.
        """
        if not self._pg_schema_ensured:
            self._ensure_pg_schema()
            self._pg_schema_ensured = True
        new_secret = secrets.token_urlsafe(32)
        with self._pg_connect() as conn:
            conn.execute(
                """
                INSERT INTO cluster_secrets (key_name, key_value)
                VALUES ('jwt_secret', %s)
                ON CONFLICT (key_name) DO UPDATE
                    SET key_value = EXCLUDED.key_value,
                        updated_at = NOW()
                """,
                (new_secret,),
            )
            conn.commit()
        return new_secret

    def _migrate_local_secret_to_pg(self) -> None:
        """
        Copy the existing local-file secret to PostgreSQL (AC7).

        Called during upgrade from standalone to cluster mode.  Uses
        ON CONFLICT DO NOTHING so that if another node already wrote the
        secret to PG, the local-file value does not overwrite it.
        """
        if not self._pg_dsn:
            return
        if not self.secret_file_path.exists():
            return
        local_secret = self._load_secret_from_file()
        if not local_secret:
            return
        if not self._pg_schema_ensured:
            self._ensure_pg_schema()
            self._pg_schema_ensured = True
        with self._pg_connect() as conn:
            conn.execute(
                """
                INSERT INTO cluster_secrets (key_name, key_value)
                VALUES ('jwt_secret', %s)
                ON CONFLICT (key_name) DO NOTHING
                """,
                (local_secret,),
            )
            conn.commit()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def get_or_create_secret(self) -> str:
        """
        Get existing JWT secret or create a new one if none exists.

        In PostgreSQL mode: reads from / writes to cluster_secrets table.
        In file mode:
          Priority order:
          1. Existing secret file
          2. JWT_SECRET_KEY environment variable
          3. Generate new random secret

        Returns:
            JWT secret key string
        """
        if self._pg_dsn:
            # AC7: If upgrading from file to PG mode, migrate local secret first
            self._migrate_local_secret_to_pg()
            return self._get_or_create_secret_pg()

        # --- file-based path (unchanged from pre-Story-#528 behaviour) ---

        # Try to load existing secret from file
        if self.secret_file_path.exists():
            try:
                secret = self._load_secret_from_file()
                if secret:
                    return secret
            except Exception:
                # If file is corrupted, we'll create a new secret
                pass

        # Try to get secret from environment variable
        import os

        env_secret = os.environ.get("JWT_SECRET_KEY")
        if env_secret and env_secret.strip():
            self._save_secret_to_file(env_secret.strip())
            return env_secret.strip()

        # Generate new random secret
        secret = secrets.token_urlsafe(32)
        self._save_secret_to_file(secret)
        return secret

    def _load_secret_from_file(self) -> Optional[str]:
        """
        Load JWT secret from file.

        Returns:
            Secret string if successful, None if file doesn't exist or is empty
        """
        try:
            secret = self.secret_file_path.read_text().strip()
            return secret if secret else None
        except (FileNotFoundError, PermissionError):
            return None

    def _save_secret_to_file(self, secret: str):
        """
        Save JWT secret to file with secure permissions.

        Args:
            secret: JWT secret string to save
        """
        # Write secret to file
        self.secret_file_path.write_text(secret)

        # Set secure permissions (readable by owner only)
        self.secret_file_path.chmod(0o600)

    def rotate_secret(self) -> str:
        """
        Generate and save a new JWT secret (invalidates all existing tokens).

        In PostgreSQL mode: updates the cluster_secrets table.
        In file mode: writes a new secret to the local file.

        Returns:
            New JWT secret key string
        """
        if self._pg_dsn:
            return self._rotate_secret_pg()

        new_secret = secrets.token_urlsafe(32)
        self._save_secret_to_file(new_secret)
        return new_secret
