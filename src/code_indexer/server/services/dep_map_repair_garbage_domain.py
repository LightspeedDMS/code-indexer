"""
GARBAGE_DOMAIN_REJECTED repair helpers for Phase 3.7 (Story #911).

Extracted from dep_map_repair_executor.py per MESSI Rule 6.
Pure functions with no class state.

Public exports:
  build_inverted_repo_index  -- Dict[repo_alias -> set of domain names]
  prepare_outgoing_rewrite   -- parse outgoing table, return (new_content, cells) without writing
  find_existing_incoming_row -- idempotence check for mirror backfill
  insert_incoming_row        -- append mirror row to target incoming section
"""

import logging
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Section heading constants (match dep_map_parser_tables.py)
# ---------------------------------------------------------------------------

_OUTGOING_SECTION = "### Outgoing Dependencies"
_INCOMING_SECTION = "### Incoming Dependencies"
_OUTGOING_HEADER_SENTINEL = "This Repo"
_INCOMING_HEADER_SENTINEL = "External Repo"
_SECTION_LEVEL = 3  # ### headings

# ---------------------------------------------------------------------------
# Named column indices for outgoing dependency table rows (0-based).
# _COL_EXT_REPO is "This Repo" in outgoing, maps to External Repo in incoming.
# ---------------------------------------------------------------------------
_COL_EXT_REPO = 0
_COL_DEPENDS_ON = 1
_COL_DEP_TYPE = 3
_COL_WHY = 4
_COL_EVIDENCE = 5


def _resolve_source_path(
    output_dir: "Path",
    raw_file: str,
    is_safe_fn: Any,
    errors: List[str],
) -> Optional[Tuple[str, "Path"]]:
    """Return (stem, source_path) if safe and exists, else None with error appended."""
    from pathlib import Path as _Path

    if ".." in raw_file:
        errors.append("Phase 3.7: rejected unsafe path in garbage-domain anomaly")
        return None
    stem = _Path(raw_file).stem
    if not is_safe_fn(stem):
        errors.append("Phase 3.7: rejected unsafe path in garbage-domain anomaly")
        return None
    source_path = output_dir / f"{stem}.md"
    if not source_path.exists():
        errors.append(
            f"Phase 3.7: cannot rescue garbage-domain in {raw_file}: file not found"
        )
        return None
    return stem, source_path


def _extract_candidates(message: str, repo_to_domains: Dict[str, Set[str]]) -> Set[str]:
    """Tokenize message and return candidate domain names via inverted index lookup."""
    import re

    tokens = re.findall(r"[A-Za-z0-9_-]+", message)
    candidates: Set[str] = set()
    for tok in tokens:
        candidates |= repo_to_domains.get(tok, set())
    candidates.discard("")
    return candidates


def _find_remapped_outgoing_cells(
    content: str, target_domain: str, prose_fragment: str
) -> Optional[List[str]]:
    """Return outgoing row cells only when the anomaly row has already been repaired.

    Both conditions must hold: (1) no row with cells[2] == prose_fragment remains
    (the anomaly row was rewritten), and (2) a row with cells[2] == target_domain
    exists (the repaired anomaly row). A pre-existing legitimate row pointing to
    target_domain cannot suppress repair of the anomaly row because prose_fragment
    still present means the anomaly row has not yet been rewritten.
    Requires at least 6 cells so the result is usable by insert_incoming_row.
    """
    lines = content.split("\n")
    in_outgoing = False
    prose_row_found = False
    remapped_cells: Optional[List[str]] = None

    for line in lines:
        stripped = line.strip()
        if stripped == _OUTGOING_SECTION:
            in_outgoing = True
            continue
        if in_outgoing and stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl <= _SECTION_LEVEL:
                in_outgoing = False
        if in_outgoing and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if (
                len(cells) >= 3
                and cells[0] != _OUTGOING_HEADER_SENTINEL
                and not (set(cells[0]) <= frozenset("-"))
            ):
                if cells[2] == prose_fragment:
                    prose_row_found = True
                elif (
                    len(cells) >= 6
                    and cells[2] == target_domain
                    and remapped_cells is None
                ):
                    remapped_cells = cells

    return None if prose_row_found else remapped_cells


