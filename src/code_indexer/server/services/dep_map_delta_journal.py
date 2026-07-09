"""
Per-domain frontmatter journal helpers for Story #1053.

Resumable Delta Dep-Map Analysis: each domain .md file carries a YAML
frontmatter block that records the fingerprint of the last successfully
applied delta.  On resume, the runner reads this block and skips domains
whose fingerprint matches the current delta — so a SIGKILL or auto-updater
restart loses at most the single in-flight Claude call.

Public API
----------
compute_delta_fingerprint(changed, new, removed) -> str
    sha256 of sorted JSON of {changed, new, removed} alias lists.

parse_frontmatter(md_text, domain_hint=None) -> (dict, str)
    Split a domain .md file into (YAML dict, body text).
    Malformed YAML falls back to ({}, full_text) and emits WARNING.

render_md(frontmatter, body) -> str
    Reconstruct a domain .md from frontmatter dict and body.

write_atomic(path, content)
    Write content to a temp file in the same directory, then os.replace.
    If os.replace raises, the original file is left byte-equal to its
    pre-write state (no partial write at the target path).

all_new_repos_have_domain_assignments(new_repos, domains_json_path) -> bool
    True iff every alias in new_repos appears as a member of some entry in
    _domains.json.  Returns False on missing file, JSONDecodeError, or wrong
    JSON shape (not list or dict).
"""

import hashlib
import json
import logging
import os
import re
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import yaml

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# YAML timestamp-safe helpers (Bug #1114 Defect 2)
#
# yaml.safe_dump wraps ISO-8601 strings in single quotes to prevent
# re-interpretation as timestamps.  This post-processing strips those quotes
# so downstream line-based readers receive the canonical ISO format with 'T'.
# ---------------------------------------------------------------------------

_ISO_TS_SINGLE_QUOTED_RE = re.compile(r"'(\d{4}-\d{2}-\d{2}T[^']+)'")


def _safe_dump_iso_aware(data: Dict[str, Any], **kwargs: Any) -> str:
    """yaml.safe_dump wrapper that un-quotes single-quoted ISO timestamps."""
    raw = yaml.safe_dump(data, **kwargs)
    return _ISO_TS_SINGLE_QUOTED_RE.sub(r"\1", raw)


