"""
MALFORMED_YAML repair helpers for Phase 3.7 (Story #910).

Extracted from dep_map_repair_executor.py per MESSI Rule 6 when executor
exceeded 1100 lines. Pure functions with dependency injection — no class state.

Public exports:
  resolve_malformed_yaml_target         -- path safety + domain_info lookup
  rewrite_malformed_yaml_file           -- bytes-level frontmatter re-emit (returns bool)
  apply_malformed_yaml_fallback         -- Phase 1 fallback when bounds unrecoverable
  repair_single_malformed_yaml_anomaly  -- handle one AnomalyEntry|AnomalyAggregate
  run_malformed_yaml_repairs            -- Phase 3.7 MALFORMED_YAML orchestrator
"""

from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple, Union

from code_indexer.server.services.dep_map_repair_phase37 import (
    acquire_domain_lock,
    body_byte_offset,
    build_and_append_malformed_yaml_journal_entry,
    reemit_frontmatter_from_domain_info,
)

if TYPE_CHECKING:
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyAggregate,
        AnomalyEntry,
    )


def repair_single_malformed_yaml_anomaly(
    output_dir: Path,
    anomaly: "Union[AnomalyEntry, AnomalyAggregate]",
    domain_list: List[Dict[str, Any]],
    fixed: List[str],
    errors: List[str],
    *,
    domain_analyzer: Optional[Callable],
    log_fn: Callable[[str], None],
    locate_frontmatter_bounds_fn: Callable[[str], Optional[Tuple[int, int]]],
    is_safe_domain_name_fn: Callable[[str], bool],
) -> None:
    """Handle one MALFORMED_YAML anomaly (AnomalyEntry or AnomalyAggregate).

    Expands AnomalyAggregate examples and dispatches each to the three-step
    pipeline: resolve_malformed_yaml_target -> rewrite_malformed_yaml_file ->
    apply_malformed_yaml_fallback. Dependency-injected; no class state.

    domain_list must be loaded once by the caller and passed in to preserve
    the original load-once-per-repair-run semantics.
    """
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyAggregate

    examples = (
        [anomaly] if not isinstance(anomaly, AnomalyAggregate) else anomaly.examples
    )
    for ex in examples:
        tgt = resolve_malformed_yaml_target(
            output_dir,
            ex.file,
            domain_list,
            errors,
            is_safe_domain_name_fn=is_safe_domain_name_fn,
        )
        if tgt is None:
            continue
        file_path, stem, domain_info = tgt
        bounds_found = rewrite_malformed_yaml_file(
            file_path,
            stem,
            ex.file,
            domain_info,
            fixed,
            errors,
            log_fn=log_fn,
            locate_frontmatter_bounds_fn=locate_frontmatter_bounds_fn,
        )
        if not bounds_found:
            apply_malformed_yaml_fallback(
                output_dir,
                ex.file,
                domain_info,
                domain_list,
                fixed,
                errors,
                domain_analyzer=domain_analyzer,
            )


def run_malformed_yaml_repairs(
    output_dir: Path,
    fixed: List[str],
    errors: List[str],
    *,
    domain_analyzer: Optional[Callable],
    load_domains_json_fn: Callable[[Path], List[Dict[str, Any]]],
    log_fn: Callable[[str], None],
    locate_frontmatter_bounds_fn: Callable[[str], Optional[Tuple[int, int]]],
    is_safe_domain_name_fn: Callable[[str], bool],
) -> None:
    """Load MALFORMED_YAML anomalies and repair each.

    Called by DepMapRepairExecutor._run_phase37 after the SELF_LOOP pass.
    Dependency-injected to avoid importing executor state.
    """
    from code_indexer.server.services.dep_map_mcp_parser import DepMapMCPParser
    from code_indexer.server.services.dep_map_parser_hygiene import AnomalyType

    parser = DepMapMCPParser(dep_map_path=output_dir.parent)
    _, all_anomalies, _p, _d = parser.get_cross_domain_graph_with_channels()
    malformed = [a for a in all_anomalies if a.type == AnomalyType.MALFORMED_YAML]
    if not malformed:
        return
    domain_list = load_domains_json_fn(output_dir)
    for anomaly in malformed:
        repair_single_malformed_yaml_anomaly(
            output_dir,
            anomaly,
            domain_list,
            fixed,
            errors,
            domain_analyzer=domain_analyzer,
            log_fn=log_fn,
            locate_frontmatter_bounds_fn=locate_frontmatter_bounds_fn,
            is_safe_domain_name_fn=is_safe_domain_name_fn,
        )


