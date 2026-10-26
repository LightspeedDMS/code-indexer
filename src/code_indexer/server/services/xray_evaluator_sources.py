# ^ invariant only the search event writer thread and the admin export touch xray_evaluator_sources; never a search request
import hashlib
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

# ^ why cap 262144 chars, not unbounded; one submitted source bounds one row (8x the XLSX cell)
EVALUATOR_SOURCE_MAX_CHARS = 262_144

# ^ invariant a 64-hex sha256 never holds HASH_SEPARATOR; a batch row joins its hashes with it
HASH_SEPARATOR = ","
# ^ coupled with services/xray_pattern_names.py:is_storable_pattern_name; a stored name never holds "/"
PATTERN_NAME_SEPARATOR = "/"

# Hashes per lookup statement (bounded IN list).
_LOOKUP_CHUNK = 500

_TABLE_COLUMNS = "evaluator_sha256, source, source_truncated, first_seen, last_seen"

_CREATE_LAST_SEEN_INDEX = (
    "CREATE INDEX IF NOT EXISTS idx_xes_last_seen ON xray_evaluator_sources (last_seen)"
)


@dataclass(frozen=True)
class EvaluatorSource:
    sha256: str
    source: str
    truncated: bool


def storable_text(value: str) -> str:
    # ^ external SQLite and psycopg refuse a lone surrogate (not UTF-8) => stored as its backslash escape
    return value.encode("utf-8", "backslashreplace").decode("utf-8")


def evaluator_source(source: str) -> EvaluatorSource:
    # ^ invariant sha256 = full source's surrogatepass bytes, also when the stored text is cut or escaped
    digest = hashlib.sha256(source.encode("utf-8", "surrogatepass")).hexdigest()
    cut = source[:EVALUATOR_SOURCE_MAX_CHARS]
    stored = storable_text(cut)
    # ^ invariant truncated = stored text != the hashed source (cut or escaped)
    return EvaluatorSource(
        sha256=digest,
        source=stored,
        truncated=len(source) > EVALUATOR_SOURCE_MAX_CHARS or stored != cut,
    )


def split_hashes(value: Optional[str]) -> List[str]:
    return [h for h in (value or "").split(HASH_SEPARATOR) if h]


def sqlite_ddl() -> Tuple[str, ...]:
    return (
        "CREATE TABLE IF NOT EXISTS xray_evaluator_sources ("
        "evaluator_sha256 TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "source_truncated INTEGER NOT NULL, first_seen REAL NOT NULL, "
        "last_seen REAL NOT NULL)",
        _CREATE_LAST_SEEN_INDEX,
    )


# ^ coupled with storage/postgres/migrations/sql/078_xray_evaluator_identity.sql; same table
def pg_ddl() -> Tuple[str, ...]:
    return (
        "CREATE TABLE IF NOT EXISTS xray_evaluator_sources ("
        "evaluator_sha256 TEXT PRIMARY KEY, source TEXT NOT NULL, "
        "source_truncated BOOLEAN NOT NULL, first_seen DOUBLE PRECISION NOT NULL, "
        "last_seen DOUBLE PRECISION NOT NULL)",
        _CREATE_LAST_SEEN_INDEX,
    )


def _distinct_sources(records: Iterable[Any]) -> List[Tuple[EvaluatorSource, float]]:
    # ^ order sorted by hash => one lock order for concurrent PostgreSQL writers
    newest: Dict[str, Tuple[EvaluatorSource, float]] = {}
    for record in records:
        for source in record.evaluator_sources:
            seen = newest.get(source.sha256)
            if seen is None or record.timestamp > seen[1]:
                newest[source.sha256] = (source, record.timestamp)
    return [newest[sha] for sha in sorted(newest)]


def store_sources(conn: Any, records: Sequence[Any], *, sqlite: bool) -> None:
    # ^ invariant insert-if-absent; a stored source only advances last_seen
    rows = _distinct_sources(records)
    if not rows:
        return
    ph = "?" if sqlite else "%s"
    newest = (
        "MAX(last_seen, excluded.last_seen)"
        if sqlite
        else "GREATEST(xray_evaluator_sources.last_seen, EXCLUDED.last_seen)"
    )
    sql = (
        f"INSERT INTO xray_evaluator_sources ({_TABLE_COLUMNS}) "
        f"VALUES ({', '.join([ph] * 5)}) "
        f"ON CONFLICT (evaluator_sha256) DO UPDATE SET last_seen = {newest}"
    )
    if not sqlite:
        conn.execute("SELECT pg_advisory_xact_lock_shared(%s)", (_RETENTION_LOCK_KEY,))
    for source, seen in rows:
        flag: Any = int(source.truncated) if sqlite else source.truncated
        conn.execute(sql, (source.sha256, source.source, flag, seen, seen))


# ^ invariant source writes hold this key shared, prune holds it exclusive; SQLite's write lock does the same
# ^ hazard dropping either side => orphaned source; test:test_postgres_prune_racing_a_delayed_old_write_never_orphans_its_source
_RETENTION_LOCK_KEY = 2203_000_000_001


def lock_for_prune(conn: Any) -> None:
    # ^ order before prune's first delete; held until its commit
    conn.execute("SELECT pg_advisory_xact_lock(%s)", (_RETENTION_LOCK_KEY,))


def prune_sources(conn: Any, cutoff_timestamp: float, *, sqlite: bool) -> None:
    # ^ invariant rows naming a hash have timestamp <= its last_seen; they are pruned first
    ph = "?" if sqlite else "%s"
    conn.execute(
        f"DELETE FROM xray_evaluator_sources WHERE last_seen < {ph}",
        (cutoff_timestamp,),
    )


def lookup_sources(
    conn: Any,
    hashes: Iterable[str],
    *,
    sqlite: bool,
    max_chars: Optional[int] = None,
) -> Dict[str, Tuple[str, bool]]:
    # ^ perf max_chars cuts each source in SQL; an export batch never loads whole sources
    wanted = sorted(set(hashes))
    ph = "?" if sqlite else "%s"
    column = "source" if max_chars is None else f"substr(source, 1, {ph})"
    leading: List[Any] = [] if max_chars is None else [int(max_chars)]
    found: Dict[str, Tuple[str, bool]] = {}
    for start in range(0, len(wanted), _LOOKUP_CHUNK):
        chunk = wanted[start : start + _LOOKUP_CHUNK]
        rows = conn.execute(
            f"SELECT evaluator_sha256, {column}, source_truncated "
            "FROM xray_evaluator_sources "
            f"WHERE evaluator_sha256 IN ({', '.join([ph] * len(chunk))})",
            leading + chunk,
        ).fetchall()
        for sha, source, truncated in rows:
            found[sha] = (source, bool(truncated))
    return found