def _execute_unique_rewrite(
    source_path: "Path",
    target_path: "Path",
    prose_fragment: str,
    target_domain: str,
    errors: List[str],
    dry_run: bool = False,
    would_be_writes: Optional[List] = None,
) -> Tuple[List[str], bool]:
    """Read source, prepare rewrite, check target exists, write source atomically.

    Returns (outgoing_cells, success). Detects already-remapped rows for idempotence:
    if a prior call already replaced the prose fragment, skips the source write and
    returns the existing row's cells so the backfill guard can still run.
    dry_run=True: skips _atomic_write; appends to would_be_writes instead.
    """
    from code_indexer.server.services.dep_map_repair_phase37 import (
        acquire_domain_lock,
        atomic_write_text as _atomic_write,
    )

    try:
        source_content = source_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"Phase 3.7: cannot read {source_path.name}: {exc}")
        return [], False

    if not target_path.exists():
        errors.append(
            f"Phase 3.7: target domain markdown {target_domain}.md missing for mirror backfill"
        )
        return [], False

    # Idempotence: if the outgoing cell was already rewritten by a prior call, skip
    # the source write and let the backfill guard (find_existing_incoming_row) decide.
    already_cells = _find_remapped_outgoing_cells(
        source_content, target_domain, prose_fragment
    )
    if already_cells:
        return already_cells, True

    new_content, outgoing_cells = prepare_outgoing_rewrite(
        source_content, prose_fragment, target_domain, errors, source_path.name
    )
    if new_content is None:
        return [], False

    if dry_run:
        if would_be_writes is not None:
            would_be_writes.append((str(source_path), "outgoing_cell_rewritten"))
        return outgoing_cells, True

    try:
        with acquire_domain_lock(source_path.stem):
            if not _atomic_write(source_path, new_content, errors):
                return [], False
    except TimeoutError as exc:
        errors.append(f"Phase 3.7: domain lock timeout for {source_path.stem}: {exc}")
        return [], False

    return outgoing_cells, True


def repair_one_garbage_domain_anomaly(
    output_dir: "Path",
    example: Any,
    repo_to_domains: Dict[str, Set[str]],
    journal: Any,
    fixed: List[str],
    errors: List[str],
    *,
    is_safe_domain_name_fn: Any,
    append_journal_fn: Any,
    journal_and_backfill_fn: Any,
    extract_prose_fn: Any,
    log_fn: Any = None,
    dry_run: bool = False,
    journal_disabled: bool = False,
    effective_mode: str = "enabled",
    would_be_writes: Optional[List] = None,
) -> None:
    """Process one AnomalyEntry for GARBAGE_DOMAIN_REJECTED repair.

    Journals manual review for all unresolvable cases (ambiguous, no-match,
    unsafe target). Delegates I/O to _resolve_source_path, _extract_candidates,
    and _execute_unique_rewrite. log_fn (optional) receives sorted candidate
    domain names for both the ambiguous and no-match paths.
    dry_run=True: passes through to _execute_unique_rewrite and
    journal_and_backfill_fn; skips file writes but does NOT suppress journaling.
    journal_disabled=True: suppresses all journaling (invocation-level dry_run).
    Per-type dry_run: dry_run=True, journal_disabled=False => no file writes, journal IS written.
    effective_mode: label written to journal entries ('enabled' or 'dry_run').
    """
    from code_indexer.server.services.dep_map_repair_phase37 import Action

    resolved = _resolve_source_path(
        output_dir, example.file, is_safe_domain_name_fn, errors
    )
    if resolved is None:
        return
    stem, source_path = resolved

    candidates = _extract_candidates(example.message, repo_to_domains)
    if len(candidates) != 1:
        label = "no" if len(candidates) == 0 else "ambiguous"
        errors.append(
            f"Phase 3.7: {label} mapping in {example.file}; manual review required"
        )
        if log_fn is not None:
            log_fn(
                f"Phase 3.7: {label} garbage-domain mapping in {example.file}: "
                f"candidates = {sorted(candidates)}"
            )
        if not journal_disabled:
            append_journal_fn(
                journal,
                stem,
                "",
                Action.garbage_domain_ambiguous_review,
                [example.message],
                errors=errors,
                effective_mode=effective_mode,
            )
        return

    target_domain = next(iter(candidates))
    if not is_safe_domain_name_fn(target_domain):
        errors.append(
            f"Phase 3.7: rejected unsafe target domain name: {target_domain!r}"
        )
        if not journal_disabled:
            append_journal_fn(
                journal,
                stem,
                "",
                Action.garbage_domain_ambiguous_review,
                [example.message],
                errors=errors,
                effective_mode=effective_mode,
            )
        return

    target_path = output_dir / f"{target_domain}.md"
    outgoing_cells, ok = _execute_unique_rewrite(
        source_path,
        target_path,
        extract_prose_fn(example.message),
        target_domain,
        errors,
        dry_run=dry_run,
        would_be_writes=would_be_writes,
    )
    if not ok:
        return

    journal_and_backfill_fn(
        journal,
        stem,
        target_domain,
        source_path,
        target_path,
        outgoing_cells,
        fixed,
        errors,
        dry_run=dry_run,
        journal_disabled=journal_disabled,
        effective_mode=effective_mode,
        would_be_writes=would_be_writes,
    )


