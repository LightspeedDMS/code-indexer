"""
Backfill cluster for BIDIRECTIONAL_MISMATCH repairs (Story #912).

Pure domain-safety and table-navigation helpers plus the full RMW backfill
write path for cross-domain dependency edges.

Module-level definitions (exhaustive list):
  logger                      -- standard Python logger
  _is_safe_domain_name        -- no path-traversal chars in domain name; safe for None
  _find_incoming_insert_index -- line index after Incoming Dependencies separator row
  _get_domain_repos           -- participating_repos list for a domain name
  backfill_target_mirror_row  -- full RMW on target .md under domain lock
"""

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from code_indexer.server.services.dep_map_repair_garbage_domain import (
    find_existing_incoming_row,
)
from code_indexer.server.services.dep_map_repair_phase37 import (
    acquire_domain_lock,
    atomic_write_text,
)

logger = logging.getLogger(__name__)


def _is_safe_domain_name(name: object) -> bool:
    """Return True when name is a non-empty str with no path-traversal characters.

    Returns False for any non-str input (including None) without raising.
    """
    if not isinstance(name, str):
        return False
    return bool(name) and "/" not in name and "\\" not in name and ".." not in name


def _find_incoming_insert_index(lines: List[str]) -> Optional[int]:
    """Find the line index immediately after the Incoming Dependencies separator row.

    Scans for the heading, then the header row with 'External Repo', then the
    separator row (cells of dashes). Returns that index or None if not found.
    """
    in_incoming = False
    saw_header = False
    for idx, line in enumerate(lines):
        stripped = line.strip()
        if stripped == "### Incoming Dependencies":
            in_incoming = True
            continue
        if not in_incoming:
            continue
        if stripped.startswith("#"):
            return None
        if not saw_header and stripped.startswith("|") and "External Repo" in stripped:
            saw_header = True
            continue
        if saw_header and stripped.startswith("|"):
            inner = stripped.replace("|", "").replace(" ", "")
            if inner and set(inner) <= frozenset("-"):
                return idx + 1
    return None


def _get_domain_repos(
    domain_name: str, domains_json: List[Dict[str, Any]]
) -> List[str]:
    """Return participating_repos list for domain_name, or empty list if not found."""
    for d in domains_json:
        if d.get("name") == domain_name:
            repos = d.get("participating_repos") or []
            return list(repos) if isinstance(repos, list) else []
    return []


def backfill_target_mirror_row(
    output_dir: Path,
    target_domain: str,
    source_domain: str,
    source_repos: List[str],
    dep_type: str,
    why: str,
    evidence: str,
    errors: List[str],
    dry_run: bool = False,
    would_be_writes: Optional[List] = None,
) -> bool:
    """Append one incoming row to target_domain.md under the domain lock.

    Acquires the lock BEFORE reading so the full read-modify-write is synchronized.
    Returns True on success, False on any error (error appended to errors).
    Raises ValueError for None required inputs (programming error).
    dry_run=True: skips atomic_write_text; appends to would_be_writes instead.
    """
    if output_dir is None:
        raise ValueError("backfill_target_mirror_row: output_dir must not be None")
    if source_domain is None:
        raise ValueError("backfill_target_mirror_row: source_domain must not be None")
    if errors is None:
        raise ValueError("backfill_target_mirror_row: errors must not be None")
    if not _is_safe_domain_name(target_domain):
        errors.append(
            f"backfill_target_mirror_row: unsafe target domain {target_domain!r}"
        )
        return False
    if source_repos is None:
        logger.warning(
            "backfill_target_mirror_row: source_repos is None for %s->%s; defaulting to []",
            source_domain,
            target_domain,
        )
        safe_repos: List[str] = []
    else:
        safe_repos = source_repos
    md_path = output_dir / f"{target_domain}.md"
    repos_str = ", ".join(safe_repos) if safe_repos else source_domain
    new_row = f"| {repos_str} | {repos_str} | {source_domain} | {dep_type or 'Code-level'} | {why or ''} | {evidence or ''} |\n"
    try:
        with acquire_domain_lock(target_domain):
            try:
                original = md_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.warning(
                    "backfill_target_mirror_row: cannot read %s.md: %s",
                    target_domain,
                    exc,
                )
                errors.append(
                    f"backfill_target_mirror_row: cannot read {target_domain}.md: {exc}"
                )
                return False
            if find_existing_incoming_row(
                original, source_domain, dep_type or "Code-level"
            ):
                return True
            lines = original.splitlines(keepends=True)
            insert_idx = _find_incoming_insert_index(lines)
            if insert_idx is None:
                errors.append(
                    f"backfill_target_mirror_row: no Incoming Dependencies separator in {target_domain}.md"
                )
                return False
            lines.insert(insert_idx, new_row)
            if dry_run:
                if would_be_writes is not None:
                    would_be_writes.append(
                        (str(md_path), "bidirectional_mirror_backfilled")
                    )
                return True
            return bool(atomic_write_text(md_path, "".join(lines), errors))
    except TimeoutError as exc:
        logger.warning(
            "backfill_target_mirror_row: lock timeout for %s: %s", target_domain, exc
        )
        errors.append(
            f"backfill_target_mirror_row: lock timeout for {target_domain}: {exc}"
        )
        return False
