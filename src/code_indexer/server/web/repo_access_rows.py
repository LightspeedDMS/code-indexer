"""Rows of the Groups > Repository Access matrix."""

from typing import Any, Dict, List

from code_indexer.utils.credential_redaction import with_masked_repo_url


def repo_access_rows(repos: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """One row per golden repository, by alias, with its clone URL masked.

    *repos* are raw ``list_golden_repos()`` rows, whose ``repo_url`` is NOT
    masked; the clone URL is shown as a hover, so its userinfo is redacted.
    """
    rows: List[Dict[str, Any]] = []
    for repo in sorted(repos, key=lambda r: str(r["alias"]).lower()):
        masked = with_masked_repo_url(repo)
        rows.append({"name": repo["alias"], "repo_url": masked.get("repo_url") or ""})
    return rows