def _validate_non_journal_args(
    stem: str,
    target_domain: str,
    source_path: Path,
    target_path: Path,
    outgoing_cells: List[str],
    fixed: List[str],
    errors: List[str],
) -> None:
    """Raise TypeError for any invalid non-journal argument.

    Called on both the dry-run and normal code paths so argument integrity is
    enforced regardless of whether journal validation is applicable.
    """
    if not isinstance(stem, str) or not stem:
        raise TypeError("stem must be a non-empty str")
    if not isinstance(target_domain, str) or not target_domain:
        raise TypeError("target_domain must be a non-empty str")
    if not isinstance(source_path, Path):
        raise TypeError("source_path must be a Path instance")
    if not isinstance(target_path, Path):
        raise TypeError("target_path must be a Path instance")
    if not isinstance(outgoing_cells, list):
        raise TypeError("outgoing_cells must be a list")
    if not isinstance(fixed, list):
        raise TypeError("fixed must be a list")
    if not isinstance(errors, list):
        raise TypeError("errors must be a list")


def _validate_journal_backfill_args(
    journal: Any,
    stem: str,
    target_domain: str,
    source_path: Path,
    target_path: Path,
    outgoing_cells: List[str],
    fixed: List[str],
    errors: List[str],
    append_journal_fn: Any,
) -> None:
    """Raise TypeError for any invalid argument to journal_and_backfill_garbage_domain."""
    _validate_non_journal_args(
        stem, target_domain, source_path, target_path, outgoing_cells, fixed, errors
    )
    if journal is None or not hasattr(journal, "append"):
        raise TypeError("journal must have an append() method")
    if not callable(append_journal_fn):
        raise TypeError("append_journal_fn must be callable")


def _write_target_backfill(
    target_path: Path,
    stem: str,
    target_domain: str,
    outgoing_cells: List[str],
    fixed: List[str],
    errors: List[str],
    journal: Any,
    append_journal_fn: Any,
    dry_run: bool = False,
    journal_disabled: bool = False,
    effective_mode: str = "enabled",
    would_be_writes: Optional[List] = None,
) -> None:
    """Read target, detect duplicate row, insert+write if new, journal the backfill.

    dry_run=True: skips atomic_write_text; records would-be write in would_be_writes.
    journal_disabled=True: suppresses append_journal_fn call (invocation-level dry_run).
    Per-type dry_run: dry_run=True, journal_disabled=False => no file write, journal IS written.
    effective_mode: label written to journal entry ('enabled' or 'dry_run').
    """
    from code_indexer.server.services.dep_map_repair_phase37 import (
        Action,
        acquire_domain_lock,
        atomic_write_text,
    )

    try:
        target_content = target_path.read_text(encoding="utf-8")
    except OSError as exc:
        errors.append(f"Phase 3.7: cannot read {target_path.name} for backfill: {exc}")
        return

    dep_type = (
        outgoing_cells[_COL_DEP_TYPE] if len(outgoing_cells) > _COL_DEP_TYPE else ""
    )
    if find_existing_incoming_row(target_content, stem, dep_type):
        fixed.append(
            f"Phase 3.7: mirror row already present for {stem}->{target_domain}; skipped backfill"
        )
        return

    try:
        new_target = insert_incoming_row(target_content, stem, outgoing_cells)
    except ValueError as exc:
        errors.append(f"Phase 3.7: backfill failed for {target_path.name}: {exc}")
        return

    if dry_run:
        if would_be_writes is not None:
            would_be_writes.append((str(target_path), "mirror_row_backfilled"))
        if not journal_disabled:
            append_journal_fn(
                journal,
                stem,
                target_domain,
                Action.garbage_domain_remapped,
                [],
                [
                    {
                        "path": str(target_path),
                        "change": "mirror row backfilled (dry-run)",
                    }
                ],
                errors=errors,
                effective_mode=effective_mode,
            )
        return

    try:
        with acquire_domain_lock(target_domain):
            if atomic_write_text(target_path, new_target, errors):
                append_journal_fn(
                    journal,
                    stem,
                    target_domain,
                    Action.garbage_domain_remapped,
                    [],
                    [{"path": str(target_path), "change": "mirror row backfilled"}],
                    errors=errors,
                    effective_mode=effective_mode,
                )
    except TimeoutError as exc:
        errors.append(
            f"Phase 3.7: domain lock timeout for target {target_domain}: {exc}"
        )


