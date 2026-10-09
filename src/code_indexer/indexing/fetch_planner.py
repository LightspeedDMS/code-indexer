"""Group files into fetch requests bounded by BYTES, not by file count.

Why bytes
---------
A delegated backend returns the bytes of every requested path in one response,
and the client holds a record at a time while parsing it -- but the *request* is
what decides how much work is in flight and how large the response grows. Peak
client memory tracks the biggest batch's BYTES, and a count-based batch cannot
bound that, because file sizes vary by orders of magnitude inside one
repository.

Measured against a real server, 30 files of 8 MB each:

=================  ========  ========  ==================
strategy           requests  peak MB   biggest batch MB
=================  ========  ========  ==================
25 files per call         2    209.0               200.0
8 MB per call            30     24.1                 8.0
=================  ========  ========  ==================

workspace-mcp admits a single file up to 64 MiB, so twenty-five of them is a
1.6 GB request. A byte budget is flat by construction, and costs nothing to
compute: the manifest the pass has already paged carries every file's size.

Why not one file per call
-------------------------
The opposite extreme is worse, and not mainly for the reason one expects. Over
500 files at 2 ms per request, per-file took 500 requests and 2.02 s against 20
requests and 0.47 s for batches -- but the decisive number is connections. The
plugin's HTTP client pools nothing, so a request is a TCP connection:

=================  =========  =====================
strategy           files      TCP connections used
=================  =========  =====================
per-file                 500                    501
batched                  500                     21
=================  =========  =====================

A 50,000-file repository fetched one file at a time needs 50,000 connections per
pass, against an ephemeral port range of roughly 28,000 and a 60 s TIME_WAIT.
That is port exhaustion partway through an index, on a pod that does this for
hundreds of repositories.

Threads do not substitute: the same per-file work across eight of them was still
slower than batching on one, at twice the memory.

Why the request is bounded too
------------------------------
The request is a JSON body, ``{"source_path": ..., "paths": [...]}``, and
workspace-mcp refuses a body over 100 KB (Nest's default JSON limit) with a 413.
A file COUNT does not bound that: 1000 paths of 85 bytes -- an ordinary depth
for a TypeScript monorepo -- is 87 KB of paths alone, and measured against the
server a batch like that from agent-e2e came back 413, failing every file in
it. So a batch is also capped by the encoded size of its paths, counted exactly
as the plugin serialises them.
"""

from __future__ import annotations

import json
from typing import Callable, Iterator, List, Optional, Sequence

#: Bytes per fetch request. Chosen from the measurements above: large enough
#: that round trips stop dominating, small enough that peak client memory stays
#: in tens of megabytes whatever the file sizes are.
DEFAULT_FETCH_BUDGET_BYTES = 8 * 1024 * 1024

#: Paths per fetch request. Memory does not need this -- the byte budget covers
#: it -- but the REQUEST is a JSON array of paths, and a repository of tiny
#: files would otherwise put a hundred thousand of them in one body. At a
#: typical 40-byte path this keeps the request itself under ~40 KB.
DEFAULT_FETCH_BUDGET_FILES = 1000

#: Encoded bytes of the ``paths`` array per fetch request. workspace-mcp's body
#: limit is 100 KB; this leaves room for the envelope and ``source_path`` (at
#: most PATH_MAX), so no batch the planner forms can be refused for its size.
DEFAULT_FETCH_BUDGET_REQUEST_BYTES = 64 * 1024


def encoded_path_cost(path: str) -> int:
    """Bytes ``path`` adds to the request body, exactly as the plugin encodes it.

    ``json.dumps`` with its defaults (quotes, escapes, ``ensure_ascii``) plus
    the ``", "`` separator -- so a path of non-ASCII names or quotes is charged
    for its escaped length, not its character count.
    """
    return len(json.dumps(path)) + 2


def plan_fetch_batches(
    paths: Sequence[str],
    size_of: Callable[[str], Optional[int]],
    *,
    budget_bytes: int = DEFAULT_FETCH_BUDGET_BYTES,
    budget_files: int = DEFAULT_FETCH_BUDGET_FILES,
    budget_request_bytes: int = DEFAULT_FETCH_BUDGET_REQUEST_BYTES,
) -> Iterator[List[str]]:
    """Yield the requested paths grouped into batches, in order.

    Args:
        paths: repo-relative paths, in the order they should be fetched.
        size_of: a file's size in bytes, or None when it is not known. The
            manifest's ``size`` is exactly this.
        budget_bytes: ceiling on the summed size of one batch.
        budget_files: ceiling on the number of paths in one batch.
        budget_request_bytes: ceiling on the encoded size of one batch's paths
            in the request body (see :func:`encoded_path_cost`).

    A file whose own size exceeds the budget ends up ALONE rather than dropped
    or refused: the point is to bound a batch, not to decide what is indexable,
    and the server admits an oversized file on its own for the same reason.
    The budget is then exceeded by exactly one file's worth, which is the
    smallest overshoot possible while still making progress.

    A file of UNKNOWN size is also sent alone. There is no safe way to add an
    unmeasurable file to a batch whose whole purpose is a measured ceiling, and
    the case is rare -- a manifest miss means the file appeared after the pass
    began. A lone request is still bounded, by the server's own per-file cap.

    Order is preserved and every path is yielded exactly once, so a caller can
    zip the results back against its own list.
    """
    if budget_bytes <= 0:
        raise ValueError(f"budget_bytes must be positive, got {budget_bytes}")
    if budget_files <= 0:
        raise ValueError(f"budget_files must be positive, got {budget_files}")
    if budget_request_bytes <= 0:
        raise ValueError(
            f"budget_request_bytes must be positive, got {budget_request_bytes}"
        )

    batch: List[str] = []
    used = 0
    request = 0

    for path in paths:
        size = size_of(path)

        # Unmeasurable: it travels alone, because there is no way to add an
        # unknown quantity to a measured ceiling. Flush whatever was
        # accumulating first so ordering is preserved.
        #
        # There is deliberately NO branch here for a file that is merely
        # oversized. The rule below already gives it a batch of its own: it
        # either starts one (the accumulating batch is flushed because it cannot
        # fit) or occupies an empty one and forces a flush at the next file.
        # An explicit branch for it was written, and mutation testing showed
        # deleting it changed nothing -- 2000 randomised shapes, zero
        # divergence. It was dead code dressed as a safeguard.
        if size is None:
            if batch:
                yield batch
                batch, used, request = [], 0, 0
            yield [path]
            continue

        cost = encoded_path_cost(path)
        if batch and (
            used + size > budget_bytes
            or len(batch) >= budget_files
            or request + cost > budget_request_bytes
        ):
            yield batch
            batch, used, request = [], 0, 0

        batch.append(path)
        used += size
        request += cost

    if batch:
        yield batch
