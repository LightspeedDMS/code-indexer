"""
Shared constants for CIDX Server services.

This module defines constants for:
- Default group names (admins, powerusers, users)
- Special repository names (cidx-meta)

These constants should be used instead of hardcoded strings throughout
the codebase to ensure consistency and ease of maintenance.
"""

# Default group names
# These are the three groups created at system bootstrap
DEFAULT_GROUP_ADMINS = "admins"
DEFAULT_GROUP_POWERUSERS = "powerusers"
DEFAULT_GROUP_USERS = "users"

# Special repository names
# cidx-meta is always accessible to all groups
CIDX_META_REPO = "cidx-meta"

# cidx-meta-global is the globally-activated form of the internal cidx-meta
# bookkeeping repo (see CIDX_META_REPO above).
CIDX_META_REPO_GLOBAL = "cidx-meta-global"

# Anchored set of internal meta-repo aliases -- the ONLY authority for
# is_internal_meta_repo() below.
_INTERNAL_META_REPO_ALIASES = frozenset({CIDX_META_REPO, CIDX_META_REPO_GLOBAL})


def is_internal_meta_repo(alias: str) -> bool:
    """Return True only for the exact internal cidx-meta bookkeeping repo alias.

    Bug #1287 Defect B (code-reviewer finding 1): this is an ANCHORED / EXACT
    match against {"cidx-meta", "cidx-meta-global"} -- NEVER a prefix or
    substring check. A prefix check such as ``alias.startswith("cidx-meta")``
    over-matches legitimate user repos whose real name merely starts with the
    same characters (e.g. "cidx-metadata-global" or
    "cidx-meta-analytics-global"), silently dropping them from search fan-out.
    """
    return alias in _INTERNAL_META_REPO_ALIASES


# =============================================================================
# Story #3 Phase 2: P2 Configuration Validation Limits (AC12-AC26)
# =============================================================================

# Git Timeouts (AC12-AC14)
MIN_GIT_LOCAL_TIMEOUT_SECONDS = 5
MIN_GIT_REMOTE_TIMEOUT_SECONDS = 30

# Error Handling (AC16-AC18)
MIN_RETRY_ATTEMPTS = 1
MAX_RETRY_ATTEMPTS = 10
MIN_BASE_RETRY_DELAY_SECONDS = 0.01
MAX_BASE_RETRY_DELAY_SECONDS = 5.0
MIN_MAX_RETRY_DELAY_SECONDS = 1
MAX_MAX_RETRY_DELAY_SECONDS = 300

# API Limits (AC19-AC24)
MIN_DEFAULT_FILE_READ_LINES = 100
MAX_DEFAULT_FILE_READ_LINES = 5000
MIN_MAX_FILE_READ_LINES = 500
MAX_MAX_FILE_READ_LINES = 50000
MIN_DEFAULT_DIFF_LINES = 100
MAX_DEFAULT_DIFF_LINES = 5000
MIN_MAX_DIFF_LINES = 500
MAX_MAX_DIFF_LINES = 50000
MIN_DEFAULT_LOG_COMMITS = 10
MAX_DEFAULT_LOG_COMMITS = 500
MIN_MAX_LOG_COMMITS = 50
MAX_MAX_LOG_COMMITS = 5000

# Web Security (AC25-AC26)
MIN_CSRF_MAX_AGE_SECONDS = 60
MAX_CSRF_MAX_AGE_SECONDS = 3600
MIN_WEB_SESSION_TIMEOUT_SECONDS = 1800
MAX_WEB_SESSION_TIMEOUT_SECONDS = 86400

# =============================================================================
# Story #3 Phase 2: P3 Configuration Validation Limits (AC27-AC39)
# =============================================================================

# API Provider Timeouts (AC27-AC28)
MIN_GITHUB_API_TIMEOUT_SECONDS = 5
MAX_GITHUB_API_TIMEOUT_SECONDS = 120
MIN_GITLAB_API_TIMEOUT_SECONDS = 5
MAX_GITLAB_API_TIMEOUT_SECONDS = 120

# SCIP Query Limits (AC31-AC34)
MIN_SCIP_REFERENCE_LIMIT = 10
MAX_SCIP_REFERENCE_LIMIT = 10000
MIN_SCIP_DEPENDENCY_DEPTH = 1
MAX_SCIP_DEPENDENCY_DEPTH = 20
MIN_SCIP_CALLCHAIN_MAX_DEPTH = 1
MAX_SCIP_CALLCHAIN_MAX_DEPTH = 50
MIN_SCIP_CALLCHAIN_LIMIT = 1
MAX_SCIP_CALLCHAIN_LIMIT = 1000

# Audit Log Limits (AC35)
MIN_AUDIT_LOG_DEFAULT_LIMIT = 10
MAX_AUDIT_LOG_DEFAULT_LIMIT = 1000

# OAuth Extension Threshold (AC36)
MIN_OAUTH_EXTENSION_THRESHOLD_HOURS = 1
MAX_OAUTH_EXTENSION_THRESHOLD_HOURS = 24

# Metrics Cache TTL (AC37)
MIN_SYSTEM_METRICS_CACHE_TTL_SECONDS = 1
MAX_SYSTEM_METRICS_CACHE_TTL_SECONDS = 60

# Log Aggregator Page Sizes (AC38-AC39)
MIN_LOG_PAGE_SIZE_DEFAULT = 10
MAX_LOG_PAGE_SIZE_DEFAULT = 500
MIN_LOG_PAGE_SIZE_MAX = 100
MAX_LOG_PAGE_SIZE_MAX = 5000
