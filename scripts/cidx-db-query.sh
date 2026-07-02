#!/usr/bin/env bash
# cidx-db-query.sh — Authorized database query wrapper for the CIDX Research Assistant.
#
# Story #872: allows the research agent to query/modify CIDX databases (SQLite or
# PostgreSQL) without direct shell access or knowledge of connection details.
#
# DESIGN NOTE: This script intentionally forwards arbitrary SQL to sqlite3/psql.
# Full CRUD access (SELECT/INSERT/UPDATE/DELETE) is a required feature per Story #872.
# Access is scoped by:
#   - Scope enforcement: SQLite db must reside inside CIDX data directory
#   - Permission model: Claude CLI only allows this script via explicit Bash allow-rule
#
# Usage:
#   cidx-db-query.sh [--db <sqlite_path>] [--pg <dsn>] "<sql_statement>"
#   cidx-db-query.sh "<sql_statement>"   # fully auto-detected mode
#
# Auto-detection reads ${CIDX_SERVER_DATA_DIR:-~/.cidx-server}/config.json.
# On config parse failure: Python logs WARNING to stderr (flows to caller), falls back to sqlite.
# If python3 fails to start: CONFIG_RESULT is empty; code falls through to sqlite defaults.
# SQLite default db: ${CIDX_SERVER_DATA_DIR:-~/.cidx-server}/data/cidx_server.db
# Scope enforcement (SQLite only): db path must reside inside the CIDX data dir.

set -euo pipefail

# ---------------------------------------------------------------------------
# Parse optional flags: --db <path> and --pg <dsn>
# ---------------------------------------------------------------------------
EXPLICIT_DB=""
EXPLICIT_DSN=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --db)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --db requires a path argument" >&2
                exit 1
            fi
            EXPLICIT_DB="$2"
            shift 2
            ;;
        --pg)
            if [[ $# -lt 2 ]]; then
                echo "ERROR: --pg requires a DSN argument" >&2
                exit 1
            fi
            EXPLICIT_DSN="$2"
            shift 2
            ;;
        --)
            shift
            break
            ;;
        -*)
            echo "ERROR: unknown option: $1" >&2
            exit 1
            ;;
        *)
            break
            ;;
    esac
done

if [[ $# -lt 1 ]]; then
    echo "Usage: cidx-db-query.sh [--db <path>] [--pg <dsn>] \"<sql>\"" >&2
    exit 1
fi

SQL="$1"

# ---------------------------------------------------------------------------
# Determine data directory and config path
# ---------------------------------------------------------------------------
CIDX_DATA_DIR="${CIDX_SERVER_DATA_DIR:-${HOME}/.cidx-server}"
CONFIG_PATH="${CIDX_DATA_DIR}/config.json"

# ---------------------------------------------------------------------------
# Auto-detect mode when no explicit flags provided
# ---------------------------------------------------------------------------
MODE="sqlite"
DB_PATH="${CIDX_DATA_DIR}/data/cidx_server.db"
DSN=""

# Static Python source — config path is passed as sys.argv[1], never embedded.
# On parse failure, Python emits WARNING to stderr (flows to caller) and prints "sqlite|".
# "|| true" suppresses set -e so python3 startup failure leaves CONFIG_RESULT="",
# which falls through to sqlite defaults in the if-check below.
_PARSE_CONFIG='
import json, sys
config_path = sys.argv[1]
try:
    with open(config_path) as f:
        cfg = json.load(f)
    storage_mode = cfg.get("storage_mode", "sqlite") or "sqlite"
    postgres_dsn = cfg.get("postgres_dsn", "") or ""
    print(storage_mode + "|" + postgres_dsn)
except Exception as exc:
    sys.stderr.write("WARNING: could not parse config.json, defaulting to sqlite: " + str(exc) + "\n")
    print("sqlite|")
'

if [[ -z "$EXPLICIT_DB" && -z "$EXPLICIT_DSN" ]]; then
    if [[ -f "$CONFIG_PATH" ]]; then
        CONFIG_RESULT=$(python3 -c "$_PARSE_CONFIG" "$CONFIG_PATH" || true)
        if [[ -n "$CONFIG_RESULT" ]]; then
            STORAGE_MODE="${CONFIG_RESULT%%|*}"
            PG_DSN="${CONFIG_RESULT#*|}"

            if [[ "$STORAGE_MODE" == "postgres" && -n "$PG_DSN" ]]; then
                MODE="postgres"
                DSN="$PG_DSN"
            fi
        fi
        # Empty CONFIG_RESULT means python3 failed to start; sqlite defaults remain.
    fi
elif [[ -n "$EXPLICIT_DSN" ]]; then
    MODE="postgres"
    DSN="$EXPLICIT_DSN"
else
    MODE="sqlite"
    DB_PATH="$EXPLICIT_DB"
fi

# ---------------------------------------------------------------------------
# Scope enforcement (SQLite only)
# ---------------------------------------------------------------------------
if [[ "$MODE" == "sqlite" ]]; then
    CANONICAL_DB=$(readlink -f "$DB_PATH" 2>/dev/null || echo "$DB_PATH")
    CANONICAL_DATA=$(readlink -f "$CIDX_DATA_DIR" 2>/dev/null || echo "$CIDX_DATA_DIR")

    if [[ "$CANONICAL_DB" != "${CANONICAL_DATA}/"* ]]; then
        echo "ERROR: target database is outside CIDX data directory" >&2
        exit 1
    fi
fi

# ---------------------------------------------------------------------------
# Execute — forwards exit code from sqlite3/psql verbatim
# ---------------------------------------------------------------------------
if [[ "$MODE" == "sqlite" ]]; then
    sqlite3 -header -column "$DB_PATH" "$SQL"
else
    psql "$DSN" -c "$SQL"
fi
