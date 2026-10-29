from __future__ import annotations

import random
import re
import time
from typing import Callable, List
from urllib.parse import unquote, unquote_plus

import pytest

from code_indexer.server.utils.access_log_redaction import (
    redact_sensitive_query_values,
)
from code_indexer.utils.credential_redaction import (
    _percent_decode,
    _percent_decode_plus,
    mask_url_credentials,
    redact_command_output,
    redact_secret_fields,
)

_SIZE = 1_000_000
_SCALING_BASE = 125_000
# ^ why a time RATIO, not an absolute bound; 8 parallel lane chunks pushed a 0.7s linear case past 1.0s
_SCALE_FACTOR = 8
# ^ why normalised by a linear reference, not a raw ratio; full-lane load drove a raw x4 linear ratio to x9.8
# ^ why 3.0; measured linear <= 1.95 under load, quadratic >= 8.5, the x10.06@x4 auth regression ~= 4.0
_MAX_NORMALISED_RATIO = 3.0
# ^ why grow n until measurable, not skip fast cases; a quadratic with a tiny constant is ~1ms at 500k chars
_MEASURABLE_SECONDS = 0.02
_MAX_DOUBLINGS = 6
_MAX_CHARS = _SCALING_BASE * 2**_MAX_DOUBLINGS
_MAX_REFERENCE_REPS = 1024
_CPU_HANG_GUARD_SECONDS = 10.0
_WALL_HANG_GUARD_SECONDS = 120.0
_ROUNDS = 3
_REFERENCE = re.compile(r"[\x00-\x08]")
_SUPPLIED = "Example%Secret"
_ARGS = ["git", "clone", "https://example.com/r.git"]


