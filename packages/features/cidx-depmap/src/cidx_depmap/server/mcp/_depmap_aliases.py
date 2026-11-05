"""
Depmap dual-write alias helper — Story #888 AC5.

Centralized handler-layer helper that writes both canonical and deprecated-alias
keys during the one-release compatibility window. All 5 depmap handlers must use
these helpers so the alias logic cannot drift across handlers.

Canonical → deprecated alias mapping (compat window, removed in vN+1):
  domain       → domain_name   (single-domain contexts)
  repo         → consuming_repo (consumer entries in find_consumers)

RESOLUTION_STATES: the complete set of valid resolution literal strings shared
by all depmap_* handlers.
"""

from typing import Dict, Literal

ResolutionLiteral = Literal[
    "ok",
    "invalid_input",
    "repo_not_indexed",
    "domain_not_indexed",
    "repo_has_no_consumers",
]

RESOLUTION_STATES: frozenset = frozenset(
    {
        "ok",
        "invalid_input",
        "repo_not_indexed",
        "domain_not_indexed",
        "repo_has_no_consumers",
    }
)


def apply_consumer_aliases(entry: Dict[str, str]) -> Dict[str, str]:
    """Add canonical 'repo' + deprecated 'consuming_repo' alias, and
    canonical 'domain' + deprecated 'domain_name' alias on consumer entries.

    Takes a consumer entry dict (as produced by DepMapMCPParser.find_consumers)
    and returns a new dict with both the canonical 'repo' key and the deprecated
    'consuming_repo' alias set to the same value, and likewise for domain/domain_name.

    Explicit presence checks are used — no silent empty-string fallbacks (Rule 13).
    The deprecated 'consuming_repo' and 'domain_name' keys are preserved for
    one-release compat.
    """
    result = {**entry}

    # repo <-> consuming_repo dual-write
    if "repo" in entry:
        result["consuming_repo"] = entry["repo"]
    elif "consuming_repo" in entry:
        result["repo"] = entry["consuming_repo"]

    # domain <-> domain_name dual-write
    if "domain" in entry:
        result["domain_name"] = entry["domain"]
    elif "domain_name" in entry:
        result["domain"] = entry["domain_name"]

    return result


def apply_domain_membership_aliases(entry: Dict[str, str]) -> Dict[str, str]:
    """Add canonical 'domain' key alongside deprecated 'domain_name' alias.

    Takes a domain membership entry dict (as produced by
    DepMapMCPParser.get_repo_domains) and returns a new dict with both the
    canonical 'domain' key and the deprecated 'domain_name' alias set to the
    same value.

    Explicit presence checks are used — no silent empty-string fallbacks (Rule 13).
    The deprecated 'domain_name' key is preserved for one-release compat.
    """
    result = {**entry}

    if "domain" in entry:
        result["domain_name"] = entry["domain"]
    elif "domain_name" in entry:
        result["domain"] = entry["domain_name"]

    return result


def assert_resolution_valid(resolution: str) -> None:
    """Invariant (MESSI rule 15): resolution must be one of RESOLUTION_STATES.
    Stripped under `python -O`.
    """
    assert resolution in RESOLUTION_STATES, (
        f"Invalid resolution: {resolution!r}; must be one of {sorted(RESOLUTION_STATES)}"
    )


def assert_success_resolution_consistent(success: bool, resolution: str) -> None:
    """Invariant (MESSI rule 15): success=False implies resolution!='ok';
    success=True implies resolution=='ok'. Stripped under `python -O`.
    """
    if not success:
        assert resolution != "ok", "Invariant: success=False with resolution='ok'"
    else:
        assert resolution == "ok", (
            f"Invariant: success=True with resolution={resolution!r}"
        )
