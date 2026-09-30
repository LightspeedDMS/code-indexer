"""Configuration has a single audited writer.

After the audited-change refactor, ``save_config(`` is called only inside
``ConfigService`` (its one publish path) and by the two bootstrap writers
that are not operator actions (the installer and the MCP self-registration
service).  Any other caller would publish configuration without an audit
row.  The scan walks the syntax tree of every module in the package, so a
spaced call, a saved method reference and a by-name lookup are all caught.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List

import code_indexer

_PACKAGE = Path(code_indexer.__file__).parent
_ALLOWED = frozenset(
    {
        "server/services/config_service.py",
        "server/installer.py",
        "server/services/mcp_self_registration_service.py",
    }
)
_WRITER = "save_config"


def _writer_uses(source: str) -> List[int]:
    """Line numbers that call, reference or look up ``save_config``."""
    lines: List[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Attribute) and node.attr == _WRITER:
            lines.append(node.lineno)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == _WRITER
        ):
            lines.append(node.lineno)
    return lines


def test_only_the_config_service_and_bootstrap_writers_save_config() -> None:
    offenders: List[str] = []
    for path in sorted(_PACKAGE.rglob("*.py")):
        relative = path.relative_to(_PACKAGE).as_posix()
        if relative in _ALLOWED:
            continue
        offenders.extend(
            f"{relative}:{line}" for line in _writer_uses(path.read_text())
        )
    assert offenders == []


def test_the_allowed_writers_still_exist() -> None:
    for relative in _ALLOWED:
        assert _writer_uses((_PACKAGE / relative).read_text()), relative


def test_the_scan_detects_every_way_of_naming_the_writer() -> None:
    """Negative controls: each spelling of a direct save is flagged."""
    assert _writer_uses("svc.save_config(cfg)") == [1]
    assert _writer_uses("svc.save_config (cfg)") == [1]
    assert _writer_uses("fn = svc.save_config\nfn(cfg)") == [1]
    assert _writer_uses("getattr(svc, 'save_config')(cfg)") == [1]
    assert _writer_uses("def save_config(request):\n    pass") == []
    assert _writer_uses("svc.apply_audited_change(m, actor=a, target_id=t)") == []
