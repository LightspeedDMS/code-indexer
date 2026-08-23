"""
Regex search service for global repository file pattern matching.

Provides ripgrep-style regex search with grep fallback for searching
directly against files on disk in global repositories.

Module-size note (disclosed to reviewers in Issue #1601 round 3/4, now
recorded in-code per both reviewers' feedback that the disclosure lived
only in review commentary, not here): this module is ~1600+ lines,
comfortably over this project's own 500-line-per-module guideline
(CLAUDE.md Messi Rule #6, Anti-File-Bloat). It has been deliberately left
as a single module rather than split during the #1601 remediation series
-- the remediation's own scope is already large (subprocess supervision,
bounded reads, per-match content bounds, event-loop offload, trigram
pre-filtering) and a structural split is a separate, higher-risk
refactor better done on its own, with its own dedicated review, rather
than bundled into a bug-fix series. A future split should keep
``_BoundedLineReader`` and the module-level bounding helpers
(``_bounded_match_content``, the lazy trigram-build helpers) as
standalone units, and ``RegexSearchService`` split along its two engine
backends (ripgrep vs grep/python-multiline).
"""

import json
import logging
import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, List, Optional, Tuple, Union, cast

import anyio.to_thread

from code_indexer.server.services.subprocess_executor import (
    SubprocessExecutor,
    ExecutionStatus,
)

logger = logging.getLogger(__name__)

# Default timeout for search operations (5 minutes)
DEFAULT_SEARCH_TIMEOUT_SECONDS = 300

# Issue #1601: hard byte-size backstop on how much ripgrep/grep output this
# service will ever read/parse into memory for a single call, independent
# of max_results. Applied both at the write side (SubprocessExecutor kills
# the subprocess if its output file crosses this while still running -- see
# Fix direction 4a) and at the read side (the bounded line reader below
# stops pulling further chunks once this many bytes have been consumed).
# 64 MiB comfortably exceeds any legitimate single-call output while
# staying far below a size that could threaten server memory even
# multiplied across a fan-out request at production's ~900-repo scale.
#
# AC-A7 finding (recorded here per the issue's requirement): on this
# development machine -- a representative Linux deployment target -- the
# directory returned by ``tempfile.gettempdir()`` ("/tmp") is backed by
# XFS on ``/dev/mapper/rl-root`` (verified via ``df -T "$(python3 -c
# 'import tempfile; print(tempfile.gettempdir())')"``), NOT tmpfs. The temp
# file itself is therefore disk-backed: the unbounded *write* does not, by
# itself, inflate process RAM the way the unbounded *read* this issue fixes
# does. This makes early subprocess termination (Fix direction 4a) a
# strong additional safety margin here (bounds worst-case disk I/O and CPU,
# and shortens how long a runaway pattern keeps a worker busy) rather than
# the single load-bearing mitigation it would have to be on a deployment
# where TMPDIR is tmpfs (RAM-backed) -- on such a deployment, the write
# itself would already be consuming RAM before any read occurs, making 4a
# strictly mandatory rather than a margin. Both mechanisms are implemented
# unconditionally below regardless of this finding, since a different
# deployment target could legitimately have a tmpfs TMPDIR and this code
# must not assume otherwise.
_MAX_READ_BYTES = 64 * 1024 * 1024  # 64 MiB

# Chunk size used by _BoundedLineReader when streaming temp-file output, in
# raw bytes (the file is opened in binary mode so this bound is exact, not
# a text-mode "characters" approximation).
_READ_CHUNK_BYTES = 256 * 1024  # 256 KiB

# Issue #1601 remediation round 4 (Priority 2): bounding the READ
# (_MAX_READ_BYTES above) does not, by itself, bound the RESULT -- a
# single multiline match's ``m.group(0)`` can span nearly the entire read
# buffer (up to _MAX_READ_BYTES), and a single ripgrep/grep-reported
# "line" is not itself bounded by anything shorter than that same ceiling
# either (a file with no newlines reports its entire content as one
# "line"). Both would otherwise be retained verbatim as
# ``RegexMatch.line_content``/``context_before``/``context_after`` -- the
# exact per-call memory-exhaustion risk this issue exists to close, just
# moved from "the read" to "the result". 256 KiB is comfortably larger
# than any legitimate single line/match a human would want to read in a
# search result, while being negligible multiplied across max_results.
# Applied at every call site that constructs a RegexMatch's line_content
# or a context_before/context_after entry: _process_ripgrep_match_event,
# _process_ripgrep_context_event, _process_grep_match_line,
# _process_grep_context_line, and _scan_multiline_content.
_MAX_MATCH_CONTENT_BYTES = 256 * 1024  # 256 KiB

# Issue #1601 remediation round 5 (Priority 1, Codex Critical finding):
# bounding a single match's content (_MAX_MATCH_CONTENT_BYTES above) does
# not bound the AGGREGATE across every match + context line accumulated
# in ONE search() call. A request with a large max_results and non-trivial
# context_lines could still accumulate far more memory than any single
# safety budget -- e.g. 1000 matches x several context lines x up to
# 256 KiB each could reach hundreds of MB to low GB in the worst case --
# the exact class of risk #1601 exists to close, just redistributed
# across many objects instead of one. 8 MiB is comfortably larger than
# any legitimate single search response a caller would actually consume,
# while still bounding worst-case per-call memory to a small, fixed
# multiple of _MAX_READ_BYTES's own safety margin, even multiplied across
# a fan-out request at production's ~900-repo scale.
_MAX_TOTAL_RESULT_CONTENT_BYTES = 8 * 1024 * 1024  # 8 MiB

_MATCH_CONTENT_TRUNCATION_MARKER = "\n[content truncated]"
_MATCH_CONTENT_TRUNCATION_MARKER_BYTES = len(
    _MATCH_CONTENT_TRUNCATION_MARKER.encode("utf-8")
)


def _bounded_match_content(value: str) -> str:
    """Truncate ``value`` to at most ``_MAX_MATCH_CONTENT_BYTES`` UTF-8
    bytes total (INCLUDING the appended marker), when it exceeds that
    ceiling.

    Encodes to bytes and slices in BYTES (never text-mode characters) so
    the bound is exact regardless of how many multi-byte UTF-8 characters
    ``value`` contains -- the same "true bytes, not decoded characters"
    discipline already applied to ``_MAX_READ_BYTES`` elsewhere in this
    module. The marker's own byte length is reserved BEFORE slicing, so
    the final result (content + marker) never exceeds
    ``_MAX_MATCH_CONTENT_BYTES`` bytes. A raw byte slice can land
    mid-character at the cut point; decoding with ``errors="ignore"``
    silently drops that dangling partial trailing sequence rather than
    substituting a replacement character, so the returned string is
    always genuinely well-formed text, never a manufactured U+FFFD
    artifact.
    """
    encoded = value.encode("utf-8")
    if len(encoded) <= _MAX_MATCH_CONTENT_BYTES:
        return value
    keep_bytes = _MAX_MATCH_CONTENT_BYTES - _MATCH_CONTENT_TRUNCATION_MARKER_BYTES
    truncated = encoded[:keep_bytes].decode("utf-8", errors="ignore")
    return truncated + _MATCH_CONTENT_TRUNCATION_MARKER


