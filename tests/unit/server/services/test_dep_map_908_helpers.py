"""
Shared fixture helpers for Story #908 Phase 3.7 tests.

All AC test files import from here. No test classes — only helpers.
"""

import json
import re
from pathlib import Path
from typing import List

# Safe domain name: lowercase letters, digits, hyphens only.
_SAFE_DOMAIN_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")

_DOMAIN_MD_TEMPLATE = """\
---
name: {name}
participating_repos:
  - repo-a
---

## Overview

Domain {name}.

## Repository Roles

| Repository | Description | Role |
|---|---|---|
| repo-a | Test repo | primary |

### Outgoing Dependencies

| This Repo | Dependency Type | Target Domain | Why | Evidence |
|---|---|---|---|---|
{outgoing_rows}
### Incoming Dependencies

| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |
|---|---|---|---|---|---|
"""


def _validate_domain_name(name: str) -> None:
    """Raise ValueError when name is unsafe for filesystem use in tests."""
    if not name:
        raise ValueError("domain name must not be empty")
    if not _SAFE_DOMAIN_RE.match(name):
        raise ValueError(
            f"domain name contains unsafe characters: {name!r}. "
            "Only lowercase letters, digits, and leading/internal hyphens are allowed."
        )


def _build_domain_md(domain_name: str, outgoing_targets: List[str]) -> str:
    """Return domain markdown using the shared template.

    All domain names (source and targets) are validated before interpolation.
    """
    _validate_domain_name(domain_name)
    rows_text = ""
    for target in outgoing_targets:
        _validate_domain_name(target)
        rows_text += f"| repo-a | code | {target} | test dep | evidence |\n"
    return _DOMAIN_MD_TEMPLATE.format(name=domain_name, outgoing_rows=rows_text)


def make_minimal_dep_map(output_dir: Path, domain_name: str = "domain-a") -> None:
    """Write a minimal valid dep-map directory with zero graph anomalies."""
    if output_dir is None:
        raise ValueError("output_dir must not be None")
    _validate_domain_name(domain_name)
    output_dir.mkdir(parents=True, exist_ok=True)
    (output_dir / f"{domain_name}.md").write_text(
        _build_domain_md(domain_name, []), encoding="utf-8"
    )
    domains = [{"name": domain_name, "participating_repos": ["repo-a"]}]
    (output_dir / "_domains.json").write_text(json.dumps(domains), encoding="utf-8")
    (output_dir / "_index.md").write_text(
        f"# Index\n\n- [{domain_name}]({domain_name}.md)\n", encoding="utf-8"
    )


def make_domain_with_self_loop(
    output_dir: Path,
    domain_name: str,
    outgoing_rows: List[str],
) -> None:
    """Write a domain .md whose Outgoing Dependencies table has the given target rows.

    All domain names (source and targets) are validated before filesystem use.
    """
    if output_dir is None:
        raise ValueError("output_dir must not be None")
    if outgoing_rows is None:
        raise ValueError("outgoing_rows must not be None")
    md = _build_domain_md(domain_name, outgoing_rows)
    (output_dir / f"{domain_name}.md").write_text(md, encoding="utf-8")
