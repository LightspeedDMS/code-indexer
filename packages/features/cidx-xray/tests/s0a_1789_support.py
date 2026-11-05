"""Shared constants and engine helpers for the S0a X-Ray tests (story #1789).

Consumed by ``test_s0a_engine_caps_1789.py`` and
``test_s0a_engine_timeout_1789.py``; fixtures live in
``s0a_1789_fixtures.py``. Deliberately not named ``test_*`` so pytest does
not collect it as a test module.

Anti-mock posture for both consumers: no part of the system under test or its
collaborators is mocked, stubbed or replaced. Every test drives the real
two-phase pipeline -- real ripgrep for Phase 1, and a real rustc compile of a
real Rust evaluator executed by the real ``xray-cli`` subprocess for Phase 2.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Dict

# ---------------------------------------------------------------------------
# Timing budgets.
#
# Centralised and environment-overridable so a slower CI host can widen them
# without editing code.
# ---------------------------------------------------------------------------


def _env_int(name: str, default: int) -> int:
    """Read a positive integer budget from the environment, else `default`.

    A malformed or non-positive environment value falls back to `default`
    rather than failing the whole suite over an operator typo.

    Raises:
        TypeError: if `name` is not a str or `default` is not an int.
        ValueError: if `name` is empty or `default` is not positive.
    """
    if not isinstance(name, str):
        raise TypeError(f"name must be a str, got {type(name).__name__}")
    if not name.strip():
        raise ValueError("name must be a non-empty environment variable name")
    if not isinstance(default, int) or isinstance(default, bool):
        raise TypeError(f"default must be an int, got {type(default).__name__}")
    if default <= 0:
        raise ValueError(f"default must be > 0, got {default}")

    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return value if value > 0 else default


#: Budget for runs that must NOT time out. Large enough that a slow machine
#: (cold rustc evaluator compile) still completes well within it.
#: Override with XRAY_S0A_GENEROUS_TIMEOUT_SECONDS.
GENEROUS_TIMEOUT_SECONDS = _env_int("XRAY_S0A_GENEROUS_TIMEOUT_SECONDS", 120)

#: Budget for runs that MUST time out during Phase 2. Long enough for xray-cli
#: to compile the evaluator and begin executing it, short enough to keep each
#: timeout test under the project's 10s "investigate" threshold.
#: Override with XRAY_S0A_PHASE2_TIMEOUT_SECONDS.
PHASE2_TIMEOUT_SECONDS = _env_int("XRAY_S0A_PHASE2_TIMEOUT_SECONDS", 5)

#: Fixed iteration count for the deliberately-slow Phase 2 evaluator. Bounded
#: (never an open `loop`), but large enough that even a fully optimised build
#: cannot finish within PHASE2_TIMEOUT_SECONDS.
#: Override with XRAY_S0A_SLOW_EVALUATOR_ITERATIONS.
SLOW_EVALUATOR_ITERATIONS = _env_int(
    "XRAY_S0A_SLOW_EVALUATOR_ITERATIONS", 40_000_000_000
)

# ---------------------------------------------------------------------------
# Real Rust evaluators
# ---------------------------------------------------------------------------

#: Reports one finding per method declaration.
RUST_EVALUATOR = """
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
    let mut findings = Vec::new();
    for m in node.descendants_of_kind("method_declaration") {
        findings.push(EvalFinding {
            pattern: "method".to_string(),
            line: m.start_line,
            snippet: String::new(),
        });
    }
    findings
}
"""

#: Deliberately slow but STATICALLY BOUNDED evaluator used to force a genuine
#: Phase 2 subprocess timeout. The loop runs a fixed
#: SLOW_EVALUATOR_ITERATIONS times and then returns, so termination is
#: provable; the xorshift mix makes each step depend on the previous one so
#: the optimiser cannot collapse it to a closed form, and the accumulator is
#: returned so the loop cannot be eliminated as dead code.
RUST_EVALUATOR_BOUNDED_SLOW = f"""
fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{
    let mut n: u64 = (node.kind.len() as u64) | 1;
    for _ in 0..{SLOW_EVALUATOR_ITERATIONS}u64 {{
        n ^= n << 13;
        n ^= n >> 7;
        n ^= n << 17;
    }}
    vec![EvalFinding {{
        pattern: n.to_string(),
        line: node.start_line,
        snippet: String::new(),
    }}]
}}
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def write_java_files(root: Path, count: int) -> None:
    """Create `count` real .java files under `root`, each with a method.

    Each file ``F<i>.java`` declares a ``target<i>()`` method calling
    ``helper()``, so it matches the shared ``target`` driver regex and yields
    exactly one finding from RUST_EVALUATOR.

    Raises:
        TypeError: if `root` is not a Path or `count` is not an int.
        ValueError: if `count` is negative or `root` is not a directory.
    """
    if not isinstance(root, Path):
        raise TypeError(f"root must be a Path, got {type(root).__name__}")
    if not isinstance(count, int) or isinstance(count, bool):
        raise TypeError(f"count must be an int, got {type(count).__name__}")
    if count < 0:
        raise ValueError(f"count must be >= 0, got {count}")
    if not root.is_dir():
        raise ValueError(f"root must be an existing directory, got {root}")

    for i in range(count):
        (root / f"F{i}.java").write_text(
            f"class F{i} {{\n  void target{i}() {{ helper(); }}\n}}\n"
        )


def run_engine(engine: Any, repo: Path, **kwargs: Any) -> Dict[str, Any]:
    """Run the engine with the shared defaults these tests care about.

    `engine` is typed `Any` and the overrides are `**kwargs` deliberately.
    This helper is a thin pass-through to ``XRaySearchEngine.run``, whose
    keyword surface is large (driver_regex, evaluator_code, search_target,
    pcre2, case_sensitive, max_files, timeout_seconds, on_process_spawned,
    ...) and differs per test. Re-declaring that signature here would
    duplicate the production one and silently drift from it; forwarding
    verbatim keeps ``run()`` itself the single authority on what is accepted,
    including rejecting unknown keywords and validating its own values (a
    behaviour two of the tests assert directly). The structural
    preconditions this helper can meaningfully own are checked below.

    Raises:
        TypeError: if `repo` is not a Path or `engine` has no callable `run`.
    """
    if not isinstance(repo, Path):
        raise TypeError(f"repo must be a Path, got {type(repo).__name__}")
    if not callable(getattr(engine, "run", None)):
        raise TypeError(
            f"engine must expose a callable run(), got {type(engine).__name__}"
        )

    params: Dict[str, Any] = {
        "repo_path": repo,
        "driver_regex": "target",
        "evaluator_code": RUST_EVALUATOR,
        "search_target": "content",
        "timeout_seconds": GENEROUS_TIMEOUT_SECONDS,
    }
    params.update(kwargs)
    # Annotated local rather than a bare `return engine.run(...)`: `engine` is
    # `Any`, so returning its call result directly is a mypy no-any-return.
    result: Dict[str, Any] = engine.run(**params)
    return result
