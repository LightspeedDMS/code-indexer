"""
Migration service for legacy JSON to SQLite migration.

Story #702: Migrate Central JSON Files to SQLite

Provides one-time migration of legacy JSON files to SQLite database,
with idempotency support for safe re-runs.
"""

import json
import logging
import os
import sqlite3
import stat
from contextlib import closing
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from .sqlite_backends import (
    GlobalReposSqliteBackend,
    UsersSqliteBackend,
    SyncJobsSqliteBackend,
    CITokensSqliteBackend,
    SessionsSqliteBackend,
    SSHKeysSqliteBackend,
    GoldenRepoMetadataSqliteBackend,
    BackgroundJobsSqliteBackend,
)
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)

# Legacy files holding credentials (password hashes, tokens) end owner-only.
OWNER_ONLY_MODE = 0o600


def _restrict_to_owner(path: Path) -> None:
    """Drop every group/other permission bit of *path*; never add a bit."""
    os.chmod(str(path), stat.S_IMODE(path.stat().st_mode) & OWNER_ONLY_MODE)


class MigrationService:
    """
    Service for migrating legacy JSON files to SQLite.

    Handles migration of:
    - global_registry.json -> global_repos table
    - users.json -> users, user_api_keys, user_mcp_credentials tables

    Migration is idempotent - safe to run multiple times.
    """

    def __init__(
        self,
        source_dir: str,
        db_path: str,
        *,
        prepare_new_account: Optional[Callable[[str], None]] = None,
        import_users: bool = True,
    ) -> None:
        """
        Initialize the migration service.

        Args:
            source_dir: Directory containing legacy JSON files.
            db_path: Path to target SQLite database.
            prepare_new_account: The server's account pre-creation step
                (refuses a name whose earlier repositories remain, removes
                rows left under it); run before the import creates a name.
            import_users: False in cluster storage mode, where accounts live
                in PostgreSQL: the local users.json import is skipped.
        """
        self.source_dir = source_dir
        self.db_path = db_path
        self._prepare_new_account = prepare_new_account
        self._import_users = import_users

    def is_migration_needed(self) -> bool:
        """
        Check if migration is needed.

        Returns:
            True if legacy JSON files exist, False otherwise.
        """
        source_path = Path(self.source_dir)
        json_files = [
            "global_registry.json",
            "users.json",
            "jobs.json",
            "ci_tokens.json",
            "invalidated_sessions.json",
        ]

        for json_file in json_files:
            if (source_path / json_file).exists():
                return True

        # Check for SSH keys directory with JSON files
        ssh_keys_dir = source_path / "ssh_keys"
        if ssh_keys_dir.exists() and list(ssh_keys_dir.glob("*.json")):
            return True

        return False

    def migrate_global_repos(self) -> Dict[str, Any]:
        """
        Migrate global_registry.json to SQLite.

        Returns:
            Migration result with counts.
        """
        source_file = Path(self.source_dir) / "global_registry.json"

        if not source_file.exists():
            logger.info("No global_registry.json found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                registry_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-197", f"Failed to read global_registry.json: {e}"
                )
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        backend = GlobalReposSqliteBackend(self.db_path)
        migrated = 0
        already_exists = 0
        errors = 0

        try:
            for alias_name, repo_data in registry_data.items():
                try:
                    backend.register_repo(
                        alias_name=alias_name,
                        repo_name=repo_data.get("repo_name", ""),
                        repo_url=repo_data.get("repo_url"),
                        index_path=repo_data.get("index_path", ""),
                        enable_temporal=repo_data.get("enable_temporal", False),
                        temporal_options=repo_data.get("temporal_options"),
                    )
                    migrated += 1
                    logger.debug(f"Migrated repo: {alias_name}")
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"Repo already exists, skipping: {alias_name}")
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-198",
                            f"Failed to migrate repo {alias_name}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"Global repos migration complete: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )

        # Rename JSON file to .migrated after successful migration (Story #702)
        # Only real errors block rename - already_exists is expected for idempotency
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                os.rename(str(source_file), str(source_file) + ".migrated")
                logger.info(f"Renamed {source_file} to {source_file}.migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-199",
                        f"Failed to rename {source_file} to .migrated: {e}",
                    )
                )

        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_global_repos_from_path(self, source_path: str) -> Dict[str, Any]:
        """
        Migrate global_registry.json from a specific path to SQLite.

        This method allows migrating from locations other than the default
        source_dir, such as the golden-repos subdirectory.

        Args:
            source_path: Full path to the global_registry.json file.

        Returns:
            Migration result with counts.
        """
        source_file = Path(source_path)

        if not source_file.exists():
            logger.info(f"No global_registry.json found at {source_path}, skipping")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                registry_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-200",
                    f"Failed to read global_registry.json from {source_path}: {e}",
                )
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        backend = GlobalReposSqliteBackend(self.db_path)
        migrated = 0
        already_exists = 0
        errors = 0

        try:
            for alias_name, repo_data in registry_data.items():
                try:
                    backend.register_repo(
                        alias_name=alias_name,
                        repo_name=repo_data.get("repo_name", ""),
                        repo_url=repo_data.get("repo_url"),
                        index_path=repo_data.get("index_path", ""),
                        enable_temporal=repo_data.get("enable_temporal", False),
                        temporal_options=repo_data.get("temporal_options"),
                    )
                    migrated += 1
                    logger.debug(f"Migrated repo from golden-repos: {alias_name}")
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"Repo already exists, skipping: {alias_name}")
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-201",
                            f"Failed to migrate repo {alias_name}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"Global repos migration from {source_path}: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )

        # Rename JSON file to .migrated after successful migration
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                os.rename(str(source_file), str(source_file) + ".migrated")
                logger.info(f"Renamed {source_file} to {source_file}.migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-202", f"Failed to rename {source_file}: {e}"
                    )
                )

        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_users(self) -> Dict[str, Any]:
        """
        Migrate users.json to SQLite with normalized tables.

        The import completes -- completion recorded in the database, then
        users.json renamed to an owner-only ``users.json.migrated`` -- only
        when every entry was imported or already had an account.  Otherwise
        users.json is atomically rewritten (owner-only) to hold only the
        entries still to import, nothing is recorded, and the import runs
        again at the next start; start-up seeds no initial administrator
        while it is pending.  Entries that can never be imported keep it
        pending for good: the ERROR is logged at every start until an
        operator fixes or removes them in users.json.  Rows are written
        before the record and the record before the rename, so a stop at
        any point leaves the rows written or the import pending.

        Returns:
            Migration result with counts.
        """
        source_file = Path(self.source_dir) / "users.json"

        if not source_file.exists():
            logger.info("No users.json found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        # Owner-only from the first look, whatever happens to it later.
        try:
            _restrict_to_owner(source_file)
        except OSError as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-205",
                    f"Failed to make {source_file} owner-only: {e}",
                )
            )

        # Completion is recorded in the database: once the import ran it
        # never runs again, even when the file could not be renamed.
        if self._users_import_recorded():
            logger.info("users.json was already imported; not importing again")
            self._rename_imported(source_file)
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                users_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log("MCP-GENERAL-203", f"Failed to read users.json: {e}")
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        backend = UsersSqliteBackend(self.db_path)
        migrated = 0
        already_exists = 0
        errors = 0

        not_imported: Dict[str, str] = {}
        try:
            for username, user_data in users_data.items():
                try:
                    if backend.get_user(username) is not None:
                        raise sqlite3.IntegrityError(f"account exists: {username}")
                    # New name: the shared pre-creation step (refused while an
                    # earlier account's repositories remain; rows left under
                    # the name removed) runs before the account is created.
                    if self._prepare_new_account is not None:
                        self._prepare_new_account(username)
                    backend.create_user(
                        username=username,
                        password_hash=user_data.get("password_hash", ""),
                        role=user_data.get("role", "normal_user"),
                        email=user_data.get("email"),
                    )
                    self._import_legacy_credentials(backend, username, user_data)
                    migrated += 1
                    logger.debug(f"Migrated user: {username}")
                except sqlite3.IntegrityError:
                    # The name already has an account: it is never overwritten
                    # or augmented.  Only the seeded bootstrap admin, still on
                    # its default password, adopts its legacy password and
                    # credentials (an upgrade must not keep the default).
                    if self._is_untouched_bootstrap_admin(backend, username):
                        legacy_hash = user_data.get("password_hash", "")
                        if legacy_hash:
                            backend.update_password_hash(username, legacy_hash)
                        self._import_legacy_credentials(backend, username, user_data)
                        logger.info(
                            f"Bootstrap admin adopted its legacy account: {username}"
                        )
                    else:
                        logger.info(
                            f"Account already exists, legacy entry ignored: {username}"
                        )
                    already_exists += 1
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-204", f"Failed to migrate user {username}: {e}"
                        )
                    )
                    not_imported[username] = str(e)
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"Users migration complete: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )
        result = {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }
        if not_imported:
            # Not complete: users.json keeps only the entries still to import
            # (imported and existing names leave it, so a retry never brings
            # back an account removed after its import, unless the rewrite of
            # the pending file fails), no completion is recorded, and the
            # import runs again at the next start.
            self._keep_pending_entries(
                source_file, {name: users_data[name] for name in not_imported}
            )
            logger.error(
                f"users.json import incomplete: {len(not_imported)} entries could "
                "not be imported; it runs again at the next start and the initial "
                "admin is not seeded until it completes.  Fix or remove those "
                "entries in users.json."
            )
            return result

        # Every entry was imported or already had an account.  Order: rows
        # (above), then the durable completion record, then the rename -- a
        # stop at any point leaves the import pending or its rows written.
        try:
            self._record_users_import()
        except Exception as e:  # noqa: BLE001 - the rename below still records it
            logger.error(f"Recording the users.json import as complete failed: {e}")
        self._rename_imported(source_file)
        return result

    @staticmethod
    def _keep_pending_entries(source_file: Path, entries: Dict[str, Any]) -> None:
        """Atomically rewrite users.json to hold only *entries*.

        A failed rewrite leaves the original file in place: the import stays
        pending either way, and its already-imported names are skipped.
        """
        temp_file = source_file.with_name(source_file.name + ".tmp")
        try:
            temp_file.unlink(missing_ok=True)  # never reuse a stale file's mode
            fd = os.open(
                str(temp_file), os.O_WRONLY | os.O_CREAT | os.O_EXCL, OWNER_ONLY_MODE
            )
            try:
                f = os.fdopen(fd, "w")
            except BaseException:
                os.close(fd)
                raise
            with f:
                os.fchmod(f.fileno(), OWNER_ONLY_MODE)  # exact, whatever the umask
                json.dump(entries, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(str(temp_file), str(source_file))
        except OSError as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-205",
                    f"Failed to keep pending users.json entries: {e}",
                )
            )
            try:
                temp_file.unlink()
            except FileNotFoundError:
                pass

    _IMPORT_STATE_DDL = (
        "CREATE TABLE IF NOT EXISTS legacy_import_state ("
        "name TEXT PRIMARY KEY, completed_at TEXT NOT NULL)"
    )
    _USERS_IMPORT = "users.json"

    def has_pending_users_import(self) -> bool:
        """True while a users.json import with accounts is still to run.

        That is: this service imports users, users.json exists, its import is
        not recorded as complete, and the file holds at least one entry.  A
        file that cannot be read counts as pending: it may hold accounts, and
        the import itself reports the read failure.
        """
        source_file = Path(self.source_dir) / "users.json"
        if not self._import_users or not source_file.exists():
            return False
        if self._users_import_recorded():
            return False
        try:
            with open(source_file, "r") as f:
                users_data = json.load(f)
        except (json.JSONDecodeError, OSError):
            return True
        return bool(users_data)

    def _users_import_recorded(self) -> bool:
        """True once the users.json import has completed (database marker)."""
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            conn.execute(self._IMPORT_STATE_DDL)
            row = conn.execute(
                "SELECT 1 FROM legacy_import_state WHERE name = ?",
                (self._USERS_IMPORT,),
            ).fetchone()
        return row is not None

    def _record_users_import(self) -> None:
        """Record the users.json import as complete (idempotent)."""
        with closing(sqlite3.connect(self.db_path, timeout=30)) as conn:
            conn.execute(self._IMPORT_STATE_DDL)
            conn.execute(
                "INSERT OR IGNORE INTO legacy_import_state (name, completed_at) "
                "VALUES (?, datetime('now'))",
                (self._USERS_IMPORT,),
            )
            conn.commit()

    @staticmethod
    def _rename_imported(source_file: Path) -> None:
        """Rename an imported users.json to .migrated, owner-only; a failure
        is loud."""
        try:
            _restrict_to_owner(source_file)
            os.rename(str(source_file), str(source_file) + ".migrated")
            logger.info(f"Renamed {source_file} to {source_file}.migrated")
        except OSError as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-205",
                    f"Failed to rename {source_file} to .migrated: {e}",
                )
            )

    @staticmethod
    def _is_untouched_bootstrap_admin(
        backend: UsersSqliteBackend, username: str
    ) -> bool:
        """True for the seeded bootstrap admin still on its default password."""
        if username != "admin":
            return False
        from code_indexer.server.auth.password_manager import PasswordManager

        existing = backend.get_user(username)
        if existing is None or not existing.get("password_hash"):
            return False
        return PasswordManager().verify_password("admin", existing["password_hash"])

    @staticmethod
    def _import_legacy_credentials(
        backend: UsersSqliteBackend, username: str, user_data: Dict[str, Any]
    ) -> None:
        """Add the legacy API keys and MCP credentials of *username*."""
        for key in user_data.get("api_keys", []):
            try:
                backend.add_api_key(
                    username=username,
                    key_id=key.get("key_id", ""),
                    key_hash=key.get("hash", ""),
                    key_prefix=key.get("key_prefix", ""),
                    name=key.get("name"),
                )
            except sqlite3.IntegrityError:
                logger.debug(f"API key already exists: {key.get('key_id', '')}")
        for cred in user_data.get("mcp_credentials", []):
            try:
                backend.add_mcp_credential(
                    username=username,
                    credential_id=cred.get("credential_id", ""),
                    client_id=cred.get("client_id", ""),
                    client_secret_hash=cred.get("client_secret_hash", ""),
                    client_id_prefix=cred.get("client_id_prefix", ""),
                    name=cred.get("name"),
                )
            except sqlite3.IntegrityError:
                logger.debug(
                    f"MCP credential already exists: {cred.get('credential_id', '')}"
                )

    def migrate_sync_jobs(self) -> Dict[str, Any]:
        """Migrate jobs.json to SQLite sync_jobs table."""
        source_file = Path(self.source_dir) / "jobs.json"
        if not source_file.exists():
            logger.info("No jobs.json found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                jobs_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log("MCP-GENERAL-206", f"Failed to read jobs.json: {e}")
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        job_records = {k: v for k, v in jobs_data.items() if not k.startswith("_")}
        backend = SyncJobsSqliteBackend(self.db_path)
        migrated, already_exists, errors = 0, 0, 0

        try:
            for job_id, job_data in job_records.items():
                try:
                    backend.create_job(
                        job_id=job_id,
                        username=job_data.get("username", ""),
                        user_alias=job_data.get("user_alias", ""),
                        job_type=job_data.get("job_type", ""),
                        status=job_data.get("status", ""),
                        repository_url=job_data.get("repository_url"),
                    )
                    migrated += 1
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"Sync job already exists, skipping: {job_id}")
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-207",
                            f"Failed to migrate sync job {job_id}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"Sync jobs migration: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                os.rename(str(source_file), str(source_file) + ".migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-208", f"Failed to rename {source_file}: {e}"
                    )
                )
        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_background_jobs(self) -> Dict[str, Any]:
        """Migrate jobs.json to SQLite background_jobs table."""
        source_file = Path(self.source_dir) / "jobs.json"
        if not source_file.exists():
            logger.info("No jobs.json found, skipping background jobs migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                jobs_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log("MCP-GENERAL-209", f"Failed to read jobs.json: {e}")
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        # Filter out internal keys prefixed with underscore
        job_records = {k: v for k, v in jobs_data.items() if not k.startswith("_")}
        backend = BackgroundJobsSqliteBackend(self.db_path)
        migrated, already_exists, errors = 0, 0, 0

        try:
            for job_id, job_data in job_records.items():
                try:
                    backend.save_job(
                        job_id=job_id,
                        operation_type=job_data.get("operation_type", ""),
                        status=job_data.get("status", ""),
                        created_at=job_data.get("created_at", ""),
                        started_at=job_data.get("started_at"),
                        completed_at=job_data.get("completed_at"),
                        result=job_data.get("result"),
                        error=job_data.get("error"),
                        progress=job_data.get("progress", 0),
                        username=job_data.get("username", ""),
                        is_admin=job_data.get("is_admin", False),
                        cancelled=job_data.get("cancelled", False),
                        repo_alias=job_data.get("repo_alias"),
                        resolution_attempts=job_data.get("resolution_attempts", 0),
                        claude_actions=job_data.get("claude_actions"),
                        failure_reason=job_data.get("failure_reason"),
                        extended_error=job_data.get("extended_error"),
                        language_resolution_status=job_data.get(
                            "language_resolution_status"
                        ),
                    )
                    migrated += 1
                    logger.debug(f"Migrated background job: {job_id}")
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"Background job already exists, skipping: {job_id}")
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-210",
                            f"Failed to migrate background job {job_id}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"Background jobs migration: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                os.rename(str(source_file), str(source_file) + ".migrated")
                logger.info(f"Renamed {source_file} to {source_file}.migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-211", f"Failed to rename {source_file}: {e}"
                    )
                )
        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_ci_tokens(self) -> Dict[str, Any]:
        """Migrate ci_tokens.json to SQLite ci_tokens table."""
        source_file = Path(self.source_dir) / "ci_tokens.json"
        if not source_file.exists():
            logger.info("No ci_tokens.json found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                tokens_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-212", f"Failed to read ci_tokens.json: {e}"
                )
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        backend = CITokensSqliteBackend(self.db_path)
        migrated, already_exists, errors = 0, 0, 0

        try:
            for platform, token_data in tokens_data.items():
                try:
                    # Support both "token" (legacy JSON format) and "encrypted_token" keys
                    encrypted_token = token_data.get("token") or token_data.get(
                        "encrypted_token", ""
                    )
                    backend.save_token(
                        platform=platform,
                        encrypted_token=encrypted_token,
                        base_url=token_data.get("base_url"),
                    )
                    migrated += 1
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"CI token already exists, skipping: {platform}")
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "MCP-GENERAL-213",
                            f"Failed to migrate CI token {platform}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"CI tokens migration: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                _restrict_to_owner(source_file)
                os.rename(str(source_file), str(source_file) + ".migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "MCP-GENERAL-214", f"Failed to rename {source_file}: {e}"
                    )
                )
        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_sessions(self) -> Dict[str, Any]:
        """Migrate invalidated_sessions.json to SQLite invalidated_sessions table."""
        source_file = Path(self.source_dir) / "invalidated_sessions.json"
        if not source_file.exists():
            logger.info("No invalidated_sessions.json found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                sessions_data = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log(
                    "MCP-GENERAL-215", f"Failed to read invalidated_sessions.json: {e}"
                )
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        backend = SessionsSqliteBackend(self.db_path)
        migrated, already_exists, errors = 0, 0, 0

        try:
            for username, session_list in sessions_data.items():
                tokens = (
                    session_list
                    if isinstance(session_list, list)
                    else list(session_list.keys())
                )
                for token_id in tokens:
                    try:
                        backend.invalidate_session(username=username, token_id=token_id)
                        migrated += 1
                    except sqlite3.IntegrityError:
                        already_exists += 1
                        logger.debug(
                            f"Session already invalidated, skipping: {username}/{token_id}"
                        )
                    except Exception as e:
                        logger.error(
                            format_error_log(
                                "QUERY-GENERAL-008",
                                f"Failed to migrate session {username}/{token_id}: {e}",
                            )
                        )
                        errors += 1
        finally:
            backend.close()

        logger.info(
            f"Sessions migration: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                os.rename(str(source_file), str(source_file) + ".migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "QUERY-GENERAL-009", f"Failed to rename {source_file}: {e}"
                    )
                )
        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_ssh_keys(self) -> Dict[str, Any]:
        """Migrate ssh_keys/*.json files to SQLite ssh_keys and ssh_key_hosts tables."""
        ssh_keys_dir = Path(self.source_dir) / "ssh_keys"
        if not ssh_keys_dir.exists():
            logger.info("No ssh_keys directory found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        json_files = list(ssh_keys_dir.glob("*.json"))
        if not json_files:
            logger.info("No SSH key JSON files found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        backend = SSHKeysSqliteBackend(self.db_path)
        migrated, already_exists, errors = 0, 0, 0

        try:
            for json_file in json_files:
                try:
                    with open(json_file, "r") as f:
                        key_data = json.load(f)
                    key_name = key_data.get("name", json_file.stem)
                    backend.create_key(
                        name=key_name,
                        fingerprint=key_data.get("fingerprint", ""),
                        key_type=key_data.get("key_type", ""),
                        private_path=key_data.get("private_path", ""),
                        public_path=key_data.get("public_path", ""),
                        public_key=key_data.get("public_key"),
                        email=key_data.get("email"),
                        description=key_data.get("description"),
                        is_imported=key_data.get("is_imported", False),
                    )
                    for hostname in key_data.get("hosts", []):
                        try:
                            backend.assign_host(key_name=key_name, hostname=hostname)
                        except sqlite3.IntegrityError:
                            pass  # Host assignment already exists
                    migrated += 1
                    os.rename(str(json_file), str(json_file) + ".migrated")
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"SSH key already exists, skipping: {json_file.stem}")
                    # Still rename file since key exists in DB
                    try:
                        os.rename(str(json_file), str(json_file) + ".migrated")
                    except OSError:
                        pass
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "QUERY-GENERAL-010",
                            f"Failed to migrate SSH key from {json_file}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"SSH keys migration: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )
        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_golden_repos_metadata(self, golden_repos_dir: str) -> Dict[str, Any]:
        """
        Migrate golden-repos/metadata.json to SQLite golden_repos_metadata table.

        Story #711: Migrate GoldenRepoManager metadata.json to SQLite.

        Args:
            golden_repos_dir: Path to the golden-repos directory containing metadata.json.

        Returns:
            Migration result with counts.
        """
        source_file = Path(golden_repos_dir) / "metadata.json"

        if not source_file.exists():
            logger.info("No golden-repos/metadata.json found, skipping migration")
            return {"migrated": 0, "errors": 0, "skipped": True}

        try:
            with open(source_file, "r") as f:
                metadata = json.load(f)
        except (json.JSONDecodeError, IOError) as e:
            logger.error(
                format_error_log(
                    "QUERY-GENERAL-011",
                    f"Failed to read golden-repos/metadata.json: {e}",
                )
            )
            return {"migrated": 0, "errors": 1, "skipped": False}

        backend = GoldenRepoMetadataSqliteBackend(self.db_path)
        migrated, already_exists, errors = 0, 0, 0

        try:
            for alias, repo_data in metadata.items():
                try:
                    backend.add_repo(
                        alias=alias,
                        repo_url=repo_data.get("repo_url", ""),
                        default_branch=repo_data.get("default_branch", "main"),
                        clone_path=repo_data.get("clone_path", ""),
                        created_at=repo_data.get("created_at", ""),
                        enable_temporal=repo_data.get("enable_temporal", False),
                        temporal_options=repo_data.get("temporal_options"),
                    )
                    migrated += 1
                    logger.debug(f"Migrated golden repo: {alias}")
                except sqlite3.IntegrityError:
                    already_exists += 1
                    logger.debug(f"Golden repo already exists, skipping: {alias}")
                except Exception as e:
                    logger.error(
                        format_error_log(
                            "QUERY-GENERAL-012",
                            f"Failed to migrate golden repo {alias}: {e}",
                        )
                    )
                    errors += 1
        finally:
            backend.close()

        logger.info(
            f"Golden repos metadata migration: {migrated} migrated, "
            f"{already_exists} already existed, {errors} errors"
        )

        # Rename JSON file to .migrated after successful migration
        if errors == 0 and (migrated > 0 or already_exists > 0):
            try:
                os.rename(str(source_file), str(source_file) + ".migrated")
                logger.info(f"Renamed {source_file} to {source_file}.migrated")
            except OSError as e:
                logger.warning(
                    format_error_log(
                        "QUERY-GENERAL-013", f"Failed to rename {source_file}: {e}"
                    )
                )

        return {
            "migrated": migrated,
            "already_exists": already_exists,
            "errors": errors,
            "skipped": False,
        }

    def migrate_all(self) -> Dict[str, Dict[str, Any]]:
        """
        Run all migrations.

        Returns:
            Dictionary with results for each migration type.
        """
        results = {}

        logger.info("Starting migration of legacy JSON files to SQLite")

        results["global_repos"] = self.migrate_global_repos()
        results["users"] = (
            self.migrate_users() if self._import_users else self._skip_users_import()
        )
        # background_jobs must run BEFORE sync_jobs because jobs.json contains
        # BackgroundJobManager jobs (operation_type field), not SyncJobManager jobs
        results["background_jobs"] = self.migrate_background_jobs()
        results["sync_jobs"] = self.migrate_sync_jobs()
        results["ci_tokens"] = self.migrate_ci_tokens()
        results["sessions"] = self.migrate_sessions()
        results["ssh_keys"] = self.migrate_ssh_keys()

        total_migrated = sum(r.get("migrated", 0) for r in results.values())
        total_errors = sum(r.get("errors", 0) for r in results.values())

        logger.info(
            f"Migration complete: {total_migrated} total records migrated, "
            f"{total_errors} total errors"
        )

        return results

    def _skip_users_import(self) -> Dict[str, Any]:
        """Cluster mode: accounts live in PostgreSQL, never in a local file."""
        logger.info("Legacy users.json import skipped in cluster storage mode")
        if (Path(self.source_dir) / "users.json").exists():
            logger.warning(
                "users.json is present and ignored: in cluster storage mode "
                "accounts live in PostgreSQL"
            )
        return {"migrated": 0, "errors": 0, "skipped": True}
