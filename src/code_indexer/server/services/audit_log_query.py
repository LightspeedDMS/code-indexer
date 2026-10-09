"""The single shared read path for the audit log.

The Web Audit Logs page, the MCP ``query_audit_logs`` tool and the REST
``GET /api/v1/audit-logs`` route all read ``audit_logs`` through
:func:`query_audit_log`.  Each door only maps its arguments in and serializes
:class:`CanonicalAuditRow` out; no door filters, pages or shapes on its own.

Scale rules enforced here:

- keyset paging on ``(timestamp DESC, id DESC)`` (``id`` is the tie-breaker,
  so rows sharing a timestamp never shift between pages).  The cursor is a
  self-contained token (``base64url(json([timestamp, id]))``): nothing is kept
  in memory, so any node of a cluster can serve the next page;
- every page is bounded (``AUDIT_LOG_MAX_LIMIT``), the compatibility offset is
  bounded (``AUDIT_LOG_MAX_OFFSET``) and the count is capped
  (``AUDIT_COUNT_CAP``): no unbounded ``COUNT(*)``;
- the authentication-activity aggregate is a SQL ``GROUP BY`` over a bounded
  default window; scanning all history requires an explicit ``all_time``.

Bad arguments raise :class:`AuditQueryError` (a ``ValueError``) so every door
can refuse them loudly (HTTP 400 / MCP ``success: false``).
"""

from __future__ import annotations

import base64
import binascii
import json
import re
from dataclasses import dataclass, field, replace
from datetime import datetime, time, timedelta, timezone
from typing import (
    Any,
    Dict,
    FrozenSet,
    List,
    Literal,
    Mapping,
    Optional,
    Sequence,
    Set,
    Tuple,
    Union,
    overload,
)
from urllib.parse import urlsplit, urlunsplit

from code_indexer.server.middleware.audit_request_context import (
    SOURCE_MCP,
    SOURCE_REST,
    SOURCE_SYSTEM,
    SOURCE_WEB,
)
from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    BOOL,
    INT,
    JOB_BASED_ACTION_TYPES,
    OPAQUE_ID,
    OUTCOMES,
    REPO_ALIAS,
    GIT_REF,
    USERNAME,
    FieldType,
    conforms,
)
from code_indexer.server.storage.json_column import parse_json_column

# ---------------------------------------------------------------------------
# Constants (code-level; not operator settings)
# ---------------------------------------------------------------------------

TIER_SECURITY = "security"
TIER_AUTH_ACTIVITY = "auth_activity"
TIER_ALL = "all"
AUDIT_TIERS = frozenset({TIER_SECURITY, TIER_AUTH_ACTIVITY, TIER_ALL})

DIRECTION_OLDER = "older"
DIRECTION_NEWER = "newer"
AUDIT_DIRECTIONS = frozenset({DIRECTION_OLDER, DIRECTION_NEWER})

AUDIT_SOURCES = frozenset({SOURCE_REST, SOURCE_MCP, SOURCE_WEB, SOURCE_SYSTEM})

# The authentication target type.  Most of its rows are high-volume
# authentication activity; the ones below are security events and belong to
# the Security tier.
AUTH_TARGET_TYPE = "auth"
SECURITY_PROMOTED_AUTH_ACTIONS: Tuple[str, ...] = (
    "impersonation_cleared",
    "impersonation_denied",
    "impersonation_set",
    "password_change_concurrent_conflict",
    "password_change_failure",
    "password_change_rate_limit",
    "password_change_success",
    "security_incident",
)

DEFAULT_AUDIT_LOG_LIMIT = 100
AUDIT_LOG_MAX_LIMIT = 1000
AUDIT_LOG_MAX_OFFSET = 50_000
AUDIT_COUNT_CAP = 10_000
AUTH_ACTIVITY_DEFAULT_WINDOW = timedelta(hours=24)
SECURITY_VIEW_DEFAULT_WINDOW = timedelta(days=7)  # Web page default only
ATTEMPTED_GRACE_SECONDS = 300
AGGREGATE_MAX_GROUPS = 500
MAX_FILTER_LENGTH = 255

PAIRING_PENDING = "pending"
PAIRING_UNKNOWN = "unknown"

_OUTCOME_ATTEMPTED = "attempted"
_PRE_AUTHENTICATION_METHOD = "none"
_MAX_CURSOR_LENGTH = 512
# Correlation ids per pairing lookup statement (well under SQLite's
# host-parameter limit).
_PAIRING_CHUNK = 400

# Columns every read returns, in one place for both backends.
AUDIT_READ_COLUMNS = (
    "id, timestamp, admin_id, action_type, target_type, target_id, details, "
    "outcome, source, ip_address, correlation_id, node_id, auth_method, "
    "actor_is_system, event_uuid, impersonated_user"
)


class AuditQueryError(ValueError):
    """A read argument is invalid; the door must refuse the request."""


# ---------------------------------------------------------------------------
# Filters
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AuditFilters:
    """Validated read filters.  Build with :func:`build_filters`.

    ``date_from`` and ``date_to`` are timezone-aware UTC datetimes and both
    bounds are inclusive.
    """

    action_type: Optional[str] = None
    actor: Optional[str] = None
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    outcome: Optional[str] = None
    source: Optional[str] = None
    ip_address: Optional[str] = None
    correlation_id: Optional[str] = None
    date_from: Optional[datetime] = None
    date_to: Optional[datetime] = None

    def has_date_range(self) -> bool:
        return self.date_from is not None or self.date_to is not None


