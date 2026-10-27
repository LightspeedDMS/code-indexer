"""Bug #2212: the server's X-Ray extension map matches the Rust engine's source.

Pure source checks: no tree-sitter needed, so these always run.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import List, Set

import pytest

from code_indexer.xray.languages import EXTENSION_MAP

_LANGUAGES_RS = (
    Path(__file__).resolve().parents[3] / "rust" / "xray-core" / "src" / "languages.rs"
)
_QUOTED = r'"[^"\\]+"'
_ARM_PATTERN = re.compile(rf"((?:{_QUOTED}\s*\|\s*)*{_QUOTED}|_)\s*=>")


# ^ why no char-literal handling; `'static` lifetimes would read as one; a `'"'` literal => strict parsers raise
def strip_rust_comments(source: str) -> str:
    out: List[str] = []
    i, depth, in_string = 0, 0, False
    while i < len(source):
        pair = source[i : i + 2]
        if depth:
            if pair == "*/":
                depth, i = depth - 1, i + 2
            elif pair == "/*":
                depth, i = depth + 1, i + 2
            else:
                i += 1
        elif in_string:
            out.append(pair if source[i] == "\\" else source[i])
            in_string = source[i] != '"'
            i += 2 if source[i] == "\\" else 1
        elif pair == "//":
            end = source.find("\n", i)
            i = len(source) if end < 0 else end
        elif pair == "/*":
            depth, i = 1, i + 2
        else:
            in_string = source[i] == '"'
            out.append(source[i])
            i += 1
    if depth or in_string:
        raise ValueError("unterminated comment or string")
    return "".join(out)


def supported_extensions_from(source: str) -> Set[str]:
    body = re.search(
        r"fn supported_extensions\(\)[^{]*\{\s*&\[(?P<items>.*?)\]\s*\}",
        strip_rust_comments(source),
        re.DOTALL,
    )
    if body is None:
        raise ValueError("supported_extensions() not found")
    items = [item.strip() for item in body.group("items").split(",")]
    if items and items[-1] == "":
        items.pop()
    bad = [item for item in items if not re.fullmatch(_QUOTED, item)]
    if bad or not items:
        raise ValueError(f"unexpected supported_extensions() entries: {bad}")
    return {item.strip('"') for item in items}


def match_arm_extensions_from(source: str) -> Set[str]:
    code = strip_rust_comments(source)
    start = re.search(
        r"fn language_for_extension\b[^{]*\{\s*match (?P<scrutinee>[^{]+?)\s*\{", code
    )
    if start is None:
        raise ValueError("`match` in language_for_extension() not found")
    if start.group("scrutinee") != "ext.to_ascii_lowercase().as_str()":
        raise ValueError(f"lookup must lowercase ext, got {start.group('scrutinee')!r}")
    depth, end = 1, start.end()
    while depth and end < len(code):
        depth += {"{": 1, "}": -1}.get(code[end], 0)
        end += 1
    if depth:
        raise ValueError("unterminated `match ext` block")
    body = code[start.end() : end - 1]
    arms = list(_ARM_PATTERN.finditer(body))
    for arm in arms:
        before = body[: arm.start()].rstrip()
        if before and before[-1] not in ",{}":
            raise ValueError(f"unexpected match arm syntax before {arm.group(0)!r}")
    patterns = [arm.group(1) for arm in arms]
    if len(arms) != body.count("=>") or patterns.count("_") != 1:
        raise ValueError("unexpected match arm syntax in language_for_extension()")
    return {ext for p in patterns if p != "_" for ext in re.findall(r'"([^"]+)"', p)}


def _list_source(items: str) -> str:
    return (
        "pub fn supported_extensions() -> &'static [&'static str] {\n"
        f"    &[\n{items}\n    ]\n}}\n"
    )


def _match_source(
    arms: str, scrutinee: str = "ext.to_ascii_lowercase().as_str()"
) -> str:
    return (
        "pub fn language_for_extension(ext: &str) -> Option<Language> {\n"
        f"    match {scrutinee} {{\n{arms}\n        _ => None,\n    }}\n}}\n"
    )


class TestSupportedExtensionsParser:
    def test_line_comment_entry_is_not_counted(self) -> None:
        source = _list_source('        "java", "py",\n        // "rs",')
        assert supported_extensions_from(source) == {"java", "py"}

    def test_block_comment_entry_is_not_counted(self) -> None:
        source = _list_source('        "java", /* "rs", */ "py",')
        assert supported_extensions_from(source) == {"java", "py"}

    def test_unexpected_list_syntax_fails(self) -> None:
        source = _list_source('        "java", EXTRA_EXTENSIONS, "py",')
        with pytest.raises(ValueError):
            supported_extensions_from(source)


class TestMatchArmParser:
    def test_active_arms_are_extracted(self) -> None:
        source = _match_source(
            '        "java" => Some(a),\n'
            '        // "rs" => Some(b),\n'
            '        "kt" | "kts" => {\n            Some(c)\n        }\n'
        )
        assert match_arm_extensions_from(source) == {"java", "kt", "kts"}

    def test_guarded_arm_fails(self) -> None:
        source = _match_source('        "java" if strict => Some(a),')
        with pytest.raises(ValueError):
            match_arm_extensions_from(source)

    def test_case_sensitive_lookup_fails(self) -> None:
        source = _match_source('        "java" => Some(a),', scrutinee="ext")
        with pytest.raises(ValueError, match="lowercase"):
            match_arm_extensions_from(source)


class TestRustAndPythonExtensionSetsAgree:
    def test_match_arms_list_and_python_map_are_equal(self) -> None:
        source = _LANGUAGES_RS.read_text(encoding="utf-8")
        arms = match_arm_extensions_from(source)
        listed = supported_extensions_from(source)
        python = {ext.lstrip(".") for ext in EXTENSION_MAP}
        for name, left, right in (
            ("match arms vs supported_extensions", arms, listed),
            ("supported_extensions vs EXTENSION_MAP", listed, python),
        ):
            assert left == right, (
                f"{name}: only left {sorted(left - right)}, "
                f"only right {sorted(right - left)}"
            )

    def test_listed_extensions_are_lowercase(self) -> None:
        source = _LANGUAGES_RS.read_text(encoding="utf-8")
        listed = (
            match_arm_extensions_from(source)
            | supported_extensions_from(source)
            | set(EXTENSION_MAP)
        )
        assert sorted(e for e in listed if e != e.lower()) == []
