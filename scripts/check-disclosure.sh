#!/bin/bash
# check-disclosure.sh — scan git diff for internal identifiers that must not appear in this public repo.
# Exit 0 = clean, exit 1 = findings (printed to stdout).
# Usage: scripts/check-disclosure.sh <base-sha> <head-sha>
#   or:  scripts/check-disclosure.sh              (defaults to scanning staged changes)
set -euo pipefail

if [ $# -ge 2 ]; then
  DIFF_CMD="git diff --diff-filter=ACM -U0 $1...$2"
elif [ $# -eq 1 ]; then
  DIFF_CMD="git diff --diff-filter=ACM -U0 $1...HEAD"
else
  DIFF_CMD="git diff --cached --diff-filter=ACM -U0"
fi

ADDED_LINES=$($DIFF_CMD | grep -E '^\+[^+]' | sed 's/^\+//' || true)

if [ -z "$ADDED_LINES" ]; then
  echo "No added lines to scan."
  exit 0
fi

FINDINGS=""
FOUND=0

check_pattern() {
  local label="$1"
  local pattern="$2"
  local matches
  matches=$(echo "$ADDED_LINES" | grep -nE "$pattern" 2>/dev/null || true)
  if [ -n "$matches" ]; then
    FOUND=1
    FINDINGS="${FINDINGS}\n=== ${label} ===\n${matches}\n"
  fi
}

# Internal Jira key prefixes (known internal projects)
check_pattern "Internal Jira keys" \
  '\b(EVO|TDMS|ARCH|TDJLTS|SCCAC|AICC)-[0-9]{3,6}\b'

# Internal Kubernetes DNS names
check_pattern "Internal K8s DNS / svc.cluster.local" \
  'svc\.cluster\.local|\.svc\b.*\.(local|internal)'

# Company domain hostnames (common patterns)
check_pattern "Company domain hostnames" \
  'lightspeeddms\.(com|net|io)|lightspeed-[0-9]+\.d\.codeartifact'

# 12-digit AWS account IDs in ARN/ECR context
check_pattern "AWS account IDs" \
  '\b[0-9]{12}\.(dkr\.ecr|amazonaws)'

# GitLab private registry / project IDs
check_pattern "GitLab private paths" \
  '/projects/[0-9]+/packages|lightspeeddms/private/'

# Internal MR references
check_pattern "Internal MR references" \
  '\bMR ![0-9]+\b|merge_request![0-9]+'

# Internal service vocabulary that shouldn't appear in comments/docs
# (allowed in code that implements delegation — only flag in comments/docs/strings)
check_pattern "Internal service names in comments" \
  '#.*\b(workspace-mcp|git-mcp|sandbox-mcp|mcp-gateway|agent-router|dev-agent|evo-sandbox)\b'

if [ "$FOUND" -eq 1 ]; then
  echo "DISCLOSURE SCAN FAILED — internal identifiers found in added lines:"
  echo -e "$FINDINGS"
  echo ""
  echo "This is a public repository. Remove or replace internal identifiers before pushing."
  echo "See CLAUDE.md disclosure rules for details."
  exit 1
else
  echo "Disclosure scan passed — no internal identifiers found."
  exit 0
fi