def _repeat(unit: str, size: int) -> str:
    return unit * (size // len(unit))


TextBuilder = Callable[[int], str]

_ADVERSARIAL: dict = {
    "percent": lambda n: _repeat("%", n),
    "equals": lambda n: _repeat("=", n),
    "assignments": lambda n: _repeat("a=", n),
    "double_quotes": lambda n: _repeat('"', n),
    "single_quotes": lambda n: _repeat("'", n),
    "assignment_quotes": lambda n: 'a="' + _repeat("a=", n),
    "jwt_dots": lambda n: _repeat("eyJ.", n),
    "jwt_run": lambda n: _repeat("eyJa", n),
    "letters": lambda n: _repeat("a", n),
    "auth_headers": lambda n: _repeat("authorization:", n),
    "flags": lambda n: _repeat("-a ", n),
    "query": lambda n: "?" + _repeat("&a", n),
    "unterminated_key": lambda n: "-----BEGIN PRIVATE KEY-----" + _repeat("A", n),
    "key_label": lambda n: "-----BEGIN " + _repeat("A", n),
}

_ENTRY_POINTS: List[Callable[[str], object]] = [
    redact_secret_fields,
    mask_url_credentials,
    redact_sensitive_query_values,
    lambda text: redact_command_output(text, _ARGS, [_SUPPLIED]),
]


def _cpu_once(
    fn: Callable[[str], object], text: str, label: str, reps: int = 1
) -> float:
    wall_started = time.perf_counter()
    cpu_started = time.process_time()
    for _ in range(reps):
        fn(text)
    cpu = time.process_time() - cpu_started
    wall = time.perf_counter() - wall_started
    assert cpu < _CPU_HANG_GUARD_SECONDS, f"{label}: {cpu:.1f}s CPU"
    assert wall < _WALL_HANG_GUARD_SECONDS, f"{label}: {wall:.1f}s elapsed"
    return cpu


def _reference_reps(text: str, label: str) -> int:
    reps = 1
    while reps < _MAX_REFERENCE_REPS:
        if _cpu_once(_REFERENCE.search, text, label, reps) >= _MEASURABLE_SECONDS:
            break
        reps *= 2
    return reps


_PROBE_ROUNDS = 2
# ^ perf a quadratic case costs ~x64 the floor per round at x8; one round past twice the limit is decisive
_CERTAIN_FAILURE_FACTOR = 2.0


def _assert_scales_linearly(
    fn: Callable[[str], object], build: TextBuilder, base: int, label: str
) -> None:
    size = base
    probe = min(_cpu_once(fn, build(size), label) for _ in range(_PROBE_ROUNDS))
    for _ in range(_MAX_DOUBLINGS):
        if probe >= _MEASURABLE_SECONDS or size * 2 > _MAX_CHARS:
            break
        size *= 2
        probe = min(_cpu_once(fn, build(size), label) for _ in range(_PROBE_ROUNDS))
    assert probe >= _MEASURABLE_SECONDS, (
        f"{label}: {probe:.4f}s CPU at {size} chars (cap); the input does not "
        "exercise the code, so linear time cannot be measured"
    )
    small_text, large_text = build(size), build(size * _SCALE_FACTOR)
    reps = _reference_reps(small_text, label)
    times: dict = {"case_small": [], "case_large": [], "ref_small": [], "ref_large": []}

    def fail_if(limit: float) -> None:
        case_ratio = min(times["case_large"]) / min(times["case_small"])
        reference_ratio = min(times["ref_large"]) / min(times["ref_small"])
        normalised = case_ratio / reference_ratio
        assert normalised < limit, (
            f"{label}: x{_SCALE_FACTOR} input took x{case_ratio:.1f} CPU time "
            f"against x{reference_ratio:.1f} for a linear pass (normalised "
            f"x{normalised:.2f}) at {size} chars"
        )

    # ^ order case and reference interleaved per round; both then see the same machine load
    for _ in range(_ROUNDS):
        times["case_small"].append(_cpu_once(fn, small_text, label))
        times["ref_small"].append(_cpu_once(_REFERENCE.search, small_text, label, reps))
        times["case_large"].append(_cpu_once(fn, large_text, label))
        times["ref_large"].append(_cpu_once(_REFERENCE.search, large_text, label, reps))
        fail_if(_MAX_NORMALISED_RATIO * _CERTAIN_FAILURE_FACTOR)
    fail_if(_MAX_NORMALISED_RATIO)


@pytest.mark.parametrize("name", sorted(_ADVERSARIAL))
@pytest.mark.parametrize("entry", range(len(_ENTRY_POINTS)))
def test_adversarial_input_redacts_in_linear_time(name: str, entry: int) -> None:
    _assert_scales_linearly(
        _ENTRY_POINTS[entry], _ADVERSARIAL[name], _SCALING_BASE, name
    )


_FIND_STRIDE = 2048


def _strided_repeated_find(text: str) -> None:
    for start in range(0, len(text), _FIND_STRIDE):
        text.find("x", start)


_BACKTRACKING = re.compile(r"=+x")
_LINEAR_SUB = re.compile(r"=")
# ^ see git:e1700c322; the two pre-fix production patterns the linear-time fix replaced
_PRE_FIX_AUTH = re.compile(r"(?i)(authorization\s*[:=]\s*)(\S+)\s+(\S+)")
_PRE_FIX_JWT = re.compile(r"eyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]*")


def _equals(n: int) -> str:
    return "=" * n


@pytest.mark.parametrize(
    "fn, build, base, rejected_with",
    # ^ why match "CPU", not "CPU time"; under load a quadratic's 8n run trips the CPU hang guard first
    # ^ perf each rejection case = one 8n quadratic run ~= 64x the floor; 3s idle, 10.9s under load => slow lane
    [
        pytest.param(
            _strided_repeated_find,
            _equals,
            _SCALING_BASE,
            "CPU",
            id="fast_quadratic_find",
            marks=pytest.mark.slow,
        ),
        pytest.param(
            _BACKTRACKING.search,
            _equals,
            4_000,
            "CPU",
            id="backtracking_regex",
            marks=pytest.mark.slow,
        ),
        pytest.param(
            lambda text: _PRE_FIX_AUTH.sub("x", text),
            _ADVERSARIAL["auth_headers"],
            4_000,
            "CPU",
            id="pre_fix_auth_regex",
            marks=pytest.mark.slow,
        ),
        pytest.param(
            lambda text: _PRE_FIX_JWT.sub("x", text),
            _ADVERSARIAL["jwt_run"],
            4_000,
            "CPU",
            id="pre_fix_jwt_regex",
            marks=pytest.mark.slow,
        ),
        pytest.param(
            lambda text: None, _equals, _SCALING_BASE, "does not exercise", id="no_op"
        ),
        pytest.param(
            lambda text: _LINEAR_SUB.sub("-", text),
            _equals,
            _SCALING_BASE,
            None,
            id="linear_sub",
        ),
    ],
)
def test_scaling_check_rejects_quadratic_and_accepts_linear(
    fn: Callable[[str], object], build: TextBuilder, base: int, rejected_with: str
) -> None:
    def check() -> None:
        _assert_scales_linearly(fn, build, base, "self-check")

    if rejected_with is None:
        check()
    else:
        with pytest.raises(AssertionError, match=rejected_with):
            check()


@pytest.mark.parametrize(
    "secret, form",
    [
        (_SUPPLIED, _SUPPLIED),
        (_SUPPLIED, "Example%25Secret"),
        (_SUPPLIED, "Example%2525Secret"),
        ("Example/Secret", "Example/Secret"),
        ("Example/Secret", "Example%2FSecret"),
        ("Example/Secret", "Example%2fSecret"),
        ("Example/Secret", "Example%252FSecret"),
    ],
)
def test_long_tokens_still_mask_supplied_secret(secret: str, form: str) -> None:
    padding = "x" * 10_000
    text = f"failed: {padding}{form}{padding} end"

    redacted = redact_command_output(text, _ARGS, [secret])

    assert form not in redacted
    assert redacted.startswith("failed: ")
    assert redacted.endswith(" end")


_PARTLY_ENCODED = "Example%2f%53ecret"


def _partly_encoded_text(length: int) -> str:
    token = "x" * (length - len(_PARTLY_ENCODED)) + _PARTLY_ENCODED
    return f"failed: {token} end"


def _mask_slash_secret(text: str) -> str:
    return str(redact_command_output(text, _ARGS, ["Example/Secret"]))


@pytest.mark.parametrize("length", [4_095, 4_096, 4_097, _SIZE])
def test_supplied_secret_masked_in_partly_encoded_token_of_any_length(
    length: int,
) -> None:
    redacted = _mask_slash_secret(_partly_encoded_text(length))

    assert "ecret" not in redacted
    assert redacted.startswith("failed: ")
    assert redacted.endswith(" end")
    if length == _SIZE:
        _assert_scales_linearly(
            _mask_slash_secret,
            _partly_encoded_text,
            _SIZE // _SCALE_FACTOR,
            "partly encoded token",
        )


def test_percent_decoders_match_stdlib_unquote() -> None:
    crafted = [
        "",
        "%",
        "%%",
        "%4",
        "%4g",
        "%2f%53ecret",
        "%2F%2f",
        "%C3%A9",
        "%c3%a9x",
        "%C3x%A9",
        "%E2%82",
        "%E2%82%",
        "%FF%FE",
        "é%41ü",
        "%C3é",
        "a+b%2B%2b+",
        "%%41%2541",
    ]
    rng = random.Random(1234)
    alphabet = ["%", "2", "f", "F", "C", "3", "a", "9", "+", "x", "é"]
    fuzzed = [
        "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 24)))
        for _ in range(2_000)
    ]

    for text in crafted + fuzzed:
        assert _percent_decode(text) == unquote(text), repr(text)
        assert _percent_decode_plus(text) == unquote_plus(text), repr(text)
