"""Package dependency contracts: the ratchet, and proof of what it can see.

``scripts/check_import_contracts.py`` evaluates the contracts in
``.importlinter`` with import-linter and compares the violations with
``scripts/import_contracts_ratchet.json``. This module runs it against the real
tree (the CI gate) and pins the properties the gate depends on:

* function-level imports, imports under ``TYPE_CHECKING``, relative imports
  and ``from pkg import submodule`` are all counted -- most of the cycles the
  modularization has to break hide inside functions;
* dynamic imports by string literal, which import-linter cannot see, are
  counted by the script's own AST pass;
* the comparison fails on a new import AND on a stale baseline entry;
* every top-level package is either in a "no server" contract or a declared
  shell, so a new package cannot escape the rule by not being listed;
* the import-linter version is pinned identically everywhere it is installed.
"""

from __future__ import annotations

import importlib.util
import re
import sys
import textwrap
from pathlib import Path
from types import ModuleType
from typing import Dict

import pytest

_REPO = Path(__file__).resolve().parents[3]
_SCRIPT = _REPO / "scripts" / "check_import_contracts.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_import_contracts", _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_import_contracts"] = module
    spec.loader.exec_module(module)
    return module


checker = _load_checker()


# -- the gate -------------------------------------------------------------------


@pytest.fixture(scope="module")
def real_violations() -> Dict[str, Dict[str, int]]:
    result: Dict[str, Dict[str, int]] = checker.collect_violations()
    return result


def test_violations_match_the_baseline(
    real_violations: Dict[str, Dict[str, int]],
) -> None:
    found = real_violations
    result = checker.compare(checker.load_baseline(), found)
    assert not result.grew, (
        "new imports break a package dependency contract (see "
        "docs/architecture/invariants/import-contracts.md):\n  "
        + "\n  ".join(result.grew)
    )
    assert not result.shrank, (
        "violations were removed -- lower the baseline so the headroom cannot "
        "be reused: python3 scripts/check_import_contracts.py --write\n  "
        + "\n  ".join(result.shrank)
    )


def test_the_scan_sees_the_known_heavy_edges(
    real_violations: Dict[str, Dict[str, int]],
) -> None:
    """Fail loudly, not vacuously, if the graph build silently finds nothing."""
    library = real_violations["library-packages-no-server"]
    from_global_repos = sum(
        n for k, n in library.items() if k.startswith("code_indexer.global_repos.")
    )
    assert from_global_repos > 50, "the scan is probably broken"


def test_every_top_level_module_is_classified() -> None:
    contracts = checker.read_contracts(checker.CONFIG_PATH)
    assert checker.unclassified_top_level_modules(contracts) == []


def test_import_linter_pin_is_the_same_everywhere() -> None:
    pattern = re.compile(r"import-linter==([0-9][0-9.]*)")
    sources = {
        name: pattern.findall((_REPO / name).read_text(encoding="utf-8"))
        for name in ("pyproject.toml", ".gitlab-ci.yml", ".pre-commit-config.yaml")
    }
    versions = {v for found in sources.values() for v in found}
    assert all(sources.values()), f"import-linter is not pinned in: {sources}"
    assert len(versions) == 1, f"import-linter pins disagree: {sources}"


# -- what the scan can see ------------------------------------------------------

_CONFIG = """
[importlinter]
root_package = ilproof

[importlinter:contract:lib-no-server]
name = lib does not import server
type = forbidden
source_modules =
    ilproof.lib
forbidden_modules =
    ilproof.server
allow_indirect_imports = True
"""