def journal_and_backfill_garbage_domain(
    journal: Any,
    stem: str,
    target_domain: str,
    source_path: Path,
    target_path: Path,
    outgoing_cells: List[str],
    fixed: List[str],
    errors: List[str],
    *,
    append_journal_fn: Any,
    dry_run: bool = False,
    journal_disabled: bool = False,
    effective_mode: str = "enabled",
    would_be_writes: Optional[List] = None,
) -> None:
    """Journal source rewrite, backfill mirror row in target, journal backfill.

    dry_run=True: skips file writes; records would-be writes; does NOT suppress journaling.
    journal_disabled=True: suppresses ALL journaling (invocation-level dry_run, Story #919).
    Per-type dry_run: dry_run=True, journal_disabled=False => no file writes, journal IS written.
    journal=None is valid only when journal_disabled=True.
    effective_mode: must be 'enabled' or 'dry_run'; written to journal entries.
    """
    if effective_mode not in ("enabled", "dry_run"):
        raise ValueError(
            f"effective_mode must be 'enabled' or 'dry_run', got {effective_mode!r}"
        )
    from code_indexer.server.services.dep_map_repair_phase37 import Action

    _validate_non_journal_args(
        stem, target_domain, source_path, target_path, outgoing_cells, fixed, errors
    )
    if not journal_disabled:
        _validate_journal_backfill_args(
            journal,
            stem,
            target_domain,
            source_path,
            target_path,
            outgoing_cells,
            fixed,
            errors,
            append_journal_fn,
        )
        append_journal_fn(
            journal,
            stem,
            target_domain,
            Action.garbage_domain_remapped,
            [],
            [{"path": str(source_path), "change": "outgoing cell rewritten"}],
            errors=errors,
            effective_mode=effective_mode,
        )

    rescue_msg = (
        f"Phase 3.7: rescued garbage-domain cell in {stem}.md -> {target_domain}"
    )
    if dry_run:
        if would_be_writes is not None:
            would_be_writes.append((str(source_path), "remapped_outgoing_row"))
        fixed.append(rescue_msg)
        return

    _write_target_backfill(
        target_path,
        stem,
        target_domain,
        outgoing_cells,
        fixed,
        errors,
        journal,
        append_journal_fn,
        dry_run=False,
        journal_disabled=journal_disabled,
        effective_mode=effective_mode,
        would_be_writes=would_be_writes,
    )
    fixed.append(rescue_msg)


def build_inverted_repo_index(domain_list: List[Dict[str, Any]]) -> Dict[str, Set[str]]:
    """Build a mapping from repo alias to the set of domain names that contain it.

    Entries missing "name" or "participating_repos" are logged and skipped.
    """
    repo_to_domains: Dict[str, Set[str]] = {}
    for domain in domain_list:
        try:
            domain_name = domain["name"]
            repos = domain["participating_repos"]
        except KeyError as exc:
            logger.warning(
                "build_inverted_repo_index: missing key %s in domain entry", exc
            )
            continue
        if not isinstance(repos, list):
            logger.warning(
                "build_inverted_repo_index: participating_repos is not a list for %r",
                domain_name,
            )
            continue
        for repo in repos:
            if repo not in repo_to_domains:
                repo_to_domains[repo] = set()
            repo_to_domains[repo].add(domain_name)
    return repo_to_domains


def prepare_outgoing_rewrite(
    content: str,
    prose_fragment: str,
    target_domain: str,
    errors: List[str],
    filename: str,
) -> Tuple[Optional[str], List[str]]:
    """Scan the outgoing table and return (new_content, original_cells) without writing.

    Returns (None, []) and appends to errors on any failure.
    The caller writes new_content to disk only after confirming the target file exists.
    """
    lines = content.split("\n")
    in_outgoing = False
    result_lines: List[str] = []
    found_and_replaced = False
    outgoing_cells: List[str] = []

    for line in lines:
        stripped = line.strip()

        if stripped == _OUTGOING_SECTION:
            in_outgoing = True
            result_lines.append(line)
            continue

        if in_outgoing and stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl <= _SECTION_LEVEL:
                in_outgoing = False

        if in_outgoing and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if (
                len(cells) >= 3
                and cells[0] != _OUTGOING_HEADER_SENTINEL
                and not (set(cells[0]) <= frozenset("-"))
                and not found_and_replaced
                and cells[2] == prose_fragment
            ):
                parts = stripped.split("|")
                # parts[0] is "" (before leading |); column index 2 is parts[3]
                parts[3] = f" {target_domain} "
                line = "|".join(parts)
                outgoing_cells = cells
                found_and_replaced = True

        result_lines.append(line)

    if not found_and_replaced:
        errors.append(
            f"Phase 3.7: prose-fragment row not found in {filename}: {prose_fragment!r}"
        )
        return None, []

    return "\n".join(result_lines), outgoing_cells


