"""Generate ``docs/reference/error-codes/`` from the server error-code registry.

Output: ``README.md`` (format, how the registry is used, subsystem index) and
one page per subsystem. Codes whose registry description is still a
placeholder (``TODO``) are listed compactly instead of as table rows.

Usage (from the repository root)::

    PYTHONPATH=src python3 -m tools.docs.error_codes [--check]
"""

from __future__ import annotations

import textwrap
from pathlib import Path
from typing import TYPE_CHECKING, AbstractSet, Dict, List, Mapping, Optional, Sequence

from tools.docs._runner import REPO_ROOT, generated_header, md_cell, run

if TYPE_CHECKING:  # server modules load inside render(), after the tree guard
    from code_indexer.server.error_codes import ErrorDefinition

COMMAND = "python3 -m tools.docs.error_codes"
TARGET_DIR = REPO_ROOT / "docs" / "reference" / "error-codes"
REGISTRY_FILE = "src/code_indexer/server/error_codes.py"
LOGGING_FILE = "src/code_indexer/server/logging_utils.py"

# Source line width of the compact "without a description" code lists.
LIST_WIDTH = 100

_PLACEHOLDERS = {"", "TODO"}

# One line per registry subsystem (the comments beside SUBSYSTEMS in the
# registry). render() fails when the registry gains a prefix missing here.
SUBSYSTEM_TITLES: Mapping[str, str] = {
    "APP": "Application lifecycle",
    "AUTH": "Authentication and authorization",
    "CACHE": "Caching",
    "DEPLOY": "Deployment",
    "GIT": "Git operations",
    "MCP": "MCP protocol and tools",
    "MONITOR": "Self-monitoring",
    "QUERY": "Query operations",
    "REPO": "Repository management",
    "SCIP": "SCIP code intelligence",
    "STORE": "Storage operations",
    "SVC": "Service operations",
    "TELEM": "Telemetry",
    "VALID": "Validation",
    "WEB": "Web routes and handlers",
}


def is_placeholder(text: str) -> bool:
    """True when a registry text field is empty or the ``TODO`` placeholder."""
    return text.strip().upper() in _PLACEHOLDERS


def _group_by_subsystem(
    registry: Mapping[str, ErrorDefinition], subsystems: AbstractSet[str]
) -> Dict[str, List[ErrorDefinition]]:
    groups: Dict[str, List[ErrorDefinition]] = {}
    for key in sorted(registry):
        definition = registry[key]
        if definition.code != key:
            raise ValueError(
                f"Registry key {key!r} holds a definition for {definition.code!r}"
            )
        prefix = key.split("-", 1)[0]
        if prefix not in subsystems:
            raise ValueError(f"Error code {key!r} uses unknown subsystem {prefix!r}")
        if prefix not in SUBSYSTEM_TITLES:
            raise ValueError(
                f"Subsystem {prefix!r} has no title in SUBSYSTEM_TITLES; add one"
            )
        groups.setdefault(prefix, []).append(definition)
    return {prefix: groups[prefix] for prefix in sorted(groups)}


def _documented(definitions: List[ErrorDefinition]) -> List[ErrorDefinition]:
    return [d for d in definitions if not is_placeholder(d.description)]