_MODULES: Dict[str, str] = {
    "__init__.py": "",
    "server/__init__.py": "",
    "server/a.py": "",
    "server/b.py": "",
    "server/c.py": "",
    "server/d.py": "",
    "server/e.py": "",
    "server/f.py": "",
    "server/g.py": "",
    "lib/__init__.py": "",
    "lib/top.py": "from ilproof.server import a\n",
    "lib/funcs.py": """
        from typing import TYPE_CHECKING

        if TYPE_CHECKING:
            from ilproof.server import c

        def plain():
            from ilproof.server import b
            return b

        def nested():
            def inner():
                import ilproof.server.d
                return ilproof.server.d
            return inner

        class Service:
            def method(self):
                try:
                    from ..server import e
                except ImportError:
                    e = None
                return e

        def submodule_attribute():
            from ilproof import server
            return server
        """,
    "lib/dyn.py": """
        import importlib

        def load():
            importlib.import_module("ilproof.server.f")
            __import__("ilproof.server.g")
            importlib.import_module(".server.f", "ilproof")
        """,
}


@pytest.fixture
def proof_package(tmp_path: Path) -> Path:
    src = tmp_path / "src"
    for rel, body in _MODULES.items():
        path = src / "ilproof" / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(textwrap.dedent(body), encoding="utf-8")
    (tmp_path / ".importlinter").write_text(_CONFIG, encoding="utf-8")
    return tmp_path


def _violations(root: Path) -> Dict[str, int]:
    found = checker.collect_violations(root / ".importlinter", [root / "src"])
    return dict(found["lib-no-server"])


def test_function_level_imports_are_counted(proof_package: Path) -> None:
    found = _violations(proof_package)
    expected_static = {
        "ilproof.lib.top -> ilproof.server.a": 1,  # module level
        "ilproof.lib.funcs -> ilproof.server.c": 1,  # under TYPE_CHECKING
        "ilproof.lib.funcs -> ilproof.server.b": 1,  # inside a function
        "ilproof.lib.funcs -> ilproof.server.d": 1,  # nested function, plain import
        "ilproof.lib.funcs -> ilproof.server.e": 1,  # method, try block, relative
        "ilproof.lib.funcs -> ilproof.server": 1,  # from package import submodule
    }
    for key, lines in expected_static.items():
        assert found.get(key) == lines, f"{key} not counted: {found}"


def test_dynamic_imports_escape_import_linter_and_are_counted_here(
    proof_package: Path,
) -> None:
    # import-linter alone (the static report) cannot see a string import...
    sys.path.insert(0, str(proof_package / "src"))
    try:
        static = checker._static_violations(proof_package / ".importlinter")
    finally:
        sys.path.remove(str(proof_package / "src"))
    assert not any(k.startswith("ilproof.lib.dyn") for k in static["lib-no-server"])
    # ...so the script's AST pass counts it. A relative string is not resolved.
    found = _violations(proof_package)
    assert found["ilproof.lib.dyn -> ilproof.server.f (dynamic)"] == 1
    assert found["ilproof.lib.dyn -> ilproof.server.g (dynamic)"] == 1


def test_an_unsupported_contract_is_refused(tmp_path: Path) -> None:
    config = tmp_path / ".importlinter"
    config.write_text(
        _CONFIG.replace("allow_indirect_imports = True", ""), encoding="utf-8"
    )
    with pytest.raises(checker.ContractConfigError, match="allow_indirect_imports"):
        checker.read_contracts(config)


# -- the comparison -------------------------------------------------------------


class TestCompare:
    _BASE = {"c": {"a -> b": 2, "x -> y": 1}}

    def test_equal_is_ok(self) -> None:
        assert checker.compare(self._BASE, {"c": {"a -> b": 2, "x -> y": 1}}).ok

    def test_a_new_edge_or_line_grows(self) -> None:
        result = checker.compare(
            self._BASE, {"c": {"a -> b": 3, "x -> y": 1, "n -> m": 1}}
        )
        assert len(result.grew) == 2 and not result.shrank

    def test_a_removed_edge_or_line_is_stale(self) -> None:
        result = checker.compare(self._BASE, {"c": {"a -> b": 1}})
        assert len(result.shrank) == 2 and not result.grew

    def test_a_contract_missing_from_the_tree_is_stale(self) -> None:
        result = checker.compare(self._BASE, {})
        assert result.shrank and not result.grew

    def test_a_new_contract_starts_at_zero(self) -> None:
        result = checker.compare({}, {"c": {"a -> b": 1}})
        assert result.grew == ["c: a -> b (0 -> 1 lines)"]