def compute_delta_fingerprint(
    changed: List[Dict[str, Any]],
    new: List[Dict[str, Any]],
    removed: List[str],
) -> str:
    """
    Compute a deterministic fingerprint for a delta set.

    The fingerprint is the SHA-256 hex digest of the canonical JSON
    representation of {changed, new, removed} alias lists — sorted so
    that input ordering does not affect the result.

    Args:
        changed: List of repo dicts with at least an "alias" key.
        new:     List of new-repo dicts with at least an "alias" key.
        removed: List of removed repo alias strings.

    Returns:
        64-character lowercase hex string (SHA-256 digest).
    """
    payload = {
        "changed": sorted(r["alias"] for r in changed),
        "new": sorted(r["alias"] for r in new),
        "removed": sorted(removed),
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


def parse_frontmatter(
    md_text: str,
    domain_hint: Optional[str] = None,
) -> Tuple[Dict[str, Any], str]:
    """
    Split a domain .md file into (YAML frontmatter dict, body text).

    The frontmatter block is the region between the first two ``---`` lines.
    If the text does not start with ``---\\n``, or has no closing ``---\\n``,
    or the YAML is malformed, returns ``({}, md_text)`` (full text as body)
    and emits a WARNING log when YAML parsing failed.

    Args:
        md_text:     Raw content of a domain .md file.
        domain_hint: Optional domain name used in WARNING messages.

    Returns:
        Tuple of (frontmatter_dict, body_text).
    """
    if not md_text.startswith("---\n"):
        return {}, md_text

    # Find the closing delimiter
    rest = md_text[4:]  # skip opening "---\n"
    close_idx = rest.find("\n---\n")
    if close_idx == -1:
        return {}, md_text

    yaml_block = rest[:close_idx]
    # Skip "\n---\n" (5 chars). render_md adds one extra "\n" blank separator
    # between the closing "---" line and the body, so skip it too if present.
    after_delimiter = rest[close_idx + 5 :]  # skip "\n---\n"
    body = after_delimiter[1:] if after_delimiter.startswith("\n") else after_delimiter

    try:
        parsed = yaml.safe_load(yaml_block)
    except yaml.YAMLError as exc:
        hint = f" for domain '{domain_hint}'" if domain_hint else ""
        logger.warning(
            "parse_frontmatter: malformed YAML%s — treating as no frontmatter. Error: %s",
            hint,
            exc,
        )
        return {}, md_text

    if not isinstance(parsed, dict):
        # Rare: YAML parsed but not a mapping (e.g. a bare scalar)
        hint = f" for domain '{domain_hint}'" if domain_hint else ""
        logger.warning(
            "parse_frontmatter: YAML block%s is not a mapping (got %s) "
            "— treating as no frontmatter",
            hint,
            type(parsed).__name__,
        )
        return {}, md_text

    return parsed, body


def render_md(frontmatter: Dict[str, Any], body: str) -> str:
    """
    Reconstruct a domain .md file from a frontmatter dict and body text.

    The resulting text has exactly one YAML block, bracketed by ``---`` lines.
    ``parse_frontmatter(render_md(fm, body))`` round-trips cleanly.

    Args:
        frontmatter: Dict to serialise as YAML.
        body:        Body text (everything after the closing ``---``).

    Returns:
        Complete .md content string.
    """
    yaml_text = _safe_dump_iso_aware(
        frontmatter, sort_keys=False, default_flow_style=False
    )
    return f"---\n{yaml_text}---\n\n{body}"


def write_atomic(path: Path, content: str) -> None:
    """
    Write *content* to *path* atomically using a temp file + os.replace.

    A NamedTemporaryFile is created in the same directory as *path* so that
    the ``os.replace`` call is guaranteed to be on the same filesystem
    (POSIX atomic rename).  The temp file is fsync'd before renaming.

    If ``os.replace`` raises, the temp file is cleaned up and the original
    *path* is left byte-equal to its pre-write state.

    Args:
        path:    Target file path.
        content: UTF-8 string content to write.

    Raises:
        Any exception raised by ``os.replace`` (after temp-file cleanup).
    """
    tmp_fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), suffix=".tmp")
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as fh:
            fh.write(content)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_name, str(path))
    except Exception:
        # Clean up the temp file so no orphan is left; suppress secondary errors.
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise


def all_new_repos_have_domain_assignments(
    new_repos: List[Dict[str, Any]],
    domains_json_path: Path,
) -> bool:
    """
    Check whether all new-repo aliases already appear in _domains.json.

    This is the Phase C idempotency guard: if every alias in *new_repos*
    is already a member of some entry in *domains_json_path*, the monolithic
    domain-discovery Claude CLI call can be safely skipped on resume.

    Handles the following gracefully (returns False without raising):
    - *domains_json_path* does not exist
    - File contains invalid JSON (JSONDecodeError)
    - File contains valid JSON but not a list or dict (wrong shape)

    Args:
        new_repos:         List of repo dicts with at least an "alias" key.
        domains_json_path: Path to _domains.json in the dependency-map dir.

    Returns:
        True iff every alias in new_repos appears as a participating_repos
        member in some entry.  True vacuously when new_repos is empty.
    """
    if not domains_json_path.exists():
        return False

    try:
        raw = json.loads(domains_json_path.read_text())
    except json.JSONDecodeError as exc:
        logger.debug(
            "all_new_repos_have_domain_assignments: JSONDecodeError on %s: %s",
            domains_json_path,
            exc,
        )
        return False

    # Collect assigned aliases from the parsed structure
    assigned: set = set()

    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, dict):
                for member in entry.get("participating_repos", []):
                    assigned.add(member)
    elif isinstance(raw, dict):
        for entry in raw.values():
            if isinstance(entry, dict):
                for member in entry.get("participating_repos", []):
                    assigned.add(member)
    else:
        # Wrong shape (null, integer, string, …) — treat as incomplete
        return False

    new_aliases = [r["alias"] for r in new_repos]
    return all(alias in assigned for alias in new_aliases)
