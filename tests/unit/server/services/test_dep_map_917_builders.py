"""
Story #917 — Builders and helpers for production-path lock tests.

Three exported helpers:
  write_domain_fixtures  -- write all needed .md files for a given test scenario
  make_anomaly           -- create an AnomalyEntry for any anomaly type
  make_lock_observer     -- return a pass-through wrapper that records lock state at call time
"""

from pathlib import Path
from typing import Callable, List, Tuple


def write_domain_fixtures(
    output_dir: Path,
    scenario: str,
    *,
    domain_name: str,
    prose: str = "",
) -> None:
    """Write .md files for a given repair scenario into output_dir.

    scenario values:
      "self_loop"      -- domain.md with one self-loop outgoing row
      "malformed_yaml" -- domain.md with malformed YAML frontmatter
      "garbage_source" -- source domain.md with a prose-fragment outgoing cell
      "garbage_target" -- minimal target domain.md for mirror backfill
    """
    if scenario == "self_loop":
        content = (
            f"---\nname: {domain_name}\nparticipating_repos:\n  - repo-a\n---\n\n"
            f"### Outgoing Dependencies\n\n"
            f"| This Repo | Dependency Type | Target Domain | Why | Evidence |\n"
            f"|---|---|---|---|---|\n"
            f"| repo-a | code | {domain_name} | self dep | evidence |\n"
            f"| repo-a | code | domain-b | other dep | evidence |\n"
        )
        (output_dir / f"{domain_name}.md").write_text(content, encoding="utf-8")
    elif scenario == "malformed_yaml":
        content = (
            "---\n"
            "name: wrong-name\n"
            "last_analyzed 2024-01-15T10:00:00\n"
            "participating_repos:\n  - repo-old\n"
            "---\n\n## Overview\n\nBody.\n"
        )
        (output_dir / f"{domain_name}.md").write_bytes(content.encode("utf-8"))
    elif scenario == "garbage_source":
        content = (
            f"---\nname: {domain_name}\nparticipating_repos:\n  - service-a\n---\n\n"
            f"### Outgoing Dependencies\n\n"
            f"| This Repo | Dependency Type | Target Domain | Why | Evidence | Notes |\n"
            f"|---|---|---|---|---|---|\n"
            f"| service-a | Service integration | {prose} | because | yes | none |\n"
            f"\n### Incoming Dependencies\n\n"
            f"| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |\n"
            f"|---|---|---|---|---|---|\n"
        )
        (output_dir / f"{domain_name}.md").write_text(content, encoding="utf-8")
    elif scenario == "garbage_target":
        content = (
            f"---\nname: {domain_name}\nparticipating_repos:\n  - target-repo\n---\n\n"
            f"### Outgoing Dependencies\n\n"
            f"| This Repo | Dependency Type | Target Domain | Why | Evidence | Notes |\n"
            f"|---|---|---|---|---|---|\n"
            f"\n### Incoming Dependencies\n\n"
            f"| External Repo | Depends On | Source Domain | Dep Type | Why | Evidence |\n"
            f"|---|---|---|---|---|---|\n"
        )
        (output_dir / f"{domain_name}.md").write_text(content, encoding="utf-8")
    else:
        raise ValueError(f"Unknown scenario: {scenario!r}")


def make_anomaly(anomaly_type_name: str, domain_name: str, message: str = ""):
    """Create a real AnomalyEntry for the given anomaly_type_name.

    anomaly_type_name must match a member of AnomalyType (e.g. 'SELF_LOOP').
    """
    from code_indexer.server.services.dep_map_parser_hygiene import (
        AnomalyEntry,
        AnomalyType,
    )

    atype = getattr(AnomalyType, anomaly_type_name)
    channel = (
        "data"
        if anomaly_type_name in ("SELF_LOOP", "GARBAGE_DOMAIN_REJECTED")
        else "parser"
    )
    return AnomalyEntry(
        type=atype,
        file=f"{domain_name}.md",
        message=message or f"{anomaly_type_name}: {domain_name}",
        channel=channel,
        count=1,
    )


def make_lock_observer(
    domain_name: str,
    underlying_fn: Callable,
) -> Tuple[Callable, List[bool]]:
    """Return a (wrapper_fn, lock_was_held) pair for deterministic lock-state observation.

    wrapper_fn accepts any args/kwargs and:
      1. Records whether the domain lock for domain_name is currently locked
         into lock_was_held (True = locked at call time, False = not locked).
      2. Delegates the call to underlying_fn with the same args/kwargs unchanged.

    lock_was_held is a shared mutable list — each call appends one bool.
    The test asserts lock_was_held[0] is True after the production function runs.

    This is deterministic: it observes lock state at call time, no timing dependence.
    """
    from code_indexer.server.services.dep_map_repair_phase37 import get_domain_file_lock

    lock = get_domain_file_lock(domain_name)
    lock_was_held: List[bool] = []

    def _wrapper(*args, **kwargs):
        lock_was_held.append(lock.locked())
        return underlying_fn(*args, **kwargs)

    return _wrapper, lock_was_held
