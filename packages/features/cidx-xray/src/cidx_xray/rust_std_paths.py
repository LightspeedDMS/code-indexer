from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

# ^ coupled PERMITTED_STD_PATHS with rust/xray-core/src/validator/std_paths.rs:PERMITTED_STD_PATHS; test:python_mirror_matches_permitted_std_paths
PERMITTED_STD_PATHS: Tuple[str, ...] = (
    "std::collections", "std::fmt", "std::iter", "std::cmp", "std::str", "std::string", "std::vec",
    "std::option", "std::result", "std::ops", "std::convert", "std::char", "std::num", "std::hash",
    "std::borrow", "std::default", "std::boxed", "std::rc", "std::cell", "std::array", "std::slice",
    "std::marker", "std::primitive", "std::f32", "std::f64", "std::i8", "std::i16", "std::i32", "std::i64",
    "std::i128", "std::u8", "std::u16", "std::u32", "std::u64", "std::u128", "std::isize", "std::usize",
    "std::error", "std::any",
    "core::fmt", "core::iter", "core::cmp", "core::str", "core::option", "core::result", "core::ops",
    "core::convert", "core::char", "core::num", "core::hash", "core::borrow", "core::default", "core::cell",
    "core::array", "core::slice", "core::marker", "core::primitive", "core::f32", "core::f64", "core::i8",
    "core::i16", "core::i32", "core::i64", "core::i128", "core::u8", "core::u16", "core::u32", "core::u64",
    "core::u128", "core::isize", "core::usize", "core::error", "core::any",
    "alloc::vec", "alloc::string", "alloc::collections", "alloc::boxed", "alloc::rc",
)  # fmt: skip

_STD_ROOTS = frozenset({"std", "core", "alloc"})
_IDENT = r"(?:r#)?[A-Za-z_][A-Za-z0-9_]*"
_PATH_PATTERN = re.compile(rf"(?:::\s*)?{_IDENT}(?:\s*::\s*{_IDENT})*")
_USE_PATTERN = re.compile(r"\buse\b([^;]*);")
_USE_TOKEN = re.compile(rf"\s*(::|[{{}},*]|{_IDENT})")

_Leaf = Tuple[List[str], bool]


def _canonical(token: str) -> str:
    return token[2:] if token.startswith("r#") else token


def _violation(segments: List[str], renamed: bool) -> Optional[str]:
    if not segments or segments[0] not in _STD_ROOTS:
        return None
    joined = "::".join(segments)
    permitted = any(
        joined == p or joined.startswith(p + "::") for p in PERMITTED_STD_PATHS
    )
    if renamed or not permitted:
        return "::".join(segments[:2])
    return None


def _tokenize_use_body(body: str) -> Optional[List[str]]:
    tokens: List[str] = []
    pos = 0
    while body[pos:].strip():
        match = _USE_TOKEN.match(body, pos)
        if match is None:
            return None
        tokens.append(match.group(1))
        pos = match.end()
    return tokens


# ^ invariant _violation reads only the first _VERDICT_SEGMENTS segments => leaves keep only those
_VERDICT_SEGMENTS = max(p.count("::") + 1 for p in PERMITTED_STD_PATHS)


# ^ why malformed use tree => [] (no Python verdict), not a rejection; rust/xray-core/src/validator/std_paths.rs is authoritative
# ^ invariant every step consumes >= 1 token => loop runs <= len(tokens) times; no recursion on path length or group depth
# ^ perf leaf copy = O(_VERDICT_SEGMENTS), not O(prefix); walk stays linear in tokens
def _use_tree_leaves(body: str) -> List[_Leaf]:
    tokens = _tokenize_use_body(body)
    if not tokens:
        return []
    leaves: List[_Leaf] = []
    prefix: List[str] = []
    group_bases: List[int] = []
    state = "tree"
    i, n = 0, len(tokens)
    while i < n:
        token = tokens[i]
        if state == "after" or (state == "member" and token == "}"):
            if not group_bases or token not in (",", "}"):
                return []
            if token == ",":
                del prefix[group_bases[-1] :]
                state = "member"
            else:
                del prefix[group_bases.pop() :]
                state = "after"
            i += 1
            continue
        if token == "::":
            i += 1
            if i == n:
                return []
            token = tokens[i]
        if token in ("}", ","):
            return []
        i += 1
        head = prefix[:_VERDICT_SEGMENTS]
        if token == "{":
            group_bases.append(len(prefix))
            state = "member"
        elif token == "*":
            leaves.append((head, False))
            state = "after"
        elif i < n and tokens[i] == "::":
            prefix.append(_canonical(token))
            state = "tree"
            i += 1
        else:
            name = _canonical(token)
            renamed = i < n and tokens[i] == "as"
            leaf = head if name == "self" else (head + [name])[:_VERDICT_SEGMENTS]
            leaves.append((leaf, renamed))
            state = "after"
            i += 2 if renamed else 0
    complete = state == "after" and not group_bases and i == n
    return leaves if complete else []


# ^ invariant scannable_code = output of code_indexer/xray/sandbox.py:_blank_out_strings_and_comments
def find_std_path_violations(scannable_code: str) -> Dict[int, str]:
    violations: Dict[int, str] = {}
    for match in _USE_PATTERN.finditer(scannable_code):
        line = scannable_code.count("\n", 0, match.start()) + 1
        for segments, renamed in _use_tree_leaves(match.group(1)):
            construct = _violation(segments, renamed)
            if construct is not None:
                violations.setdefault(line, construct)
                break
    for lineno, text in enumerate(scannable_code.splitlines(), start=1):
        for match in _PATH_PATTERN.finditer(text):
            segments = [
                _canonical(s.strip()) for s in match.group(0).lstrip(": \t").split("::")
            ]
            construct = _violation(segments, False) if len(segments) >= 2 else None
            if construct is not None:
                violations.setdefault(lineno, construct)
                break
    return violations