def _index_page(
    groups: Mapping[str, List[ErrorDefinition]], registry: Mapping[str, object]
) -> str:
    from code_indexer.server.error_codes import validate_error_code_format

    total = len(registry)
    documented = sum(len(_documented(defs)) for defs in groups.values())
    nonconforming = sorted(k for k in registry if not validate_error_code_format(k))
    example = next(iter(groups.values()))[0].code
    lines = [
        generated_header(COMMAND),
        "",
        "# Error Codes",
        "",
        f"The server error-code registry, `ERROR_REGISTRY` in `{REGISTRY_FILE}`, "
        f"defines {total} codes across {len(groups)} subsystems: {documented} "
        f"have a description and {total - documented} do not yet. Each "
        "subsystem has its own page.",
        "",
        "## Format",
        "",
        "A code has the form `SUBSYSTEM-CATEGORY-NUMBER`: SUBSYSTEM is 2-6 "
        "uppercase letters naming the functional area, CATEGORY is 2-8 uppercase "
        "letters naming the component or operation, and NUMBER is three digits "
        f"(checked by `validate_error_code_format`). Example: `{example}`.",
    ]
    if nonconforming:
        listed = ", ".join(f"`{code}`" for code in nonconforming)
        lines += [
            "",
            f"{len(nonconforming)} registered codes do not follow this format: "
            f"{listed}.",
        ]
    lines += [
        "",
        "## How the registry is used",
        "",
        "The registry is a catalogue. Each log call site writes its code into the "
        f"message itself through `format_error_log(code, message, ...)` in "
        f"`{LOGGING_FILE}`, which produces `[CODE] message key=value`. Nothing "
        "looks a code up in the registry at runtime, so the description and "
        "suggested action on the subsystem pages document a code; they are not "
        "the text of the log line. To find occurrences, search the logs for the "
        "code in square brackets.",
        "",
        "## Subsystems",
        "",
        "| Prefix | Subsystem | Codes | With description |",
        "|--------|-----------|-------|------------------|",
    ]
    for prefix, definitions in groups.items():
        lines.append(
            f"| [{prefix}]({prefix.lower()}.md) | {SUBSYSTEM_TITLES[prefix]} "
            f"| {len(definitions)} | {len(_documented(definitions))} |"
        )
    return "\n".join(lines) + "\n"


def _subsystem_page(prefix: str, definitions: List[ErrorDefinition]) -> str:
    documented = _documented(definitions)
    undocumented = [d.code for d in definitions if d not in documented]
    lines = [
        generated_header(COMMAND),
        "",
        f"# {prefix} error codes",
        "",
        f"{SUBSYSTEM_TITLES[prefix]}. {len(definitions)} codes: "
        f"{len(documented)} with a description, {len(undocumented)} without. "
        "Format and other subsystems: [Error Codes](README.md).",
        "",
    ]
    if documented:
        lines += [
            "| Code | Severity | Description | Suggested action |",
            "|------|----------|-------------|------------------|",
        ]
        for d in documented:
            action = "" if is_placeholder(d.action) else md_cell(d.action)
            lines.append(
                f"| `{d.code}` | {d.severity.value} | {md_cell(d.description)} "
                f"| {action} |"
            )
    else:
        lines.append("No code in this subsystem has a description yet.")
    if undocumented:
        codes = ", ".join(f"`{code}`" for code in undocumented)
        lines += ["", f"## Codes without a description yet ({len(undocumented)})", ""]
        lines += textwrap.wrap(
            codes, width=LIST_WIDTH, break_long_words=False, break_on_hyphens=False
        )
    return "\n".join(lines) + "\n"


def render(
    registry: Optional[Mapping[str, ErrorDefinition]] = None,
    subsystems: Optional[AbstractSet[str]] = None,
) -> Dict[str, str]:
    """Return ``{page name: Markdown}`` for the error-code reference."""
    from code_indexer.server.error_codes import ERROR_REGISTRY, SUBSYSTEMS

    registry = ERROR_REGISTRY if registry is None else registry
    subsystems = SUBSYSTEMS if subsystems is None else subsystems
    groups = _group_by_subsystem(registry, subsystems)
    if not groups:
        raise ValueError("The error-code registry is empty")
    pages = {"README.md": _index_page(groups, registry)}
    for prefix, definitions in groups.items():
        pages[f"{prefix.lower()}.md"] = _subsystem_page(prefix, definitions)
    return pages


def main(argv: Optional[Sequence[str]] = None, target_dir: Path = TARGET_DIR) -> int:
    return run(argv, target_dir=target_dir, render=render, command=COMMAND)


if __name__ == "__main__":
    raise SystemExit(main())
