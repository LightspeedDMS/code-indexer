"""Tests for the error-code reference generator (Story #2082)."""

import re
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.error_codes import (
    ERROR_REGISTRY,
    SUBSYSTEMS,
    ErrorDefinition,
    Severity,
    validate_error_code_format,
)
from tools.docs import error_codes as gen

# Matched against each line separately, so ^ anchors a line.
_ROW = re.compile(r"^\| `([A-Z]+(?:-[A-Z]+)+-\d+)` \|")
_CODE = re.compile(r"`([A-Z]+(?:-[A-Z]+)+-\d+)`")
_UNDOCUMENTED = "## Codes without a description yet"


def _definition(code: str, description: str = "d", action: str = "a"):
    return ErrorDefinition(
        code=code, description=description, severity=Severity.ERROR, action=action
    )


def _rows(text: str) -> List[str]:
    return [m.group(1) for m in map(_ROW.match, text.splitlines()) if m]


def _undocumented(text: str) -> List[str]:
    if _UNDOCUMENTED not in text:
        return []
    return _CODE.findall(text.split(_UNDOCUMENTED, 1)[1])


def _subsystem_pages(pages: Dict[str, str]) -> Dict[str, str]:
    return {name: text for name, text in pages.items() if name != "README.md"}


def _nonconforming_note(text: str) -> str:
    return next(line for line in text.splitlines() if "do not follow" in line)


def test_real_registry_one_page_per_subsystem_plus_index() -> None:
    pages = gen.render()
    assert set(pages) == {"README.md"} | {f"{p.lower()}.md" for p in SUBSYSTEMS}
    assert set(gen.SUBSYSTEM_TITLES) == SUBSYSTEMS


def test_real_registry_every_code_listed_exactly_once() -> None:
    listed: List[str] = []
    for text in _subsystem_pages(gen.render()).values():
        listed += _rows(text) + _undocumented(text)
    assert sorted(listed) == sorted(ERROR_REGISTRY)


def test_real_registry_renders_no_placeholder_text() -> None:
    for name, text in gen.render().items():
        assert "TODO" not in text, name


def test_real_registry_index_counts() -> None:
    documented = [
        k for k, d in ERROR_REGISTRY.items() if not gen.is_placeholder(d.description)
    ]
    readme = gen.render()["README.md"]
    assert f"defines {len(ERROR_REGISTRY)} codes" in readme
    assert f"{len(documented)} have a description" in readme
    assert f"{len(ERROR_REGISTRY) - len(documented)} do not yet" in readme


def test_real_registry_nonconforming_note_matches_validator() -> None:
    bad = sorted(k for k in ERROR_REGISTRY if not validate_error_code_format(k))
    readme = gen.render()["README.md"]
    if not bad:
        assert "do not follow" not in readme
        return
    assert _CODE.findall(_nonconforming_note(readme)) == bad


def test_every_page_starts_with_generated_header() -> None:
    for text in gen.render().values():
        assert text.splitlines()[0] == gen.generated_header(gen.COMMAND)
    assert gen.COMMAND == "python3 -m tools.docs.error_codes"


def test_index_describes_catalogue_not_runtime_dispatch() -> None:
    readme = gen.render()["README.md"]
    assert "catalogue" in readme
    assert "format_error_log" in readme
    assert "SUBSYSTEM-CATEGORY-NUMBER" in readme


def test_synthetic_pages_sorted_grouped_and_escaped() -> None:
    registry = {
        "GIT-PULL-002": _definition("GIT-PULL-002", "pull | failed", "retry"),
        "AUTH-LOGIN-001": _definition("AUTH-LOGIN-001", "bad <user>"),
        "GIT-CLONE-001": _definition("GIT-CLONE-001"),
    }
    pages = gen.render(registry=registry, subsystems={"AUTH", "GIT"})
    assert set(pages) == {"README.md", "auth.md", "git.md"}
    assert _rows(pages["git.md"]) == ["GIT-CLONE-001", "GIT-PULL-002"]
    assert "| `GIT-PULL-002` | error | pull \\| failed | retry |" in pages["git.md"]
    assert "bad &lt;user&gt;" in pages["auth.md"]
    assert _UNDOCUMENTED not in pages["git.md"]
    readme = pages["README.md"]
    assert "defines 3 codes across 2 subsystems" in readme
    assert "| [AUTH](auth.md) | Authentication and authorization | 1 | 1 |" in readme
    assert "| [GIT](git.md) | Git operations | 2 | 2 |" in readme
    assert readme.index("[AUTH]") < readme.index("[GIT]")
    assert "do not follow" not in readme
    assert "[Error Codes](README.md)" in pages["git.md"]


