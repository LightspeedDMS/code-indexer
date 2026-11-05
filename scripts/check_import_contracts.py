#!/usr/bin/env python3
"""Ratchet for the package dependency contracts in ``.importlinter``.

The contracts state where the code must end up (modular packaging, see
``plans/designs/modular-endstate.md`` section 3). Today's tree breaks them, so
this script does not require zero violations; it requires that the set of
violations matches the committed baseline exactly:

* a violation not in the baseline (a new forbidden import, or one more line
  for an import that is already there) fails;
* a baseline entry that no longer occurs, or occurs on fewer lines, fails
  too, until the baseline is lowered -- otherwise the freed headroom would let
  a new import in unnoticed.

Violations are counted by import-linter itself (same engine, same contract
semantics as ``lint-imports``): one per import statement LINE, keyed
``importer -> imported``. import-linter parses every import statement in a
module, including those inside functions and under ``TYPE_CHECKING``. What it
cannot see is a dynamic import by string, ``importlib.import_module("x.y")`` or
``__import__("x.y")``; those are found here by AST and counted under
``importer -> imported (dynamic)``.

Usage::

    python3 scripts/check_import_contracts.py           # check (CI, lint.sh)
    python3 scripts/check_import_contracts.py --write   # rewrite the baseline

Rewrite the baseline only to LOWER it after removing imports. Adding an entry
to make a new import pass defeats the contract.
"""

from __future__ import annotations

import argparse
import ast
import importlib
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterator, List, Optional, Sequence, Tuple

REPO_ROOT = Path(__file__).resolve().parents[1]
CONFIG_PATH = REPO_ROOT / ".importlinter"
BASELINE_PATH = REPO_ROOT / "scripts" / "import_contracts_ratchet.json"
#: Directories put on sys.path so import-linter can find every root package.
SEARCH_PATHS: Tuple[Path, ...] = (
    REPO_ROOT / "src",
    REPO_ROOT / "plugins" / "cidx_git_backend",
    REPO_ROOT / "plugins" / "cidx_orchestrator_invoker",
    REPO_ROOT / "packages" / "backends" / "cidx-backend-workspace" / "src",
    REPO_ROOT / "packages" / "backends" / "cidx-backend-git" / "src",
    REPO_ROOT / "packages" / "cidx-testkit" / "src",
    REPO_ROOT / "packages" / "cidx-sdk" / "src",
    REPO_ROOT / "packages" / "features" / "cidx-xray" / "src",
    REPO_ROOT / "packages" / "features" / "cidx-wiki" / "src",
    REPO_ROOT / "packages" / "features" / "cidx-memory" / "src",
    REPO_ROOT / "packages" / "features" / "cidx-depmap" / "src",
    REPO_ROOT / "packages" / "features" / "cidx-scip" / "src",
)

#: Top-level modules of ``code_indexer`` that are shells (CLI, daemon, server)
#: rather than libraries: they may import the server. Every other top-level
#: module must be a source of a "no server" contract; see
#: ``unclassified_top_level_modules``.
SHELL_MODULES = frozenset(
    {
        "server",
        "daemon",
        "cli_utils",
        "disabled_commands",
        "mode_specific_handlers",
        "remote_status",
        "remote_uninstall",
        "teach_ai_templates",
    }
)
#: Any top-level module whose name starts with this is a CLI shell module.
SHELL_PREFIX = "cli"

DYNAMIC_SUFFIX = " (dynamic)"
_DYNAMIC_IMPORT_FUNCTIONS = frozenset({"import_module", "__import__"})

Violations = Dict[str, Dict[str, int]]


class ContractConfigError(Exception):
    """The contract file uses something this ratchet cannot count."""


class ImportLinterMissing(Exception):
    """import-linter is not installed."""


@dataclass(frozen=True)
class Contract:
    """One ``forbidden`` contract, as the ratchet needs it."""

    contract_id: str
    name: str
    source_modules: Tuple[str, ...]
    forbidden_modules: Tuple[str, ...]


