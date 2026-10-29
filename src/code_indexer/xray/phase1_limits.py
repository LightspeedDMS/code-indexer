from __future__ import annotations

import logging
import math
import subprocess
import time
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Tuple

if TYPE_CHECKING:
    from code_indexer.global_repos.regex_search import RegexSearchResult

logger = logging.getLogger(__name__)

# ^ hazard binding it as a default argument => tests cannot lower it; test:test_match_cap_cut_marks_result_partial
PHASE1_MAX_MATCHES = 100_000

# ^ why one read_limit reason, not per cause; RegexSearchResult.read_capped merges output ceiling, reader cap, content budget
READ_LIMIT = "read_limit"
MATCH_CAP = "match_cap"

_ZERO_MATCH_HINT = (
    "Pattern matched 0 files. Use `**/time.py` (with **) for "
    "recursive matching across directories."
)

HasFiles = Callable[[Path, str, float], Optional[bool]]


def phase1_cut(
    result: "RegexSearchResult",
) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    # ^ order read_capped before truncated; a read-limited scan cannot know whether the cap was passed
    if result.read_capped:
        return READ_LIMIT, {
            "type": "content_search_read_capped",
            "hint": (
                "Content search hit an internal read limit before scanning "
                "could complete; results may be a subset of all matches. "
                "Narrow the pattern or include_patterns to reduce match volume."
            ),
        }
    if result.truncated:
        return MATCH_CAP, {
            "type": "phase1_match_cap_reached",
            "max_matches": PHASE1_MAX_MATCHES,
            "hint": (
                f"The driver regex matched more than {PHASE1_MAX_MATCHES} "
                "lines; only the files holding the first matches were "
                "evaluated. Narrow the driver regex or include_patterns."
            ),
        }
    return None, None


def rg_include_pattern_has_files(
    repo_path: Path, pattern: str, timeout_seconds: float
) -> Optional[bool]:
    from code_indexer.services.path_pattern_matcher import normalize_glob_pattern

    budget = max(1, math.ceil(timeout_seconds))
    # ^ external rg --files -q exits 0 at the first listed file, 1 when none; doc:ripgrep#quiet
    cmd = ["rg", "--files", "-q"]
    for normalized in normalize_glob_pattern(pattern.strip()):
        cmd.extend(["-g", normalized])
    cmd.extend(["-g", "!.code-indexer/**", "-g", "!.git/**", "--", str(repo_path)])
    try:
        completed = subprocess.run(
            cmd, cwd=str(repo_path), capture_output=True, timeout=budget, check=False
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"zero-match probe for {pattern!r} exceeded {budget}s"
        ) from exc
    if completed.returncode in (0, 1):
        return completed.returncode == 0
    logger.warning(
        "XRaySearchEngine: zero-match probe for %r exited %d: %s",
        pattern,
        completed.returncode,
        completed.stderr.decode("utf-8", errors="replace")[:500],
    )
    return None


def probe_zero_match_patterns(
    repo_path: Path,
    include_patterns: List[str],
    timeout_seconds: float,
    has_files: HasFiles,
) -> List[Dict[str, Any]]:
    warnings: List[Dict[str, Any]] = []
    start = time.monotonic()
    # ^ invariant one budget shared by every pattern, never per pattern; #1598
    for idx, pat in enumerate(include_patterns):
        remaining = timeout_seconds - (time.monotonic() - start)
        if remaining <= 0:
            raise TimeoutError(
                f"zero-match-pattern probe shared budget of {timeout_seconds}s "
                f"exhausted after probing {idx} of {len(include_patterns)} "
                "include_pattern(s)"
            )
        found = has_files(repo_path, pat, remaining)
        if found is False:
            warnings.append(
                {
                    "type": "zero_match_include_pattern",
                    "pattern": pat,
                    "hint": _ZERO_MATCH_HINT,
                }
            )
        elif found is None:
            warnings.append(
                {
                    "type": "zero_match_probe_failed",
                    "pattern": pat,
                    "hint": (
                        "The zero-match check for this include_pattern could "
                        "not complete; a zero-match warning may be missing. "
                        "The search results are unaffected."
                    ),
                }
            )
    return warnings