def _scan_incoming_section(lines: List[str]) -> Iterator[Tuple[int, List[str]]]:
    """Yield (line_index, cells) for every data row in the incoming dependencies section.

    Skips header rows (sentinel "External Repo") and separator rows (cells[0] all dashes).
    Stops when a same-or-higher-level heading is encountered after the section start.
    """
    in_incoming = False
    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == _INCOMING_SECTION:
            in_incoming = True
            continue
        if in_incoming and stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl <= _SECTION_LEVEL:
                return
        if in_incoming and stripped.startswith("|") and stripped.endswith("|"):
            cells = [c.strip() for c in stripped.split("|")[1:-1]]
            if (
                len(cells) >= 4
                and cells[0] != _INCOMING_HEADER_SENTINEL
                and not (set(cells[0]) <= frozenset("-"))
            ):
                yield i, cells


def find_existing_incoming_row(content: str, source_domain: str, dep_type: str) -> bool:
    """Return True if the incoming table already has a row for (source_domain, dep_type)."""
    for _idx, cells in _scan_incoming_section(content.split("\n")):
        if cells[2] == source_domain and cells[3] == dep_type:
            return True
    return False


def insert_incoming_row(
    content: str,
    source_domain: str,
    outgoing_cells: List[str],
) -> str:
    """Insert a mirror row into the incoming section using outgoing_cells for field values.

    Uses named column constants (_COL_*) — no magic indices.
    Raises ValueError when outgoing_cells has fewer than 6 entries (all columns required).
    Raises ValueError if the incoming section heading is absent.
    Inserts after the last pipe-delimited line in the section (including header/separator),
    so it works correctly whether the section is empty or has existing data rows.
    """
    if not isinstance(content, str):
        raise ValueError("content must be a str")
    if not isinstance(source_domain, str) or not source_domain:
        raise ValueError("source_domain must be a non-empty str")
    if not isinstance(outgoing_cells, list) or len(outgoing_cells) < 6:
        raise ValueError(
            f"outgoing_cells must have at least 6 entries, "
            f"got {len(outgoing_cells) if isinstance(outgoing_cells, list) else type(outgoing_cells)}"
        )

    ext_repo = outgoing_cells[_COL_EXT_REPO]
    depends_on = outgoing_cells[_COL_DEPENDS_ON]
    dep_type = outgoing_cells[_COL_DEP_TYPE]
    why = outgoing_cells[_COL_WHY]
    evidence = outgoing_cells[_COL_EVIDENCE]

    new_row = f"| {ext_repo} | {depends_on} | {source_domain} | {dep_type} | {why} | {evidence} |"

    lines = content.split("\n")
    in_incoming = False
    section_start_idx: Optional[int] = None
    last_pipe_idx: Optional[int] = (
        None  # last |…| line in the section (header, sep, or data)
    )

    for i, line in enumerate(lines):
        stripped = line.strip()
        if stripped == _INCOMING_SECTION:
            in_incoming = True
            section_start_idx = i
            continue
        if in_incoming and stripped.startswith("#"):
            lvl = len(stripped) - len(stripped.lstrip("#"))
            if lvl <= _SECTION_LEVEL:
                break
        if in_incoming and stripped.startswith("|") and stripped.endswith("|"):
            last_pipe_idx = i

    if section_start_idx is None:
        raise ValueError(
            f"insert_incoming_row: incoming section '{_INCOMING_SECTION}' not found in content"
        )

    # Insert after the last pipe line (header, separator, or last data row).
    # When the section has only header+separator, last_pipe_idx points to the separator.
    insert_at = (last_pipe_idx if last_pipe_idx is not None else section_start_idx) + 1
    result = lines[:insert_at] + [new_row] + lines[insert_at:]
    return "\n".join(result)
