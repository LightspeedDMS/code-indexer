#!/usr/bin/env python3
"""
Config centralization migration helper (Story #578).

Extracts runtime config from config.json, inserts into PostgreSQL
server_config table, strips local file to bootstrap-only.

Usage:
    python3 config_migration_helper.py migrate --config /path/to/config.json
    python3 config_migration_helper.py verify --config /path/to/config.json
"""

import argparse
import json
import os
import sys
import tempfile

BOOTSTRAP_KEYS = frozenset(
    {
        "server_dir",
        "host",
        "port",
        "workers",
        "log_level",
        "storage_mode",
        "postgres_dsn",
        "ontap",
        "cluster",
    }
)

MAX_BOOTSTRAP_KEYS = 10
MIN_EXPECTED_RUNTIME_KEYS = 20


def _load_config(config_path: str) -> dict:
    with open(config_path) as f:
        return json.load(f)


def _split_config(config: dict) -> tuple:
    """Split config into (bootstrap_dict, runtime_dict)."""
    bootstrap = {k: v for k, v in config.items() if k in BOOTSTRAP_KEYS}
    runtime = {k: v for k, v in config.items() if k not in BOOTSTRAP_KEYS}
    return bootstrap, runtime


def _write_bootstrap_only(config_path: str, bootstrap: dict) -> None:
    """Atomically write bootstrap-only config to file."""
    config_dir = os.path.dirname(config_path)
    tmp_fd, tmp_path = tempfile.mkstemp(
        dir=config_dir, prefix=".config_migrate_", suffix=".tmp"
    )
    try:
        with os.fdopen(tmp_fd, "w") as f:
            json.dump(bootstrap, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, config_path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def cmd_migrate(args: argparse.Namespace) -> int:
    """Extract runtime config from file, insert into PG, strip file."""
    config = _load_config(args.config)
    bootstrap, runtime = _split_config(config)

    if not runtime:
        print(
            f"Config already bootstrap-only ({len(bootstrap)} keys). Nothing to migrate."
        )
        return 0

    print(
        f"Config split: {len(bootstrap)} bootstrap + {len(runtime)} runtime = {len(config)} total"
    )

    pg_dsn = bootstrap.get("postgres_dsn", "")
    if not pg_dsn:
        print("ERROR: No postgres_dsn in config", file=sys.stderr)
        return 1

    import psycopg

    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT version, length(config_json::text) FROM server_config "
            "WHERE config_key = %s",
            ("runtime",),
        ).fetchone()

        if row:
            print(
                f"PG already has runtime config (version={row[0]}, size={row[1]} bytes)"
            )
            print("Skipping INSERT (idempotent)")
        else:
            conn.execute(
                "INSERT INTO server_config (config_key, config_json, version, updated_by) "
                "VALUES (%s, %s, 1, %s)",
                ("runtime", json.dumps(runtime), "cluster-config-migrate.sh"),
            )
            conn.commit()
            print(f"Inserted {len(runtime)} runtime keys into server_config table")

    _write_bootstrap_only(args.config, bootstrap)
    print(f"Local config.json stripped to {len(bootstrap)} bootstrap keys")
    return 0


def cmd_verify(args: argparse.Namespace) -> int:
    """Verify migration: local has bootstrap-only, PG has runtime."""
    errors = 0
    config = _load_config(args.config)

    if len(config) <= MAX_BOOTSTRAP_KEYS:
        print(f"OK: Local config has {len(config)} keys (bootstrap-only)")
    else:
        print(
            f"FAIL: Local config has {len(config)} keys (expected <= {MAX_BOOTSTRAP_KEYS})"
        )
        errors += 1

    pg_dsn = config.get("postgres_dsn", "")
    if not pg_dsn:
        print("FAIL: No postgres_dsn in config")
        return errors + 1

    import psycopg

    with psycopg.connect(pg_dsn) as conn:
        row = conn.execute(
            "SELECT config_json, version FROM server_config WHERE config_key = %s",
            ("runtime",),
        ).fetchone()
        if row:
            runtime = json.loads(row[0]) if isinstance(row[0], str) else row[0]
            if len(runtime) >= MIN_EXPECTED_RUNTIME_KEYS:
                print(f"OK: PG has {len(runtime)} runtime keys (version={row[1]})")
            else:
                print(
                    f"FAIL: PG has only {len(runtime)} runtime keys (expected >= {MIN_EXPECTED_RUNTIME_KEYS})"
                )
                errors += 1
        else:
            print("FAIL: PG server_config table has no runtime row")
            errors += 1

    return errors


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Config centralization migration helper"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    migrate_p = sub.add_parser("migrate", help="Migrate runtime config to PG")
    migrate_p.add_argument("--config", required=True, help="Path to config.json")

    verify_p = sub.add_parser("verify", help="Verify migration state")
    verify_p.add_argument("--config", required=True, help="Path to config.json")

    args = parser.parse_args()
    if args.command == "migrate":
        return cmd_migrate(args)
    elif args.command == "verify":
        return cmd_verify(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())
