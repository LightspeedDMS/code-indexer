"""Bug #1876 N8: context lines attach to the wrong match WITHIN one file.

Distinct from Defect 2 / test_regex_search_context_lines_cross_file_1876.py
(context leaking ACROSS files, already fixed via begin/end-event resets).
This is a same-file, cross-MATCH misattribution: `_process_ripgrep_context_
event` decides whether a "context" JSON event belongs to the previous
match's context_after using ONLY the boolean `can_attach_context_after`,
which stays True for the rest of the file once a match has occurred -- it
never checks whether the context line's own line_number still falls within
that match's trailing window (`match.line_number + context_lines`). With
two matches spaced further apart than `context_lines` in the same file, the
FIRST match's context_after silently swallows the SECOND match's leading
context lines too, and the second match's context_before is left empty.

Fix (per the issue): attach a context line as trailing context only when
``line_number <= last_match.line_number + context_lines``; otherwise it is
leading context for the NEXT match.

Both transcripts below are real, schema-verified ``rg --json -C 2``
recordings (not guessed) -- see docstrings for the exact repro command and
source file content.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.global_repos.regex_search import RegexSearchService

CONTEXT_LINES = 2
MAX_RESULTS = 1000


def _transcript(path: str, events: list) -> str:
    lines = [f'{{"type":"begin","data":{{"path":{{"text":"{path}"}}}}}}']
    lines.extend(events)
    lines.append(f'{{"type":"end","data":{{"path":{{"text":"{path}"}},"stats":{{}}}}}}')
    return "\n".join(lines)


def _match_event(path: str, line_number: int, text: str) -> str:
    return (
        f'{{"type":"match","data":{{"path":{{"text":"{path}"}},'
        f'"lines":{{"text":"{text}\\n"}},"line_number":{line_number},'
        f'"submatches":[{{"match":{{"text":"HIT"}},"start":0,"end":3}}]}}}}'
    )


def _context_event(path: str, line_number: int, text: str) -> str:
    return (
        f'{{"type":"context","data":{{"path":{{"text":"{path}"}},'
        f'"lines":{{"text":"{text}\\n"}},"line_number":{line_number},'
        f'"submatches":[]}}}}'
    )


def test_context_within_one_file_two_matches_far_apart(tmp_path: Path):
    """Real transcript verified against ``rg --json -C 2 -e HIT a.txt``
    over a 30-line file with matches at lines 10 and 20 (10 apart, well
    outside each other's 2-line context window). Match@10 must get
    ONLY lines 11,12 as context_after (never 18,19 -- those belong to
    match@20's leading context); match@20 must get lines 18,19 as
    context_before (never empty)."""
    path = str(tmp_path / "a.txt")
    events = [
        _context_event(path, 8, "line8"),
        _context_event(path, 9, "line9"),
        _match_event(path, 10, "HIT10"),
        _context_event(path, 11, "line11"),
        _context_event(path, 12, "line12"),
        _context_event(path, 18, "line18"),
        _context_event(path, 19, "line19"),
        _match_event(path, 20, "HIT20"),
        _context_event(path, 21, "line21"),
        _context_event(path, 22, "line22"),
    ]
    transcript = _transcript(path, events)

    svc = RegexSearchService(tmp_path)
    matches, total = svc._parse_ripgrep_json_output(
        transcript, MAX_RESULTS, CONTEXT_LINES
    )

    assert total == 2
    assert len(matches) == 2
    match_10, match_20 = matches[0], matches[1]
    assert match_10.line_number == 10
    assert match_10.context_before == ["line8", "line9"]
    assert match_10.context_after == ["line11", "line12"], (
        f"match@10 context_after is {match_10.context_after!r} -- must NOT "
        "contain line18/line19, which belong to match@20's leading context"
    )
    assert match_20.line_number == 20
    assert match_20.context_before == ["line18", "line19"], (
        f"match@20 context_before is {match_20.context_before!r} -- these "
        "lines were misattached as match@10's trailing context instead"
    )
    assert match_20.context_after == ["line21", "line22"]


def test_context_within_one_file_two_matches_adjacent_overlapping_context(
    tmp_path: Path,
):
    """Real transcript verified against ``rg --json -C 2 -e HIT a.txt``
    over a 20-line file with matches at lines 10 and 13 (3 apart --
    closer than 2*context_lines+1, so the two matches' context windows
    overlap and ripgrep emits the shared lines 11,12 exactly once).
    Correct attribution: lines 11,12 are match@10's trailing context
    (11,12 <= 10+2); match@13's context_before is therefore empty
    (nothing left to attach -- real ripgrep never re-emits 11/12), and
    match@13's own trailing context is lines 14,15."""
    path = str(tmp_path / "a.txt")
    events = [
        _context_event(path, 8, "line8"),
        _context_event(path, 9, "line9"),
        _match_event(path, 10, "HIT10"),
        _context_event(path, 11, "line11"),
        _context_event(path, 12, "line12"),
        _match_event(path, 13, "HIT13"),
        _context_event(path, 14, "line14"),
        _context_event(path, 15, "line15"),
    ]
    transcript = _transcript(path, events)

    svc = RegexSearchService(tmp_path)
    matches, total = svc._parse_ripgrep_json_output(
        transcript, MAX_RESULTS, CONTEXT_LINES
    )

    assert total == 2
    assert len(matches) == 2
    match_10, match_13 = matches[0], matches[1]
    assert match_10.context_before == ["line8", "line9"]
    assert match_10.context_after == ["line11", "line12"]
    assert match_13.context_before == [], (
        f"match@13 context_before is {match_13.context_before!r}, expected "
        "[] -- lines 11/12 were already consumed as match@10's trailing "
        "context and must not also appear here"
    )
    assert match_13.context_after == ["line14", "line15"]


def _multiline_match_event(path: str, start_line: int, lines_text: str) -> str:
    """A ripgrep --json 'match' event whose matched text spans multiple
    lines (real ripgrep --multiline behavior): "line_number" is the
    match's START line, and "lines"."text" contains the FULL matched
    text (embedded newlines, JSON-escaped as \\n)."""
    return (
        f'{{"type":"match","data":{{"path":{{"text":"{path}"}},'
        f'"lines":{{"text":"{lines_text}"}},"line_number":{start_line},'
        f'"submatches":[{{"match":{{"text":"HIT"}},"start":0,"end":3}}]}}}}'
    )


def test_context_after_multiline_match_uses_match_end_line_not_start_line(
    tmp_path: Path,
):
    """Bug #1876 round-5 finding F3: ``regex_search.py``'s trailing-
    context window was bounded by ``matches[-1].line_number +
    context_lines`` -- the match's START line -- instead of its END
    line. Repro file (1-indexed): l1, l2, START(3), mid(4), END(5),
    t1(6), t2(7). Pattern ``START\\nmid\\nEND`` (multiline=True),
    context_lines=2. The match's own text spans lines 3-5 (2 embedded
    newlines), so its trailing window must be lines 6-7 (5+2), NOT
    lines 3-5 (3+2) -- the old bound would incorrectly drop t1/t2 as
    unattached leading context for a next match that never arrives."""
    path = str(tmp_path / "a.txt")
    events = [
        _context_event(path, 1, "l1"),
        _context_event(path, 2, "l2"),
        _multiline_match_event(path, 3, "START\\nmid\\nEND"),
        _context_event(path, 6, "t1"),
        _context_event(path, 7, "t2"),
    ]
    transcript = _transcript(path, events)

    svc = RegexSearchService(tmp_path)
    matches, total = svc._parse_ripgrep_json_output(
        transcript, MAX_RESULTS, CONTEXT_LINES
    )

    assert total == 1
    assert len(matches) == 1
    match = matches[0]
    assert match.line_number == 3
    assert match.context_before == ["l1", "l2"]
    assert match.context_after == ["t1", "t2"], (
        f"match context_after is {match.context_after!r}, expected "
        "['t1', 't2'] -- the trailing window must extend from the "
        "match's END line (5), not its start line (3)"
    )