def test_codes_without_description_listed_compactly() -> None:
    registry = {
        "GIT-PULL-001": _definition("GIT-PULL-001", "pull failed", "TODO"),
        "GIT-PULL-002": _definition("GIT-PULL-002", "TODO", "TODO"),
        "GIT-PULL-003": _definition("GIT-PULL-003", " todo ", "retry"),
    }
    for number in range(4, 40):
        code = f"GIT-CLONE-{number:03d}"
        registry[code] = _definition(code, "TODO", "TODO")
    page = gen.render(registry=registry, subsystems={"GIT"})["git.md"]
    assert _rows(page) == ["GIT-PULL-001"]
    assert "| `GIT-PULL-001` | error | pull failed |  |" in page
    listed = _undocumented(page)
    assert listed == sorted(set(registry) - {"GIT-PULL-001"})
    assert f"{_UNDOCUMENTED} ({len(listed)})" in page
    section = page.split(_UNDOCUMENTED, 1)[1].strip().splitlines()[1:]
    body = [line for line in section if line]
    assert all(len(line) <= gen.LIST_WIDTH for line in body)
    assert len(body) <= len(" ".join(f"`{c}`," for c in listed)) // 80 + 1


def test_fully_undocumented_subsystem_has_no_table() -> None:
    registry = {"GIT-PULL-001": _definition("GIT-PULL-001", "TODO", "TODO")}
    page = gen.render(registry=registry, subsystems={"GIT"})["git.md"]
    assert "| Code |" not in page
    assert "No code in this subsystem has a description yet." in page
    assert _undocumented(page) == ["GIT-PULL-001"]


def test_subsystem_without_codes_has_no_page() -> None:
    registry = {"AUTH-LOGIN-001": _definition("AUTH-LOGIN-001")}
    pages = gen.render(registry=registry, subsystems={"AUTH", "GIT"})
    assert set(pages) == {"README.md", "auth.md"}


def test_nonconforming_code_rendered_and_listed() -> None:
    registry = {
        "AUTH-LOGIN-001": _definition("AUTH-LOGIN-001"),
        "AUTH-LOGIN-FLOW-002": _definition("AUTH-LOGIN-FLOW-002", "extra part"),
    }
    pages = gen.render(registry=registry, subsystems={"AUTH"})
    assert _rows(pages["auth.md"]) == ["AUTH-LOGIN-001", "AUTH-LOGIN-FLOW-002"]
    assert _CODE.findall(_nonconforming_note(pages["README.md"])) == [
        "AUTH-LOGIN-FLOW-002"
    ]


def test_unknown_subsystem_prefix_fails_loudly() -> None:
    registry = {"ZZZ-LOGIN-001": _definition("ZZZ-LOGIN-001")}
    with pytest.raises(ValueError, match="ZZZ"):
        gen.render(registry=registry, subsystems={"AUTH"})


def test_subsystem_without_title_fails_loudly() -> None:
    registry = {"NEWSUB-LOGIN-001": _definition("NEWSUB-LOGIN-001")}
    with pytest.raises(ValueError, match="NEWSUB"):
        gen.render(registry=registry, subsystems={"NEWSUB"})


def test_key_and_definition_code_mismatch_fails_loudly() -> None:
    registry = {"AUTH-LOGIN-001": _definition("AUTH-LOGIN-002")}
    with pytest.raises(ValueError, match="AUTH-LOGIN-002"):
        gen.render(registry=registry, subsystems={"AUTH"})


def test_render_is_deterministic() -> None:
    assert gen.render() == gen.render()


def test_check_fails_on_stale_and_passes_after_generation(tmp_path: Path) -> None:
    target = tmp_path / "error-codes"
    target.mkdir()
    (target / "README.md").write_text("# hand edited\n", encoding="utf-8")
    assert gen.main(["--check"], target_dir=target) == 1
    assert gen.main([], target_dir=target) == 0
    assert gen.main(["--check"], target_dir=target) == 0
    for name, text in gen.render().items():
        assert (target / name).read_text(encoding="utf-8") == text
