---
name: feedback-keep-local-server-running
description: Always keep the local dev cidx-server (uvicorn on :8000) running — never stop it unless explicitly asked
metadata: 
  node_type: memory
  type: feedback
  originSessionId: aab55ef6-fddf-4741-80e4-0c92b4fbaebc
---

Never stop the local development cidx-server (uvicorn `code_indexer.server.app:app` on `0.0.0.0:8000`). The user wants it permanently up for manual testing/viewing. After using it, leave it running; do not offer to stop it as cleanup. If a code/template change needs to take effect, relaunch it immediately rather than leaving it down.

**Why:** The user said "leave it on, always leave it on" — they rely on the local server being continuously available.

**How to apply:** Launch detached so it survives the shell: `PYTHONPATH=./src setsid nohup python3 -m uvicorn code_indexer.server.app:app --host 0.0.0.0 --port 8000 > <log> 2>&1 < /dev/null &`. To stop/restart it, kill by PID (`pgrep` the master, `kill <pid>`) — NEVER `pkill -f "uvicorn code_indexer.server.app"`, because that pattern matches your own shell command line and kills the script mid-run. Templates are re-read from disk per request, so a template change does NOT need a restart; only Python/code changes do. See [[feedback_no_confirmation_on_commands]].
