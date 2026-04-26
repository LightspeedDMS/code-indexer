"""
Phase 3.7 journal types, writer, and repair helpers for DepMapRepairExecutor.

Story #908. Extracted from dep_map_repair_executor.py per MESSI Rule 6 when
executor exceeded 1100 lines. Pure structural move — zero new logic introduced.

All content here existed verbatim in dep_map_repair_executor.py before extraction.
dep_map_repair_executor.py imports from here; there is no duplication.

Public exports (re-exported by executor for backward compat):
  Action, _VALID_VERDICTS, JournalEntry, RepairJournal
  resolve_self_loop_md_path, remove_self_loop_rows, atomic_write_text
  resolve_repair_journal, build_and_append_journal_entry, run_phase37
"""

import json
import logging
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, List, Optional

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyEntry

logger = logging.getLogger(__name__)


class Action(str, Enum):
    """Journal action identifiers. Story #908 contribution: self_loop_deleted."""

    self_loop_deleted = "self_loop_deleted"


_VALID_VERDICTS: frozenset = frozenset({"CONFIRMED", "REFUTED", "INCONCLUSIVE", "N_A"})


@dataclass(frozen=True)
class JournalEntry:
    """12-field frozen journal entry (AC5). Raises ValueError on bad action/verdict."""

    anomaly_type: str
    source_domain: str
    target_domain: str
    source_repos: List[str]
    target_repos: List[str]
    verdict: str
    action: str
    citations: List[str]
    file_writes: List[Dict[str, str]]
    claude_response_raw: str
    effective_mode: str
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def __post_init__(self) -> None:
        valid_actions = {a.value for a in Action}
        if self.action not in valid_actions:
            raise ValueError(
                f"JournalEntry: action {self.action!r} not in Action enum. "
                f"Valid: {sorted(valid_actions)}"
            )
        if self.verdict not in _VALID_VERDICTS:
            raise ValueError(
                f"JournalEntry: verdict {self.verdict!r} not in {sorted(_VALID_VERDICTS)}"
            )

    def to_dict(self) -> Dict[str, Any]:
        """Return all 12 schema fields as a JSON-serializable dict."""
        return {
            "timestamp": self.timestamp,
            "anomaly_type": self.anomaly_type,
            "source_domain": self.source_domain,
            "target_domain": self.target_domain,
            "source_repos": list(self.source_repos),
            "target_repos": list(self.target_repos),
            "verdict": self.verdict,
            "action": self.action,
            "citations": list(self.citations),
            "file_writes": list(self.file_writes),
            "claude_response_raw": self.claude_response_raw,
            "effective_mode": self.effective_mode,
        }

    def serialize(self) -> str:
        """Return compact JSON + newline (no embedded newlines, AC5)."""
        return (
            json.dumps(self.to_dict(), separators=(",", ":"), ensure_ascii=False) + "\n"
        )


# Journal writer constants — single canonical source; executor imports from here.
# _JOURNAL_LOCK_TIMEOUT_S: AC6 contract (5s defensive against deadlock).
# _DEFAULT_CIDX_DATA_DIR: Bug #879 IPC alignment (mirrors CLAUDE.md pattern).
# _JOURNAL_FILENAME: stable contract between cidx-server and cidx-auto-update.
# Runtime override via CIDX_DATA_DIR env var is enforced in _resolve_journal_data_dir.
_JOURNAL_LOCK_TIMEOUT_S: int = 5
_DEFAULT_CIDX_DATA_DIR: Path = Path.home() / ".cidx-server"
_JOURNAL_FILENAME: str = "dep_map_repair_journal.jsonl"
_write_lock: threading.Lock = threading.Lock()


def _resolve_journal_data_dir() -> Path:
    """Resolve journal data dir from CIDX_DATA_DIR env var or project default."""
    raw = os.environ.get("CIDX_DATA_DIR", "")
    stripped = raw.strip()
    if not stripped:
        return _DEFAULT_CIDX_DATA_DIR
    candidate = Path(stripped)
    if not candidate.is_absolute():
        raise ValueError(
            f"CIDX_DATA_DIR must be an absolute path when set, got: {stripped!r}"
        )
    return candidate


class RepairJournal:
    """Append-only JSONL journal for Phase 3.7 repairs (AC5-AC7).

    Honors CIDX_DATA_DIR env var (AC7/Bug #879). Atomic per-line writes (AC6).
    """

    def __init__(self, journal_path: Optional[Path] = None) -> None:
        if journal_path is not None:
            if not isinstance(journal_path, Path):
                raise TypeError(
                    f"journal_path must be a pathlib.Path, got {type(journal_path).__name__}"
                )
            if not journal_path.is_absolute():
                raise ValueError(
                    f"journal_path must be absolute when provided, got: {journal_path}"
                )
            self.journal_path = journal_path
        else:
            self.journal_path = _resolve_journal_data_dir() / _JOURNAL_FILENAME
        self.journal_path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, entry: JournalEntry) -> None:
        """Atomically append one entry (AC6). Raises TypeError/RuntimeError on failure."""
        if entry is None:
            raise TypeError("entry must not be None")
        line = entry.serialize()
        acquired = _write_lock.acquire(timeout=_JOURNAL_LOCK_TIMEOUT_S)
        if not acquired:
            raise RuntimeError("journal lock acquisition timed out")
        try:
            with open(self.journal_path, "a", encoding="utf-8") as fh:
                fh.write(line)
                fh.flush()
                os.fsync(fh.fileno())
        finally:
            _write_lock.release()


