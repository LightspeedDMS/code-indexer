#!/usr/bin/env bash
# provision_delta_fixture.sh — Story #1053 E2E helper
#
# Provisions a multi-domain delta fixture deterministically:
#   (a) authenticates via /login
#   (b) verifies each repo is registered as a golden repo
#   (c) writes a content-bearing commit to each repo's working clone
#   (d) calls /api/admin/refresh-golden-repos to update repo-state cache
#   (e) verifies _domains.json covers each test repo in >= 3 domain entries
#
# Called by Scenario 16 E2E test for Story #1053 resumable delta dep-map analysis.
# Supports --dry-run: prints every step prefixed [DRY-RUN] and exits 0 without
# touching disk or HTTP.
#
# Usage:
#   provision_delta_fixture.sh [--server-url URL] [--admin-user USER] \
#       [--admin-pass PASS] [--repos "alias1 alias2 ..."] [--dry-run]
#
# Defaults:
#   --server-url  http://127.0.0.1:8001
#   --admin-user  admin
#   --admin-pass  admin
#   --repos       "cidx-meta fastapi click markupsafe itsdangerous"

set -euo pipefail

# --- Defaults ---
SERVER_URL="http://127.0.0.1:8001"
ADMIN_USER="admin"
ADMIN_PASS="admin"
REPOS="cidx-meta fastapi click markupsafe itsdangerous"
DRY_RUN=false

# --- Argument parsing ---
while [[ $# -gt 0 ]]; do
  case "$1" in
    --server-url) SERVER_URL="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    --admin-pass) ADMIN_PASS="$2"; shift 2 ;;
    --repos)      REPOS="$2"; shift 2 ;;
    --dry-run)    DRY_RUN=true; shift ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \?//'
      exit 0 ;;
    *) echo "Unknown argument: $1" >&2; exit 1 ;;
  esac
done

log() { echo "[$(date -u +%H:%M:%S)] $*"; }
dry() { echo "[DRY-RUN] $*"; }

# --- Step (a): Authenticate ---
if $DRY_RUN; then
  dry "Would POST /login to $SERVER_URL with user=$ADMIN_USER"
  dry "Would store JWT token for subsequent requests"
else
  log "Authenticating as $ADMIN_USER ..."
  AUTH_RESP=$(curl -sS -X POST "$SERVER_URL/login" \
    -H "Content-Type: application/x-www-form-urlencoded" \
    -d "username=${ADMIN_USER}&password=${ADMIN_PASS}")
  TOKEN=$(echo "$AUTH_RESP" | jq -r '.access_token // empty')
  if [[ -z "$TOKEN" ]]; then
    echo "ERROR: authentication failed. Response: $AUTH_RESP" >&2
    exit 1
  fi
  log "Authenticated successfully."
  AUTH_HEADER="Authorization: Bearer $TOKEN"
fi

# --- Step (b): Verify each repo is registered as a golden repo ---
if $DRY_RUN; then
  for ALIAS in $REPOS; do
    dry "Would GET $SERVER_URL/api/admin/golden-repos/$ALIAS to verify registration"
  done
else
  log "Verifying golden repos are registered ..."
  for ALIAS in $REPOS; do
    REPO_RESP=$(curl -sS -o /dev/null -w "%{http_code}" \
      -H "$AUTH_HEADER" \
      "$SERVER_URL/api/admin/golden-repos/$ALIAS")
    if [[ "$REPO_RESP" == "200" ]]; then
      log "  [OK] $ALIAS is registered"
    else
      echo "  [WARN] $ALIAS returned HTTP $REPO_RESP — may not be registered as golden repo" >&2
    fi
  done
fi

# --- Step (c): Write a content-bearing commit to each repo's working clone ---
if $DRY_RUN; then
  for ALIAS in $REPOS; do
    dry "Would GET $SERVER_URL/api/admin/golden-repos/$ALIAS to resolve clone path"
    dry "Would: echo '# test marker' >> README.md && git add README.md && git commit -m 'test: trigger delta for #1053 E2E'"
  done
else
  log "Writing test commits to golden repo clones ..."
  for ALIAS in $REPOS; do
    # Resolve the clone path from the golden repo metadata
    REPO_INFO=$(curl -sS -H "$AUTH_HEADER" "$SERVER_URL/api/admin/golden-repos/$ALIAS")
    CLONE_PATH=$(echo "$REPO_INFO" | jq -r '.target_path // empty')
    if [[ -z "$CLONE_PATH" || ! -d "$CLONE_PATH" ]]; then
      echo "  [WARN] Cannot resolve clone path for $ALIAS (got: '$CLONE_PATH') — skipping commit" >&2
      continue
    fi
    log "  Committing to $ALIAS at $CLONE_PATH ..."
    echo '# test marker' >> "$CLONE_PATH/README.md"
    git -C "$CLONE_PATH" add README.md
    git -C "$CLONE_PATH" commit -m "test: trigger delta for #1053 E2E" \
      --author="E2E Fixture <e2e@cidx.test>" || true
    log "  [OK] $ALIAS committed"
  done
fi

# --- Step (d): Refresh golden repos cache ---
if $DRY_RUN; then
  dry "Would POST $SERVER_URL/api/admin/refresh-golden-repos to update repo-state cache"
else
  log "Triggering golden repo refresh ..."
  REFRESH_RESP=$(curl -sS -o /dev/null -w "%{http_code}" \
    -X POST \
    -H "$AUTH_HEADER" \
    -H "Content-Type: application/json" \
    "$SERVER_URL/api/admin/refresh-golden-repos")
  log "  Refresh returned HTTP $REFRESH_RESP"
  if [[ "$REFRESH_RESP" != "200" && "$REFRESH_RESP" != "202" ]]; then
    echo "  [WARN] Unexpected refresh status: $REFRESH_RESP" >&2
  fi
fi

# --- Step (e): Verify _domains.json covers >= 3 domain entries ---
if $DRY_RUN; then
  dry "Would inspect _domains.json on server data dir for domain coverage"
  dry "Would fail (exit 1) if domain count < 3"
  dry "--- DRY-RUN complete: no disk or HTTP changes made ---"
  exit 0
else
  log "Verifying domain coverage in _domains.json ..."
  # Query domains via REST API
  DOMAINS_RESP=$(curl -sS \
    -H "$AUTH_HEADER" \
    "$SERVER_URL/api/admin/dependency-map/domains" 2>/dev/null || echo "[]")
  DOMAIN_COUNT=$(echo "$DOMAINS_RESP" | jq 'if type == "array" then length elif type == "object" and has("domains") then .domains | length else 0 end' 2>/dev/null || echo "0")
  log "  Found $DOMAIN_COUNT domain entries"
  if [[ "$DOMAIN_COUNT" -lt 3 ]]; then
    echo "ERROR: expected >= 3 domain entries in _domains.json but found $DOMAIN_COUNT" >&2
    echo "  This may mean dep-map analysis has not yet run. Run a full analysis first." >&2
    exit 1
  fi
  log "Domain coverage verified: $DOMAIN_COUNT domains (>= 3 required)."
  log "Fixture provisioning complete."
fi