def resolve_malformed_yaml_target(
    output_dir: Path,
    raw_file: str,
    domain_list: List[Dict[str, Any]],
    errors: List[str],
    *,
    is_safe_domain_name_fn: Callable[[str], bool],
) -> Optional[Tuple[Path, str, Dict[str, Any]]]:
    """Validate path + find domain_info; return (file_path, stem, domain_info) or None.

    Appends to errors[] and returns None on any validation failure.
    """
    if ".." in raw_file:
        errors.append("Phase 3.7: rejected unsafe path in malformed-yaml anomaly")
        return None
    stem = Path(raw_file).stem
    if not is_safe_domain_name_fn(stem):
        errors.append("Phase 3.7: rejected unsafe path in malformed-yaml anomaly")
        return None
    file_path = output_dir / f"{stem}.md"
    if not file_path.exists():
        errors.append(
            f"Phase 3.7: cannot repair malformed-yaml {raw_file}: file not found"
        )
        return None
    domain_info = next((d for d in domain_list if d.get("name") == stem), None)
    if domain_info is None:
        errors.append(
            f"Phase 3.7: cannot re-emit frontmatter for {raw_file}: not in _domains.json"
        )
        return None
    return file_path, stem, domain_info


def rewrite_malformed_yaml_file(
    file_path: Path,
    stem: str,
    raw_file: str,
    domain_info: Dict[str, Any],
    fixed: List[str],
    errors: List[str],
    *,
    log_fn: Callable[[str], None],
    locate_frontmatter_bounds_fn: Callable[[str], Optional[Tuple[int, int]]],
) -> bool:
    """Read, locate bounds, re-emit frontmatter, write. Returns False if bounds missing.

    Uses bytes-level I/O to preserve mixed line endings in the body (AC5).
    False signals the caller to route to Phase 1 fallback (AC2).
    """
    try:
        raw_bytes = file_path.read_bytes()
    except OSError as e:
        errors.append(f"Phase 3.7: cannot read {raw_file}: {e}")
        return True
    try:
        content = raw_bytes.decode("utf-8")
    except UnicodeDecodeError as e:
        errors.append(f"Phase 3.7: cannot decode {raw_file} as UTF-8: {e}")
        return True
    bounds = locate_frontmatter_bounds_fn(content)
    if bounds is None:
        return False
    _, close_idx = bounds
    new_content = reemit_frontmatter_from_domain_info(content, bounds, domain_info)
    new_bounds = locate_frontmatter_bounds_fn(new_content)
    if new_bounds is None:
        errors.append(
            f"Phase 3.7: re-emitted content has no closing --- for {raw_file}"
        )
        return True
    _, new_close_idx = new_bounds
    new_fm_text = "\n".join(new_content.split("\n")[: new_close_idx + 1])
    body_start = body_byte_offset(raw_bytes, close_idx)
    new_bytes = new_fm_text.encode("utf-8") + b"\n" + raw_bytes[body_start:]
    if new_bytes == raw_bytes:
        log_fn(f"Phase 3.7: malformed-yaml repair no-op for {raw_file}")
        return True
    try:
        with acquire_domain_lock(file_path.stem):
            file_path.write_bytes(new_bytes)
            fixed.append(f"Phase 3.7: re-emitted frontmatter for {stem}")
            log_fn(f"Phase 3.7 frontmatter re-emitted: {raw_file}")
            build_and_append_malformed_yaml_journal_entry(file_path, stem, errors)
    except TimeoutError as exc:
        errors.append(
            f"Phase 3.7 MALFORMED_YAML: domain lock timeout for {file_path.stem}: {exc}"
        )
    except OSError as e:
        errors.append(f"Phase 3.7: cannot write {raw_file}: {e}")
    return True


def apply_malformed_yaml_fallback(
    output_dir: Path,
    raw_file: str,
    domain_info: Dict[str, Any],
    domain_list: List[Dict[str, Any]],
    fixed: List[str],
    errors: List[str],
    *,
    domain_analyzer: Optional[Callable],
) -> None:
    """AC2: Phase 1 fallback when frontmatter bounds are unrecoverable."""
    if domain_analyzer is None:
        errors.append(
            f"Phase 3.7: {raw_file} needs full re-analysis but no domain_analyzer wired"
        )
        return
    try:
        result = domain_analyzer(output_dir, domain_info, domain_list, [])
        if result:
            fixed.append(f"Phase 3.7: re-analyzed {raw_file}")
    except Exception as e:  # noqa: BLE001
        errors.append(f"Phase 3.7: full re-analysis failed for {raw_file}: {e}")
