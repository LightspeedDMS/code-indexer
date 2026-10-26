"""Story #2203: one pattern-name rule decides both what the pattern library
stores and what the X-Ray audit row accepts (xray_pattern_names)."""

import subprocess
from pathlib import Path

from code_indexer.server.services.xray_pattern_names import is_storable_pattern_name
from code_indexer.server.services.xray_pattern_service import XrayPatternService

# ^ invariant staged as `.<name>.yaml.XXXXXXXX.tmp`; 236 + 19 bytes = the 255-byte filename limit
_LONGEST = "n" * 236
_TOO_LONG = "n" * 237


def _pattern_yaml(name: str) -> str:
    return (
        f"name: {name}\n"
        'description: "example"\n'
        "language: java\n"
        "evaluator_code: |\n"
        "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
    )


def _store(tmp_path: Path, name: str) -> dict:
    cidx_meta = tmp_path / "cidx-meta"
    cidx_meta.mkdir(exist_ok=True)
    subprocess.run(["git", "init", "-q"], cwd=cidx_meta, check=True)
    return dict(
        XrayPatternService(cidx_meta).store_xray_pattern_audited(
            actor="example-user",
            may_replace_global=True,
            scope="__any__",
            pattern_yaml=_pattern_yaml(name),
        )
    )


def test_store_refuses_a_name_the_shared_rule_refuses(tmp_path: Path) -> None:
    assert is_storable_pattern_name(_LONGEST) is True
    assert is_storable_pattern_name(_TOO_LONG) is False

    refused = _store(tmp_path, _TOO_LONG)
    assert refused.get("error") == "invalid_pattern_name", refused

    accepted = _store(tmp_path, _LONGEST)
    assert accepted.get("error") != "invalid_pattern_name", accepted