def _validate_positive_int(value: int, name: str) -> None:
    """Reject anything but a genuine positive int (bool/float/str/None all
    rejected) for a byte-size constructor argument."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{name} must be an int, got {type(value).__name__}")
    if value <= 0:
        raise ValueError(f"{name} must be positive, got {value!r}")


class _ResultContentBudget:
    """Tracks cumulative UTF-8 byte usage of all match/context content
    accumulated for ONE ``search()`` call, enforcing
    ``_MAX_TOTAL_RESULT_CONTENT_BYTES`` (Issue #1601 remediation round 5,
    Priority 1). Bounding a single match's content
    (``_MAX_MATCH_CONTENT_BYTES``/``_bounded_match_content``) does not
    bound the AGGREGATE across many matches + their context lines in one
    response; this tracker closes that gap.

    ``max_bytes`` defaults to ``_MAX_TOTAL_RESULT_CONTENT_BYTES`` looked
    up as a free variable INSIDE ``__init__`` (evaluated at call time),
    deliberately NOT as the parameter's own default value (which would
    be bound once, at function-definition time, and never see a later
    ``unittest.mock.patch.object`` on the module constant) -- the same
    "read the module global at call time, not at def time" discipline
    ``_bounded_match_content`` already relies on for its own test
    patchability.
    """

    def __init__(self, max_bytes: Optional[int] = None) -> None:
        if max_bytes is None:
            max_bytes = _MAX_TOTAL_RESULT_CONTENT_BYTES
        _validate_positive_int(max_bytes, "max_bytes")
        self.max_bytes = max_bytes
        self.bytes_used = 0
        # True once any try_reserve() call has returned False -- the one
        # durable signal a caller holding this object can observe after
        # the specific call site that saw the False return has gone out
        # of scope.
        self.capped = False

    def try_reserve(self, value: str) -> bool:
        """Attempt to account for ``value``'s UTF-8 byte size against the
        budget.

        On success: commits the reservation (``self.bytes_used``
        increases by ``value``'s byte length) and returns True.

        On failure (committing would exceed the budget): ``self.bytes_used``
        is left UNCHANGED -- the byte counter itself never mutates on a
        rejected reservation -- but ``self.capped`` is deliberately set
        True as this method's failure signal (not an incidental side
        effect): it is the one durable way a caller can observe "a
        reservation was rejected" once the specific call site that saw
        the False return has gone out of scope. Returns False.
        """
        size = len(value.encode("utf-8"))
        if self.bytes_used + size > self.max_bytes:
            self.capped = True
            return False
        self.bytes_used += size
        return True


class _BoundedLineReader:
    """Iterates complete lines from a file via bounded, chunked byte reads.

    Issue #1601 (Fix direction #1): replaces the old ``f.read()`` (whole
    file into one string) + ``.splitlines()`` (a second full-string copy)
    pattern with line-by-line streaming, so a single call's own memory use
    is bounded independent of the file's actual size.

    Reads the file in binary mode with each chunk clamped to never read
    past ``max_bytes`` total -- ``bytes_read`` never exceeds ``max_bytes``,
    an exact byte-count guarantee rather than an approximation. An
    incremental UTF-8 decoder handles multi-byte characters that straddle a
    chunk boundary correctly (never corrupting a character by decoding
    chunks independently). Never yields a corrupted or merged LINE either:
    any line fragment still buffered when the byte ceiling is crossed is
    discarded, not yielded partially (AC-A5).

    ``path`` is always an internally-generated ``tempfile.mkstemp()`` path
    (or, in ``_search_python_multiline``, an already-validated repo-relative
    file path) -- never raw external input -- so no path-containment check
    is performed here; that policy lives in ``_to_repo_relative`` for the
    paths ripgrep/grep report back inside their output.

    Re-iterating an already-exhausted reader silently yields nothing (the
    file is re-opened and re-read from the start each time ``__iter__`` is
    called, but ``bytes_read``/``read_capped`` accumulate ACROSS calls, so
    a caller that iterates twice will see the ceiling crossed twice as
    fast) -- this is intentional: a fresh ``_BoundedLineReader`` per read is
    the supported usage; re-iterating the same instance is not a documented
    contract and callers should not rely on its exact behavior.
    """

    def __init__(
        self,
        path: str,
        max_bytes: int,
        chunk_bytes: int = _READ_CHUNK_BYTES,
    ) -> None:
        _validate_positive_int(max_bytes, "max_bytes")
        _validate_positive_int(chunk_bytes, "chunk_bytes")
        self.path = path
        self.max_bytes = max_bytes
        self.chunk_bytes = chunk_bytes
        self.bytes_read = 0
        # Issue #1601 remediation round 4 (Priority 4): set True by
        # __iter__ when a one-byte boundary probe (see below) proves the
        # file's real size is EXACTLY max_bytes with nothing beyond it --
        # disambiguates genuine EOF-at-the-boundary from a truncated read,
        # which the naive ``bytes_read >= max_bytes`` comparison alone
        # cannot tell apart.
        self._confirmed_not_capped_at_boundary = False

    @property
    def read_capped(self) -> bool:
        """True once total bytes read has reached/exceeded max_bytes.

        Meaningful after iteration has stopped, for any reason -- including
        a consumer that breaks out of its own ``for`` loop early (e.g. once
        it has enough matches). This deliberately reflects whether the
        ceiling was crossed by the point consumption stopped, regardless of
        *why* it stopped: if a consumer's own early-stop condition (such as
        max_results) happens to coincide with the same chunk that also
        crosses the byte ceiling, both conditions are legitimately true at
        once (see AC-A3c(iv)) -- there is nothing to reconcile.

        Issue #1601 remediation round 4 (Priority 4) exception: when the
        internal read loop ran to its own natural completion (never cut
        short by an external consumer breaking early) and a boundary probe
        confirmed the file's real size is exactly ``max_bytes`` with no
        data beyond it, this is NOT a capped read -- it is a complete read
        that merely happens to land exactly on the ceiling.
        """
        if self.bytes_read < self.max_bytes:
            return False
        if self._confirmed_not_capped_at_boundary:
            return False
        return True

    def __iter__(self):
        import codecs

        # Issue #1601 Priority 9: pending_parts accumulates fragments of
        # the CURRENT unfinished line across chunks via cheap list
        # appends. The old `buffer += decoder.decode(raw)` pattern
        # repeatedly copied the entire accumulated string on every chunk
        # read whenever no newline had appeared yet -- O(n^2) for a
        # single pathologically long line spanning many chunks. Joining
        # only happens once a newline actually appears (or at EOF),
        # bounding the total work to O(total bytes).
        pending_parts: List[str] = []
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        with open(self.path, "rb") as f:
            while self.bytes_read < self.max_bytes:
                remaining = self.max_bytes - self.bytes_read
                raw = f.read(min(self.chunk_bytes, remaining))
                if not raw:
                    break  # genuine EOF, within budget
                self.bytes_read += len(raw)
                decoded = decoder.decode(raw)
                if "\n" not in decoded:
                    pending_parts.append(decoded)
                    continue
                pending_parts.append(decoded)
                buffer = "".join(pending_parts)
                lines = buffer.split("\n")
                pending_parts = [lines.pop()]
                yield from lines
            else:
                # Issue #1601 remediation round 4 (Priority 4): the while
                # loop's own condition (bytes_read < max_bytes) went False
                # without an explicit break -- i.e. bytes_read has reached
                # max_bytes exactly. This ``else`` (Python while/else runs
                # whenever the loop exits normally, including when the
                # condition was never true to begin with) is exactly the
                # ambiguous case: bytes_read == max_bytes could mean
                # either "there is more data beyond the ceiling" or "the
                # file's real size IS max_bytes, nothing more". Disambig-
                # uate with a single extra 1-byte read at the current file
                # position -- deliberately NOT counted in bytes_read, to
                # preserve the documented exact byte-count guarantee. An
                # empty probe read proves genuine EOF: not capped.
                if not f.read(1):
                    self._confirmed_not_capped_at_boundary = True
        # EOF reached naturally, within budget: flush the decoder and emit
        # the trailing partial line (no newline at EOF), if any. If the
        # byte ceiling was instead what stopped the loop, the trailing
        # buffer is a genuinely incomplete fragment of a not-fully-read
        # line and must be discarded, never yielded corrupted/merged
        # (AC-A5).
        if not self.read_capped:
            pending_parts.append(decoder.decode(b"", final=True))
            buffer = "".join(pending_parts)
            if buffer:
                yield buffer


# Timeout for PCRE2 availability check subprocess
_PCRE2_CHECK_TIMEOUT_SEC = 5

# Above this many trigram-index candidates, skip the pre-filter and full-scan:
# the ripgrep arg list would be unwieldy and the filter is not selective enough
# to beat a plain scan.
_MAX_PREFILTER_CANDIDATES = 8000

# Bug #1590 review round 2 (F1): floor for the ripgrep-phase timeout computed
# from the remaining shared deadline -- never hand SubprocessExecutor a
# zero-or-negative timeout (which would be ambiguous/reject outright); one
# second is a negligible floor relative to any real search budget.
_MIN_RIPGREP_TIMEOUT_SECONDS = 1

# Bug #1590 review round 6: floor for the post-hop thread-watchdog budget
# handed to _run_with_thread_watchdog by the two phases that run AFTER an
# earlier phase has already consumed the shared deadline -- the
# ripgrep-output parse phase (_search_ripgrep's remaining_parse) and the
# trigram pre-filter's candidate-path resolve phase
# (_prefilter_candidate_files's remaining). Same rationale as
# _MIN_RIPGREP_TIMEOUT_SECONDS above, applied one level deeper: the
# ripgrep-subprocess-phase timeout is itself computed as
# max(_MIN_RIPGREP_TIMEOUT_SECONDS, math.ceil(remaining)) -- rounding UP --
# so ripgrep can legitimately finish up to ~1s past the true deadline even
# though it stayed within ITS OWN allowance. When that happens, the next
# phase's "remaining = deadline - time.monotonic()" is already <= 0, and
# _run_with_thread_watchdog's Thread.join(timeout=0) can never "win" even
# against a worker that would complete in microseconds -- a deadline that
# has technically already elapsed must never be handed through verbatim,
# or a fully successful, already-completed operation is discarded as a
# spurious TimeoutError. One second matches _MIN_RIPGREP_TIMEOUT_SECONDS's
# order of magnitude: negligible against any real search budget, but
# enough for a worker thread to actually be scheduled and finish typical
# parse/resolve work (a JSON parse of ripgrep output, or resolving up to
# _MAX_PREFILTER_CANDIDATES=8000 paths).
_MIN_PARSE_TIMEOUT_SECONDS = 1.0

# Lazy trigram-index rebuild: when a regex search finds no compatible trigram
# index (missing, or stale/old-format per the schema-version guard), kick off a
# one-shot background build so the index self-heals before the next scheduled
# golden-repo refresh. This request still full-scans; later ones use the index.
# Disable by setting CIDX_TRIGRAM_LAZY_BUILD=0.
#
# Issue #1601 AC-C3 finding (manual, evidence-based -- documented here per
# the issue's own explicitly permitted alternative to an automated test,
# since a real test would otherwise need a literal 300s sleep): an earlier
# draft of Issue #1601 retracted a claim that a repeat-call RSS plateau
# "establishes the mechanism precisely" as a cold-build cost, noting two
# unruled-out confounds -- (1) the broad character-class pattern used in
# that investigation has NO derivable literal trigram at all
# (extract_required_trigrams returns None for it), so the trigram cache is
# real but INERT for that specific pattern class regardless of whether an
# index exists; (2) allocator high-water-mark retention can make a flat
# RSS plateau look identical whether or not the second call actually did
# less work. This was disambiguated here with a direct, non-RSS
# measurement: for a SELECTIVE pattern that DOES have a derivable literal
# trigram (unlike the confound pattern), with this cooldown patched to a
# near-zero value so the background build could complete without a real
# wait, two successive real calls (asyncio-driven RegexSearchService.search,
# tracemalloc-measured, not RSS-inferred) against the same ~3,000-file
# repository showed peak traced allocation of ~2.0MB on the first call (no
# index yet, full scan) versus ~0.48MB on the second call (index built,
# trigram-narrowed scan) -- a genuine ~4x reduction in actual per-call
# work, not merely a flat/retained RSS reading. Conclusion: the trigram
# cache mechanism is real and measurably reduces cost on repeat calls for
# patterns with a derivable trigram; it does NOT explain -- and is
# unrelated to -- the plateau observed in the original incident's
# no-derivable-trigram pattern class, which remains attributable only to
# confound (2) (allocator retention) rather than any genuine cost
# reduction for that pattern shape.
_LAZY_BUILD_COOLDOWN_SEC = 300
_lazy_build_lock = threading.Lock()
_lazy_build_in_progress: "set[str]" = set()
_lazy_build_last_attempt: "dict[str, float]" = {}


def _lazy_build_enabled() -> bool:
    return os.environ.get("CIDX_TRIGRAM_LAZY_BUILD", "1") not in ("0", "false", "False")


def _maybe_trigger_lazy_index_build(repo_path: Path) -> None:
    """Start a background trigram-index build for ``repo_path`` if none is
    already running/recent. Best-effort; never raises into the caller."""
    if not _lazy_build_enabled():
        return
    key = str(repo_path)
    now = time.monotonic()
    with _lazy_build_lock:
        if key in _lazy_build_in_progress:
            return  # a build is already running for this repo
        last = _lazy_build_last_attempt.get(key)
        if last is not None and now - last < _LAZY_BUILD_COOLDOWN_SEC:
            return  # backed off after a recent attempt (avoid retry storms)
        _lazy_build_in_progress.add(key)
        _lazy_build_last_attempt[key] = now

    def _run() -> None:
        try:
            from .trigram_index_manager import TrigramIndexManager

            index_dir = repo_path / ".code-indexer" / "trigram_index"
            n = TrigramIndexManager(index_dir).build(repo_path)
            logger.info("lazy trigram index build complete for %s (%d files)", key, n)
        except Exception as exc:  # never let the background build crash the server
            logger.warning("lazy trigram index build failed for %s: %s", key, exc)
        finally:
            with _lazy_build_lock:
                _lazy_build_in_progress.discard(key)
                _lazy_build_last_attempt[key] = time.monotonic()

    threading.Thread(target=_run, name="trigram-lazy-build", daemon=True).start()


class RipgrepExecutionError(Exception):
    """Raised when ripgrep/grep exits with a non-zero code AND has stderr output.

    Finding 3.1 (v10.4.4): Previously these errors were swallowed (log + return
    empty), causing callers to see COMPLETED status with silently empty results.
    Raising here lets XRaySearchEngine surface phase1_failed in the job result.
    """


@dataclass
class RegexMatch:
    """A single regex match result."""

    file_path: str
    line_number: int
    column: int
    line_content: str
    context_before: List[str] = field(default_factory=list)
    context_after: List[str] = field(default_factory=list)


@dataclass
class RegexSearchResult:
    """Result of a regex search operation.

    ``total_matches`` contract (Issue #1601, Fix direction #2): exact ONLY
    when the scan completed without hitting either stopping condition below
    (``truncated`` and ``read_capped`` both False). Once either condition
    stops the scan early, ``total_matches`` becomes a LOWER BOUND ("at
    least this many matches exist"), not an exact count -- scanning further
    lines purely to keep counting exactly is exactly what this fix removes.

    ``truncated`` and ``read_capped`` are two DISTINCT, independently-
    settable signals -- never conflate them:

    - ``truncated``: more matches existed than ``max_results`` allowed.
      Only ever True when the scan affirmatively OBSERVED a match beyond
      ``max_results`` before stopping -- never inferred, never set merely
      because ``read_capped`` is also True.
    - ``read_capped``: the scan stopped because it hit the hard byte-size
      ceiling (see ``_MAX_READ_BYTES``) before it could determine the true
      total -- independent of whether ``max_results`` was also reached.
      When the byte ceiling is what stopped the scan before ``max_results``
      would have, ``truncated`` is correctly False (the scan does not know
      whether more than ``max_results`` matches existed) and ``read_capped``
      signals the incompleteness instead.

    Both flags may be True simultaneously without contradiction when the
    two thresholds are crossed at effectively the same point (AC-A3c(iv)).
    """

    matches: List[RegexMatch]
    total_matches: int
    truncated: bool
    search_engine: str
    search_time_ms: float
    read_capped: bool = False


class RegexSearchService:
    """Service for performing regex searches on repository files."""

    # Set once, process-wide, the first time we degrade to the grep fallback so
    # the warning in _detect_search_engine is emitted a single time, not per call.
    _grep_fallback_warned: bool = False

    def __init__(
        self,
        repo_path: Path,
        subprocess_max_workers: int = 2,
        alias: Optional[str] = None,
    ):
        """Initialize the regex search service.

        Args:
            repo_path: Path to the repository root
            subprocess_max_workers: Maximum concurrent workers for subprocess execution
                (default: 2, per Story #27 resource audit recommendation)
            alias: Issue #1601 Priority 9 (optional, server-context only):
                the user-facing repository alias, when the caller has one
                available (e.g. the MCP handler). A path is not
                necessarily the alias -- especially for versioned
                snapshots/omni searches -- so this is threaded through
                separately for observability (see the read-capped
                WARNING log in ``search()``) rather than inferred from
                ``repo_path``.

        Raises:
            RuntimeError: If neither ripgrep nor grep is available
        """
        # Bug #1401: canonicalize the repo root ONCE, here, rather than at
        # scattered per-call-site resolutions. self.repo_path is the single
        # source of truth every relative_to() comparison inside this service
        # (trigram pre-filter, ripgrep JSON parsing, grep-mode parsing,
        # Python-multiline fallback parsing) is checked against, via
        # _to_repo_relative(). Without this, an unresolved symlinked repo
        # root desyncs against candidate paths that subprocesses report back
        # resolved, raising an uncaught pathlib ValueError downstream.
        self.repo_path = Path(repo_path).resolve()
        self._subprocess_max_workers = subprocess_max_workers
        self._alias = alias
        self._search_engine = self._detect_search_engine()
        self._pcre2_supported: Optional[bool] = None  # Lazy-detected, cached
        # Issue #1601: per-call carrier set by _search_ripgrep/_search_grep/
        # _search_python_multiline when the hard byte-size ceiling stopped
        # the scan early. search() reads this immediately after dispatching
        # to whichever engine ran, to build the final RegexSearchResult --
        # it is reset at the top of every search() call, so a service
        # instance reused across multiple .search() calls never leaks a
        # stale value from a previous call.
        self._last_search_read_capped: bool = False
        # Issue #1601 (AC-I3): the exact bytes read when a call was
        # read-capped, for the structured WARNING log in search().
        self._last_read_capped_bytes: Optional[int] = None

    def _detect_search_engine(self) -> str:
        """Detect available search engine (ripgrep preferred).

        Returns:
            String identifying the search engine ("ripgrep" or "grep")

        Raises:
            RuntimeError: If neither ripgrep nor grep is found
        """
        if shutil.which("rg"):
            return "ripgrep"
        elif shutil.which("grep"):
            # ripgrep is absent -> we degrade to a linear `grep -r` scan that has
            # no gitignore awareness and reads the entire working tree. On large
            # repos this reliably hits the search timeout. Warn loudly (once) so
            # the degradation is visible instead of manifesting as opaque 30s
            # timeouts. Install ripgrep in the deployment image to avoid this.
            if not RegexSearchService._grep_fallback_warned:
                RegexSearchService._grep_fallback_warned = True
                logger.warning(
                    "ripgrep (rg) not found on PATH; falling back to 'grep -r'. "
                    "Regex search will linearly scan the full working tree and "
                    "may time out on large repos. Install ripgrep in the image."
                )
            return "grep"
        else:
            raise RuntimeError("Neither ripgrep nor grep found on system")

    # Story #1491 AC2: PROCESS-WIDE pcre2 probe result plus the lock that makes
    # the check-then-probe atomic across concurrent requests, so the probe runs
    # exactly once per process even under parallel load. Whether the installed
    # ripgrep binary has PCRE2 compiled in cannot change while this process is
    # alive. Same reasoning as the _grep_fallback_warned class flag above.
    _pcre2_supported_global: Optional[bool] = None
    _pcre2_probe_lock = threading.Lock()

    def _detect_pcre2_support(self) -> bool:
        """Detect whether ripgrep has PCRE2 support. Cached process-wide.

        Before this story the cache was per-instance, but a fresh
        RegexSearchService is constructed for EVERY request
        (handlers/search.py's _execute_regex_search), so the
        `rg --pcre2-version` fork+exec ran on every pcre2 request -- on the
        event loop, per report Finding B2.

        An explicitly set per-instance value still wins, so a caller (or test)
        that pins the capability keeps overriding the probe entirely.

        STICKY FAILURE (deliberate, documented): a probe that fails -- ripgrep
        missing from PATH, the probe timing out, or any OSError -- caches False
        for the REST OF THE PROCESS LIFETIME. If ripgrep is installed or
        upgraded to a PCRE2-capable build while the server is running, pcre2
        patterns stay rejected until the process restarts. That is the accepted
        trade-off for not re-forking a subprocess per request on every request
        of a deployment that genuinely lacks PCRE2 (the exact per-request
        fork+exec this cache exists to eliminate). The reset hook, if a caller
        ever needs to re-probe without a restart, is a single assignment:
        ``RegexSearchService._pcre2_supported_global = None`` -- which is
        precisely what TestDetectPcre2Support's autouse fixture does.

        Issue #1601 remediation round 4 (Priority 3): this method itself
        remains synchronous (a subprocess.run() fork+exec on the FIRST
        call only, thereafter a plain cached-bool return) -- callers
        reached from an ``async def`` context MUST invoke it via
        ``anyio.to_thread.run_sync`` (see ``search()``) rather than
        calling it directly, so even that first-call fork+exec never
        blocks the event loop.
        """
        if self._pcre2_supported is not None:
            return self._pcre2_supported

        with RegexSearchService._pcre2_probe_lock:
            if RegexSearchService._pcre2_supported_global is None:
                try:
                    result = subprocess.run(
                        ["rg", "--pcre2-version"],
                        capture_output=True,
                        text=True,
                        timeout=_PCRE2_CHECK_TIMEOUT_SEC,
                    )
                    RegexSearchService._pcre2_supported_global = result.returncode == 0
                except (
                    FileNotFoundError,
                    subprocess.TimeoutExpired,
                    OSError,
                ) as probe_error:
                    # A probe that cannot run means we cannot claim PCRE2
                    # support, so "unsupported" is the correct, safe answer --
                    # the caller then rejects pcre2 patterns explicitly rather
                    # than handing ripgrep a flag it may not understand. This
                    # is a capability answer, not a swallowed error: log it so
                    # a broken/missing ripgrep is visible. Logged once per
                    # process, since the result is cached below.
                    logger.warning(
                        "Could not probe ripgrep PCRE2 support (%s: %s); "
                        "treating PCRE2 as unavailable for this process",
                        type(probe_error).__name__,
                        probe_error,
                    )
                    RegexSearchService._pcre2_supported_global = False

        self._pcre2_supported = RegexSearchService._pcre2_supported_global
        return self._pcre2_supported

    async def search(
        self,
        pattern: str,
        path: Optional[str] = None,
        include_patterns: Optional[List[str]] = None,
        exclude_patterns: Optional[List[str]] = None,
        case_sensitive: bool = True,
        context_lines: int = 0,
        max_results: int = 100,
        timeout_seconds: Optional[int] = None,
        multiline: bool = False,
        pcre2: bool = False,
    ) -> RegexSearchResult:
        """Execute regex search and return structured results.

        Args:
            pattern: Regular expression pattern to search for
            path: Subdirectory to search within (relative to repo root)
            include_patterns: Glob patterns for files to include
            exclude_patterns: Glob patterns for files to exclude
            case_sensitive: Whether search is case-sensitive
            context_lines: Number of context lines before/after match
            max_results: Maximum number of matches to return
            timeout_seconds: Maximum execution time in seconds (optional)
            multiline: Enable multi-line matching (pattern spans lines)
            pcre2: Enable PCRE2 engine for lookahead/lookbehind

        Returns:
            RegexSearchResult with matches and metadata

        Raises:
            ValueError: If path doesn't exist or PCRE2 unavailable
            TimeoutError: If search exceeds timeout_seconds
        """
        # Story #4 AC2: Regex metrics tracked at MCP handler layer with
        # correct username attribution (_legacy.py:245). Removed duplicate
        # increment_regex_search() call here that caused _anonymous attribution.

        # Issue #1601 remediation round 4 (Priority 3): both reviewers'
        # round-3 passes independently agreed the read+parse work inside
        # _search_ripgrep/_search_grep is correctly offloaded; Codex's
        # round-3 pass additionally flagged that this pre-dispatch setup
        # work -- the PCRE2 probe (a real subprocess.run() fork+exec on
        # its first call), the search_path existence check (a filesystem
        # stat that, per this project's own Production Scale invariant,
        # can block FOREVER against an unresponsive `hard` NFSv3 mount),
        # and the trigram pre-filter (opens a real sqlite3 connection
        # against an index file that, per trigram_index_manager.py's own
        # docstring, "lives on shared NFS under the golden repo" in
        # cluster mode) -- was NOT offloaded, and runs directly on the
        # caller's thread whenever ``search()`` is awaited from an async
        # context (both the REST route and the MCP handler go through
        # this exact method). Investigation confirmed this was a real
        # gap: each is wrapped in ``anyio.to_thread.run_sync`` below,
        # mirroring the existing granular per-sub-call offload pattern
        # already established for the read+parse methods, rather than
        # wrapping the entire orchestration in one hop (which would
        # require the subprocess-execution calls below -- themselves
        # already async and awaited -- to be reachable from inside a
        # worker thread with no running event loop, a much larger and
        # riskier restructuring for no additional safety benefit here).
        if pcre2 and not await anyio.to_thread.run_sync(self._detect_pcre2_support):
            raise ValueError(
                "PCRE2 not available. Install libpcre2 and ensure ripgrep "
                "is built with PCRE2 support (rg --pcre2-version)."
            )

        # Issue #1601: reset the per-call read_capped carrier so a service
        # instance reused across multiple .search() calls never leaks a
        # stale value from a previous call.
        self._last_search_read_capped = False
        self._last_read_capped_bytes = None

        start_time = time.time()
        start_monotonic = time.monotonic()

        # Bug #1590 review round 5 finding 1: hoisted from inside the
        # ripgrep-engine branch below. The round-4 comment there justified
        # leaving the search_path existence check (immediately below)
        # unbounded by claiming ``deadline`` "only becomes an absolute
        # deadline further down, inside the ripgrep-engine branch, so
        # bounding this earlier, engine-agnostic check would require
        # restructuring engine dispatch" -- review round 5 proved that
        # false: ``deadline`` depends only on ``start_monotonic`` (two
        # lines above) and ``timeout_seconds`` (already a parameter),
        # neither of which has anything to do with which engine gets
        # dispatched.
        deadline: Optional[float] = (
            start_monotonic + timeout_seconds if timeout_seconds is not None else None
        )

        search_path = self.repo_path / path if path else self.repo_path
        # Bug #1590 review round 5 finding 1 fix: bound via the same
        # thread-watchdog idiom (_run_with_thread_watchdog) used throughout
        # this file, instead of the plain anyio.to_thread.run_sync offload
        # alone -- on the `hard` NFSv3 golden-repo mount this project's own
        # Production Scale invariant calls out, a bare os.stat() can block
        # in uninterruptible kernel retry and never return; the plain
        # offload (still used below when there is no deadline at all)
        # bounds only the EVENT LOOP, not this request. No sqlite
        # connection is ever opened by Path.exists, so the watchdog's
        # optional ``holder`` parameter is simply omitted.
        from .trigram_index_manager import _run_with_thread_watchdog

        if deadline is not None:
            remaining_exists = max(0.0, deadline - time.monotonic())
            path_exists, exists_timed_out = await anyio.to_thread.run_sync(
                _run_with_thread_watchdog,
                search_path.exists,
                remaining_exists,
                "regex_search_path_exists",
            )
            if exists_timed_out:
                raise TimeoutError(
                    f"Path existence check for {search_path} exceeded its "
                    f"{remaining_exists:.3f}s remaining search deadline"
                )
        else:
            path_exists = await anyio.to_thread.run_sync(search_path.exists)
        if not path_exists:
            raise ValueError(f"Path does not exist: {path}")

        if self._search_engine == "ripgrep":
            # Bug #1590 (AC1) + review round 2 findings F1/F3 + review
            # round 3 finding B1 + review round 4 finding R1 + review
            # round 5 findings 1/2 (comment rewritten to be genuinely,
            # fully true -- round 4's version here disclosed
            # search_path.exists() as a known exception and never
            # disclosed the parse phase's per-match resolve as one at all;
            # both are now fixed, so no exception remains): a single
            # ABSOLUTE MONOTONIC deadline (never a duration recomputed
            # piecemeal, and never wall-clock time.time() -- an NTP step
            # could otherwise corrupt the arithmetic into a spurious
            # instant-timeout or an inflated budget), computed once above
            # BEFORE this branch is even entered, is threaded through
            # EVERY blocking step in the chain: the search_path existence
            # check above, the trigram pre-filter's two internal sqlite
            # calls (exists() then query()), the resolve phase that turns
            # each returned candidate into an absolute path (up to
            # _MAX_PREFILTER_CANDIDATES=8000 Path.resolve() calls -- see
            # _prefilter_candidate_files), the ripgrep subprocess phase
            # (bounded by SubprocessExecutor's own timeout machinery), and
            # the post-ripgrep JSON parse phase below
            # (_read_and_parse_ripgrep, which resolves each match's
            # reported path via _to_repo_relative -- see _search_ripgrep's
            # ``deadline`` parameter). HONEST contract: this call is
            # bounded by timeout_seconds PLUS up to ~3 total seconds of
            # overrun, from THREE independent 1-second floors --
            # _MIN_RIPGREP_TIMEOUT_SECONDS on the ripgrep subprocess phase
            # (plus its own extra ceil() second of rounding), and
            # _MIN_PARSE_TIMEOUT_SECONDS (review round 6) applied
            # separately to BOTH the trigram-prefilter-resolve phase
            # (_prefilter_candidate_files) and the post-ripgrep parse
            # phase (_read_and_parse_ripgrep, above) -- each floor can
            # independently contribute up to ~1s if an earlier phase on
            # the same shared deadline has already run it past zero. This
            # is a small, deliberate design tradeoff (~10% of the 30s
            # production MCP/REST search timeout, ~2.5% of xray Phase 1's
            # 120s default), not an unbounded gap.

            # Index-assisted pre-filter: when a trigram index is present, narrow
            # the scan to files that could match instead of walking the whole
            # (NFS-backed) working tree. Returns None to fall back to a full
            # scan; an empty list means no file can match. Raises TimeoutError
            # (propagates naturally through anyio.to_thread.run_sync) when the
            # trigram index itself blows the remaining budget -- see
            # _prefilter_candidate_files's docstring.
            candidate_files = await anyio.to_thread.run_sync(
                self._prefilter_candidate_files,
                pattern,
                search_path,
                path,
                case_sensitive,
                deadline,
            )
            if candidate_files is not None and not candidate_files:
                matches, total = [], 0
            else:
                if deadline is not None:
                    remaining = max(0.0, deadline - time.monotonic())
                    ripgrep_timeout: Optional[int] = max(
                        _MIN_RIPGREP_TIMEOUT_SECONDS, math.ceil(remaining)
                    )
                else:
                    ripgrep_timeout = timeout_seconds
                matches, total = await self._search_ripgrep(
                    pattern,
                    search_path,
                    include_patterns,
                    exclude_patterns,
                    case_sensitive,
                    context_lines,
                    max_results,
                    ripgrep_timeout,
                    multiline=multiline,
                    pcre2=pcre2,
                    candidate_files=candidate_files,
                    deadline=deadline,
                )
        else:
            matches, total = await self._search_grep(
                pattern,
                search_path,
                include_patterns,
                exclude_patterns,
                case_sensitive,
                context_lines,
                max_results,
                timeout_seconds,
                multiline=multiline,
                pcre2=pcre2,
            )

        elapsed_ms = (time.time() - start_time) * 1000

        if self._last_search_read_capped:
            # Issue #1601 (AC-I3): the only observability this condition
            # gets today -- silent capping at ~900-repo fleet scale would
            # make this exact failure mode invisible to future debugging.
            logger.warning(
                "regex_search read-capped: repo_path=%s alias=%s pattern=%r "
                "bytes_read_at_cutoff=%s",
                self.repo_path,
                self._alias,
                pattern,
                self._last_read_capped_bytes,
                extra={
                    "repo_path": str(self.repo_path),
                    # Issue #1601 Priority 9: a path isn't necessarily the
                    # user-facing alias (versioned snapshots, omni
                    # searches) -- surface it separately when the caller
                    # provided one via the constructor.
                    "alias": self._alias,
                    "pattern": pattern,
                    "bytes_read_at_cutoff": self._last_read_capped_bytes,
                },
            )

        return RegexSearchResult(
            matches=matches,
            total_matches=total,
            truncated=total > max_results,
            search_engine=self._search_engine,
            search_time_ms=elapsed_ms,
            read_capped=self._last_search_read_capped,
        )

    def _extract_line_text(self, lines_data: dict) -> str:
        """Extract text content from ripgrep JSON lines data.

        Ripgrep uses two formats for line content:
        - {"text": "..."} for valid UTF-8 content
        - {"bytes": "..."} for binary/non-UTF8 (base64-encoded)
        """
        if "text" in lines_data:
            return str(lines_data["text"]).rstrip("\n")
        elif "bytes" in lines_data:
            import base64

            return (
                base64.b64decode(lines_data["bytes"])
                .decode("utf-8", errors="replace")
                .rstrip("\n")
            )
        return ""

    def _to_repo_relative(self, raw_path: str) -> Optional[str]:
        """Convert a subprocess-reported path into a repo-relative string.

        Bug #1401: this is the single shared containment-check policy used
        by every output-parsing site in this service (ripgrep JSON, grep-mode,
        Python-multiline fallback), checked against the canonical
        ``self.repo_path`` set once at construction.

        - Absolute paths are compared directly against the canonical root.
        - Relative paths are joined onto the canonical root first -- being
          relative is NOT a free pass; a ``../`` escape or an internal
          symlink that resolves outside the repo is rejected the same way
          an absolute-outside-repo path is ("genuinely relative" means
          "relative AND contained", not "relative, therefore trust it").

        Returns None (and logs a warning) if the path does not resolve to
        somewhere inside the repository root; callers must drop that match
        rather than ever storing an absolute/escaping path as ``file_path``.
        """
        candidate = Path(raw_path)
        if not candidate.is_absolute():
            candidate = self.repo_path / candidate
        try:
            resolved = candidate.resolve()
            rel = resolved.relative_to(self.repo_path)
        except ValueError:
            logger.warning(
                "regex search match path %r resolves outside repository root "
                "%s; dropping match to avoid an incorrect absolute file_path",
                raw_path,
                self.repo_path,
            )
            return None
        return str(rel)

    def _process_ripgrep_match_event(
        self,
        data: dict,
        matches: List[RegexMatch],
        total: int,
        max_results: int,
        context_before: List[str],
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Handle one ripgrep --json 'match' event.

        Issue #1601: capacity checked first -- max_results, then the
        aggregate content budget (round 5 Priority 1; see
        ``_ResultContentBudget``) -- so scanning stops immediately on
        either, via the SAME ``len(matches) + 1`` lower-bound sentinel
        for both (at the max_results check, ``len(matches) == max_results``,
        so this is algebraically identical to the pre-existing
        ``max_results + 1``). Returns (stop, total, context_before).
        ``budget`` defaults to a fresh, effectively-unbounded instance
        when not supplied.
        """
        if budget is None:
            budget = _ResultContentBudget()
        if len(matches) >= max_results:
            return True, len(matches) + 1, context_before

        match_data = data["data"]
        rel_path = self._to_repo_relative(self._extract_line_text(match_data["path"]))
        if rel_path is None:
            return False, total, context_before  # Bug #1401: drop, don't stop

        submatches = match_data.get("submatches", [])
        column = submatches[0]["start"] + 1 if submatches else 1
        bounded_content = _bounded_match_content(
            self._extract_line_text(match_data["lines"])
        )
        if not budget.try_reserve(bounded_content):
            return True, len(matches) + 1, context_before

        matches.append(
            RegexMatch(
                file_path=rel_path,
                line_number=match_data["line_number"],
                column=column,
                line_content=bounded_content,
                context_before=context_before.copy(),
                context_after=[],
            )
        )
        return False, total + 1, []

    def _process_ripgrep_context_event(
        self,
        data: dict,
        matches: List[RegexMatch],
        context_before: List[str],
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Handle one ripgrep --json 'context' event.

        Issue #1601 remediation round 4 (Priority 2): a context line is
        subject to the identical unbounded-single-line risk as a match
        line -- bound it the same way. Issue #1601 round 5 (Priority 1):
        also reserve it against the aggregate ``budget`` -- if this
        alone would exceed it, signal ``stop=True`` so the caller ends
        the whole scan rather than accumulating further context lines
        that a subsequent match would never even get a chance to check.

        Returns: (context_before, stop).
        """
        if budget is None:
            budget = _ResultContentBudget()
        ctx = _bounded_match_content(self._extract_line_text(data["data"]["lines"]))
        if not budget.try_reserve(ctx):
            return context_before, True
        if matches and data["data"]["line_number"] > matches[-1].line_number:
            matches[-1].context_after.append(ctx)
            return context_before, False
        context_before.append(ctx)
        return context_before, False

    def _parse_ripgrep_json_output(
        self,
        output: Union[str, Iterable[str]],
        max_results: int,
        context_lines: int,
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Parse ripgrep JSON output (``str``, split into lines, or an
        iterable of lines -- see ``_search_ripgrep``) into RegexMatch
        objects. ``budget``: see ``_ResultContentBudget``; defaults to a
        fresh instance. Return stays (matches, total) for direct-caller
        compatibility -- pass an explicit ``budget`` and read
        ``budget.capped`` afterward to observe the aggregate-cap signal.
        """
        if budget is None:
            budget = _ResultContentBudget()
        matches: List[RegexMatch] = []
        total = 0
        context_before: List[str] = []

        lines = output.splitlines() if isinstance(output, str) else output
        for line in lines:
            try:
                data = json.loads(line)
            except json.JSONDecodeError:
                logger.debug(
                    f"Skipping non-JSON line from ripgrep output: {str(line)[:100]}"
                )
                continue

            event_type = data.get("type")
            if event_type == "match":
                stop, total, context_before = self._process_ripgrep_match_event(
                    data, matches, total, max_results, context_before, budget
                )
                if stop:
                    break
            elif event_type == "context" and context_lines > 0:
                context_before, stop = self._process_ripgrep_context_event(
                    data, matches, context_before, budget
                )
                if stop:
                    break

        return matches, total

    def _read_and_parse_ripgrep(
        self,
        temp_path: str,
        max_results: int,
        context_lines: int,
    ) -> tuple:
        """Synchronous unit of work: stream-read ``temp_path`` and parse it
        as ripgrep JSON output, in one call.

        Issue #1601 remediation (Priority 2): this is the exact synchronous
        body offloaded to a worker thread via ``anyio.to_thread.run_sync``
        in ``_search_ripgrep`` -- file I/O, UTF-8 decode, and JSON/regex
        parsing must never run directly on the asyncio event loop.

        Returns:
            (matches, total, bytes_read, read_capped, content_capped) --
            ``content_capped`` (Issue #1601 round 5, Priority 1) is True
            when the aggregate result-content budget, not the byte-read
            ceiling, is what stopped the scan early.
        """
        reader = _BoundedLineReader(temp_path, _MAX_READ_BYTES)
        budget = _ResultContentBudget()
        matches, total = self._parse_ripgrep_json_output(
            reader, max_results, context_lines, budget
        )
        return (
            matches,
            total,
            reader.bytes_read,
            reader.read_capped,
            budget.capped,
        )

    def _prefilter_candidate_files(
        self,
        pattern: str,
        search_path: Path,
        path: Optional[str],
        case_sensitive: bool,
        deadline: Optional[float] = None,
    ) -> Optional[List[Path]]:
        """Return candidate file paths from the trigram index, or None.

        None means "no usable pre-filter -> scan the whole ``search_path``". A
        (possibly empty) list means the scan can be restricted to exactly those
        files -- a guaranteed superset of matches (see :mod:`trigram_index_manager`).
        Any failure degrades to None (full scan); the pre-filter never narrows
        unsafely.

        Issue #1601 remediation round 4 (Priority 3): this method opens a
        real sqlite3 connection against the trigram index file, which (per
        ``trigram_index_manager.py``'s own docstring) lives on shared NFS
        under the golden repo in cluster mode -- a genuine synchronous
        filesystem/DB call. Callers reached from an ``async def`` context
        MUST invoke this via ``anyio.to_thread.run_sync`` (see
        ``search()``) rather than calling it directly.

        Bug #1590 (AC1) + review round 2 finding F1: ``deadline`` is an
        ABSOLUTE ``time.monotonic()`` timestamp (NOT a duration). The
        remaining budget is recomputed via ``max(0.0, deadline -
        time.monotonic())`` immediately before EACH of the two internal
        sqlite calls (``index.exists()`` then ``index.query()``), so they
        share ONE real end-to-end budget instead of each independently
        receiving a fresh full copy of the caller's original
        ``timeout_seconds`` -- passing the same static duration to both
        (the pre-fix bug) let a slow ``exists()`` alone consume the whole
        budget and still hand ``query()`` a brand-new full allotment,
        permitting up to ~2x the requested deadline with no exception at
        all. A genuine deadline overrun on either call raises
        ``TrigramIndexTimeoutError``, which this method re-raises as a
        plain ``TimeoutError`` -- the SAME sentinel the ripgrep subprocess
        phase already raises on its own timeout -- so ``search()``'s
        caller treats a stuck trigram pre-filter identically to a stuck
        ripgrep run: the whole search fails fast with a bounded deadline
        instead of falling back to a full scan whose own ripgrep-phase
        timeout has already been eaten by the stuck prefilter. Every OTHER
        failure (index genuinely absent, corrupt, or any other exception)
        still degrades to None (full scan, itself bounded by the
        ripgrep-phase timeout ``search()`` computes from the SAME
        deadline) exactly as before -- only a genuine timeout escalates.

        Bug #1590 review round 3 finding B1 + review round 4 finding R1:
        the SAME ``deadline`` also bounds the phase that resolves each
        ``index.query()`` candidate to an absolute path (up to
        ``_MAX_PREFILTER_CANDIDATES`` = 8000 ``Path.resolve()`` calls, each
        several real filesystem syscalls), including ``search_path.resolve()``
        itself. Round 3's fix only checked the deadline BETWEEN loop
        iterations, which left a SINGLE wedged resolve call (e.g. the
        `hard` NFSv3 mount blocking in uninterruptible kernel retry, this
        project's own documented failure mode) completely unbounded --
        live reproduction proved this is not merely a bounded overrun: in
        the single-candidate case the loop has no "next iteration" to
        notice the overrun on at all, so it silently returns the
        candidate, ripgrep runs, and the search completes SUCCESSFULLY
        with no ``TimeoutError`` ever raised. Round 4 replaces the
        inter-iteration check with the SAME thread-based watchdog
        ``exists()``/``query()`` already use (``_run_with_thread_watchdog``
        + ``_TrigramConnectionHolder``, whose ``interrupt()`` is a
        documented no-op here since this phase never publishes a sqlite
        connection): the entire resolve phase runs as one unit on a daemon
        thread joined with the remaining budget, so a single stuck syscall
        is bounded by a real ``Thread.join(timeout=)``, not by hoping
        another iteration comes along to check a clock. A deadline overrun
        raises the same ``TrigramIndexTimeoutError`` -> ``TimeoutError``
        sentinel as ``exists()``/``query()`` above. Round 5 LOW note: this
        phase is called with no connection holder at all (the watchdog's
        ``holder`` parameter is optional precisely because this phase
        never publishes a sqlite connection, so there is nothing to
        cancel).
        """
        # TrigramIndexTimeoutError is imported unconditionally here, unlike
        # the other trigram_index_manager imports below (which live inside
        # the try: per round 5 LOW note 2), because it is referenced by
        # name in the `except TrigramIndexTimeoutError` clause at the
        # bottom of this method -- if that import itself failed inside the
        # try, Python would need to resolve this same name to match the
        # exception and find it unbound, raising a confusing
        # UnboundLocalError instead of gracefully falling through to the
        # `except Exception` full-scan fallback this method's docstring
        # promises.
        from .trigram_index_manager import TrigramIndexTimeoutError

        def _remaining_budget() -> Optional[float]:
            if deadline is None:
                return None
            return max(0.0, deadline - time.monotonic())

        try:
            from .regex_trigram import extract_required_trigrams
            from .trigram_index_manager import (
                TrigramIndexManager,
                _run_with_thread_watchdog,
            )

            index = TrigramIndexManager(
                self.repo_path / ".code-indexer" / "trigram_index"
            )
            if not index.exists(timeout_seconds=_remaining_budget()):
                # No compatible index (missing or stale/old-format). Self-heal in
                # the background so later searches are pre-filtered; this one
                # full-scans.
                _maybe_trigger_lazy_index_build(self.repo_path)
                return None
            required = extract_required_trigrams(
                pattern, case_insensitive=not case_sensitive
            )
            if not required:
                return None
            rel_candidates = index.query(required, timeout_seconds=_remaining_budget())
            if rel_candidates is None:
                return None
            if len(rel_candidates) > _MAX_PREFILTER_CANDIDATES:
                # Too many candidates: the arg list would be huge and the filter
                # is not selective -- a full scan is simpler and comparable.
                return None

            def _resolve_candidates() -> List[Path]:
                # Runs on the watchdog worker thread below (or directly,
                # unbounded, when there is no deadline). Both
                # search_path.resolve() (round 4 finding R1: previously had
                # NO deadline protection of any kind) and the per-candidate
                # resolve loop (up to _MAX_PREFILTER_CANDIDATES=8000 real
                # filesystem syscalls) live here as one unit so a single
                # wedged call anywhere in this phase is caught by the
                # watchdog's Thread.join(timeout=), not by an inter-
                # iteration clock check.
                scope = search_path.resolve()
                resolved: List[Path] = []
                for rel in rel_candidates:
                    ap = (self.repo_path / rel).resolve()
                    try:
                        ap.relative_to(scope)  # keep only files under scan scope
                    except ValueError:
                        continue
                    resolved.append(ap)
                return resolved

            remaining = _remaining_budget()
            if remaining is None:
                # No caller deadline -- behavior unchanged from before R1.
                return _resolve_candidates()

            # Round 5 LOW note: no holder passed -- this phase never opens
            # a sqlite connection, so there is nothing for .interrupt() to
            # ever cancel; constructing a throwaway _TrigramConnectionHolder()
            # here just to satisfy a required parameter was a naming/
            # cohesion nit now resolved by making the parameter optional.
            #
            # Bug #1590 review round 6: floor `remaining` at
            # _MIN_PARSE_TIMEOUT_SECONDS (same pattern as the ripgrep-
            # output parse phase's identical fix -- see that constant's
            # comment) rather than handing the raw, possibly-zero
            # `_remaining_budget()` value straight to the watchdog. A
            # deadline that has technically already elapsed (e.g. an
            # earlier call on this SAME shared deadline overshooting its
            # own rounded-up allowance) must not turn into
            # Thread.join(timeout=0), which can never "win" even against
            # a resolve loop that completes in milliseconds.
            bounded_remaining = max(_MIN_PARSE_TIMEOUT_SECONDS, remaining)
            result, timed_out = _run_with_thread_watchdog(
                _resolve_candidates, bounded_remaining, "trigram_prefilter_resolve"
            )
            if timed_out:
                raise TrigramIndexTimeoutError(
                    "trigram pre-filter candidate-path resolution for "
                    f"{self.repo_path} exceeded its shared deadline while "
                    f"resolving up to {len(rel_candidates)} candidate(s)"
                )
            return cast(List[Path], result)
        except TrigramIndexTimeoutError as exc:
            # Bug #1590 AC1/AC2 (review round 2 finding F4: corrected
            # wording -- the prior comment here incorrectly called the
            # full-scan fallback "unbounded"; it is NOT -- search()
            # already computes the ripgrep-phase timeout from the SAME
            # shared deadline this exception represents having exhausted).
            # A genuine deadline overrun on the trigram index itself is
            # escalated here instead of silently falling back to that
            # full scan, because the deadline is ALREADY exhausted by the
            # time this fires -- a fresh full scan would have nothing
            # left of the budget to run in anyway.
            raise TimeoutError(str(exc)) from exc
        except Exception as exc:  # never let the optimization break search
            logger.debug("trigram pre-filter unavailable (%s); full scan", exc)
            return None

    async def _search_ripgrep(
        self,
        pattern: str,
        search_path: Path,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        case_sensitive: bool,
        context_lines: int,
        max_results: int,
        timeout_seconds: Optional[int],
        multiline: bool = False,
        pcre2: bool = False,
        candidate_files: Optional[List[Path]] = None,
        deadline: Optional[float] = None,
    ) -> tuple:
        """Search using ripgrep with JSON output and timeout protection.

        When ``candidate_files`` is provided, ripgrep searches exactly those
        files (the trigram pre-filter's superset) instead of walking
        ``search_path``; include/exclude globs still apply.

        ``deadline`` (Bug #1590 review round 5 finding 2; optional, an
        ABSOLUTE ``time.monotonic()`` timestamp -- the SAME one
        ``search()`` threads through the trigram pre-filter) additionally
        bounds the post-subprocess JSON parse phase
        (``_read_and_parse_ripgrep``) against the shared search deadline.
        Direct callers (e.g. existing unit tests) that omit it get the
        prior, unbounded-parse-phase behavior unchanged.
        """
        cmd = ["rg", "--json", "-e", pattern]

        if multiline:
            cmd.extend(["--multiline", "--multiline-dotall"])
        if pcre2:
            cmd.append("--pcre2")

        if not case_sensitive:
            cmd.append("-i")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])

        if include_patterns:
            for pat in include_patterns:
                cmd.extend(["-g", pat])
        if exclude_patterns:
            for pat in exclude_patterns:
                cmd.extend(["-g", f"!{pat}"])

        # Always exclude CIDX internal directories (Bug #158)
        cmd.extend(["-g", "!.code-indexer/**"])
        cmd.extend(["-g", "!.git/**"])

        if candidate_files is not None:
            # Trigram pre-filter narrowed the search to specific files; ripgrep
            # searches those directly (gitignore is irrelevant for explicit paths,
            # and the candidates already came from a gitignore-aware index).
            cmd.append("--")
            cmd.extend(str(f) for f in candidate_files)
        else:
            cmd.extend(["--", str(search_path)])

        # Create temp file for output
        temp_fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="rg_search_")
        os.close(temp_fd)

        try:
            # Execute with SubprocessExecutor for async + timeout protection.
            # Issue #1601 (Fix direction 4a): max_output_bytes makes the
            # executor terminate rg itself if its output crosses the byte
            # ceiling WHILE STILL RUNNING, not merely bound a later read.
            executor = SubprocessExecutor(max_workers=self._subprocess_max_workers)
            try:
                result = await executor.execute_with_limits(
                    command=cmd,
                    working_dir=str(self.repo_path),
                    timeout_seconds=timeout_seconds or DEFAULT_SEARCH_TIMEOUT_SECONDS,
                    output_file_path=temp_path,
                    max_output_bytes=_MAX_READ_BYTES,
                )

                if result.timed_out:
                    raise TimeoutError(
                        f"Search timed out after {result.timeout_seconds} seconds "
                        f"(pattern='{pattern}', path='{search_path}')"
                    )

                if result.status == ExecutionStatus.ERROR:
                    # Bug #173: Differentiate exit code 1 (no matches) from actual errors
                    if result.exit_code == 1 and not result.stderr_output:
                        # Exit code 1 with no stderr = no matches found (normal ripgrep behavior)
                        logger.debug("ripgrep found no matches (exit code 1)")
                        return [], 0
                    else:
                        # Exit code 2+ or stderr present = actual error (Finding 3.1, v10.4.4)
                        stderr = result.stderr_output or result.error_message or ""
                        raise RipgrepExecutionError(
                            f"ripgrep failed: exit_code={result.exit_code}, stderr={stderr}"
                        )

                # Issue #1601 remediation (Priority 2): the read+parse is a
                # synchronous, potentially expensive operation (file I/O,
                # UTF-8 decode, JSON/regex parsing) -- run it in a worker
                # thread via anyio.to_thread.run_sync so it never blocks
                # the asyncio event loop for the whole server.
                #
                # Bug #1590 review round 5 finding 2 fix: when a shared
                # ``deadline`` was supplied, additionally bound this
                # offloaded hop with the SAME thread-watchdog idiom used
                # elsewhere in this file. This phase calls
                # _to_repo_relative once PER MATCH, which resolves the
                # reported path again (candidate.resolve()) -- a second,
                # previously undisclosed instance of the identical
                # Path.resolve()-on-NFS class of gap round 4's R1 fix
                # addressed for the trigram pre-filter's resolve loop; a
                # live reproduction proved a single wedged resolve here
                # absorbs a full multi-second wedge past an
                # already-exhausted budget and raises nothing. When there
                # is no deadline, this stays the plain
                # anyio.to_thread.run_sync offload exactly as before.
                #
                # Bug #1590 review round 6: floored at
                # _MIN_PARSE_TIMEOUT_SECONDS (not 0.0) -- see that
                # constant's comment. Without the floor, a deadline that
                # has technically already elapsed (because the prior
                # ripgrep-subprocess phase's ceil()-rounded-up allowance
                # let real wall-clock time run a little past it, even
                # though ripgrep itself finished within ITS OWN
                # allowance) hands Thread.join(timeout=0) to the
                # watchdog, which can never "win" even against a parse
                # that completes in milliseconds -- spuriously discarding
                # a fully successful, already-completed result.
                if deadline is not None:
                    from .trigram_index_manager import _run_with_thread_watchdog

                    remaining_parse = max(
                        _MIN_PARSE_TIMEOUT_SECONDS, deadline - time.monotonic()
                    )
                    parse_result, parse_timed_out = await anyio.to_thread.run_sync(
                        _run_with_thread_watchdog,
                        lambda: self._read_and_parse_ripgrep(
                            temp_path, max_results, context_lines
                        ),
                        remaining_parse,
                        "regex_search_read_and_parse_ripgrep",
                    )
                    if parse_timed_out:
                        raise TimeoutError(
                            f"Parsing ripgrep output for pattern={pattern!r} "
                            f"path={search_path} exceeded its "
                            f"{remaining_parse:.3f}s remaining search deadline"
                        )
                else:
                    parse_result = await anyio.to_thread.run_sync(
                        self._read_and_parse_ripgrep,
                        temp_path,
                        max_results,
                        context_lines,
                    )
                (
                    matches,
                    total,
                    bytes_read,
                    reader_read_capped,
                    content_capped,
                ) = parse_result
                # read_capped is the OR of THREE independent signals: the
                # executor killed the still-running subprocess because its
                # output crossed the ceiling (result.output_capped -- a
                # real dataclass field, accessed directly so a future
                # rename fails loud rather than being silently masked),
                # this read itself stopped early because the (already
                # complete) temp file exceeded the ceiling
                # (reader_read_capped), and/or the aggregate result-content
                # budget stopped accumulation early (content_capped --
                # Issue #1601 round 5 Priority 1). Any one alone means the
                # scan was incomplete.
                self._last_search_read_capped = (
                    bool(result.output_capped) or reader_read_capped or content_capped
                )
                # Issue #1601 Priority 9: when the SUBPROCESS itself was
                # killed by the byte cap, the real cutoff is the output
                # file's on-disk size at that moment -- which can be far
                # LARGER than the reader's own bytes_read if max_results
                # stopped parsing after only the first internal chunk.
                # Reporting bytes_read in that case would understate the
                # true cutoff.
                if result.output_capped:
                    try:
                        self._last_read_capped_bytes = os.path.getsize(temp_path)
                    except OSError as size_error:
                        logger.debug(
                            "Could not stat capped output file %s (%s); "
                            "falling back to reader bytes_read (%d)",
                            temp_path,
                            size_error,
                            bytes_read,
                        )
                        self._last_read_capped_bytes = bytes_read
                else:
                    self._last_read_capped_bytes = bytes_read

            finally:
                # Issue #1601 round 5 (Priority 2): offload this
                # synchronous, potentially-blocking call off the event loop.
                await anyio.to_thread.run_sync(executor.shutdown, True)
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return matches, total

    @staticmethod
    def _read_and_parse_glob_output(
        output_path: str,
    ) -> Tuple[List[str], Optional[str]]:
        """Read and parse glob_files.py's JSON output file.

        Bug #1608 follow-up (F1): extracted so the caller can offload this
        synchronous file I/O + UTF-8 decode + json.loads via
        anyio.to_thread.run_sync, mirroring _read_and_parse_grep and
        _read_and_parse_ripgrep's identical offload rationale -- it must
        never run directly on the event loop.

        Returns:
            (files, decode_error) -- decode_error is the JSONDecodeError's
            message when the (non-empty) output failed to parse as a JSON
            list, else None. Bug #1608 follow-up (F3): returning the raw
            decode-failure signal (rather than logging it here) lets the
            caller decide whether a failure is a genuine parse error or
            capacity-truncation (result.output_capped), which this
            file-only helper has no visibility into.
        """
        with open(output_path, "r") as f:
            output = f.read().strip()

        if not output:
            return [], None

        try:
            files = json.loads(output)
        except json.JSONDecodeError as e:
            return [], str(e)

        if not isinstance(files, list):
            logger.warning(f"glob_files.py returned non-list: {type(files)}")
            return [], None

        return files, None

    async def _find_files_by_patterns(
        self,
        search_path: Path,
        include_patterns: List[str],
        exclude_patterns: Optional[List[str]],
        timeout_seconds: int,
    ) -> List[str]:
        """Use subprocess-based glob script with timeout and process isolation.

        Supports the following pattern types to match ripgrep's -g flag behavior:
        - "**/file.java" - Recursive search from search_path
        - "code/**/file.java" - Recursive search from search_path/code
        - "code/src/file.java" - Explicit path (non-recursive)
        - "*.java" - Simple pattern (recursive from search_path)

        Args:
            search_path: Base directory to search from. All patterns resolved relative to this path.
            include_patterns: List of glob patterns following ripgrep -g flag syntax.
            exclude_patterns: Optional list of patterns to exclude from results.
            timeout_seconds: Maximum time to spend searching (enforced via subprocess timeout).

        Returns:
            List of relative file paths as strings for all files matching include patterns
            and not matching exclude patterns. Empty list if no matches found.

        Raises:
            ValueError: If search_path doesn't exist.
            TimeoutError: If file discovery exceeds timeout_seconds.
        """
        # Validate search path exists. Issue #1608: offloaded via
        # anyio.to_thread.run_sync -- this project's Production Scale
        # invariant (CLAUDE.md) forbids a synchronous filesystem call
        # directly inside async def, since on the `hard` NFSv3 golden-repo
        # mount a bare os.stat() can block the whole event loop
        # indefinitely. Mirrors the plain (no-deadline) offload of the
        # identical check in search() above.
        path_exists = await anyio.to_thread.run_sync(search_path.exists)
        if not path_exists:
            raise ValueError(f"Search path does not exist: {search_path}")

        # Create temp files for config and output
        config_fd, config_path = tempfile.mkstemp(suffix=".json", prefix="glob_config_")
        output_fd, output_path = tempfile.mkstemp(suffix=".json", prefix="glob_output_")
        os.close(config_fd)
        os.close(output_fd)

        try:
            # Write glob config to temp file
            config = {
                "search_path": str(search_path),
                "include_patterns": include_patterns,
                "exclude_patterns": exclude_patterns,
            }
            with open(config_path, "w") as f:
                json.dump(config, f)

            # Get path to glob_files.py script (in scripts/ directory)
            # Path from src/code_indexer/global_repos/regex_search.py -> project_root/scripts/glob_files.py
            script_path = (
                Path(__file__).parent.parent.parent.parent / "scripts" / "glob_files.py"
            )
            # Bug #1608 follow-up (F2): offload via anyio.to_thread.run_sync,
            # mirroring the search_path.exists() offload above -- this
            # project's Production Scale invariant (CLAUDE.md) forbids a
            # synchronous filesystem call directly inside async def
            # regardless of how fast it looks locally (the destination here
            # happens to be the install directory, not the NFS golden-repo
            # mount, but the invariant is categorical, not risk-scored).
            script_exists = await anyio.to_thread.run_sync(script_path.exists)
            if not script_exists:
                raise RuntimeError(f"glob_files.py script not found at {script_path}")

            # Execute glob script with subprocess executor for timeout + async protection
            cmd = ["python3", str(script_path), config_path]

            executor = SubprocessExecutor(max_workers=self._subprocess_max_workers)
            try:
                # Issue #1608 (mirrors #1601 Fix direction 4a elsewhere in
                # this module): max_output_bytes makes the executor
                # terminate the glob script itself if its stdout crosses
                # the byte ceiling WHILE STILL RUNNING, bounding both the
                # temp output file and the subsequent f.read() below.
                result = await executor.execute_with_limits(
                    command=cmd,
                    working_dir=str(self.repo_path),
                    timeout_seconds=timeout_seconds,
                    output_file_path=output_path,
                    max_output_bytes=_MAX_READ_BYTES,
                )

                if result.timed_out:
                    raise TimeoutError(
                        f"File discovery timed out after {timeout_seconds} seconds"
                    )

                if result.status == ExecutionStatus.ERROR:
                    logger.warning(f"glob_files.py failed: {result.error_message}")
                    # Return empty list on error (graceful degradation)
                    return []

                # Bug #1608 follow-up (F1): read+parse is a synchronous,
                # potentially expensive operation (file I/O, UTF-8 decode,
                # json.loads) -- run it in a worker thread via
                # anyio.to_thread.run_sync so it never blocks the event
                # loop, mirroring _read_and_parse_ripgrep/
                # _read_and_parse_grep's identical offload rationale.
                files, decode_error = await anyio.to_thread.run_sync(
                    self._read_and_parse_glob_output, output_path
                )

                # Bug #1608 follow-up (F3): the glob script emits ONE
                # atomic JSON document (a single print(json.dumps(files))
                # in scripts/glob_files.py). When max_output_bytes
                # truncates it mid-write, json.loads necessarily fails.
                # Consume result.output_capped here -- exactly like
                # _search_ripgrep/_search_grep's self._last_search_read_capped
                # signal -- so a truncation is diagnosed as a capacity
                # limit (feeding the existing AC-I3 fleet-scale warning
                # already logged by search()), not silently reported as
                # "no matches" behind a misleading parse-error log.
                if result.output_capped:
                    self._last_search_read_capped = True
                elif decode_error:
                    logger.warning(
                        f"Failed to parse glob output as JSON: {decode_error}"
                    )
                return files

            finally:
                # Issue #1601 round 5 (Priority 2): offload this
                # synchronous, potentially-blocking call off the event loop.
                await anyio.to_thread.run_sync(executor.shutdown, True)

        finally:
            # Clean up temp files
            if os.path.exists(config_path):
                os.remove(config_path)
            if os.path.exists(output_path):
                os.remove(output_path)

    def _build_grep_command(
        self,
        pattern: str,
        case_sensitive: bool,
        context_lines: int,
        recursive: bool,
        file_list: Optional[List[str]] = None,
    ) -> List[str]:
        """Build grep command with common flags.

        Always includes -H flag to force filename output even when searching
        a single file. This ensures consistent parsing of grep output where
        the regex expects 'filename:line:content' format.
        """
        cmd = ["grep", "-E", "-H"]
        if recursive:
            cmd.append("-rn")
        else:
            cmd.append("-n")
        if not case_sensitive:
            cmd.append("-i")
        if context_lines > 0:
            cmd.extend(["-C", str(context_lines)])
        cmd.append(pattern)
        if file_list:
            cmd.extend(file_list)
        return cmd

    def _process_grep_match_line(
        self,
        match_line,
        matches: List[RegexMatch],
        total: int,
        max_results: int,
        context_before: List[str],
        collecting_context_after: bool,
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Handle one grep match-line record ("path:linenum:content").

        Mirrors ``_process_ripgrep_match_event``: capacity checked first
        (max_results, then the round-5 Priority 1 aggregate ``budget``),
        both stopping via the same ``len(matches) + 1`` sentinel.
        ``collecting_context_after`` stays UNCHANGED when a line is
        dropped for an invalid path/line-number -- only an accepted
        match sets it True.

        Returns: (stop, total, context_before, collecting_context_after)
        """
        if budget is None:
            budget = _ResultContentBudget()
        if len(matches) >= max_results:
            return True, len(matches) + 1, context_before, collecting_context_after

        rel_path = self._to_repo_relative(match_line.group(1))
        if rel_path is None:
            # Bug #1401: never silently store an absolute/escaping path as
            # file_path -- drop the match (already warned), don't stop.
            return False, total, context_before, collecting_context_after

        try:
            line_num = int(match_line.group(2))
        except ValueError:
            logger.warning(f"Invalid line number in grep output: {match_line.group(0)}")
            return False, total, context_before, collecting_context_after

        bounded_content = _bounded_match_content(match_line.group(3))
        if not budget.try_reserve(bounded_content):
            return True, len(matches) + 1, context_before, collecting_context_after

        matches.append(
            RegexMatch(
                file_path=rel_path,
                line_number=line_num,
                column=1,
                line_content=bounded_content,
                context_before=context_before.copy(),
                context_after=[],
            )
        )
        return False, total + 1, [], True

    @staticmethod
    def _process_grep_context_line(
        context_line,
        matches: List[RegexMatch],
        context_before: List[str],
        collecting_context_after: bool,
        context_lines: int,
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Handle one grep context-line record ("path-linenum-content").

        Mirrors ``_process_ripgrep_context_event``'s bound + aggregate
        ``budget`` reservation (round 5 Priority 1); ``stop=True`` when
        this line alone would exceed the budget.

        Returns: (context_before, collecting_context_after, stop)
        """
        if budget is None:
            budget = _ResultContentBudget()
        line_content = _bounded_match_content(context_line.group(3))
        if not budget.try_reserve(line_content):
            return context_before, collecting_context_after, True
        if collecting_context_after and matches:
            if len(matches[-1].context_after) < context_lines:
                matches[-1].context_after.append(line_content)
                return context_before, collecting_context_after, False
            context_before.append(line_content)
            return context_before, False, False
        context_before.append(line_content)
        return context_before, collecting_context_after, False

    def _parse_grep_output(
        self,
        output: Union[str, Iterable[str]],
        max_results: int,
        context_lines: int,
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Parse grep match/context lines into RegexMatch objects (see
        ``_ResultContentBudget`` for ``budget``). Return stays
        (matches, total) for direct-caller compatibility."""
        if budget is None:
            budget = _ResultContentBudget()
        matches: List[RegexMatch] = []
        total = 0
        context_before: List[str] = []
        collecting = False
        lines = output.splitlines() if isinstance(output, str) else output
        for line in lines:
            if line.strip() == "--":
                collecting, context_before = False, []
                continue
            m = re.match(r"^(.+?):(\d+):(.*)$", line)
            if m:
                stop, total, context_before, collecting = self._process_grep_match_line(
                    m, matches, total, max_results, context_before, collecting, budget
                )
                if stop:
                    break
                continue
            c = re.match(r"^(.+?)-(\d+)-(.*)$", line)
            if c:
                context_before, collecting, stop = self._process_grep_context_line(
                    c, matches, context_before, collecting, context_lines, budget
                )
                if stop:
                    break
        return matches, total

    def _read_and_parse_grep(
        self,
        temp_path: str,
        max_results: int,
        context_lines: int,
    ) -> tuple:
        """Synchronous unit of work: stream-read ``temp_path`` and parse it
        as grep output, in one call.

        Issue #1601 remediation (Priority 2): this is the exact synchronous
        body offloaded to a worker thread via ``anyio.to_thread.run_sync``
        in ``_search_grep`` -- mirrors ``_read_and_parse_ripgrep``.

        Returns:
            (matches, total, bytes_read, read_capped, content_capped) --
            ``content_capped`` (Issue #1601 round 5, Priority 1) mirrors
            ``_read_and_parse_ripgrep``'s identical signal.
        """
        reader = _BoundedLineReader(temp_path, _MAX_READ_BYTES)
        budget = _ResultContentBudget()
        matches, total = self._parse_grep_output(
            reader, max_results, context_lines, budget
        )
        return (
            matches,
            total,
            reader.bytes_read,
            reader.read_capped,
            budget.capped,
        )

    @staticmethod
    def _scan_multiline_content(
        content: str,
        compiled,
        rel_path: str,
        matches: List[RegexMatch],
        max_results: int,
        budget: Optional["_ResultContentBudget"] = None,
    ) -> tuple:
        """Scan one file's content for matches; capacity checked first
        (max_results, then the aggregate ``budget`` -- see
        ``_ResultContentBudget``). Returns (stop, count): unchanged
        2-tuple for direct-call compatibility."""
        if budget is None:
            budget = _ResultContentBudget()
        count = 0
        for m in compiled.finditer(content):
            if len(matches) >= max_results:
                return True, count + 1
            bounded_content = _bounded_match_content(m.group(0))
            if not budget.try_reserve(bounded_content):
                return True, count + 1
            count += 1
            start_line = content[: m.start()].count("\n") + 1
            col = m.start() - content.rfind("\n", 0, m.start())
            matches.append(
                RegexMatch(
                    file_path=rel_path,
                    line_number=start_line,
                    column=col,
                    line_content=bounded_content,
                    context_before=[],
                    context_after=[],
                )
            )
        return False, count

    def _search_python_multiline(
        self,
        pattern: str,
        search_path: Path,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        case_sensitive: bool,
        max_results: int,
    ) -> tuple:
        """Python re.DOTALL search for multiline patterns.

        Used when multiline=True on the grep engine path, or as a fallback
        when ripgrep is unavailable.

        Fully synchronous (its own ``os.walk`` + per-file ``open()``/
        ``read()``) -- Issue #1601 remediation (Priority 2): callers MUST
        invoke this via ``anyio.to_thread.run_sync`` (see ``_search_grep``)
        rather than calling it directly from an ``async def``, so this
        potentially expensive filesystem walk never blocks the event loop.

        Issue #1601 (Fix direction #5/AC-D2): each file's content is read
        with a byte ceiling identical in scope to the two temp-file read
        sites (``_MAX_READ_BYTES``) -- a narrower exposure (bounded by a
        single file's size, not repo-wide match volume) but the same
        "read everything before deciding whether to keep it" anti-pattern.
        A file whose real size exceeds the ceiling is scanned only up to
        the ceiling; its contribution to ``total`` becomes a lower bound
        and ``self._last_search_read_capped`` (hence the eventual
        ``RegexSearchResult.read_capped``) is set True. This never crashes
        or produces a corrupted match -- ``compiled.finditer`` only ever
        sees the (possibly truncated) content it is given, so any match
        returned is a genuine match within that substring.
        """
        from fnmatch import fnmatch

        flags = re.DOTALL
        if not case_sensitive:
            flags |= re.IGNORECASE
        try:
            compiled = re.compile(pattern, flags)
        except re.error as e:
            raise ValueError(f"Invalid regex pattern: {e}") from e

        matches: List[RegexMatch] = []
        total = 0
        any_file_capped = False
        # Issue #1601 round 5 (Priority 1): ONE budget shared across the
        # whole walk, so the aggregate is tracked across ALL files, not
        # reset per file.
        budget = _ResultContentBudget()

        for root, _dirs, files in os.walk(search_path):
            # Skip internal directories
            rel_root = os.path.relpath(root, search_path)
            if rel_root.startswith(".code-indexer") or rel_root.startswith(".git"):
                continue

            for fname in files:
                file_path = os.path.join(root, fname)
                rel_path = self._to_repo_relative(file_path)
                if rel_path is None:
                    # Bug #1401: never silently store an absolute/escaping
                    # path as file_path -- skip the file (already warned).
                    continue

                if include_patterns and not any(
                    fnmatch(fname, p) for p in include_patterns
                ):
                    continue
                if exclude_patterns and any(
                    fnmatch(fname, p) for p in exclude_patterns
                ):
                    continue

                try:
                    with open(file_path, "rb") as f:
                        # Read one byte beyond the ceiling as a cheap probe
                        # for "did this file actually exceed the ceiling",
                        # without a second pass over the file. Priority 3
                        # remediation: read in BINARY mode so the ceiling
                        # is enforced in true BYTES, not text-mode decoded
                        # characters (a text-mode f.read(n) counts
                        # characters, letting a multi-byte-UTF-8-heavy file
                        # read up to ~4x the intended byte ceiling before
                        # tripping).
                        raw_content = f.read(_MAX_READ_BYTES + 1)
                except OSError:
                    logger.debug("Skipping unreadable file: %s", file_path)
                    continue

                file_capped = len(raw_content) > _MAX_READ_BYTES
                if file_capped:
                    raw_content = raw_content[:_MAX_READ_BYTES]
                    any_file_capped = True
                content = raw_content.decode("utf-8", errors="replace")

                stop, file_count = self._scan_multiline_content(
                    content, compiled, rel_path, matches, max_results, budget=budget
                )
                total += file_count
                if stop:
                    self._last_search_read_capped = any_file_capped or budget.capped
                    self._last_read_capped_bytes = (
                        _MAX_READ_BYTES if any_file_capped else None
                    )
                    return matches, total

        self._last_search_read_capped = any_file_capped or budget.capped
        self._last_read_capped_bytes = _MAX_READ_BYTES if any_file_capped else None
        return matches, total

    async def _search_grep(
        self,
        pattern: str,
        search_path: Path,
        include_patterns: Optional[List[str]],
        exclude_patterns: Optional[List[str]],
        case_sensitive: bool,
        context_lines: int,
        max_results: int,
        timeout_seconds: Optional[int],
        multiline: bool = False,
        pcre2: bool = False,
    ) -> tuple:
        """Fallback search using grep with timeout protection."""
        # For multiline searches, use Python re.DOTALL fallback. Issue
        # #1601 remediation (Priority 2): _search_python_multiline is a
        # fully synchronous method (its own os.walk + per-file read +
        # regex scan) -- offload it to a worker thread so it never blocks
        # the event loop.
        if multiline:
            return await anyio.to_thread.run_sync(
                self._search_python_multiline,
                pattern,
                search_path,
                include_patterns,
                exclude_patterns,
                case_sensitive,
                max_results,
            )

        timeout = timeout_seconds or DEFAULT_SEARCH_TIMEOUT_SECONDS
        has_path_patterns = include_patterns and any(
            "/" in pat for pat in include_patterns
        )

        if has_path_patterns:
            # Use find to get files matching all patterns (both path and simple)
            # Type assertion: include_patterns is not None here (checked by has_path_patterns)
            assert include_patterns is not None
            file_list = await self._find_files_by_patterns(
                search_path, include_patterns, exclude_patterns, timeout
            )
            if not file_list:
                return [], 0

            cmd = self._build_grep_command(
                pattern, case_sensitive, context_lines, False, file_list
            )
        else:
            # Original behavior: recursive grep with --include/--exclude
            cmd = self._build_grep_command(pattern, case_sensitive, context_lines, True)
            if include_patterns:
                for pat in include_patterns:
                    cmd.extend(["--include", pat])
            if exclude_patterns:
                for pat in exclude_patterns:
                    cmd.extend(["--exclude", pat])
            # Always exclude CIDX internal directories (Bug #158)
            cmd.extend(["--exclude-dir", ".code-indexer"])
            cmd.extend(["--exclude-dir", ".git"])
            cmd.append(str(search_path))

        # Create temp file for output
        temp_fd, temp_path = tempfile.mkstemp(suffix=".txt", prefix="grep_search_")
        os.close(temp_fd)

        try:
            # Execute with SubprocessExecutor for async + timeout protection.
            # Issue #1601 (Fix direction 4a): max_output_bytes makes the
            # executor terminate grep itself if its output crosses the byte
            # ceiling WHILE STILL RUNNING, not merely bound a later read.
            executor = SubprocessExecutor(max_workers=self._subprocess_max_workers)
            try:
                result = await executor.execute_with_limits(
                    command=cmd,
                    working_dir=str(self.repo_path),
                    timeout_seconds=timeout,
                    output_file_path=temp_path,
                    max_output_bytes=_MAX_READ_BYTES,
                )

                if result.timed_out:
                    raise TimeoutError(
                        f"Search timed out after {result.timeout_seconds} seconds "
                        f"(pattern='{pattern}', path='{search_path}')"
                    )

                if result.status == ExecutionStatus.ERROR:
                    # Bug #173: Differentiate exit code 1 (no matches) from actual errors
                    if result.exit_code == 1 and not result.stderr_output:
                        # Exit code 1 with no stderr = no matches found (normal grep behavior)
                        logger.debug("grep found no matches (exit code 1)")
                        return [], 0
                    else:
                        # Exit code 2+ or stderr present = actual error (Finding 3.1, v10.4.4)
                        stderr = result.stderr_output or result.error_message or ""
                        raise RipgrepExecutionError(
                            f"grep failed: exit_code={result.exit_code}, stderr={stderr}"
                        )

                # Issue #1601 remediation (Priority 2): offload the
                # synchronous read+parse to a worker thread (see
                # _search_ripgrep's identical rationale above).
                (
                    matches,
                    total,
                    bytes_read,
                    reader_read_capped,
                    content_capped,
                ) = await anyio.to_thread.run_sync(
                    self._read_and_parse_grep,
                    temp_path,
                    max_results,
                    context_lines,
                )
                # See _search_ripgrep's identical comment: read_capped is
                # the OR of the executor's early-kill signal (direct
                # attribute access -- a future rename fails loud rather
                # than being silently masked), this read's own
                # byte-ceiling signal, and the aggregate result-content
                # budget signal (Issue #1601 round 5, Priority 1).
                self._last_search_read_capped = (
                    bool(result.output_capped) or reader_read_capped or content_capped
                )
                # Issue #1601 Priority 9: see _search_ripgrep's identical
                # rationale -- report the real output file size when the
                # subprocess itself was killed, not the reader's own
                # (potentially much smaller) bytes_read.
                if result.output_capped:
                    try:
                        self._last_read_capped_bytes = os.path.getsize(temp_path)
                    except OSError as size_error:
                        logger.debug(
                            "Could not stat capped output file %s (%s); "
                            "falling back to reader bytes_read (%d)",
                            temp_path,
                            size_error,
                            bytes_read,
                        )
                        self._last_read_capped_bytes = bytes_read
                else:
                    self._last_read_capped_bytes = bytes_read

            finally:
                # Issue #1601 round 5 (Priority 2): offload this
                # synchronous, potentially-blocking call off the event loop.
                await anyio.to_thread.run_sync(executor.shutdown, True)
        finally:
            # Clean up temp file
            if os.path.exists(temp_path):
                os.remove(temp_path)

        return matches, total
