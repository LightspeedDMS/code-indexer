#!/usr/bin/env bash
# audit_processes.sh — Story #1053 E2E helper
#
# Single-shot process audit. Prints either "ALL DOWN" (exit 0) or the surviving
# PIDs with their command lines (exit 1).
#
# Checks for cidx-server uvicorn instances and any `claude` CLI subprocesses
# they have spawned (recognised by the --print flag — cidx-server uses
# `claude --print --dangerously-skip-permissions` via claude_invoker.py; this
# pattern does NOT match Claude Code's interactive sessions which omit --print).
#
# Called by Story #1053 Scenario 16 to verify that the entire cidx-server
# process tree (uvicorn + script wrapper + timeout wrapper + claude) was
# terminated by the kill recipe, with no orphaned `claude` subprocess
# continuing to consume tokens after the parent died.
#
# Usage:
#   audit_processes.sh                   # match any uvicorn cidx-server instance
#   audit_processes.sh --port 8001       # narrow uvicorn match to a specific port
#                                        # (useful when coexisting with another
#                                        # cidx-server on a different port)

set -euo pipefail

PORT=""
while [ $# -gt 0 ]; do
  case "$1" in
    --port) PORT="$2"; shift 2 ;;
    -h|--help) sed -n '2,22p' "$0" | sed 's/^# //; s/^#//'; exit 0 ;;
    *) echo "audit_processes.sh: unknown arg '$1'" >&2; exit 2 ;;
  esac
done

if [ -n "$PORT" ]; then
  UVICORN_PATTERN="uvicorn code_indexer.server.app.*--port $PORT"
else
  UVICORN_PATTERN="uvicorn code_indexer.server.app"
fi

# Patterns:
#   $UVICORN_PATTERN    — cidx-server uvicorn process (optionally narrowed by port)
#   script .* claude    — the PTY wrapper around claude (claude_invoker.py uses
#                         `script -q -e -c "timeout <N> claude ..." /dev/null`)
#   timeout .* claude   — the inner shell-timeout wrapper invoking claude
#   claude .*--print    — the actual claude CLI process spawned by cidx-server
#                         (Claude Code's interactive session does NOT use --print)
PIDS=$(pgrep -af "$UVICORN_PATTERN|script .* claude|timeout .* claude|claude .*--print" || true)

if [ -z "$PIDS" ]; then
  echo "ALL DOWN"
  exit 0
else
  echo "STRAGGLERS REMAIN:"
  echo "$PIDS"
  exit 1
fi
