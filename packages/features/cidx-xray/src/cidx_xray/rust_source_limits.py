from __future__ import annotations

from typing import Optional, Tuple

# ^ coupled both limits with rust/xray-core/src/validator/source_limits.rs; test:python_mirror_matches_source_limits
MAX_EVALUATOR_SOURCE_BYTES = 131_072
MAX_BRACKET_DEPTH = 64

_OPENERS = frozenset("{([")
_CLOSERS = frozenset("})]")


# ^ order find_source_limit_violation before any regex or use-tree walk; returns (error_code, line, reason)
def find_source_limit_violation(code: str) -> Optional[Tuple[str, int, str]]:
    # ^ external Rust measures str::len() in UTF-8 bytes; surrogatepass keeps lone surrogates countable
    size = len(code.encode("utf-8", "surrogatepass"))
    if size > MAX_EVALUATOR_SOURCE_BYTES:
        return (
            "source_too_large",
            1,
            f"evaluator source is {size} bytes, over the maximum of "
            f"{MAX_EVALUATOR_SOURCE_BYTES} bytes",
        )
    depth = 0
    line = 1
    for char in code:
        if char == "\n":
            line += 1
        elif char in _OPENERS:
            depth += 1
            if depth > MAX_BRACKET_DEPTH:
                return (
                    "nesting_too_deep",
                    line,
                    f"bracket nesting depth exceeds the maximum of {MAX_BRACKET_DEPTH} "
                    "(`{`, `(` and `[` counted together)",
                )
        elif char in _CLOSERS and depth > 0:
            depth -= 1
    return None
