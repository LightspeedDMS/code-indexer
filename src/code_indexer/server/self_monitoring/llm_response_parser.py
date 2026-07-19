"""
Robust JSON extraction from noisy LLM (Claude CLI) responses.

Why this exists
---------------
pace-maker is installed on every cidx-server cluster node, so the server's own
``claude -p`` invocations get a telemetry preamble injected at byte 0, e.g.::

    § △0.0 ◎surg ■other ◇1.0 ↻1

(and sometimes a ``Warning: no stdin data received ...`` line, or markdown
``json`` code fences). A leading ``§`` makes a bare ``json.loads`` raise exactly
``Expecting value: line 1 column 1 (char 0)`` -- which is precisely how the
staging self-monitoring scans failed (6 of 8 recent runs).

This module provides a single reusable helper that strips that noise and
returns the real JSON payload, while still raising a clear, loud error for a
genuinely empty/garbage response (MESSI rule #13, anti-silent-failure -- a
failed scan must NEVER be silently treated as a success).

The line-stripping mirrors the spirit of pace-maker's own ``_strip_llm_noise``
(drop lines whose first non-space character is ``§``).
"""

import json
from typing import Any, Iterator, Optional

# Prefix of the pace-maker telemetry preamble line.
_PACEMAKER_PREFIX = "§"

# Prefix of the occasional Claude CLI stderr-merged warning line.
_WARNING_PREFIX = "warning:"


def _strip_noise_lines(text: str) -> str:
    """Drop pace-maker ``§`` telemetry lines and ``Warning:`` prose lines.

    Mirrors pace-maker's ``_strip_llm_noise``: a line is noise if its first
    non-space character starts the ``§`` telemetry marker, or if it begins
    (case-insensitively) with ``Warning:``.
    """
    kept = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith(_PACEMAKER_PREFIX):
            continue
        if stripped.lower().startswith(_WARNING_PREFIX):
            continue
        kept.append(line)
    return "\n".join(kept)


def _strip_code_fences(text: str) -> str:
    """Remove leading/trailing markdown code fences (``` or ```json)."""
    lines = text.splitlines()
    # Drop the first fence line if present (``` optionally followed by a lang).
    if lines and lines[0].strip().startswith("```"):
        lines = lines[1:]
    # Drop a trailing closing fence if present.
    if lines and lines[-1].strip().startswith("```"):
        lines = lines[:-1]
    return "\n".join(lines)


def _iter_balanced_json_candidates(text: str) -> Iterator[str]:
    """Yield every balanced top-level ``{...}``/``[...]`` substring in order.

    String-literal aware: braces/brackets inside double-quoted JSON strings
    (honoring backslash escapes) do NOT affect nesting depth, so a ``}`` inside
    a string value cannot prematurely close the object -- this applies to
    EVERY candidate scan, not just the first.

    Scans for successive opening ``{``/``[`` positions. When an opener is
    found and its matching closer is located (depth returns to zero), the
    balanced span is yielded and scanning resumes for the next candidate
    starting just past that span's end -- so nested/overlapping spans are
    never yielded as separate top-level candidates.

    When an opener has no matching closer by the end of the text (a stray,
    unbalanced fragment -- e.g. a partial markdown reference like ``[ref]``
    followed later by real JSON), that broken candidate is skipped: scanning
    simply resumes at the very next character position looking for another
    opening bracket/brace. This is what lets a real JSON payload later in the
    same string still be found even when an earlier stray bracket/brace
    fragment can never be closed.
    """
    open_to_close = {"{": "}", "[": "]"}
    n = len(text)
    pos = 0

    while pos < n:
        start = -1
        opener = ""
        for i in range(pos, n):
            if text[i] in open_to_close:
                start = i
                opener = text[i]
                break

        if start == -1:
            return

        closer = open_to_close[opener]
        depth = 0
        in_string = False
        escaped = False
        end = -1
        for i in range(start, n):
            ch = text[i]
            if in_string:
                if escaped:
                    escaped = False
                elif ch == "\\":
                    escaped = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == opener:
                depth += 1
            elif ch == closer:
                depth -= 1
                if depth == 0:
                    end = i
                    break

        if end == -1:
            # Unbalanced -- skip this broken candidate, keep scanning.
            pos = start + 1
            continue

        yield text[start : end + 1]
        pos = end + 1


def extract_json_from_llm_response(text: str) -> Any:
    """Extract and parse the JSON payload from a noisy LLM response.

    Strips pace-maker ``§`` telemetry lines, ``Warning:`` prose lines, and
    markdown code fences, then tries EVERY balanced top-level JSON
    object/array candidate found in the remaining text, in order, and
    returns the parsed result of the first one that actually parses via
    ``json.loads``.

    A response can contain a stray leading bracket/brace fragment that is
    NOT on its own clean line (so the line-based noise stripping above
    cannot remove it) -- e.g. a partial markdown reference like ``[ref]`` --
    followed later by a fully valid JSON payload. Locking onto only the
    first balanced span would wrongly fail on such input, so every
    candidate is tried before giving up (issue #1436).

    Args:
        text: Raw stdout captured from a Claude CLI invocation.

    Returns:
        The parsed JSON value (typically a ``dict``).

    Raises:
        ValueError: if ``text`` is empty/whitespace, contains no balanced
            JSON object/array candidate, or none of the candidates found are
            valid JSON. A failed/garbage response is reported loudly --
            never silently coerced into a success.
        TypeError: if ``text`` is not a string.
    """
    if not isinstance(text, str):
        raise TypeError(f"Expected str LLM response, got {type(text).__name__}")

    if not text.strip():
        raise ValueError("Empty LLM response: no JSON payload to parse")

    cleaned = _strip_noise_lines(text)
    cleaned = _strip_code_fences(cleaned)

    if not cleaned.strip():
        raise ValueError(
            "LLM response contained only telemetry/preamble noise, no JSON payload"
        )

    candidates = list(_iter_balanced_json_candidates(cleaned))

    if not candidates:
        raise ValueError("No JSON object or array found in LLM response")

    last_error: Optional[json.JSONDecodeError] = None
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError as e:
            last_error = e
            continue

    raise ValueError(
        f"Extracted LLM payload is not valid JSON: tried {len(candidates)} "
        f"candidate(s), none parsed; last error: {last_error}"
    )
