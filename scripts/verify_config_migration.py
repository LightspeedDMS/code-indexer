#!/usr/bin/env python3
"""
Verify config migration: compare original backup vs DB+file merged config.

Usage:
    python3 verify_config_migration.py --config ~/.cidx-server/config.json
    python3 verify_config_migration.py --config ~/.cidx-server/config.json --verbose
"""

import argparse
import json
import os
import sys
from typing import Any, List, Optional, Tuple

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

MAX_DISPLAY_LEN = 80


def _deep_compare(
    original: Any, merged: Any, path: str = ""
) -> List[Tuple[str, Any, Any]]:
    """Recursively compare two values, return list of (path, original, merged)."""
    diffs: List[Tuple[str, Any, Any]] = []
    if isinstance(original, dict) and isinstance(merged, dict):
        for k in sorted(set(original.keys()) | set(merged.keys())):
            key_path = f"{path}.{k}" if path else k
            if k not in original:
                diffs.append((key_path, "<MISSING>", merged[k]))
            elif k not in merged:
                diffs.append((key_path, original[k], "<MISSING>"))
            else:
                diffs.extend(_deep_compare(original[k], merged[k], key_path))
    elif original != merged:
        diffs.append((path, original, merged))
    return diffs


def _load_backup(config_dir: str) -> Optional[dict]:
    """Load pre-migration backup. Returns None if not found."""
    backup_file = os.path.join(
        config_dir, "config-migration-backup", "config.json.pre-centralization"
    )
    if not os.path.exists(backup_file):
        print(f"SKIP: No backup found at {backup_file}")
        return None
    with open(backup_file) as f:
        data: dict = json.load(f)
    print(f"Backup: {len(data)} keys from {backup_file}")
    return data


def _load_runtime_from_db(bootstrap: dict, config_dir: str) -> dict:
    """Load runtime config from SQLite or PostgreSQL based on storage_mode."""
    storage_mode = bootstrap.get("storage_mode", "sqlite")

    if storage_mode == "postgres":
        pg_dsn = bootstrap.get("postgres_dsn", "")
        if not pg_dsn:
            print("ERROR: No postgres_dsn in bootstrap config")
            return {}
        import psycopg

        conn = psycopg.connect(pg_dsn)
        row = conn.execute(
            "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
        ).fetchone()
        conn.close()
        if not row:
            return {}
        return json.loads(row[0]) if isinstance(row[0], str) else row[0]

    db_path = os.path.join(config_dir, "data", "cidx_server.db")
    if not os.path.exists(db_path):
        return {}
    import sqlite3

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT config_json FROM server_config WHERE config_key = 'runtime'"
    ).fetchone()
    conn.close()
    return json.loads(row[0]) if row else {}


def _report_results(
    diffs: List[Tuple[str, Any, Any]], original: dict, verbose: bool
) -> int:
    """Print results. Returns 0 on pass, 1 on fail."""
    if verbose:
        print(f"=== All {len(original)} original keys ===")
        for k in sorted(original.keys()):
            location = "file" if k in BOOTSTRAP_KEYS else "DB"
            print(f"  [OK] {k} ({location})")

    if not diffs:
        print("RESULT: PASS -- All settings match perfectly")
        return 0

    print(f"RESULT: FAIL -- {len(diffs)} discrepancies found:")
    print()
    for path, orig_val, merged_val in diffs:
        orig_str = json.dumps(orig_val, default=str)[:MAX_DISPLAY_LEN]
        merged_str = json.dumps(merged_val, default=str)[:MAX_DISPLAY_LEN]
        print(f"  {path}:")
        print(f"    Original: {orig_str}")
        print(f"    Merged:   {merged_str}")
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Verify config migration")
    parser.add_argument("--config", required=True, help="Path to config.json")
    parser.add_argument("--verbose", action="store_true")
    args = parser.parse_args()

    if not os.path.exists(args.config):
        print(f"ERROR: Config file not found: {args.config}")
        return 1

    config_dir = os.path.dirname(args.config)
    original = _load_backup(config_dir)
    if original is None:
        return 0

    with open(args.config) as f:
        bootstrap: dict = json.load(f)
    print(f"Bootstrap: {len(bootstrap)} keys")

    runtime = _load_runtime_from_db(bootstrap, config_dir)
    storage_mode = bootstrap.get("storage_mode", "sqlite")
    print(f"Runtime DB: {len(runtime)} keys ({storage_mode})")

    if not runtime:
        print("ERROR: No runtime config found in database")
        return 1

    merged = {**bootstrap, **runtime}
    print(f"Merged: {len(merged)} keys")
    print()

    diffs = _deep_compare(original, merged)
    return _report_results(diffs, original, args.verbose)


if __name__ == "__main__":
    sys.exit(main())