def resolve_self_loop_md_path(
    output_dir: Path,
    domain_name: str,
    errors: List[str],
) -> Optional[Path]:
    """Validate domain_name and return its .md path, or None with an error (AC8)."""
    if (
        not domain_name
        or "/" in domain_name
        or "\\" in domain_name
        or ".." in domain_name
    ):
        errors.append(f"Phase 3.7: unsafe path rejected (traversal): {domain_name!r}")
        return None
    md_path = output_dir / f"{domain_name}.md"
    if not md_path.exists():
        errors.append(f"Phase 3.7: cannot repair {domain_name!r} — file not found")
        return None
    return md_path


def remove_self_loop_rows(domain_name: str, lines: List[str]) -> List[str]:
    """Return lines with self-loop table rows removed.

    A self-loop row has cells[3] (4th pipe-delimited cell, after the leading
    empty cell from the leading pipe) matching domain_name after strip.
    """
    result: List[str] = []
    for line in lines:
        stripped = line.rstrip("\n")
        if stripped.startswith("|"):
            cells = stripped.split("|")
            if len(cells) > 3 and cells[3].strip() == domain_name:
                continue
        result.append(line)
    return result


def atomic_write_text(target_path: Path, content: str, errors: List[str]) -> bool:
    """Atomically write content via mkstemp+os.replace. Returns False on failure."""
    import tempfile

    tmp_path_str: Optional[str] = None
    try:
        tmp_fd, tmp_path_str = tempfile.mkstemp(dir=target_path.parent, suffix=".tmp")
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_fh:
            tmp_fh.write(content)
            tmp_fh.flush()
            os.fsync(tmp_fh.fileno())
        os.replace(tmp_path_str, target_path)
        return True
    except OSError as exc:
        if tmp_path_str is not None:
            try:
                os.unlink(tmp_path_str)
            except OSError:
                pass
        errors.append(f"Phase 3.7: atomic write failed for {target_path.name}: {exc}")
        return False


def resolve_repair_journal(
    journal_path: Optional[Path], journal: Optional[RepairJournal]
) -> RepairJournal:
    """Return journal: journal_path > journal instance > default."""
    if journal_path is not None:
        return RepairJournal(journal_path=journal_path)
    if journal is not None:
        return journal
    return RepairJournal()


def build_and_append_journal_entry(
    md_path: Path,
    domain_name: str,
    journal_path: Optional[Path],
    journal: Optional[RepairJournal],
    errors: Optional[List[str]] = None,
) -> None:
    """Build SELF_LOOP JournalEntry and append. Catches write/format exceptions (AC8)."""
    try:
        jnl = resolve_repair_journal(journal_path, journal)
        entry = JournalEntry(
            anomaly_type="SELF_LOOP",
            source_domain=domain_name,
            target_domain=domain_name,
            source_repos=[],
            target_repos=[],
            verdict="N_A",
            action=Action.self_loop_deleted.value,
            citations=[],
            file_writes=[{"path": str(md_path), "operation": "row_deleted"}],
            claude_response_raw="",
            effective_mode="deterministic",
        )
        jnl.append(entry)
    except (ValueError, TypeError, RuntimeError, OSError) as exc:
        msg = f"Phase 3.7: journal write failed for {domain_name}: {exc}"
        if errors is not None:
            errors.append(msg)
        else:
            logger.warning(msg)


def _repair_one_self_loop(
    output_dir: Path,
    anomaly: "AnomalyEntry",
    fixed: List[str],
    errors: List[str],
    journal: Optional[RepairJournal],
) -> None:
    """Remove one SELF_LOOP row and journal the repair. Never raises (AC8)."""
    raw_file = anomaly.file
    if ".." in raw_file:
        errors.append(f"Phase 3.7: unsafe path rejected (traversal): {raw_file!r}")
        return
    filename = Path(raw_file).name
    if not filename.endswith(".md"):
        errors.append(f"Phase 3.7: anomaly file does not end with .md: {raw_file!r}")
        return
    domain_name = filename[: -len(".md")]
    md_path = resolve_self_loop_md_path(output_dir, domain_name, errors)
    if md_path is None:
        return
    try:
        original_lines = md_path.read_text(encoding="utf-8").splitlines(keepends=True)
    except OSError as exc:
        errors.append(f"Phase 3.7: cannot read {domain_name}.md: {exc}")
        return
    new_lines = remove_self_loop_rows(domain_name, original_lines)
    if new_lines == original_lines:
        return
    if not atomic_write_text(md_path, "".join(new_lines), errors):
        return
    fixed.append(f"Phase 3.7: removed self-loop from {domain_name}.md")
    build_and_append_journal_entry(md_path, domain_name, None, journal, errors)


def run_phase37(output_dir: Path, fixed: List[str], errors: List[str]) -> None:
    """Repair SELF_LOOP anomalies across all domains (Phase 3.7 orchestrator).

    Called by DepMapRepairExecutor._run_phase37 shim after enable flag check.
    Creates one RepairJournal so CIDX_DATA_DIR env var is honoured (Bug #879).
    """
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType

    parser = DepMapMCPParser(dep_map_path=output_dir.parent)
    _, _, _, data_anomalies = parser.get_cross_domain_graph_with_channels()
    self_loop_anomalies = [a for a in data_anomalies if a.type == AnomalyType.SELF_LOOP]
    if not self_loop_anomalies:
        return
    journal = RepairJournal()
    for anomaly in self_loop_anomalies:
        _repair_one_self_loop(output_dir, anomaly, fixed, errors, journal)