@dataclass
class Comparison:
    grew: List[str] = field(default_factory=list)
    shrank: List[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.grew and not self.shrank


# -- reading the contracts ------------------------------------------------------


def _as_tuple(value: object) -> Tuple[str, ...]:
    if isinstance(value, str):
        return tuple(v for v in (p.strip() for p in value.splitlines()) if v)
    if isinstance(value, (list, tuple, set, frozenset)):
        return tuple(sorted(str(v) for v in value))
    raise ContractConfigError(f"unexpected option value {value!r}")


def _read_options(config_path: Path) -> Tuple[dict, List[dict]]:
    try:
        from importlinter import api
    except ImportError as exc:  # pragma: no cover - exercised only without the tool
        raise ImportLinterMissing(
            "import-linter is not installed; install the version pinned in "
            "pyproject.toml's dev extra"
        ) from exc
    options = api.read_configuration(str(config_path))
    return options["session_options"], options["contracts_options"]


def read_contracts(config_path: Path) -> List[Contract]:
    """The contracts in ``config_path``; refuses any this ratchet cannot count.

    Only ``forbidden`` contracts with ``allow_indirect_imports = True`` are
    supported: each of their violations is one direct import, which is what
    the baseline counts. Anything else would be reported as chains the
    baseline has no key for.
    """
    _, contract_options = _read_options(config_path)
    contracts = []
    for options in contract_options:
        contract_id = options.get("id") or options["name"]
        if options.get("type") != "forbidden":
            raise ContractConfigError(
                f"contract {contract_id!r}: only 'forbidden' contracts are ratcheted"
            )
        if str(options.get("allow_indirect_imports", "")).lower() != "true":
            raise ContractConfigError(
                f"contract {contract_id!r}: set allow_indirect_imports = True "
                "(the ratchet counts direct imports)"
            )
        if options.get("ignore_imports"):
            raise ContractConfigError(
                f"contract {contract_id!r}: no ignore_imports; known violations "
                f"belong in {BASELINE_PATH.name}"
            )
        contracts.append(
            Contract(
                contract_id=contract_id,
                name=options["name"],
                source_modules=_as_tuple(options["source_modules"]),
                forbidden_modules=_as_tuple(options["forbidden_modules"]),
            )
        )
    return contracts


# -- counting -------------------------------------------------------------------


def _key(importer: str, imported: str) -> str:
    return f"{importer} -> {imported}"


def _within(module: str, package: str) -> bool:
    return module == package or module.startswith(package + ".")


def _static_violations(config_path: Path) -> Violations:
    """Direct imports breaking each contract, counted by import-linter."""
    from importlinter.application import use_cases

    user_options = use_cases.read_user_options(str(config_path))
    # create_report needs the contract types registered, as lint_imports does.
    use_cases._register_contract_types(user_options)
    ids_by_name = {
        o["name"]: o.get("id") or o["name"] for o in user_options.contracts_options
    }
    report = use_cases.create_report(user_options, cache_dir=None)
    if report.could_not_run:
        raise ContractConfigError(
            f"import-linter could not run: {report.invalid_contract_options}"
        )
    found: Violations = {}
    for contract, check in report.get_contracts_and_checks():
        counts = found.setdefault(ids_by_name[contract.name], {})
        for chain_group in check.metadata.get("invalid_chains", []):
            for chain in chain_group["chains"]:
                for link in chain:
                    key = _key(link["importer"], link["imported"])
                    counts[key] = counts.get(key, 0) + len(link["line_numbers"])
    return found


def _literal_dynamic_targets(source: str) -> Iterator[str]:
    """Module names imported by string literal through import_module/__import__."""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        name = (
            func.attr
            if isinstance(func, ast.Attribute)
            else func.id
            if isinstance(func, ast.Name)
            else None
        )
        first = node.args[0]
        if (
            name in _DYNAMIC_IMPORT_FUNCTIONS
            and isinstance(first, ast.Constant)
            and isinstance(first.value, str)
            and not first.value.startswith(".")
        ):
            yield first.value


def _module_files(
    package: str, search_paths: Sequence[Path]
) -> Iterator[Tuple[str, Path]]:
    """(module name, file) for ``package`` and everything under it."""
    parts = package.split(".")
    for root in search_paths:
        base = root.joinpath(*parts)
        if base.is_dir():
            for path in sorted(base.rglob("*.py")):
                rel = path.relative_to(root).with_suffix("")
                names = list(rel.parts)
                if names[-1] == "__init__":
                    names.pop()
                yield ".".join(names), path
            return
        single = base.with_suffix(".py")
        if single.is_file():
            yield package, single
            return


def _expand(expression: str, search_paths: Sequence[Path]) -> List[str]:
    """Expand a trailing ``.*`` wildcard into the package's direct children."""
    if not expression.endswith(".*"):
        return [expression]
    parent = expression[:-2]
    children = []
    for root in search_paths:
        base = root.joinpath(*parent.split("."))
        if base.is_dir():
            for entry in sorted(base.iterdir()):
                if entry.is_dir() and (entry / "__init__.py").is_file():
                    children.append(f"{parent}.{entry.name}")
                elif entry.suffix == ".py" and entry.stem != "__init__":
                    children.append(f"{parent}.{entry.stem}")
    if not children:
        raise ContractConfigError(f"wildcard {expression!r} matches nothing")
    return children


def _dynamic_violations(
    contracts: Sequence[Contract], search_paths: Sequence[Path]
) -> Violations:
    found: Violations = {}
    for contract in contracts:
        if any("*" in m for m in contract.forbidden_modules):
            raise ContractConfigError(
                f"contract {contract.contract_id!r}: no wildcards in forbidden_modules"
            )
        counts = found.setdefault(contract.contract_id, {})
        for expression in contract.source_modules:
            for source in _expand(expression, search_paths):
                for module, path in _module_files(source, search_paths):
                    text = path.read_text(encoding="utf-8")
                    for target in _literal_dynamic_targets(text):
                        for forbidden in contract.forbidden_modules:
                            # Same overlap rule as import-linter: a package
                            # importing itself is not a violation.
                            if _within(source, forbidden) or _within(forbidden, source):
                                continue
                            if _within(target, forbidden):
                                key = _key(module, target) + DYNAMIC_SUFFIX
                                counts[key] = counts.get(key, 0) + 1
    return found


def collect_violations(
    config_path: Path = CONFIG_PATH,
    search_paths: Sequence[Path] = SEARCH_PATHS,
) -> Violations:
    """Every contract's violations: ``{contract id: {edge: line count}}``."""
    added = [str(p) for p in search_paths if str(p) not in sys.path]
    sys.path[:0] = added
    importlib.invalidate_caches()
    try:
        contracts = read_contracts(config_path)
        found = _static_violations(config_path)
        for contract_id, counts in _dynamic_violations(contracts, search_paths).items():
            merged = found.setdefault(contract_id, {})
            for key, n in counts.items():
                merged[key] = merged.get(key, 0) + n
        for contract in contracts:
            found.setdefault(contract.contract_id, {})
        return {
            cid: dict(sorted(edges.items())) for cid, edges in sorted(found.items())
        }
    finally:
        for entry in added:
            sys.path.remove(entry)


# -- the ratchet ----------------------------------------------------------------


def compare(baseline: Violations, found: Violations) -> Comparison:
    """What grew (fails: new imports) and what shrank (fails: stale baseline)."""
    result = Comparison()
    for contract_id in sorted(set(baseline) | set(found)):
        if contract_id not in found:
            result.shrank.append(
                f"{contract_id}: contract no longer exists; drop it from the baseline"
            )
            continue
        before = baseline.get(contract_id, {})
        after = found[contract_id]
        for key in sorted(set(before) | set(after)):
            was, now = before.get(key, 0), after.get(key, 0)
            if now > was:
                result.grew.append(f"{contract_id}: {key} ({was} -> {now} lines)")
            elif now < was:
                result.shrank.append(f"{contract_id}: {key} ({was} -> {now} lines)")
    return result


def load_baseline(path: Path = BASELINE_PATH) -> Violations:
    data = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ContractConfigError(f"{path} must hold a JSON object")
    return {str(k): {str(e): int(n) for e, n in v.items()} for k, v in data.items()}


def write_baseline(found: Violations, path: Path = BASELINE_PATH) -> None:
    path.write_text(
        json.dumps(found, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )


def unclassified_top_level_modules(
    contracts: Sequence[Contract], src: Path = REPO_ROOT / "src"
) -> List[str]:
    """Top-level ``code_indexer`` modules in no "no server" contract and not a shell.

    Keeps a new top-level package from escaping the server rule by not being
    listed: it has to be put in a contract or declared a shell here.
    """
    covered: set = set()
    for contract in contracts:
        if "code_indexer.server" in contract.forbidden_modules:
            covered.update(contract.source_modules)
    missing = []
    for module in _expand("code_indexer.*", [src]):
        short = module.split(".", 1)[1]
        if short in SHELL_MODULES or short.startswith(SHELL_PREFIX):
            continue
        if module not in covered:
            missing.append(module)
    return missing


def _summary(found: Violations) -> str:
    return "\n".join(
        f"  {cid}: {sum(edges.values())} import lines in {len(edges)} edges"
        for cid, edges in found.items()
    )


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--write", action="store_true", help="rewrite the baseline from the tree"
    )
    args = parser.parse_args(argv)
    try:
        contracts = read_contracts(CONFIG_PATH)
        found = collect_violations()
    except (ContractConfigError, ImportLinterMissing) as exc:
        print(f"import contracts: {exc}", file=sys.stderr)
        return 2
    unclassified = unclassified_top_level_modules(contracts)
    if unclassified:
        print(
            "import contracts: top-level modules in no server contract and not a "
            f"shell (add them to .importlinter or SHELL_MODULES): {unclassified}",
            file=sys.stderr,
        )
        return 1
    if args.write:
        write_baseline(found)
        print(f"wrote {BASELINE_PATH.relative_to(REPO_ROOT)}\n{_summary(found)}")
        return 0
    result = compare(load_baseline(), found)
    if result.grew:
        print(
            "import contracts: NEW forbidden imports (see "
            "docs/architecture/invariants/import-contracts.md):",
            *result.grew,
            sep="\n  ",
            file=sys.stderr,
        )
    if result.shrank:
        print(
            "import contracts: violations removed -- lower the baseline with "
            "`python3 scripts/check_import_contracts.py --write`:",
            *result.shrank,
            sep="\n  ",
            file=sys.stderr,
        )
    if not result.ok:
        return 1
    print(f"import contracts: baseline matches\n{_summary(found)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