def _clean_text(name: str, value: Any) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AuditQueryError(f"{name} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > MAX_FILTER_LENGTH:
        raise AuditQueryError(f"{name} is longer than {MAX_FILTER_LENGTH} characters")
    return text


def parse_audit_time(name: str, value: Any, *, end_of_day: bool) -> Optional[datetime]:
    """Parse a UTC bound: ``YYYY-MM-DD`` or an ISO-8601 date-time.

    A date alone means the start of that day (``end_of_day=False``) or its
    last microsecond (``end_of_day=True``).  A value without an offset is
    UTC.  Anything else raises :class:`AuditQueryError`.
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        parsed = value
    else:
        text = _clean_text(name, value)
        if text is None:
            return None
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            raise AuditQueryError(
                f"{name} is not an ISO-8601 date or date-time"
            ) from None
        if len(text) == 10 and end_of_day:
            parsed = datetime.combine(parsed.date(), time.max)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def build_filters(
    *,
    action_type: Any = None,
    actor: Any = None,
    target_type: Any = None,
    target_id: Any = None,
    outcome: Any = None,
    source: Any = None,
    ip_address: Any = None,
    correlation_id: Any = None,
    date_from: Any = None,
    date_to: Any = None,
) -> AuditFilters:
    """Validate raw door arguments into :class:`AuditFilters`.

    Empty strings mean "no filter".  An unknown outcome or source, an
    over-long value, an unparseable date or an inverted range is refused.
    """
    cleaned_outcome = _clean_text("outcome", outcome)
    if cleaned_outcome is not None and cleaned_outcome not in OUTCOMES:
        raise AuditQueryError(f"outcome must be one of {sorted(OUTCOMES)}")
    cleaned_source = _clean_text("source", source)
    if cleaned_source is not None and cleaned_source not in AUDIT_SOURCES:
        raise AuditQueryError(f"source must be one of {sorted(AUDIT_SOURCES)}")
    start = parse_audit_time("date_from", date_from, end_of_day=False)
    end = parse_audit_time("date_to", date_to, end_of_day=True)
    if start is not None and end is not None and start > end:
        raise AuditQueryError("date_from is after date_to")
    return AuditFilters(
        action_type=_clean_text("action_type", action_type),
        actor=_clean_text("actor", actor),
        target_type=_clean_text("target_type", target_type),
        target_id=_clean_text("target_id", target_id),
        outcome=cleaned_outcome,
        source=cleaned_source,
        ip_address=_clean_text("ip_address", ip_address),
        correlation_id=_clean_text("correlation_id", correlation_id),
        date_from=start,
        date_to=end,
    )


# ---------------------------------------------------------------------------
# Cursor: a self-contained keyset position, never stored server side
# ---------------------------------------------------------------------------


def encode_cursor(timestamp: str, row_id: int) -> str:
    """Encode the ``(timestamp, id)`` keyset position of one row."""
    raw = json.dumps([timestamp, row_id], separators=(",", ":")).encode("utf-8")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(cursor: Any) -> Tuple[str, int]:
    """Decode a cursor from :func:`encode_cursor`; refuse anything else."""
    if not isinstance(cursor, str) or not cursor or len(cursor) > _MAX_CURSOR_LENGTH:
        raise AuditQueryError("malformed cursor")
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeError):
        raise AuditQueryError("malformed cursor") from None
    if (
        not isinstance(value, list)
        or len(value) != 2
        or not isinstance(value[0], str)
        or isinstance(value[1], bool)
        or not isinstance(value[1], int)
        or value[1] < 0
    ):
        raise AuditQueryError("malformed cursor")
    try:
        datetime.fromisoformat(value[0])
    except ValueError:
        raise AuditQueryError("malformed cursor") from None
    return value[0], value[1]


# ---------------------------------------------------------------------------
# SQL (parameterised; rendered for either backend from one definition)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SqlDialect:
    """Placeholder spelling of one backend.

    ``promoted_actions_source`` is the FROM clause used to count the promoted
    authentication actions: SQLite is told to read them through the
    ``action_type`` index (its planner otherwise prefers walking every
    ``auth`` row); PostgreSQL's planner chooses from its own statistics.
    """

    param: str
    timestamp_param: str
    promoted_actions_source: str


SQLITE_DIALECT = SqlDialect(
    param="?",
    timestamp_param="?",
    promoted_actions_source="audit_logs INDEXED BY idx_audit_action_type",
)
POSTGRES_DIALECT = SqlDialect(
    param="%s",
    timestamp_param="%s::timestamptz",
    promoted_actions_source="audit_logs",
)

SqlAndParams = Tuple[str, List[Any]]


def _in_list(dialect: SqlDialect, count: int) -> str:
    return ", ".join(dialect.param for _ in range(count))


def _tier_predicate(tier: str, dialect: SqlDialect) -> SqlAndParams:
    p = dialect.param
    promoted = list(SECURITY_PROMOTED_AUTH_ACTIONS)
    in_list = _in_list(dialect, len(promoted))
    if tier == TIER_SECURITY:
        return (
            f"(target_type <> {p} OR action_type IN ({in_list}))",
            [AUTH_TARGET_TYPE] + promoted,
        )
    if tier == TIER_AUTH_ACTIVITY:
        return (
            f"(target_type = {p} AND action_type NOT IN ({in_list}))",
            [AUTH_TARGET_TYPE] + promoted,
        )
    return "", []


def _joined(*parts: SqlAndParams) -> SqlAndParams:
    sql = " AND ".join(text for text, _ in parts if text)
    return sql, [param for _, params in parts for param in params]


def _where(filters: AuditFilters, tier: str, dialect: SqlDialect) -> SqlAndParams:
    """Tier predicate plus filter predicates, joined with AND (or '')."""
    return _joined(_tier_predicate(tier, dialect), _filter_predicate(filters, dialect))


def _filter_predicate(filters: AuditFilters, dialect: SqlDialect) -> SqlAndParams:
    p = dialect.param
    conditions: List[str] = []
    params: List[Any] = []
    equality = (
        ("action_type", filters.action_type),
        ("admin_id", filters.actor),
        ("target_type", filters.target_type),
        ("target_id", filters.target_id),
        ("outcome", filters.outcome),
        ("source", filters.source),
        ("ip_address", filters.ip_address),
        ("correlation_id", filters.correlation_id),
    )
    for column, value in equality:
        if value is not None:
            conditions.append(f"{column} = {p}")
            params.append(value)
    if filters.date_from is not None:
        conditions.append(f"timestamp >= {dialect.timestamp_param}")
        params.append(filters.date_from.isoformat())
    if filters.date_to is not None:
        conditions.append(f"timestamp <= {dialect.timestamp_param}")
        params.append(filters.date_to.isoformat())
    return " AND ".join(conditions), params


def build_page_sql(
    filters: AuditFilters,
    tier: str,
    dialect: SqlDialect,
    *,
    seek: Optional[Tuple[str, int]],
    direction: str,
    limit: int,
    offset: int,
) -> SqlAndParams:
    """One page of rows, newest first ("older") or oldest first ("newer")."""
    where, params = _where(filters, tier, dialect)
    conditions = [where] if where else []
    comparison = "<" if direction == DIRECTION_OLDER else ">"
    if seek is not None:
        conditions.append(
            f"(timestamp, id) {comparison} ({dialect.timestamp_param}, {dialect.param})"
        )
        params.extend([seek[0], seek[1]])
    order = "DESC" if direction == DIRECTION_OLDER else "ASC"
    sql = f"SELECT {AUDIT_READ_COLUMNS} FROM audit_logs"
    if conditions:
        sql += " WHERE " + " AND ".join(conditions)
    sql += f" ORDER BY timestamp {order}, id {order} LIMIT {dialect.param}"
    params.append(limit)
    if offset:
        sql += f" OFFSET {dialect.param}"
        params.append(offset)
    return sql, params


def build_count_sql(
    filters: AuditFilters, tier: str, dialect: SqlDialect, *, cap: int
) -> SqlAndParams:
    """Count matching rows, reading at most ``cap + 1`` of them per part.

    The Security tier is counted as three DISJOINT parts so each is served
    by an index instead of walking the (overwhelmingly ``auth``) table:
    ``target_type < 'auth'``, ``target_type > 'auth'`` and the promoted
    ``auth`` actions.  Each part is capped at ``cap + 1``, so the sum
    exceeds ``cap`` exactly when the true count does.
    """
    if tier != TIER_SECURITY:
        return _capped_part(
            "audit_logs", _where(filters, tier, dialect), dialect, cap, "capped"
        )
    p = dialect.param
    promoted = list(SECURITY_PROMOTED_AUTH_ACTIONS)
    rest = _filter_predicate(filters, dialect)
    parts = [
        _capped_part(
            "audit_logs",
            _joined((f"target_type < {p}", [AUTH_TARGET_TYPE]), rest),
            dialect,
            cap,
            "below",
        ),
        _capped_part(
            "audit_logs",
            _joined((f"target_type > {p}", [AUTH_TARGET_TYPE]), rest),
            dialect,
            cap,
            "above",
        ),
        _capped_part(
            dialect.promoted_actions_source,
            _joined(
                (
                    f"action_type IN ({_in_list(dialect, len(promoted))}) "
                    f"AND NOT (target_type < {p} OR target_type > {p})",
                    promoted + [AUTH_TARGET_TYPE, AUTH_TARGET_TYPE],
                ),
                rest,
            ),
            dialect,
            cap,
            "promoted",
        ),
    ]
    sql = "SELECT " + " + ".join(f"({text})" for text, _ in parts) + " AS cnt"
    return sql, [param for _, params in parts for param in params]


def _capped_part(
    source: str, where: SqlAndParams, dialect: SqlDialect, cap: int, alias: str
) -> SqlAndParams:
    text, params = where
    inner = f"SELECT 1 FROM {source}" + (f" WHERE {text}" if text else "")
    return (
        f"SELECT COUNT(*) AS cnt FROM ({inner} LIMIT {dialect.param}) AS {alias}",
        params + [cap + 1],
    )


def build_aggregate_sql(
    filters: AuditFilters, tier: str, dialect: SqlDialect, *, max_groups: int
) -> SqlAndParams:
    """Group matching rows by ``(action_type, outcome)`` in SQL."""
    where, params = _where(filters, tier, dialect)
    sql = (
        "SELECT action_type, outcome, COUNT(*) AS event_count, "
        "MIN(timestamp) AS first_seen, MAX(timestamp) AS last_seen, "
        "COUNT(DISTINCT admin_id) AS distinct_actors, "
        "COUNT(DISTINCT ip_address) AS distinct_ips FROM audit_logs"
        + (f" WHERE {where}" if where else "")
        + " GROUP BY action_type, outcome"
        " ORDER BY event_count DESC, action_type ASC, COALESCE(outcome, '') ASC"
        f" LIMIT {dialect.param}"
    )
    return sql, params + [max_groups]


def build_terminal_rows_sql(
    correlation_ids: Sequence[str], dialect: SqlDialect
) -> SqlAndParams:
    """Terminal (non-attempted) rows sharing one of *correlation_ids*."""
    sql = (
        "SELECT DISTINCT correlation_id, action_type, target_id FROM audit_logs "
        f"WHERE correlation_id IN ({_in_list(dialect, len(correlation_ids))}) "
        f"AND outcome IS NOT NULL AND outcome <> {dialect.param}"
    )
    return sql, list(correlation_ids) + [_OUTCOME_ATTEMPTED]


# ---------------------------------------------------------------------------
# Results
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CanonicalAuditRow:
    """What the shared function returns -- never a wire shape.

    ``details`` is the ALLOWLISTED projection of the stored value, as a JSON
    string (:func:`project_details`); each door decides how to render it.
    """

    id: int
    timestamp: str
    admin_id: str
    action_type: str
    target_type: str
    target_id: str
    details: Optional[str]
    outcome: Optional[str]
    source: Optional[str]
    ip_address: Optional[str]
    correlation_id: Optional[str]
    node_id: Optional[str]
    auth_method: Optional[str]
    actor_is_system: bool
    event_uuid: Optional[str]
    actor_is_authenticated: bool
    pairing_state: Optional[str]
    submitted_only: bool
    # The user an administrator was impersonating over MCP (the subject);
    # ``admin_id`` is then the administrator.  None outside impersonation.
    impersonated_user: Optional[str] = None


# The fields every door exposes for one row, in export column order: the
# REST and MCP entries carry exactly these (MCP adds its older aliases), and
# the page's export writes exactly these columns.
AUDIT_ROW_FIELDS: Tuple[str, ...] = (
    "id",
    "timestamp",
    "admin_id",
    "actor_is_system",
    "actor_is_authenticated",
    "action_type",
    "target_type",
    "target_id",
    "outcome",
    "pairing_state",
    "submitted_only",
    "source",
    "ip_address",
    "node_id",
    "correlation_id",
    "auth_method",
    "event_uuid",
    "details",
    # Appended last so existing export columns keep their positions.
    "impersonated_user",
)


def row_fields(row: CanonicalAuditRow) -> Dict[str, Any]:
    """*row* as the shared field set (``details`` still a JSON string)."""
    return {name: getattr(row, name) for name in AUDIT_ROW_FIELDS}


@dataclass(frozen=True)
class AuditPage:
    """One page of rows, newest first, with its keyset navigation."""

    rows: Tuple[CanonicalAuditRow, ...]
    next_cursor: Optional[str]  # continue towards OLDER rows
    prev_cursor: Optional[str]  # continue towards NEWER rows
    has_more: bool  # more rows exist in the requested direction
    has_newer: bool
    has_older: bool
    total: Optional[int]  # None only when the caller skipped the count
    total_capped: bool


@dataclass(frozen=True)
class AuditAggregateGroup:
    action_type: str
    outcome: Optional[str]
    count: int
    first_seen: str
    last_seen: str
    distinct_actors: int
    distinct_ips: int


@dataclass(frozen=True)
class AuditAggregate:
    """Authentication activity grouped by ``(action_type, outcome)``."""

    groups: Tuple[AuditAggregateGroup, ...]
    window_from: Optional[str]
    window_to: Optional[str]
    all_time: bool
    truncated: bool = field(default=False)


def page_fields(page: AuditPage) -> Dict[str, Any]:
    """The navigation fields every door returns with a page of rows."""
    return {
        "total": page.total,
        "total_capped": page.total_capped,
        "next_cursor": page.next_cursor,
        "prev_cursor": page.prev_cursor,
        "has_more": page.has_more,
    }


def aggregate_fields(result: AuditAggregate) -> Dict[str, Any]:
    """The fields every door returns for an aggregate; ``total`` is the
    number of events in the returned groups."""
    return {
        "total": sum(group.count for group in result.groups),
        "groups": [
            {
                "action_type": group.action_type,
                "outcome": group.outcome,
                "count": group.count,
                "first_seen": group.first_seen,
                "last_seen": group.last_seen,
                "distinct_actors": group.distinct_actors,
                "distinct_ips": group.distinct_ips,
            }
            for group in result.groups
        ],
        "window_from": result.window_from,
        "window_to": result.window_to,
        "all_time": result.all_time,
        "truncated": result.truncated,
    }


# ---------------------------------------------------------------------------
# Details: only allowlisted fields ever leave the read path
# ---------------------------------------------------------------------------

OMITTED_FIELDS_KEY = "omitted_fields"
# Markers in the omitted list.  Parenthesised, so they can never collide with
# a real (identifier-shaped) field name.
OMITTED_UNSTRUCTURED = "(unstructured)"
OMITTED_NON_IDENTIFIER = "(non-identifier)"
_FIELD_NAME_CHARS = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_"
)
_MAX_FIELD_NAME_LENGTH = 64

# Read-side allowlist for the legacy-only catalog types (whose payloads
# predate the write-side allowlist).  Names and ids only: free text, peer
# addresses, client strings, session ids, URLs and filesystem paths are
# never shown.  A type missing here shows no field at all.
_GROUP_READ = {"name": OPAQUE_ID, "old_name": OPAQUE_ID, "new_name": OPAQUE_ID}
_MEMBERSHIP_READ = {
    "user_id": USERNAME,
    "group": OPAQUE_ID,
    "from_group": OPAQUE_ID,
    "to_group": OPAQUE_ID,
    "old_group": OPAQUE_ID,
    "new_group": OPAQUE_ID,
    "removed_from_group": OPAQUE_ID,
}
_REPO_ACCESS_READ = {"repo": REPO_ALIAS, "group": OPAQUE_ID}
_ACCOUNT_READ = {"username": USERNAME}
_OAUTH_READ = {"username": USERNAME, "client_id": OPAQUE_ID}
_PR_READ = {"job_id": OPAQUE_ID, "repo_alias": REPO_ALIAS, "branch_name": GIT_REF}
LEGACY_DETAILS_READ_SCHEMA: Dict[str, Dict[str, FieldType]] = {
    "group_create": _GROUP_READ,
    "group_update": _GROUP_READ,
    "group_delete": _GROUP_READ,
    "user_group_change": _MEMBERSHIP_READ,
    "user_group_assign": _MEMBERSHIP_READ,
    "user_assign": _MEMBERSHIP_READ,
    "repo_access_grant": _REPO_ACCESS_READ,
    # A bulk revoke's one summary row counts the repositories not in the group.
    "repo_access_revoke": {**_REPO_ACCESS_READ, "not_in_group_count": INT},
    "impersonation_set": {"actor_username": USERNAME, "target_username": USERNAME},
    "impersonation_cleared": {"actor_username": USERNAME, "previous_target": USERNAME},
    "impersonation_denied": {"actor_username": USERNAME, "target_username": USERNAME},
    "password_change_success": _ACCOUNT_READ,
    "password_change_failure": _ACCOUNT_READ,
    "password_change_concurrent_conflict": _ACCOUNT_READ,
    "password_change_rate_limit": {"username": USERNAME, "attempt_count": INT},
    "security_incident": {"username": USERNAME, "incident_type": OPAQUE_ID},
    "token_refresh_success": _ACCOUNT_READ,
    "token_refresh_failure": {"username": USERNAME, "security_incident": BOOL},
    "oauth_client_registration": {"client_id": OPAQUE_ID},
    "oauth_authorization": _OAUTH_READ,
    "oauth_token_exchange": {**_OAUTH_READ, "grant_type": OPAQUE_ID},
    "oauth_token_revocation": {"username": USERNAME, "token_type": OPAQUE_ID},
    "registration_attempt": {"success": BOOL},
    "pr_creation_success": {**_PR_READ, "commit_hash": OPAQUE_ID},
    "pr_creation_failure": _PR_READ,
    "pr_creation_disabled": {"job_id": OPAQUE_ID, "repo_alias": REPO_ALIAS},
}


# Read-side URL fields: shown only as their :func:`plain_web_url` form, and
# omitted (like any non-conforming field) when the stored value is not a
# plain web URL.  The PR URL is the pull request a PR-creation job opened.
PR_URL_FIELD = "pr_url"
LEGACY_DETAILS_READ_URLS: Dict[str, FrozenSet[str]] = {
    "pr_creation_success": frozenset({PR_URL_FIELD}),
}
_MAX_URL_LENGTH = 2048
_WEB_URL_SCHEMES = frozenset({"https", "http"})
_URL_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?$")
# The pull/merge-request paths the forge clients return (GitHub ``html_url``,
# GitLab ``web_url``).  GitLab nests at most 20 group levels, so a path has at
# most 20 groups + project + "-" + kind + id segments.
_MAX_PR_PATH_LENGTH = 512
_MAX_PR_PATH_SEGMENTS = 24
_PR_PATH_SEGMENT_RE = re.compile(r"^[A-Za-z0-9_.-]{1,255}$")
_PR_NUMBER_RE = re.compile(r"^[0-9]{1,20}$")
_GITHUB_PULL = "pull"
_GITLAB_MERGE_REQUESTS = "merge_requests"
_GITLAB_SEPARATOR = "-"


def _is_ordinary_segment(segment: str) -> bool:
    """Letters, digits, ``-``, ``_`` and ``.`` only; never ``.``, ``..`` or
    the bare GitLab separator ``-``."""
    return (
        bool(_PR_PATH_SEGMENT_RE.match(segment))
        and segment.strip(".") != ""
        and segment != _GITLAB_SEPARATOR
    )


def _is_pr_path(path: str) -> bool:
    """True when *path* is ``/<owner>/<repo>/pull/<n>`` or
    ``/<group>/.../<project>[/-]/merge_requests/<n>``.

    Anything else -- percent-encoding, ``;`` path parameters, empty or dot
    segments, extra trailing segments, an over-long path -- is refused.
    """
    if len(path) > _MAX_PR_PATH_LENGTH or not path.startswith("/"):
        return False
    segments = path[1:].split("/")
    if not 4 <= len(segments) <= _MAX_PR_PATH_SEGMENTS:
        return False
    *project, kind, number = segments
    if not _PR_NUMBER_RE.match(number):
        return False
    if kind == _GITHUB_PULL:
        if len(project) != 2:
            return False
    elif kind == _GITLAB_MERGE_REQUESTS:
        if project[-1] == _GITLAB_SEPARATOR:
            project = project[:-1]
        if len(project) < 2:
            return False
    else:
        return False
    return all(_is_ordinary_segment(segment) for segment in project)


def plain_web_url(value: Any) -> Optional[str]:
    """*value* as a plain PR URL -- scheme, host, port and path -- or None.

    Userinfo (``user:token@``), the query and the fragment are dropped, so
    a credential embedded in a stored URL is never shown, and the path must
    be a pull or merge-request path (:func:`_is_pr_path`), so no other
    content passes through it.  A value that is not an http(s) URL with a
    DNS-style host, is over-long, or contains whitespace or a control
    character is refused.
    """
    if not isinstance(value, str) or not 0 < len(value) <= _MAX_URL_LENGTH:
        return None
    if any(ch.isspace() or ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value):
        return None
    try:
        parts = urlsplit(value)
        port = parts.port
    except ValueError:
        return None
    scheme = parts.scheme.lower()
    host = parts.hostname or ""
    if scheme not in _WEB_URL_SCHEMES or not _URL_HOST_RE.match(host):
        return None
    if not _is_pr_path(parts.path):
        return None
    netloc = host if port is None else f"{host}:{port}"
    return urlunsplit((scheme, netloc, parts.path, "", ""))


def details_read_schema(action_type: str) -> Dict[str, FieldType]:
    """The fields of *action_type* that a reader may see."""
    spec = AUDIT_ACTION_CATALOG.get(action_type)
    if spec is not None and spec.details_schema is not None:
        return dict(spec.details_schema)
    return LEGACY_DETAILS_READ_SCHEMA.get(action_type, {})


def _omitted_name(key: Any) -> str:
    text = key if isinstance(key, str) else ""
    if 0 < len(text) <= _MAX_FIELD_NAME_LENGTH and set(text) <= _FIELD_NAME_CHARS:
        return text
    return OMITTED_NON_IDENTIFIER


def _stored_object(raw: Any) -> Optional[Dict[str, Any]]:
    """The stored JSON object, or None when the content is not one.

    ``details`` is TEXT on both backends; a dict is accepted as-is.  The
    value is never logged (it may be legacy free text).
    """
    if isinstance(raw, dict):
        return raw
    try:
        decoded = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return decoded if isinstance(decoded, dict) else None


def project_details(action_type: str, raw: Any) -> Optional[str]:
    """The allowlisted part of a stored ``details`` value, as JSON.

    Fields of the action type's read schema whose values conform are kept.
    Every other field is dropped and only its NAME is listed under
    ``omitted_fields``; content that is not a JSON object is summarised as
    ``["(unstructured)"]``.  A value is never passed through unchecked.
    Returns None when nothing was stored.
    """
    if raw is None or raw == "":
        return None
    stored = _stored_object(raw)
    if stored is None:
        return json.dumps({OMITTED_FIELDS_KEY: [OMITTED_UNSTRUCTURED]})
    kept, omitted = _allowlisted_fields(action_type, stored)
    if omitted:
        kept[OMITTED_FIELDS_KEY] = sorted(omitted)
    return json.dumps(kept) if kept else None


def _allowlisted_fields(
    action_type: str, stored: Mapping[Any, Any]
) -> Tuple[Dict[str, Any], Set[str]]:
    """The conforming fields of *stored* (URL fields in their plain form)
    and the names of the rest -- the ONE allowlist both the read projection
    and the legacy write path apply."""
    schema = details_read_schema(action_type)
    url_fields = LEGACY_DETAILS_READ_URLS.get(action_type, frozenset())
    kept: Dict[str, Any] = {}
    omitted: Set[str] = set()
    for key, value in stored.items():
        if isinstance(key, str) and key in url_fields:
            shown = plain_web_url(value)
            if shown is not None:
                kept[key] = shown
            else:
                omitted.add(key)
            continue
        ftype = schema.get(key) if isinstance(key, str) else None
        if ftype is not None and conforms(ftype, value):
            kept[key] = value
        else:
            omitted.add(_omitted_name(key))
    return kept, omitted


def restrict_legacy_details(
    action_type: str, details_json: Optional[str]
) -> Optional[str]:
    """What a legacy writer may STORE: the fields a reader may see.

    For an action type in :data:`LEGACY_DETAILS_READ_SCHEMA` only the
    conforming fields are kept (a PR URL in its :func:`plain_web_url` form);
    every other field, and content that is not a JSON object, is not
    stored.  Returns None when nothing is kept.  Other action types are
    returned unchanged (their rows are shown with no field at all).  Rows
    stored before this rule are still shown only through
    :func:`project_details`.
    """
    if action_type not in LEGACY_DETAILS_READ_SCHEMA:
        return details_json
    if details_json is None or details_json == "":
        return None
    stored = _stored_object(details_json)
    if stored is None:
        return None
    kept, _omitted = _allowlisted_fields(action_type, stored)
    return json.dumps(kept) if kept else None


def decode_details(raw: Any) -> Dict[str, Any]:
    """Decode a projected ``details`` value (:func:`project_details`)."""
    if raw is None:
        return {}
    decoded = parse_json_column(raw, dict, "audit_logs.details")
    return decoded if decoded is not None else {}


def _parse_row_time(value: str) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _terminal_keys(store: Any, rows: Sequence[Dict[str, Any]]) -> Set[Tuple]:
    """One bounded lookup per page for the terminal rows of attempted rows."""
    wanted = sorted(
        {
            row["correlation_id"]
            for row in rows
            if row.get("outcome") == _OUTCOME_ATTEMPTED and row.get("correlation_id")
        }
    )
    keys: Set[Tuple] = set()
    for start in range(0, len(wanted), _PAIRING_CHUNK):
        chunk = wanted[start : start + _PAIRING_CHUNK]
        for found in store.find_terminal_rows(chunk):
            keys.add(
                (found["correlation_id"], found["action_type"], found["target_id"])
            )
    return keys


def _pairing_state(row: Dict[str, Any], terminal: Set[Tuple], now: datetime) -> Any:
    if row.get("outcome") != _OUTCOME_ATTEMPTED:
        return None
    key = (row.get("correlation_id"), row.get("action_type"), row.get("target_id"))
    if row.get("correlation_id") and key in terminal:
        return None
    occurred = _parse_row_time(row.get("timestamp") or "")
    if occurred is not None and (now - occurred).total_seconds() < (
        ATTEMPTED_GRACE_SECONDS
    ):
        return PAIRING_PENDING
    return PAIRING_UNKNOWN


def _to_canonical(
    row: Dict[str, Any], terminal: Set[Tuple], now: datetime
) -> CanonicalAuditRow:
    action_type = row.get("action_type") or ""
    return CanonicalAuditRow(
        id=int(row["id"]),
        timestamp=str(row["timestamp"]),
        admin_id=row.get("admin_id") or "",
        action_type=action_type,
        target_type=row.get("target_type") or "",
        target_id=row.get("target_id") or "",
        details=project_details(action_type, row.get("details")),
        outcome=row.get("outcome"),
        source=row.get("source"),
        ip_address=row.get("ip_address"),
        correlation_id=row.get("correlation_id"),
        node_id=row.get("node_id"),
        auth_method=row.get("auth_method"),
        actor_is_system=bool(row.get("actor_is_system") or 0),
        event_uuid=row.get("event_uuid"),
        actor_is_authenticated=not (
            row.get("target_type") == AUTH_TARGET_TYPE
            and row.get("auth_method") == _PRE_AUTHENTICATION_METHOD
        ),
        pairing_state=_pairing_state(row, terminal, now),
        submitted_only=row.get("action_type") in JOB_BASED_ACTION_TYPES,
        impersonated_user=row.get("impersonated_user"),
    )


# ---------------------------------------------------------------------------
# The shared read function
# ---------------------------------------------------------------------------


def clamp_limit(limit: Optional[int]) -> int:
    """``limit`` bounded to ``[1, AUDIT_LOG_MAX_LIMIT]``; unset means default."""
    if limit is None or limit <= 0:
        return DEFAULT_AUDIT_LOG_LIMIT
    return min(int(limit), AUDIT_LOG_MAX_LIMIT)


def _validate_mode(
    tier: str,
    cursor: Optional[str],
    direction: str,
    legacy_offset: Optional[int],
    aggregate: bool,
    all_time: bool,
    filters: AuditFilters,
) -> None:
    if not isinstance(tier, str) or tier not in AUDIT_TIERS:
        raise AuditQueryError(f"tier must be one of {sorted(AUDIT_TIERS)}")
    if not isinstance(direction, str) or direction not in AUDIT_DIRECTIONS:
        raise AuditQueryError(f"direction must be one of {sorted(AUDIT_DIRECTIONS)}")
    if cursor and legacy_offset is not None:
        raise AuditQueryError("cursor and offset cannot be combined")
    if direction == DIRECTION_NEWER and not cursor:
        raise AuditQueryError("direction 'newer' requires a cursor")
    if legacy_offset is not None and legacy_offset < 0:
        raise AuditQueryError("offset must not be negative")
    if all_time and filters.has_date_range():
        raise AuditQueryError("all_time cannot be combined with a date range")
    if aggregate and tier != TIER_AUTH_ACTIVITY:
        raise AuditQueryError("aggregate is only available for tier 'auth_activity'")
    if aggregate and (cursor or legacy_offset is not None):
        raise AuditQueryError("aggregate does not page")


def _aggregate(
    store: Any, filters: AuditFilters, tier: str, all_time: bool, now: datetime
) -> AuditAggregate:
    applied = filters
    if not all_time and not filters.has_date_range():
        applied = replace(filters, date_from=now - AUTH_ACTIVITY_DEFAULT_WINDOW)
    groups = store.aggregate(applied, tier, max_groups=AGGREGATE_MAX_GROUPS + 1)
    truncated = len(groups) > AGGREGATE_MAX_GROUPS
    if applied.date_to is not None:
        window_to: Optional[str] = applied.date_to.isoformat()
    else:
        window_to = None if all_time else now.isoformat()
    return AuditAggregate(
        groups=tuple(
            AuditAggregateGroup(
                action_type=group["action_type"],
                outcome=group["outcome"],
                count=int(group["event_count"]),
                first_seen=str(group["first_seen"]),
                last_seen=str(group["last_seen"]),
                distinct_actors=int(group["distinct_actors"]),
                distinct_ips=int(group["distinct_ips"]),
            )
            for group in groups[:AGGREGATE_MAX_GROUPS]
        ),
        window_from=applied.date_from.isoformat() if applied.date_from else None,
        window_to=window_to,
        all_time=all_time,
        truncated=truncated,
    )


@overload
def query_audit_log(
    store: Any,
    filters: Optional[AuditFilters] = ...,
    tier: str = ...,
    cursor: Optional[str] = ...,
    direction: str = ...,
    limit: Optional[int] = ...,
    legacy_offset: Optional[int] = ...,
    aggregate: Literal[False] = ...,
    all_time: bool = ...,
    *,
    with_total: bool = ...,
    now: Optional[datetime] = ...,
) -> AuditPage: ...


@overload
def query_audit_log(
    store: Any,
    filters: Optional[AuditFilters] = ...,
    tier: str = ...,
    cursor: Optional[str] = ...,
    direction: str = ...,
    limit: Optional[int] = ...,
    legacy_offset: Optional[int] = ...,
    *,
    aggregate: Literal[True],
    all_time: bool = ...,
    with_total: bool = ...,
    now: Optional[datetime] = ...,
) -> AuditAggregate: ...


@overload
def query_audit_log(
    store: Any,
    filters: Optional[AuditFilters] = ...,
    tier: str = ...,
    cursor: Optional[str] = ...,
    direction: str = ...,
    limit: Optional[int] = ...,
    legacy_offset: Optional[int] = ...,
    aggregate: bool = ...,
    all_time: bool = ...,
    *,
    with_total: bool = ...,
    now: Optional[datetime] = ...,
) -> Union[AuditPage, AuditAggregate]: ...


def query_audit_log(
    store: Any,
    filters: Optional[AuditFilters] = None,
    tier: str = TIER_ALL,
    cursor: Optional[str] = None,
    direction: str = DIRECTION_OLDER,
    limit: Optional[int] = None,
    legacy_offset: Optional[int] = None,
    aggregate: bool = False,
    all_time: bool = False,
    *,
    with_total: bool = True,
    now: Optional[datetime] = None,
) -> Union[AuditPage, AuditAggregate]:
    """Read the audit log.  The ONE read path for every door.

    Args:
        store: The audit store (``app.state.audit_service``): anything with
            ``query_page``, ``count_capped``, ``aggregate`` and
            ``find_terminal_rows``.
        filters: Validated filters (:func:`build_filters`).
        tier: ``security``, ``auth_activity`` or ``all``.
        cursor: Keyset token from a previous page (``next_cursor`` to go
            older, ``prev_cursor`` with ``direction="newer"`` to go newer).
        direction: ``older`` (default) or ``newer`` (cursor required).
        limit: Page size, clamped to ``[1, AUDIT_LOG_MAX_LIMIT]``.
        legacy_offset: Compatibility offset for page/offset callers,
            clamped to ``AUDIT_LOG_MAX_OFFSET``.  Not combinable with cursor.
        aggregate: Group authentication activity instead of listing rows.
        all_time: With ``aggregate``, lift the default 24 h window.
        with_total: Skip the capped count when False (streaming export).
        now: Clock override for deterministic tests.

    Raises:
        AuditQueryError: for any invalid argument, including a malformed
            cursor.
    """
    filters = filters if filters is not None else AuditFilters()
    now = now if now is not None else datetime.now(timezone.utc)
    _validate_mode(tier, cursor, direction, legacy_offset, aggregate, all_time, filters)
    if aggregate:
        return _aggregate(store, filters, tier, all_time, now)

    page_size = clamp_limit(limit)
    seek = decode_cursor(cursor) if cursor else None
    offset = min(legacy_offset, AUDIT_LOG_MAX_OFFSET) if legacy_offset else 0
    fetched = store.query_page(
        filters,
        tier,
        seek=seek,
        direction=direction,
        limit=page_size + 1,
        offset=offset,
    )
    has_more = len(fetched) > page_size
    fetched = fetched[:page_size]
    if direction == DIRECTION_NEWER:
        fetched = list(reversed(fetched))
        has_newer, has_older = has_more, True
    else:
        has_newer, has_older = seek is not None or offset > 0, has_more
    terminal = _terminal_keys(store, fetched)
    rows = tuple(_to_canonical(row, terminal, now) for row in fetched)

    total: Optional[int] = None
    total_capped = False
    if with_total:
        counted = int(store.count_capped(filters, tier, cap=AUDIT_COUNT_CAP))
        total_capped = counted > AUDIT_COUNT_CAP
        total = min(counted, AUDIT_COUNT_CAP)
    return AuditPage(
        rows=rows,
        next_cursor=(
            encode_cursor(rows[-1].timestamp, rows[-1].id)
            if rows and has_older
            else None
        ),
        prev_cursor=encode_cursor(rows[0].timestamp, rows[0].id) if rows else None,
        has_more=has_more,
        has_newer=has_newer,
        has_older=has_older,
        total=total,
        total_capped=total_capped,
    )
