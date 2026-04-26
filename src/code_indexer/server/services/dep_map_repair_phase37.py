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
    """Journal action identifiers.

    Story #908: self_loop_deleted.
    Story #910: malformed_yaml_reemitted.
    """

    self_loop_deleted = "self_loop_deleted"
    malformed_yaml_reemitted = "malformed_yaml_reemitted"


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


def body_byte_offset(raw_bytes: bytes, close_idx: int) -> int:
    """Return byte offset of the first byte AFTER the closing --- line.

    Counts (close_idx + 1) newline bytes from the start of raw_bytes to
    find the position immediately after the closing '---\\n' delimiter.
    Preserves body bytes including mixed \\r\\n endings (AC5).

    Extracted from DepMapRepairExecutor._body_byte_offset (Finding #3 / Story #910).
    """
    count = 0
    target = close_idx + 1
    for i, b in enumerate(raw_bytes):
        if b == ord("\n"):
            count += 1
            if count == target:
                return i + 1
    return len(raw_bytes)


def emit_repos_lines(json_repos: List[str]) -> List[str]:
    """Return YAML lines for the participating_repos block.

    Returns a list entry for each repo, or an inline empty list.
    Used by reemit_frontmatter_from_domain_info to avoid duplicating emission logic.

    Extracted from DepMapRepairExecutor._emit_repos_lines (Finding #3 / Story #910).
    """
    from code_indexer.global_repos.yaml_emitter_utils import yaml_quote_if_unsafe

    if json_repos:
        return ["participating_repos:"] + [
            f"  - {yaml_quote_if_unsafe(r)}" for r in json_repos
        ]
    return ["participating_repos: []"]


def reemit_frontmatter_from_domain_info(
    content: str,
    bounds: tuple,
    domain_info: Dict[str, Any],
) -> str:
    """Replace frontmatter with authoritative _domains.json values; body unchanged.

    Uses split("\\n") (not splitlines()) to preserve \\r chars in body,
    keeping mixed line-ending files byte-identical after round-trip (AC5).

    Preconditions (asserted; stripped under python -O):
      - content is str
      - bounds is a 2-item tuple of ints with open_idx < close_idx
      - domain_info is a dict

    Extracted from DepMapRepairExecutor._reemit_frontmatter_from_domain_info (Finding #3).
    """
    from code_indexer.global_repos.yaml_emitter_utils import yaml_quote_if_unsafe

    assert isinstance(content, str), "content must be str"
    assert (
        isinstance(bounds, tuple)
        and len(bounds) == 2
        and isinstance(bounds[0], int)
        and isinstance(bounds[1], int)
        and bounds[0] < bounds[1]
    ), f"bounds must be (int, int) with open_idx < close_idx, got {bounds!r}"
    assert isinstance(domain_info, dict), "domain_info must be a dict"

    lines = content.split("\n")
    open_idx, close_idx = bounds
    body_lines = lines[close_idx:]  # closing "---" + everything after

    name = domain_info.get("name", "")
    last_analyzed = domain_info.get("last_analyzed", "")
    json_repos = domain_info.get("participating_repos", [])
    if not isinstance(json_repos, list):
        json_repos = []

    old_fm_lines = lines[open_idx + 1 : close_idx]
    new_fm: List[str] = []
    name_done = last_analyzed_done = repos_done = False
    skip_repos_indent = False

    for line in old_fm_lines:
        if line.startswith("name:"):
            new_fm.append(f"name: {yaml_quote_if_unsafe(name)}")
            name_done = True
            skip_repos_indent = False
        elif line.startswith("last_analyzed"):
            new_fm.append(f"last_analyzed: {yaml_quote_if_unsafe(last_analyzed)}")
            last_analyzed_done = True
            skip_repos_indent = False
        elif line.startswith("participating_repos:"):
            new_fm.extend(emit_repos_lines(json_repos))
            repos_done = True
            skip_repos_indent = True
        elif skip_repos_indent and (line.startswith("  ") or line.startswith("\t")):
            continue  # drop old list items
        else:
            skip_repos_indent = False
            new_fm.append(line)

    if not name_done:
        new_fm.append(f"name: {yaml_quote_if_unsafe(name)}")
    if not last_analyzed_done:
        new_fm.append(f"last_analyzed: {yaml_quote_if_unsafe(last_analyzed)}")
    if not repos_done:
        new_fm.extend(emit_repos_lines(json_repos))

    return "\n".join(["---"] + new_fm + body_lines)


def build_and_append_malformed_yaml_journal_entry(
    file_path: Path,
    domain_name: str,
    errors: Optional[List[str]] = None,
) -> None:
    """Build MALFORMED_YAML JournalEntry and append. Catches write/format exceptions (AC8).

    Called after a successful surgical frontmatter re-emit (Story #910).
    """
    try:
        jnl = RepairJournal()
        entry = JournalEntry(
            anomaly_type="MALFORMED_YAML",
            source_domain=domain_name,
            target_domain=domain_name,
            source_repos=[],
            target_repos=[],
            verdict="N_A",
            action=Action.malformed_yaml_reemitted.value,
            citations=[],
            file_writes=[
                {"path": str(file_path), "operation": "frontmatter_reemitted"}
            ],
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


def run_phase37(
    output_dir: Path,
    fixed: List[str],
    errors: List[str],
) -> None:
    """Repair SELF_LOOP graph-channel anomalies (Phase 3.7 SELF_LOOP orchestrator).

    Called by DepMapRepairExecutor._run_phase37 shim after enable flag check.
    Creates one RepairJournal so CIDX_DATA_DIR env var is honoured (Bug #879).

    MALFORMED_YAML repairs are handled separately by run_malformed_yaml_repairs
    in dep_map_repair_malformed_yaml.py (Story #910 extraction).
    """
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType

    parser = DepMapMCPParser(dep_map_path=output_dir.parent)
    _, _all_anomalies, _parser_anomalies, data_anomalies = (
        parser.get_cross_domain_graph_with_channels()
    )

    # SELF_LOOP repairs (Story #908)
    self_loop_anomalies = [a for a in data_anomalies if a.type == AnomalyType.SELF_LOOP]
    if self_loop_anomalies:
        journal = RepairJournal()
        for anomaly in self_loop_anomalies:
            _repair_one_self_loop(output_dir, anomaly, fixed, errors, journal)
