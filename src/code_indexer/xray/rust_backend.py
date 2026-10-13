"""RustNativeBackend: Rust-native xray evaluator backend (Story #1023).

Replaces PythonEvaluatorSandbox.run_batch() in the xray pipeline with a
Rust-native scanner backend.

Pipeline:
1. Validate Rust evaluator code via validate_rust_evaluator()
2. Write validated Rust code to a temp file
3. Invoke xray-cli subprocess with --dynlib, --json, --files-from flags
4. Parse JSON output and group findings by file path
5. Return List[(matches, errors, meta)] — one tuple per file spec

Error contract:
- ValidationError: all files get error tuples with error_type="ValidationError"
  (validation is per-file-set but the evaluator never ran, so each spec is
  answered individually)
- Missing binary: ONE deduplicated error tuple with error_type="BinaryNotFound"
  and file_path=="" -- the batch-level convention below, because no file was
  evaluated
- JSON error field set: ONE deduplicated error tuple with file_path==""
- Subprocess non-zero exit + no parseable JSON: ONE deduplicated error tuple
  with file_path==""

Batch-level failure convention: an error tuple whose file_path is the empty
string means "this failure applies to the whole batch; zero files were
evaluated". XRaySearchEngine.run() reads exactly that to decide whether a
batch item may be counted in files_processed, so any new whole-batch failure
path MUST use it -- otherwise the job reports a complete, empty scan.

Binary location: see resolve_xray_cli_path() -- $CIDX_XRAY_CLI_PATH first,
then the source-tree build output.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
import uuid
from abc import ABC, abstractmethod
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import (
    Any,
    Callable,
    Dict,
    List,
    NamedTuple,
    Optional,
    Protocol,
    Tuple,
)

from code_indexer.utils.path_confinement import is_resolved_within_root
from code_indexer.xray.execution_backend import (
    BatchResult,
    XrayExecutionBackend,
    XrayGraphRequest,
    XrayScanOutcome,
    XrayScanRequest,
)

from code_indexer.utils.worker_budget import (
    detect_worker_count,
    per_process_budget,
    read_applied_worker_count,
    read_argv_workers,
    read_config_json_workers,
)

logger = logging.getLogger(__name__)

# Sanitize server-internal paths out of error messages so they are never
# exposed to API callers.
# Rule 1: xray-cache paths — replaced with the generic name "evaluator.rs".
# Allows one optional intermediate path segment (e.g. "build-<hash>-<rand>/")
# so Bug #1425's per-invocation isolated build directory
# (xray-cache/build-<hash>-<random>/<hash>.rs — see rust/xray-core/src/compiler.rs)
# is matched alongside the original flat xray-cache/<hash>.rs shape.
_RE_XRAY_CACHE_PATH = re.compile(r"/[^\s\"']+/xray-cache/(?:[^/\s\"']+/)?[a-f0-9]+\.rs")
# Rule 2: other absolute paths under /home/, /root/, /tmp/ — replaced with a
# redaction token so callers know a path was present but cannot reconstruct it.
_RE_SERVER_PATH = re.compile(r"/(?:home|root|tmp)/[^\s\"':,\])\}]+")

# Environment variable that overrides the CIDX data directory root.
# When set, the xray cache lives at $CIDX_DATA_DIR/xray-cache instead of
# ~/.cidx-server/xray-cache, matching the server's IPC path alignment (Bug #879).
_XRAY_CACHE_DIR_ENV = "CIDX_DATA_DIR"

# Named path segments — must match Rust's get_cache_dir() in cache.rs exactly.
_CIDX_SERVER_DIR_NAME = ".cidx-server"
_XRAY_CACHE_DIR_NAME = "xray-cache"

# R2-4 (consolidated review, Issue #1811/Bug #1812, Codex re-review):
# deadline-aware semaphore bounding concurrent rustc-invoking xray-cli
# launches. The graph admission limiter (H8) bounds concurrent GRAPH JOBS,
# but nothing previously bounded concurrent COMPILES specifically --
# multiple users triggering many simultaneous evaluator compiles (each a
# real rustc process) can exhaust CPU/RAM/temp-disk/NFS at fleet scale
# (~900 repos).
#
# The budget below is NODE-WIDE, and that distinction is the fix. The
# constant used to be named as a per-process maximum of 4 and reasoned
# about as if it bounded the node; a Python module-level semaphore bounds
# exactly one OS process. The cluster runs `uvicorn --workers 2`
# (cet-cidx-overlay/cluster-base/statefulset.yaml), so there were TWO
# independent semaphores of 4 -- up to 8 concurrent rustc against a pod
# limited to `cpu: "2"` and `memory: 4Gi`. The overlay's own comment on
# that flag already requires every per-node budget to be divided by the
# worker count ("...so the HNSW/FTS cache cap (_divided_cap) and the
# provider concurrency governor divide their budgets by 2 -> no OOM");
# this one was not, so it is divided here.
#
# 4 node-wide: rustc is CPU-bound and the pod's quota is 2 CPUs, so the
# budget is 2x the quota -- enough to keep both cores busy across the I/O
# and link phases of a compile without stacking resident compilers (each
# bounded at _COMPILE_RLIMIT_DATA_BYTES of heap) past what 4Gi holds. On a
# single-worker node the effective bound is unchanged at 4, so no existing
# solo/VM deployment changes behaviour.
#
# Still hardcoded rather than a Web UI setting (project convention: no new
# config surface to gate a bug fix; mirrors the documented hardcoded
# constants in graph::analyze::process.rs), with one escape hatch:
# CIDX_XRAY_MAX_CONCURRENT_COMPILES pins the PER-PROCESS bound directly for
# an operator who needs to tune a node live without a release. The contract
# is: set it to (what this node may run concurrently) DIVIDED BY (the number
# of uvicorn workers), because it is applied per worker process.
_XRAY_COMPILE_BUDGET_PER_NODE = 4

# Operator override for the per-PROCESS bound. Deliberately not the node
# budget: an operator reaching for this is tuning one node they are looking
# at, and a per-process number is the one that can be verified against a
# process list.
_MAX_CONCURRENT_COMPILES_ENV = "CIDX_XRAY_MAX_CONCURRENT_COMPILES"

# Extra config.json location to search, on top of the shared helper's
# $CIDX_SERVER_DATA_DIR and ~/.cidx-server: xray's own cache dir, which the
# solo/CLI path sets when the server data dir is not in the environment.
_CONFIG_JSON_EXTRA_DIR_ENVS = (_XRAY_CACHE_DIR_ENV,)


def _read_config_json_workers() -> Optional[int]:
    """xray's view of ``workers`` in config.json (adds the xray cache dir)."""
    return read_config_json_workers(extra_dir_envs=_CONFIG_JSON_EXTRA_DIR_ENVS)


def _read_applied_worker_count() -> Optional[int]:
    """The applied (systemd/launch-json) worker count, or None."""
    return read_applied_worker_count()


def _read_argv_workers() -> Optional[int]:
    """``--workers N`` from this process's command line, or an ancestor's.

    Aliased here, like the two readers above, so it stays substitutable in this
    module's tests rather than being reached through the shared module.
    """
    return read_argv_workers()


def _detect_worker_count() -> int:
    """Worker processes sharing this node's compile budget.

    Passes the module-level readers above as the sources rather than letting the
    shared helper use its defaults, so the lookup happens through THIS module's
    globals -- which is what keeps the xray-specific config.json location in
    play, and what keeps them substitutable in tests.

    Supplying an explicit `sources` tuple means a rung added to the shared
    helper's DEFAULTS is not picked up here. That is exactly what happened with
    the argv rung: it was added as a default, every caller using the defaults
    got it, and this one silently stayed at 4 compiles per process (8 per pod)
    while the MR adding it claimed otherwise. Any new rung must be added here
    too.
    """
    return detect_worker_count(
        sources=(
            _read_argv_workers,
            _read_applied_worker_count,
            _read_config_json_workers,
        )
    )


# Node budget / worker count, floored at 1. Named for its original caller; the
# implementation is shared with the background-job pools now (see
# code_indexer/utils/worker_budget.py and docs/background-work-distribution.md).
_per_process_compile_limit = per_process_budget


def _resolve_max_concurrent_compiles() -> int:
    """Resolve this PROCESS's compile bound. Never raises."""
    import os  # noqa: PLC0415 - stdlib, lazy import to keep startup clean

    raw = os.environ.get(_MAX_CONCURRENT_COMPILES_ENV, "").strip()
    if raw:
        try:
            override = int(raw)
        except ValueError:
            override = 0
        if override >= 1:
            return override
        logger.warning(
            "RustNativeBackend: ignoring %s=%r -- not a positive integer",
            _MAX_CONCURRENT_COMPILES_ENV,
            raw,
        )
    return _per_process_compile_limit(
        _XRAY_COMPILE_BUDGET_PER_NODE, _detect_worker_count()
    )


# Resolved once at import: this process's share of the node budget. Each
# uvicorn worker imports this module separately and resolves its own share,
# which is precisely what makes the product of the two stay inside the node
# budget.
_MAX_CONCURRENT_COMPILES = _resolve_max_concurrent_compiles()
_compile_semaphore = threading.Semaphore(_MAX_CONCURRENT_COMPILES)


def _acquire_compile_slot(timeout_seconds: float) -> bool:
    """Acquire a compile slot, waiting at most `timeout_seconds`.

    Deadline-aware: returns False (never blocks indefinitely) once the
    caller's own remaining operation budget is exhausted, so a saturated
    compile queue degrades to a clear timeout error rather than hanging
    the request.
    """
    return _compile_semaphore.acquire(timeout=timeout_seconds)


def _release_compile_slot() -> None:
    """Release a previously-acquired compile slot."""
    _compile_semaphore.release()


# ---------------------------------------------------------------------------
# Per-compile resource bounds
#
# The compile path already bounds TIME (rustc_driver.rs's 120s
# RUSTC_COMPILE_TIMEOUT, with a process-group kill) and COUNT
# (_MAX_CONCURRENT_COMPILES above). Neither bounds how much MEMORY or SCRATCH
# DISK one compile may consume, and the thing being compiled is a Rust
# evaluator supplied by the caller.
#
# Measurements, rust:1.98.0-slim-bookworm, the real
# `rustc --edition 2021 --crate-type cdylib -C opt-level=2` invocation that
# `evaluator_rustc_command` builds:
#
#   representative evaluator (PREAMBLE + a ~40-line evaluate_node + EPILOGUE)
#     0.36s wall, 138 MiB peak RSS, 4.28 MiB .so
#     smallest RLIMIT_DATA that still compiles it:  704 MiB  (640 MiB fails
#     in the LINK step -- the floor is cc/ld, not rustc)
#     smallest RLIMIT_FSIZE that still compiles it: ~4.5 MiB (the .so itself)
#
#   a 150,000-statement function -- straight-line Rust, no loop for rustc's
#   const-eval step limit to catch and no dead constant for DCE to absorb
#     58s wall (inside the 120s timeout), 4.0 GiB peak RSS
#     on a pod whose whole memory limit is 4Gi. Under the RLIMIT_DATA below
#     the same input dies in the child with "memory allocation of 134217728
#     bytes failed" and arrives as an ordinary compile error.
#
# RLIMIT_DATA rather than RLIMIT_AS: on Linux >= 4.7 RLIMIT_DATA covers
# private anonymous mappings, i.e. the heap, which is what a compile bomb
# grows. RLIMIT_AS counts reserved ADDRESS SPACE, and the measurements above
# show the real toolchain needs over 1536 MiB of it to allocate 138 MiB of
# heap -- an RLIMIT_AS tuned to pass legitimate compiles would sit so far
# above actual usage that it bounds nothing.
#
# These are per-PROCESS limits inherited across fork/exec, so they reach
# rustc, cc and ld, each of which gets its own budget; they are not an
# aggregate cap on a process tree. Together with the concurrency bound they
# make the worst case finite, which it has not been until now -- they do not
# by themselves guarantee the pod stays inside 4Gi.
#
# Honest scope: no working compile bomb had been built against this path
# before the 150k-statement one above, and this remains defence in depth
# rather than a patch for a known live exploit. The limits are deliberately
# loose enough that no legitimate evaluator gets caught: see the headroom
# tests in tests/unit/xray/test_rust_backend_compile_resource_limits.py.
# ---------------------------------------------------------------------------

# 1 GiB of heap per compile-capable invocation: 1.45x the 704 MiB floor a
# real rustc+cc+ld compile needs, 7.4x the 138 MiB a representative one
# actually uses, and a quarter of the 4.0 GiB the straight-line bomb above
# reached. Not tighter, because this bound governs the WHOLE xray-cli
# invocation -- the legacy scan path compiles AND then runs the evaluator
# across the repository inside the same process, and that scan's memory
# profile scales with the repository, not with the evaluator.
_DEFAULT_COMPILE_RLIMIT_DATA_BYTES = 1024 * 1024 * 1024

# Operator override for the compile heap bound, in bytes -- the same
# env-var escape hatch CIDX_XRAY_MAX_CONCURRENT_COMPILES uses (no new config
# surface to gate a fix). It governs BOTH places a compile is bounded:
#   * the `--compile-only` spawn, through _apply_compile_resource_limits;
#   * the scan path's compile, which shares its process with the scan. Python
#     cannot cap only part of a process, so the value is handed to xray-cli in
#     its environment and xray-cli sets RLIMIT_DATA on the rustc child it
#     spawns (rust/xray-core/src/compiler/rustc_driver.rs). The scan itself
#     stays uncapped -- its memory scales with the repository and was never
#     measured against this number.
_COMPILE_RLIMIT_DATA_ENV = "CIDX_XRAY_COMPILE_RLIMIT_DATA_BYTES"


def _resolve_compile_rlimit_data_bytes() -> int:
    """The compile heap bound: the env override when it is a positive integer,
    otherwise the measured default. Never raises."""
    raw = os.environ.get(_COMPILE_RLIMIT_DATA_ENV, "").strip()
    if raw:
        try:
            override = int(raw)
        except ValueError:
            override = 0
        if override >= 1:
            return override
        logger.warning(
            "RustNativeBackend: ignoring %s -- not a positive integer",
            _COMPILE_RLIMIT_DATA_ENV,
        )
    return _DEFAULT_COMPILE_RLIMIT_DATA_BYTES


# Resolved once at import: _apply_compile_resource_limits runs between fork and
# exec, where it must not parse or allocate.
_COMPILE_RLIMIT_DATA_BYTES = _resolve_compile_rlimit_data_bytes()

# 128 MiB per FILE (not per compile): 30x the 4.28 MiB a compiled evaluator
# occupies, which is the largest file this path legitimately writes. Its job
# is to stop unbounded growth of a single output file on the scratch volume;
# it does not bound the total bytes a compile writes across many files.
_COMPILE_RLIMIT_FSIZE_BYTES = 128 * 1024 * 1024

# 180s of CPU, a backstop for the case where the wall-clock kill path does
# not fire -- deliberately ABOVE rustc_driver.rs's 120s wall timeout so it
# never becomes the primary timeout and starts failing slow-but-legitimate
# compiles with a SIGKILL instead of a diagnosable timeout message. Counted
# per process and reset by fork, so it does not bound a process tree.
_COMPILE_RLIMIT_CPU_SECONDS = 180

# No core dumps. A rustc that dies at the 1 GiB heap bound would otherwise
# write a core of about that size onto the same scratch volume the bound
# exists to protect.
_COMPILE_RLIMIT_CORE_BYTES = 0


# POSIX only. Imported at module scope because the two helpers below run INSIDE
# the post-fork child, where their docstrings promise no allocation and no
# locks -- and an import does both. `resource` is absent on Windows, where the
# CLI still has to import this module, so the failure is absorbed here once
# rather than inside the child.
try:
    import resource as _resource
except ImportError:  # pragma: no cover - non-POSIX
    _resource = None  # type: ignore[assignment]


def _clamp_to_hard_limit(requested: int, hard: int) -> int:
    """Return `requested`, never above the inherited HARD limit.

    An unprivileged process may lower a limit but not raise it, so asking
    for more than the inherited hard limit raises instead of capping. A
    deployment with a restrictive hard limit must still be able to compile.
    """

    if hard == _resource.RLIM_INFINITY:
        return requested
    return min(requested, hard)


def _apply_compile_resource_limits() -> None:
    """`preexec_fn` for a compile-capable xray-cli spawn: bound the child.

    Runs in the forked child between fork and exec. Everything it does is a
    plain getrlimit/setrlimit syscall -- no locks, no allocation, no logging
    -- which is what makes it safe in the multi-threaded server process
    where a preexec_fn that touched a lock held by another thread at fork
    time would deadlock the child forever.

    For the same reason it cannot report a failure: each limit is set
    independently and a failure to set one is swallowed rather than being
    allowed to abort the exec, because a child that refuses to start turns
    a hardening measure into an outage. What the limits actually are is
    asserted from inside a real spawned child by the unit tests instead.
    """
    if _resource is None:  # pragma: no cover - non-POSIX
        # The tuple below dereferences _resource before the try, so without
        # this the child would raise between fork and exec on a platform
        # lacking the module -- turning a hardening measure into the outage
        # the paragraph above says it must never become.
        return

    for which, requested in (
        (_resource.RLIMIT_DATA, _COMPILE_RLIMIT_DATA_BYTES),
        (_resource.RLIMIT_FSIZE, _COMPILE_RLIMIT_FSIZE_BYTES),
        (_resource.RLIMIT_CPU, _COMPILE_RLIMIT_CPU_SECONDS),
        (_resource.RLIMIT_CORE, _COMPILE_RLIMIT_CORE_BYTES),
    ):
        try:
            _soft, hard = _resource.getrlimit(which)
            _resource.setrlimit(which, (_clamp_to_hard_limit(requested, hard), hard))
        except (ValueError, OSError):
            continue


# ---------------------------------------------------------------------------
# The environment every xray-cli spawn runs with
#
# xray-cli compiles CALLER-SUPPLIED Rust and loads it into its own process, so
# anything in its environment is readable by that code. Inheriting the server's
# environment handed it every secret the server holds (embedding-provider API
# keys, database URLs, tokens). The source-level validator was the only
# barrier. Every xray-cli spawn therefore gets an explicit allowlist instead.
#
# Scope: this removes the INHERITED environment (std::env::vars(), a child's
# own /proc/self/environ). It does not stop a validator bypass from reading
# the server's /proc/<ppid>/environ: the child runs as the server's uid, and
# Linux lets a same-uid process read that file (verified: a child spawned with
# a scrubbed env reads a variable from its parent's environ). Closing that
# needs a separate uid, PID namespace or hidepid /proc for xray-cli -- the
# long-term fix is moving X-Ray into sandbox-mcp.
#
# What xray-cli and the toolchain it drives actually read
# (rust/xray-cli/src/main.rs, rust/xray-core/src/{cache.rs,compiler/}):
#   PATH            -- xray-cli runs `rustc` by name; rustc runs the `cc` linker.
#   HOME            -- cache dir fallback (~/.cidx-server/xray-cache, cache.rs)
#                      and rustup's default RUSTUP_HOME/CARGO_HOME.
#   TMPDIR/TMP/TEMP -- scratch files for rustc, cc and ld.
#   LANG/LANGUAGE/LC_* -- locale for the toolchain's diagnostics.
#   CIDX_DATA_DIR   -- the compile cache root; must match _get_cache_dir() or a
#                      pre-filled artifact is never seen.
#   RUSTUP_HOME/CARGO_HOME -- where a rustup-managed toolchain lives when it is
#                      not under HOME (the rustup proxy fails without them).
#                      The image ships a bare rustc on PATH and sets neither.
# Not passed: RUSTUP_TOOLCHAIN (xray-cli pins it for every rustc it spawns),
# XRAY_TARGET (only read without --files/--files-from, which cidx always
# passes), CIDX_XRAY_CLI_PATH (read by Python, not by the binary).
# ---------------------------------------------------------------------------
_XRAY_ENV_PASSTHROUGH_KEYS = (
    "PATH",
    "HOME",
    "TMPDIR",
    "TMP",
    "TEMP",
    "LANG",
    "LANGUAGE",
    _XRAY_CACHE_DIR_ENV,
    "RUSTUP_HOME",
    "CARGO_HOME",
)
_XRAY_ENV_PASSTHROUGH_PREFIXES = ("LC_",)


def xray_subprocess_env() -> Dict[str, str]:
    """The minimal, explicit environment for an xray-cli subprocess.

    Copies only the allowlisted keys that are present in this process, plus
    the compile heap bound xray-cli applies to its rustc child. Nothing else
    from the server's environment reaches the child. Callers must not log the
    values; key names are safe.
    """
    env = {
        key: value
        for key, value in os.environ.items()
        if key in _XRAY_ENV_PASSTHROUGH_KEYS
        or key.startswith(_XRAY_ENV_PASSTHROUGH_PREFIXES)
    }
    env[_COMPILE_RLIMIT_DATA_ENV] = str(_COMPILE_RLIMIT_DATA_BYTES)
    return env


def _sanitize_error_message(msg: str) -> str:
    """Replace server-internal file paths in error messages.

    Applied in order:
    1. xray-cache paths (/…/xray-cache/<hexhash>.rs, optionally nested one
       level under a Bug #1425 isolated build-*/ directory) → "evaluator.rs"
    2. Remaining /home/, /root/, /tmp/ paths → "<server-path>"
    """
    msg = _RE_XRAY_CACHE_PATH.sub("evaluator.rs", msg)
    msg = _RE_SERVER_PATH.sub("<server-path>", msg)
    return msg


# ---------------------------------------------------------------------------
# Cluster-cache integrity envelope
#
# `_try_pre_fill` writes bytes fetched from the CLUSTER cache straight into
# the local xray cache directory, where Rust finds them as a cache hit and
# `dlopen`s them with no compile. The only load-time gate on the Rust side
# (`dynlib.rs`) byte-compares an embedded rustc-version string and an ABI
# export -- both of which a hostile `.so` simply supplies. Nothing re-checked
# `so_bytes` against the key it was stored under, so a row rewritten in place
# under a legitimate key executed on every pod that queried that evaluator.
#
# The identity the row is keyed by is sha256(assembled_source) + ABI version +
# rustc version. It is NOT a hash of the `.so`, so it cannot be re-derived
# from the bytes to check them; the integrity material has to be written at
# store time and travel WITH the blob.
#
# It travels inside the blob rather than in a new column on purpose: the
# envelope needs no schema migration, no backfill and no coordinated deploy,
# and it protects any XrayCacheBackend implementation rather than just the
# PostgreSQL one. The stored BYTEA is:
#
#     magic(8) || tag(1) || digest(32) || compiled .so bytes
#
# where digest covers a context prefix binding the CACHE IDENTITY and the
# RUSTC VERSION as well as the payload. Binding the key matters as much as
# binding the bytes: without it an attacker with DB write never needs to
# forge anything, they just copy a legitimately-cached artifact of their
# choosing under the key of the evaluator they want to hit.
#
# Two digest flavours:
#   S  sha256(context || payload)          -- default, no configuration
#   H  HMAC-SHA256(key, context || payload) -- when CIDX_XRAY_CACHE_HMAC_KEY
#      is set to a secret shared by the pods and NOT by the database
#
# The S flavour detects corruption, truncation, partial writes and
# key-substitution, but an attacker who can write the whole row can also
# recompute an S digest -- it raises the bar, it does not close the hole.
# The H flavour is the one that actually defeats "attacker has DB write",
# because the key never goes in the table. It is opt-in because requiring a
# new cluster secret would be a flag day; a cluster that sets it immediately
# refuses every pre-existing S row (they recompile once, then post-fill
# rewrites them as H rows), and a node that has lost the key refuses H rows
# rather than silently dropping to the unauthenticated flavour.
#
# Refusal is always and only "recompile locally". No cache row -- poisoned,
# truncated, or merely written by an older build -- may turn into a failed
# user query.
# ---------------------------------------------------------------------------

_SO_ENVELOPE_MAGIC = b"XRAYSOv1"
_SO_ENVELOPE_TAG_SHA256 = b"S"
_SO_ENVELOPE_TAG_HMAC = b"H"
_SO_ENVELOPE_DIGEST_LEN = 32  # sha256 / HMAC-SHA256 output
_SO_ENVELOPE_HEADER_LEN = (
    len(_SO_ENVELOPE_MAGIC) + len(_SO_ENVELOPE_TAG_SHA256) + _SO_ENVELOPE_DIGEST_LEN
)

# Domain-separation prefix: keeps this digest from ever colliding with one
# computed for some other purpose over the same bytes.
_SO_ENVELOPE_CONTEXT = b"cidx-xray-cluster-cache-v1\x00"

# Optional cluster-shared secret. Read at call time, never cached, so an
# operator rotating it takes effect on the next fetch rather than the next
# restart.
_XRAY_CACHE_HMAC_KEY_ENV = "CIDX_XRAY_CACHE_HMAC_KEY"


def _cluster_cache_hmac_key() -> Optional[bytes]:
    """Return the configured cluster-cache HMAC key, or None when unset."""
    raw = os.environ.get(_XRAY_CACHE_HMAC_KEY_ENV, "").strip()
    return raw.encode("utf-8") if raw else None


def _so_envelope_digest(
    identity: str, rustc_version: str, payload: bytes, key: Optional[bytes]
) -> Tuple[bytes, bytes]:
    """Return `(tag, digest)` for `payload` bound to `(identity, rustc_version)`.

    The context prefix is length-delimited by NUL separators so that no two
    different (identity, rustc_version) pairs can produce the same preimage
    by moving characters across the boundary.
    """
    preimage = (
        _SO_ENVELOPE_CONTEXT
        + identity.encode("utf-8")
        + b"\x00"
        + rustc_version.encode("utf-8")
        + b"\x00"
        + payload
    )
    if key is not None:
        return _SO_ENVELOPE_TAG_HMAC, hmac.new(key, preimage, hashlib.sha256).digest()
    return _SO_ENVELOPE_TAG_SHA256, hashlib.sha256(preimage).digest()


def wrap_so_for_cluster_cache(
    identity: str, rustc_version: str, so_bytes: bytes
) -> bytes:
    """Wrap compiled `.so` bytes in the integrity envelope for cluster storage.

    The inverse of `verified_so_from_cluster_blob`. Everything written to a
    shared cache backend goes through here, so an unwrapped blob in the table
    is either a row predating this change or one someone else put there --
    both of which the reader refuses.
    """
    tag, digest = _so_envelope_digest(
        identity, rustc_version, so_bytes, _cluster_cache_hmac_key()
    )
    return _SO_ENVELOPE_MAGIC + tag + digest + so_bytes


def verified_so_from_cluster_blob(
    identity: str, rustc_version: str, blob: bytes
) -> Optional[bytes]:
    """Return the `.so` bytes inside `blob`, or None if it cannot be trusted.

    None means exactly one thing to every caller: do not write these bytes,
    let Rust compile the evaluator locally instead. Never raises -- `blob` is
    whatever a row in a shared table happened to contain.
    """
    if not isinstance(blob, (bytes, bytearray)):
        return None
    blob = bytes(blob)
    if len(blob) <= _SO_ENVELOPE_HEADER_LEN:
        return None
    if not blob.startswith(_SO_ENVELOPE_MAGIC):
        return None
    tag_offset = len(_SO_ENVELOPE_MAGIC)
    tag = blob[tag_offset : tag_offset + 1]
    stored_digest = blob[tag_offset + 1 : _SO_ENVELOPE_HEADER_LEN]
    payload = blob[_SO_ENVELOPE_HEADER_LEN:]

    key = _cluster_cache_hmac_key()
    expected_tag, expected_digest = _so_envelope_digest(
        identity, rustc_version, payload, key
    )
    # The tag check is what makes the policy asymmetric in the safe
    # direction: a node WITH a key refuses unauthenticated rows, and a node
    # WITHOUT one refuses authenticated rows rather than verifying them with
    # the forgeable flavour.
    if not hmac.compare_digest(tag, expected_tag):
        return None
    if not hmac.compare_digest(stored_digest, expected_digest):
        return None
    return payload


# OTEL counter cache for the refusal metric below, keyed on the resolved
# meter so a meter swap re-creates the instrument instead of reusing a stale
# one. Created lazily: merely importing this module must never pull in the
# server telemetry stack (the CLI/solo xray path imports it too).
_INTEGRITY_METRIC_STATE: Dict[str, Any] = {"meter": None, "counter": None}


def _record_cache_integrity_failure(reason: str) -> None:
    """Count a refused cluster-cache blob as an OTEL event. Never raises.

    A refusal is a security-relevant event (a row that is not what this
    cluster compiled) AND an efficiency one (a cache miss that costs a
    recompile on every pod), and a WARNING log alone is not something anyone
    greps at fleet scale. Mirrors the peek_telemetry_manager() + is_active
    gating of `_record_identity_failure_metric` above, creating the counter
    off the same `cidx.application` meter.
    """
    try:
        from code_indexer.server.telemetry.manager import (  # noqa: PLC0415
            peek_telemetry_manager,
        )
        from code_indexer.server.telemetry.metrics_instrumentation import (  # noqa: PLC0415,E501
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        if not get_application_metrics(telemetry_manager).is_active:
            return
        meter = telemetry_manager.get_meter("cidx.application")
        if _INTEGRITY_METRIC_STATE["meter"] is not meter:
            _INTEGRITY_METRIC_STATE["meter"] = meter
            _INTEGRITY_METRIC_STATE["counter"] = meter.create_counter(
                name="cidx.xray.cache_integrity_failures",
                description=(
                    "Number of cluster xray evaluator-cache blobs refused "
                    "because they failed integrity verification"
                ),
                unit="1",
            )
        _INTEGRITY_METRIC_STATE["counter"].add(1, {"reason": reason})
    except Exception as exc:  # telemetry must never break the xray pipeline
        logger.debug("Failed to record xray cache integrity failure metric: %s", exc)


def _record_identity_failure_metric(reason: str) -> None:
    """Record a cidx.xray.cache_identity_failures OTEL counter event
    (Bug #1784 review: a WARNING log alone is insufficient observability
    at fleet scale when a node's xray-cli binary is missing/broken and
    EVERY compile silently loses the cluster cache).

    Follows the same peek_telemetry_manager() + is_active gating pattern
    as code_indexer.services.embedding_metrics_telemetry -- lazily imports
    the server-side telemetry package (this module is used by the CLI/
    solo-mode xray path too, which must never eagerly import server-layer
    modules) and never raises: telemetry failures must never break the
    xray evaluator pipeline.

    Args:
        reason: Short failure classification, e.g. "nonzero_exit",
            "incomplete_output", "exception", or "deadline_exhausted".
    """
    try:
        from code_indexer.server.telemetry.manager import (  # noqa: PLC0415
            peek_telemetry_manager,
        )
        from code_indexer.server.telemetry.metrics_instrumentation import (  # noqa: PLC0415
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        app_metrics = get_application_metrics(telemetry_manager)
        if not app_metrics.is_active:
            return
        app_metrics.record_xray_cache_identity_failure(reason=reason)
    except Exception as exc:  # never break the xray evaluator pipeline
        logger.debug("Failed to record xray cache identity failure metric: %s", exc)


class XrayCacheBackend(Protocol):
    """Structural interface for cluster-aware evaluator cache backends.

    Implementations must support fetch (cache lookup) and store (cache write).
    All methods must be exception-safe — callers assume they never raise.

    The `so_bytes` crossing this interface is an OPAQUE BLOB, not a loadable
    `.so`: it is the integrity envelope produced by
    `wrap_so_for_cluster_cache` and checked by
    `verified_so_from_cluster_blob` (see their comments). A backend must
    store and return it byte-for-byte and must never try to interpret,
    rewrite or truncate it — any such change makes the blob unverifiable,
    which costs a recompile on every node. Equally, a backend is NOT the
    place to add an integrity check of its own: verification belongs at the
    point the bytes become a file that gets dlopen'd, which is
    `RustNativeBackend._try_pre_fill`, so every backend gets it.
    """

    def fetch(self, source_hash: str, rustc_version: str) -> Optional[bytes]:
        """Return the cached envelope if fresh and rustc_version matches, else None."""
        ...

    def store(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compile_ms: int = 0,
    ) -> None:
        """Upsert a compiled-evaluator envelope into the cache."""
        ...


class CacheIdentityInfo(NamedTuple):
    """Bug #1784: the ONE shared cache identity plus its component fields.

    Obtained EXCLUSIVELY from `xray-cli --print-cache-identity`
    (RustNativeBackend._get_cache_identity_info) -- Python never
    independently computes any of these values, so it structurally cannot
    drift from what Rust's compile_evaluator() uses as its own cache key.
    """

    identity: str
    source_hash: str
    abi_version: int
    rustc_version: str


_MAX_PARENT_TRAVERSAL_DEPTH = 10

# Timeout for the `xray-cli --print-cache-identity` subprocess call (Bug
# #1784) -- no compilation happens on this path, just hashing, so this stays
# generous without risking a slow build hanging the caller. This is a CEILING:
# review MAJOR-2 requires the actual timeout passed to the subprocess to be
# clamped to whatever remains of the caller's operation deadline, so it is
# never exceeded but the identity probe also never blocks longer than the
# caller has left.
_CACHE_IDENTITY_TIMEOUT_SECS = 10

# Default maximum number of evaluator-source -> CacheIdentityInfo entries
# kept in a SharedIdentityCache (Bug #1784 review MAJOR-2). Identity is a
# pure function of (evaluator source, ABI version, rustc version); the
# latter two are fixed for the lifetime of one running xray-cli binary (same
# host toolchain), so caching by source text eliminates redundant
# `xray-cli --print-cache-identity` subprocess spawns across repeated calls
# with the SAME evaluator. This is the default size for a RustNativeBackend's
# own PRIVATE cache (single-repo xray_search path); a batch orchestrator
# (xray_search_batch) constructs and shares a larger instance explicitly so
# the identity for every unique evaluator source in the batch is computed
# exactly once, regardless of how many per-cell RustNativeBackend instances
# are created.
_IDENTITY_CACHE_MAX_ENTRIES = 32

# Maximum stderr bytes to include in a --print-cache-identity failure log message.
_RUSTC_STDERR_LOG_LIMIT = 200

# Maximum stderr bytes to include in an xray-cli non-zero-exit error message.
_XRAY_CLI_STDERR_ERROR_LIMIT = 200


def xray_cli_timeout_message(timeout_seconds: Any) -> str:
    """The error an xray-cli step reports when it ran out of time.

    One wording for every place a step can time out -- this pod's
    subprocess and a delegated execution's recorded step -- so the caller
    sees the same message wherever the evaluator ran. Unsanitized: callers
    pass it through ``_sanitize_error_message`` before it leaves cidx.
    """
    return f"xray-cli timed out after {timeout_seconds}s"


def xray_cli_exit_message(returncode: int, stderr: str) -> str:
    """The error an xray-cli step reports for a non-zero exit, with the head
    of its stderr. Unsanitized, like ``xray_cli_timeout_message``."""
    return (
        f"xray-cli exited with code {returncode}: "
        f"{stderr[:_XRAY_CLI_STDERR_ERROR_LIMIT]}"
    )


# Timeout for the `xray-cli --print-graph-extractor-extensions` subprocess
# call (Bug #1907) -- no compilation and no repo walk happens on this path,
# just serializing a fixed, tiny static list, so this stays generous
# without risking a hung candidate-collection walk.
_GRAPH_EXTRACTOR_EXTENSIONS_TIMEOUT_SECS = 10

# Bug #1796: directory name for X-Ray's per-invocation temp files (evaluator
# .rs, candidate-file-list .txt), kept distinct from the compile cache above.
_XRAY_TMP_DIR_NAME = "xray-tmp"

# Bug #1909: bounds how many per-file `--refine` findings get merged into a
# single analyze_graph response. The AC2 narrowing (see
# _narrow_refine_symbols_to_driver_matched_files below) already keeps the
# refine file SET small (only files a symbol in GraphResult.refine actually
# belongs to), but a single evaluator `fn refine` callback can still emit
# more than one finding per file -- this stays a real ceiling rather than
# trusting narrowing alone to bound response size.
_REFINE_FINDINGS_MAX = 500


def _refine_symbol_file_id(symbol_id: int) -> int:
    """Extracts the 32-bit file-id half of a `SymbolId` (Bug #1909) --
    mirrors Rust's `identity::SymbolId = (file_id << 32) | local_index`
    encoding and `graph::refine::refine_set_file_ids`'s identical `>> 32`
    shift exactly. A pure bit shift, never a graph lookup.
    """
    return (symbol_id >> 32) & 0xFFFFFFFF


def _repo_relative_file_id(repo_relative_path: str) -> int:
    """Reproduces Rust's `identity::file_id(repo_relative_path)` EXACTLY
    (Bug #1909): the first 4 bytes of SHA-256(repo_relative_path), read as
    a big-endian u32 (`rust/xray-core/src/graph/identity.rs`). This is a
    PURE function of the path string alone -- the same guarantee the Rust
    doc comment states ("no file list, index, or counter") -- so
    recomputing it here for the SAME repo-relative path strings Python
    already sent to `--build-graph`/`--analyze-graph` via `--files-from`
    always agrees with the id the Rust side originally assigned. This is
    the only way Python can map a `SymbolId`'s file-id half back to one of
    those candidate paths without a second Rust round trip.
    """
    import hashlib  # noqa: PLC0415 — stdlib, lazy import mirrors module convention

    digest = hashlib.sha256(repo_relative_path.encode("utf-8")).digest()
    return int.from_bytes(digest[:4], byteorder="big")


def _narrow_refine_symbols_to_driver_matched_files(
    refine_symbols: List[int],
    candidate_file_paths: List[str],
) -> List[str]:
    """Bug #1909 (AC2): intersects the file ids referenced by
    `refine_symbols` (`GraphResult.refine`, the RefineSet `analyze_graph`
    returned) with `candidate_file_paths` (the SAME driver-matched file
    list already sent to `--build-graph`/`--analyze-graph` via
    `--files-from`) -- mirrors
    `graph::refine::narrow_refine_set_to_driver_matched`'s contract, which
    the `--refine` CLI subcommand itself never calls (it takes an
    already-narrowed `--files-from` list as given), so this Python-side
    narrowing is what actually makes AC2 real: a 2-file RefineSet must
    never re-parse the whole repo.

    A symbol whose file id matches no candidate path (should not happen
    against a graph built from that exact candidate set, but never
    assumed) is silently excluded rather than raising a lookup error.

    Returns a SORTED, deduplicated list of repo-relative paths. Bounded:
    O(len(refine_symbols) + len(candidate_file_paths)) (Rule 14).
    """
    needed_file_ids = {_refine_symbol_file_id(sym) for sym in refine_symbols}
    matched = {
        path
        for path in candidate_file_paths
        if _repo_relative_file_id(path) in needed_file_ids
    }
    return sorted(matched)


class SharedIdentityCache:
    """Bounded, thread-safe LRU mapping evaluator source text to its
    composite cache identity (Bug #1784 review blocker: MAJOR-2 was only
    PARTIALLY satisfied because `xray_search_batch` built a fresh, empty,
    per-instance identity cache for every cell instead of sharing one across
    the whole batch operation).

    An instance of this class is meant to OUTLIVE any single
    RustNativeBackend: a batch orchestrator constructs ONE
    SharedIdentityCache and passes it into every per-cell
    RustNativeBackend/XRaySearchEngine it creates, so the identity for a
    given evaluator source (constant across every repo for a given scan) is
    computed via `xray-cli --print-cache-identity` exactly once for the
    whole batch, not once per cell.

    Node-local pure-function memoization ONLY: identity = f(assembled
    source, ABI, rustc version). A cache miss simply recomputes via a fresh
    subprocess call -- this is NOT cross-request/cluster state (see the
    project's cluster-aware-state rule) and must never be promoted to a
    permanent module-level singleton shared across unrelated requests; its
    lifetime is scoped to one batch job.

    Epoch-guarded: the first successful `put()` establishes an
    (abi_version, rustc_version) epoch from the computed CacheIdentityInfo.
    A LATER `put()` reporting a DIFFERENT epoch (e.g. the auto-updater
    replaces the xray-cli binary mid-batch -- xray_search_batch jobs can run
    up to 7200s) invalidates every existing entry before storing the new
    one, so a stale identity computed under a since-replaced toolchain is
    never served. This makes a hit effectively keyed on the full
    (source, abi_version, rustc_version) identity, not source alone.
    """

    def __init__(self, max_entries: int) -> None:
        if max_entries < 1:
            raise ValueError(f"max_entries must be >= 1, got {max_entries}")
        self._max_entries = max_entries
        self._lock = threading.Lock()
        self._entries: "OrderedDict[str, CacheIdentityInfo]" = OrderedDict()
        self._epoch: Optional[Tuple[int, str]] = None

    def get(self, rust_code: str) -> Optional["CacheIdentityInfo"]:
        """Return the cached identity for `rust_code`, or None on a miss."""
        with self._lock:
            info = self._entries.get(rust_code)
            if info is not None:
                self._entries.move_to_end(rust_code)
            return info

    def put(self, rust_code: str, info: "CacheIdentityInfo") -> None:
        """Insert/refresh an entry, evicting LRU entries past max_entries.

        Clears every existing entry first if `info`'s (abi_version,
        rustc_version) differs from the epoch established by a prior put()
        -- see class docstring.
        """
        with self._lock:
            epoch = (info.abi_version, info.rustc_version)
            if self._epoch is not None and self._epoch != epoch:
                logger.warning(
                    "XrayCache: toolchain changed mid-batch (abi/rustc "
                    "epoch %r -> %r) -- resetting shared identity cache",
                    self._epoch,
                    epoch,
                )
                self._entries.clear()
            self._epoch = epoch
            self._entries[rust_code] = info
            self._entries.move_to_end(rust_code)
            while len(self._entries) > self._max_entries:
                self._entries.popitem(last=False)


def _find_project_root() -> Path:
    """Walk up from this file to find the project root (contains rust/ dir)."""
    path = Path(__file__).resolve().parent
    for _ in range(_MAX_PARENT_TRAVERSAL_DEPTH):
        if (path / "rust").is_dir():
            return path
        parent = path.parent
        if parent == path:
            break
        path = parent
    logger.warning(
        "RustNativeBackend: could not find rust/ directory within %d levels of %s;"
        " falling back to hardcoded project root",
        _MAX_PARENT_TRAVERSAL_DEPTH,
        Path(__file__),
    )
    return Path(__file__).resolve().parent.parent.parent.parent


_PROJECT_ROOT = _find_project_root()

# Environment variable that points cidx at a PREBUILT xray-cli binary.
#
# WHY THIS EXISTS: every xray-cli path in this module used to be derived
# from `_find_project_root()`, i.e. from a `rust/` directory sitting above
# `src/code_indexer/`. That holds for a source checkout and for the
# auto-updater's in-place deploys, and it holds for NOTHING ELSE. In a
# packaged/containerised install (`pip install code_indexer` into
# `/usr/local/lib/python3.11/site-packages/`, which is what the EKS image
# does) there is no `rust/` anywhere above the package, so the walk falls
# through to its `parent^4` fallback and every xray scan targets a path
# that cannot exist. The failure is silent at import time (one WARNING)
# and only shows up as "BinaryNotFound" per file at query time.
#
# Deliberately an ENV VAR and not a config.json key: the binary's location
# is a property of how the artifact was PACKAGED (Dockerfile, .deb, distro
# package, Nix derivation...), not of how a given server instance is
# configured, so it has to be settable by whoever built the image without
# touching the runtime data dir -- the same reasoning that already makes
# CIDX_SERVER_DATA_DIR an env var. Deliberately NOT named after any
# particular deployment: this is upstream behaviour, useful to anyone who
# packages cidx.
_XRAY_CLI_PATH_ENV_VAR = "CIDX_XRAY_CLI_PATH"

# Where a source checkout builds the binary -- `cd rust && cargo build --release`.
_XRAY_CLI_SOURCE_TREE_PATH = _PROJECT_ROOT / "rust" / "target" / "release" / "xray-cli"


def resolve_xray_cli_path() -> Path:
    """Resolve the xray-cli binary path. Resolution order, highest first:

    1. ``$CIDX_XRAY_CLI_PATH`` -- an explicit, absolute-or-relative path to a
       prebuilt binary, set by whoever packaged this install (see the
       Dockerfile's builder stage). ``~`` is expanded. Empty/whitespace-only
       is treated as UNSET, so an env var that got cleared to "" behaves the
       same as one that was never exported.
    2. ``<project root>/rust/target/release/xray-cli`` -- the source-tree
       build output, where ``<project root>`` is the nearest ancestor of this
       file containing a ``rust/`` directory (``_find_project_root()``).

    NO EXISTENCE CHECK and NO FALLBACK BETWEEN THE TWO. An override that
    points at a missing or broken binary resolves to that path anyway, so the
    resulting "xray-cli binary not found at <path>" error names the path the
    OPERATOR configured. Silently falling back to the source-tree path would
    turn a one-line "your CIDX_XRAY_CLI_PATH is wrong" diagnosis into
    "BinaryNotFound at some path nobody configured", which is precisely the
    class of misleading failure this change exists to remove.

    Read on every call (not frozen at import): a process that sets the
    variable after importing this module -- a test, an embedding host, a
    wrapper script -- still gets the override.
    """
    override = os.environ.get(_XRAY_CLI_PATH_ENV_VAR, "").strip()
    if override:
        return Path(override).expanduser()
    return _XRAY_CLI_SOURCE_TREE_PATH


# Import-time snapshot, kept for the callers (and tests) that want "the path
# cidx would use right now" without calling the resolver. Prefer
# resolve_xray_cli_path() in new code -- this constant cannot see an env var
# that was set after import.
_XRAY_CLI_DEFAULT = resolve_xray_cli_path()


def get_graph_extractor_extensions(
    xray_cli_path: Optional[Path] = None,
    timeout_seconds: float = _GRAPH_EXTRACTOR_EXTENSIONS_TIMEOUT_SECS,
) -> Tuple[Optional[Dict[str, str]], Optional[str]]:
    """Bug #1907: asks `xray-cli --print-graph-extractor-extensions` for the
    REAL extension-to-language map of every language with a graph
    extractor (Java, Kotlin today) -- the single source of truth
    `graph::extract::graph_extractor_extensions` owns on the Rust side.

    Candidate collection (`xray_graph.py::_collect_graph_candidate_files`)
    applies `include_patterns`/`exclude_patterns` BEFORE any file ever
    reaches Rust, so an excluded file leaves no trace in any Rust-side
    counter. Scoping to one extractable language on a mixed-language repo
    used to make the response report `fact_graph_complete: true` with
    every degradation counter at zero, while the graph was missing every
    call site in the excluded language -- narrowing the scope hid the
    incompleteness instead of improving it. This function is what lets
    Python tell, for a file it is about to exclude, whether that file's
    language would have contributed real call edges had it been read.

    Deliberately NOT a hardcoded Python-side extension list (the bug
    report's rejected option 1): that would silently under-report the
    moment a new language's extractor lands in Rust without a matching
    Python update -- the exact failure mode this function exists to
    prevent. Deliberately NOT cached across calls either (KISS/YAGNI): this
    subprocess does no compilation and no repo walk, so its cost is
    negligible next to the compile/build-graph/analyze-graph subprocesses
    `run_graph_analysis` already spawns for the SAME request, and a cache
    would need its own invalidation story for an auto-updater binary swap
    mid-fleet that this simpler call sidesteps entirely.

    Never raises: any subprocess/parse failure returns `(None, message)`
    so the caller can fail the request loudly (Rule 2, anti-fallback) --
    silently treating "lookup failed" as "no extractor here" would
    reproduce exactly the false-completeness bug this function exists to
    prevent.

    Returns `(extension_to_language, None)` on success (extensions are
    bare, lowercase, no leading dot -- e.g. `{"java": "Java", "kt":
    "Kotlin", "kts": "Kotlin"}`), or `(None, error_message)` on failure.
    """
    path = xray_cli_path if xray_cli_path is not None else resolve_xray_cli_path()
    if not path.exists():
        return None, f"xray-cli binary not found at {path}."
    try:
        result = subprocess.run(
            [str(path), "--print-graph-extractor-extensions"],
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=xray_subprocess_env(),
        )
    except Exception as exc:  # noqa: BLE001 -- never raise, always report
        return None, f"failed to invoke --print-graph-extractor-extensions: {exc}"
    if result.returncode != 0:
        return None, (
            "--print-graph-extractor-extensions exited "
            f"{result.returncode}; "
            f"stderr={result.stderr[:_XRAY_CLI_STDERR_ERROR_LIMIT]!r}"
        )
    try:
        payload = json.loads(result.stdout)
        entries = payload["extensions"]
        mapping = {
            str(entry["extension"]).lower(): str(entry["language"]) for entry in entries
        }
    except Exception as exc:  # noqa: BLE001 -- malformed CLI output
        return None, f"malformed --print-graph-extractor-extensions output: {exc}"
    if not mapping:
        return (
            None,
            "--print-graph-extractor-extensions returned an empty extension list",
        )
    return mapping, None


def _get_xray_tmp_dir() -> Path:
    """Return the CIDX-owned directory for X-Ray's per-invocation temp files
    (Bug #1796): the evaluator .rs and candidate-file-list .txt files written
    by RustNativeBackend._invoke_xray_cli().

    Honors CIDX_DATA_DIR (the same env var RustNativeBackend._get_cache_dir()
    resolves for the compile cache) when set to an absolute path --
    $CIDX_DATA_DIR/xray-tmp -- for consistency with the rest of the
    codebase's CIDX_DATA_DIR resolution. Otherwise defaults to ~/.tmp/xray-tmp
    per this project's "tmp files: ~/.tmp, never /tmp" convention.

    NEVER falls back to the process-wide system temp directory
    (tempfile.gettempdir()) -- a directory that cannot be created or written
    to must fail loudly (Messi Rules 2/13), not silently redirect there.

    Does not create the directory itself -- _write_temp_file() does, via
    mkdir(parents=True, exist_ok=True), which is safe under concurrent
    callers (rayon threads / multiple server workers racing to create it).
    """
    import os  # noqa: PLC0415 — stdlib, lazy import to keep startup clean

    raw = os.environ.get(_XRAY_CACHE_DIR_ENV, "").strip()
    if raw:
        expanded = Path(raw).expanduser()
        if expanded.is_absolute():
            return expanded.resolve() / _XRAY_TMP_DIR_NAME
        logger.warning(
            "RustNativeBackend: %s=%r is not absolute after expanduser; using default",
            _XRAY_CACHE_DIR_ENV,
            raw,
        )
    return (Path.home() / ".tmp" / _XRAY_TMP_DIR_NAME).resolve()


def _write_temp_file(content: str, suffix: str, prefix: str, directory: Path) -> Any:
    """Write content to a closed NamedTemporaryFile(delete=False) inside
    `directory`, return the handle.

    Creates `directory` (and any missing parents) first via
    mkdir(parents=True, exist_ok=True) -- concurrency-safe: exist_ok=True
    means a race between multiple callers creating the same directory never
    raises. A genuine failure to create/write into `directory` (e.g.
    permission denied) propagates as-is -- this never silently redirects to
    the process-wide system temp directory (Bug #1796; Messi Rules 2/13).

    The caller is responsible for unlinking the file (via the returned
    handle's .name attribute) once done with it. If write() raises, both
    cleanup steps (unlink the on-disk file, close the file descriptor) are
    attempted independently; any cleanup-step exception is swallowed so the
    original write() exception is always what propagates -- callers never
    see a leaked temp file/descriptor, and never see a masked root cause.
    """
    directory.mkdir(parents=True, exist_ok=True)
    tmp = tempfile.NamedTemporaryFile(
        suffix=suffix, mode="w", delete=False, prefix=prefix, dir=str(directory)
    )
    try:
        tmp.write(content)
    except Exception:
        try:
            Path(tmp.name).unlink(missing_ok=True)
        except Exception:
            pass
        try:
            tmp.close()
        except Exception:
            pass
        raise
    tmp.close()
    return tmp


# Type alias for the run_batch return type (defined at the execution seam).
_BatchResult = BatchResult


def _error_tuple(
    file_path: str,
    error_type: str,
    error_message: str,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Build a single ([], [error_dict], None) result tuple."""
    return (
        [],
        [
            {
                "file_path": file_path,
                "line_number": 0,
                "error_type": error_type,
                "error_message": error_message,
            }
        ],
        None,
    )


def _error_all(
    file_specs: List[Dict[str, Any]],
    error_type: str,
    error_message: str,
) -> _BatchResult:
    """Return error tuples for every file spec with the given error."""
    return [
        _error_tuple(spec.get("file_path", ""), error_type, error_message)
        for spec in file_specs
    ]


def _graph_error_result(
    error_type: str,
    error_message: str,
    build_status: Optional[str] = None,
) -> Dict[str, Any]:
    """Story #1811 (S5, AC2): the structured error shape every failure path
    of `RustNativeBackend.run_graph_analysis` returns -- never a raw
    exception (Bug #1612's rule). Mirrors `_error_tuple`'s role for the
    legacy `run_batch` path, adapted to graph mode's single-result (not
    per-file) shape. Every non-error field is set to an honest "unknown/
    not reached" value (`None`/empty/`False`) rather than a value that
    could be misread as a real, successful outcome.
    """
    return {
        "ok": False,
        "error": {
            "error_type": error_type,
            "error_message": _sanitize_error_message(error_message),
        },
        "status": None,
        "findings": [],
        "refine": [],
        "fact_graph_complete": None,
        "build_status": build_status,
        "degradation": None,
        "cached": False,
        "compile_ms": 0,
        # Bug #1897: same "honest not-reached" convention as
        # fact_graph_complete above -- this error path never reached a real
        # BuildGraphResult, so these are None, never 0/an empty list that
        # could be misread as "a build ran and found nothing wrong".
        "completeness_reasons": None,
        "candidate_count": None,
        "candidate_budget_limit": None,
    }


def scan_error_type(output: Dict[str, Any]) -> str:
    """The error_type for a scan whose xray-cli JSON carries a top-level error.

    Bug #1827: xray-cli's own ``error_kind`` ("compile" vs "infrastructure")
    distinguishes a genuine problem in the user's evaluator source from an
    xray-cli/toolchain/filesystem problem unrelated to it -- never label an
    infrastructure failure "CompileError" (that would send an agent to debug
    perfectly valid Rust). Absent or unrecognized error_kind conservatively
    defaults to "XRayCliError".
    """
    return "CompileError" if output.get("error_kind") == "compile" else "XRayCliError"


def parse_json_output(stdout: str) -> Tuple[Dict[str, Any], Optional[str]]:
    """Parse JSON from xray-cli stdout. Returns (output_dict, error_msg)."""
    try:
        return json.loads(stdout), None
    except json.JSONDecodeError as exc:
        msg = f"xray-cli produced non-JSON output: {exc}"
        logger.warning("RustNativeBackend: %s", msg)
        return {}, msg


def validate_rust_code(evaluator_code: str) -> Tuple[str, Optional[str]]:
    """Validate Rust evaluator code. Returns (rust_code, error_msg).

    The evaluator_code is expected to already be valid Rust.  This checks for
    the required signature and forbidden constructs without any
    transformation -- if valid, returns the code unchanged.
    """
    from code_indexer.xray.sandbox import validate_rust_evaluator  # noqa: PLC0415

    result = validate_rust_evaluator(evaluator_code)
    if not result.ok:
        msg = result.reason or "Rust validation failed"
        logger.warning("RustNativeBackend: %s", msg)
        return "", msg
    return evaluator_code, None


def parse_cache_identity_output(stdout: str) -> Optional[CacheIdentityInfo]:
    """``xray-cli --print-cache-identity`` output as a CacheIdentityInfo, or
    None when a required field is missing."""
    fields: Dict[str, str] = {}
    for line in stdout.splitlines():
        if "=" in line:
            key, _, value = line.partition("=")
            fields[key] = value
    required = ("identity", "source_hash", "abi_version", "rustc_version")
    if not all(fields.get(key) for key in required):
        logger.warning(
            "XrayCache: --print-cache-identity produced incomplete output: %r",
            stdout,
        )
        return None
    return CacheIdentityInfo(
        identity=fields["identity"],
        source_hash=fields["source_hash"],
        abi_version=int(fields["abi_version"]),
        rustc_version=fields["rustc_version"],
    )


def _safe_unlink_graph_temp_path(path: Optional[Any]) -> None:
    """Best-effort cleanup for one graph-mode temp path -- catches and
    LOGS `OSError` rather than letting a cleanup failure escape a
    `finally` block, which would otherwise override the real return value
    `run_graph_analysis`'s `try` already computed (a correctness bug, not
    merely an aesthetic one: `finally` exceptions replace whatever the
    `try`/`except` returned). `path` may be `None` (nothing to clean up
    yet) or any path-like value accepted by `Path(...)`.
    """
    if path is None:
        return
    try:
        Path(path).unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "RustNativeBackend: graph-mode temp cleanup failed for %s: %s", path, exc
        )


# ---------------------------------------------------------------------------
# Graph-mode pipeline over a pluggable step executor
#
# compile-only -> build-graph -> analyze-graph -> (refine) is decided, parsed
# and shaped HERE, once. Where each xray-cli step physically runs is the
# executor's business: this pod's subprocesses (`_LocalGraphStepExecutor`), or
# steps a delegated execution already ran and recorded (the sandbox plugin
# replays them). Both therefore get byte-identical results from the same
# outputs.
# ---------------------------------------------------------------------------

#: Graph steps, in the order the pipeline asks for them.
GRAPH_STEPS: Tuple[str, ...] = ("compile", "build", "analyze", "refine")

# Compile timeout when the caller gave no deadline at all.
_GRAPH_COMPILE_DEFAULT_TIMEOUT_SECS = 300


@dataclass(frozen=True)
class GraphStep:
    """One xray-cli graph step the pipeline asks an executor to run.

    Attributes:
        name: One of ``GRAPH_STEPS``.
        timeout_seconds: The budget the pipeline grants this step.
        so_path: The compiled evaluator ``compile`` reported; set for
            ``build``, ``analyze`` and ``refine``.
        refine_files: ``refine`` only: the repo-relative files the pipeline
            narrowed the refine set to, in order.
    """

    name: str
    timeout_seconds: int
    so_path: Optional[str] = None
    refine_files: Optional[List[str]] = None


class GraphStepExecutor(ABC):
    """Runs the graph pipeline's xray-cli steps somewhere.

    The pipeline (``run_graph_pipeline``) owns every decision -- step order,
    deadline gating, JSON parsing, status checks, refine narrowing and result
    shaping. An executor only answers two questions: how much budget is left
    before a step, and what a step printed.
    """

    @abstractmethod
    def remaining_seconds(self, step: str) -> float:
        """Seconds left before *step* (one of ``GRAPH_STEPS``). Asked once,
        right before the pipeline would run that step; <= 0 means the
        deadline is exhausted and the step is not run."""

    @abstractmethod
    def run_step(self, step: GraphStep) -> Tuple[str, Optional[str]]:
        """Run *step*; return ``(stdout, None)``, or ``("", error_message)``
        worded like ``xray_cli_timeout_message`` / ``xray_cli_exit_message``
        and already sanitized."""

    def before_compile(self, deadline_seconds: Optional[float]) -> None:
        """Hook before the compile step (the in-pod cluster-cache pre-fill)."""

    def after_compile(
        self, compile_info: Dict[str, Any], deadline_seconds: Optional[float]
    ) -> None:
        """Hook after a successful compile (the in-pod cache post-fill)."""


def _run_graph_step(
    executor: GraphStepExecutor, step: GraphStep
) -> Tuple[Dict[str, Any], Optional[str]]:
    """Run *step* and parse its JSON report: ``(output, None)`` or ``({}, error)``."""
    stdout, error = executor.run_step(step)
    if error is not None:
        return {}, error
    return parse_json_output(stdout)


def build_graph_analysis_result(
    analyze_output: Dict[str, Any],
    build_status: str,
    build_result: Dict[str, Any],
    compile_info: Dict[str, Any],
) -> Dict[str, Any]:
    """Story #1811 (AC3): assembles the final result, surfacing
    `AnalysisCompleteness` HONESTLY. Every degradation counter comes
    from the real `BuildGraphResult` via a bare `.get(key)` -- NO
    default: `build_result` is only passed here once the build's
    status was confirmed `"ok"`, at which point every field is always
    present; a missing key means malformed JSON, surfacing as `None`
    (distinct from a genuine `0`/`False`), never a silently-masked
    "clean" reading.
    """

    status = analyze_output.get("status")
    raw_result = analyze_output.get("result")
    result = raw_result if isinstance(raw_result, dict) else {}
    reported_ran_ok = status == "ran_ok"

    # H11 (consolidated review, Issue #1811/Bug #1812, Codex): a
    # ran_ok status is only trustworthy if "result" is a REAL dict
    # carrying BOTH required fields -- `.get("x", [])`-style
    # defaulting on a missing/malformed payload would otherwise accept
    # `{"status": "ran_ok"}` alone as a plausible, quietly-empty
    # success. Missing required fields under a ran_ok status is
    # malformed CLI output, never a legitimate "nothing found".
    schema_complete = (
        isinstance(raw_result, dict)
        and "findings" in raw_result
        and "refine" in raw_result
    )
    ok = reported_ran_ok and schema_complete

    if reported_ran_ok and not schema_complete:
        error = {
            "error_type": "MalformedCliOutput",
            "error_message": _sanitize_error_message(
                "--analyze-graph reported status=ran_ok but result is "
                "missing required findings/refine fields"
            ),
        }
    elif not ok:
        error = {
            "error_type": "GraphAnalysisError",
            "error_message": _sanitize_error_message(
                f"--analyze-graph reported status={status}"
            ),
        }
    else:
        error = None

    degradation_keys = (
        "files_with_parse_errors",
        "unreadable_or_unsupported_files",
        "files_with_read_errors",
        "files_with_extractor_panics",
        "files_with_collector_panics",
        "files_with_unsupported_language",
        "truncated_by_max_files",
        # Bug #1897: surfaces BuildGraphResult::index_budget_exceeded
        # verbatim -- true iff completeness_reasons contains
        # "index_budget_exceeded" specifically.
        "index_budget_exceeded",
    )
    return {
        "ok": ok,
        "error": error,
        "status": status,
        "findings": result.get("findings", []),
        "refine": result.get("refine", []),
        "fact_graph_complete": build_result.get("fact_graph_complete"),
        "build_status": build_status,
        "degradation": {key: build_result.get(key) for key in degradation_keys},
        "cached": compile_info.get("cached", False),
        "compile_ms": compile_info.get("compile_ms", 0),
        # Bug #1897: surfaces `BuildGraphResult`'s new completeness
        # fields verbatim -- the lossless list of every completeness
        # condition that held (never collapsed to a single reason), plus
        # the observed candidate count against the configured budget
        # limit. Never re-derived from `degradation` -- `build_result`
        # is only reached once the build's status was confirmed "ok",
        # same guarantee `degradation`'s own bare `.get(key)` relies on.
        "completeness_reasons": build_result.get("completeness_reasons"),
        "candidate_count": build_result.get("candidate_count"),
        "candidate_budget_limit": build_result.get("candidate_budget_limit"),
    }


def _graph_compile_phase(
    executor: GraphStepExecutor, deadline_seconds: Optional[float]
) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
    """Compile the evaluator once (``--compile-only``).

    Honours `deadline_seconds` the way `_get_cache_identity_info` already
    does: an already-exhausted deadline skips the step entirely rather than
    running one doomed to time out immediately. Returns `(so_path,
    {"cached": bool, "compile_ms": int}, error_message)`; `so_path` is
    `None` iff `error_message` is not `None`.
    """
    executor.before_compile(deadline_seconds)

    if deadline_seconds is not None and deadline_seconds <= 0:
        return None, {}, "operation deadline already exhausted before compile"
    timeout = (
        int(deadline_seconds) + 1
        if deadline_seconds is not None
        else _GRAPH_COMPILE_DEFAULT_TIMEOUT_SECS
    )

    output, error = _run_graph_step(executor, GraphStep("compile", timeout))
    if error is not None:
        return None, {}, error
    compile_error = output.get("error")
    if compile_error:
        return None, {}, str(compile_error)
    so_path = output.get("so_path")
    if not so_path:
        return None, {}, "xray-cli --compile-only produced no so_path"

    compile_info = {
        "cached": bool(output.get("cached", False)),
        "compile_ms": int(output.get("compile_ms", 0)),
    }
    executor.after_compile(compile_info, deadline_seconds)
    return so_path, compile_info, None


def _graph_build_phase(
    executor: GraphStepExecutor, so_path: str
) -> Tuple[Optional[Tuple[str, Dict[str, Any]]], Optional[Dict[str, Any]]]:
    """Runs `--build-graph`. Returns `((status, result), None)` on a real
    `"ok"` build, or `(None, error_result)` on any failure -- including an
    already-exhausted deadline, which never runs a doomed step.
    """
    remaining = executor.remaining_seconds("build")
    if remaining <= 0:
        return None, _graph_error_result(
            "Timeout", "deadline exhausted before --build-graph"
        )
    output, error = _run_graph_step(
        executor, GraphStep("build", int(remaining) + 1, so_path=so_path)
    )
    if error is not None:
        return None, _graph_error_result("XRayCliError", error)
    status = output.get("status")
    result = output.get("result") or {}
    if status != "ok":
        return None, _graph_error_result(
            "GraphBuildError",
            f"--build-graph reported status={status}",
            build_status=status,
        )
    return (status, result), None


def _graph_analyze_phase(
    executor: GraphStepExecutor, so_path: str, build_status: str
) -> Tuple[Optional[Dict[str, Any]], Optional[Dict[str, Any]]]:
    """Runs `--analyze-graph`. Returns `(output, None)` on a real step
    success (the ChildReport's OWN status may still be non-`ran_ok`; that is
    handled by `build_graph_analysis_result`, not here), or `(None,
    error_result)` on a step-level failure or an already-exhausted deadline.
    """
    remaining = executor.remaining_seconds("analyze")
    if remaining <= 0:
        return None, _graph_error_result(
            "Timeout",
            "deadline exhausted before --analyze-graph",
            build_status=build_status,
        )
    output, error = _run_graph_step(
        executor, GraphStep("analyze", int(remaining) + 1, so_path=so_path)
    )
    if error is not None:
        return None, _graph_error_result(
            "XRayCliError", error, build_status=build_status
        )
    return output, None


def _graph_refine_phase(
    executor: GraphStepExecutor, so_path: str, files_to_refine: List[str]
) -> Dict[str, Any]:
    """Bug #1909: runs `xray-cli --refine` over `files_to_refine` (already
    AC2-narrowed via `_narrow_refine_symbols_to_driver_matched_files`) and
    maps its `ChildReport<RefineBatchResult>` onto this module's own
    `refine_status`/`refine_findings` contract.

    Every terminal outcome DEGRADES the overall analyze_graph result
    honestly (Rule 13) -- this never raises and never turns a real
    analyze_graph success into a failure: refine is OPTIONAL (ADR-001), so
    a step-level failure, a non-`ran_ok` CLI status (including the common
    `absent` case, "the compiled artifact does not export xray_refine"), or
    an already-exhausted deadline all report a distinct `refine_status` and
    an empty `refine_findings`, never an exception the caller has to catch.
    """
    remaining = executor.remaining_seconds("refine")
    if remaining <= 0:
        return {"refine_status": "skipped_timeout", "refine_findings": []}

    output, error = _run_graph_step(
        executor,
        GraphStep(
            "refine",
            int(remaining) + 1,
            so_path=so_path,
            refine_files=list(files_to_refine),
        ),
    )
    if error is not None:
        return {
            "refine_status": "error",
            "refine_error": _sanitize_error_message(error),
            "refine_findings": [],
        }

    status = output.get("status")
    if status == "absent":
        return {"refine_status": "absent", "refine_findings": []}
    if status != "ran_ok":
        return {
            "refine_status": "error",
            "refine_error": _sanitize_error_message(
                f"--refine reported status={status}"
            ),
            "refine_findings": [],
        }

    raw_result = output.get("result")
    files_out = raw_result.get("files", []) if isinstance(raw_result, dict) else []
    findings: List[Dict[str, Any]] = []
    truncated = False
    for file_outcome in files_out:
        if not isinstance(file_outcome, dict):
            continue
        for finding in file_outcome.get("findings", []) or []:
            if len(findings) >= _REFINE_FINDINGS_MAX:
                truncated = True
                break
            findings.append(finding)
        if truncated:
            break

    refine_result: Dict[str, Any] = {
        "refine_status": "ran",
        "refine_findings": findings,
        "refine_files_examined": len(files_out),
    }
    if truncated:
        refine_result["refine_findings_truncated"] = True
    return refine_result


def _graph_maybe_refine(
    executor: GraphStepExecutor,
    analysis_result: Dict[str, Any],
    refine: bool,
    file_paths: List[str],
    so_path: str,
) -> Dict[str, Any]:
    """Bug #1909: the SOLE gate deciding whether xray-cli's already-built
    `--refine` phase runs at all.

    Deliberately opt-in AND cost-guarded, per two independent conditions
    (both required, checked in order so each has its own honest
    `refine_status`):
      1. `refine=False` (the default) never runs the refine step at all --
         a caller must explicitly ask for a second per-file traversal,
         since it re-opens the `timeout_seconds` budget and costs real time
         at fleet scale.
      2. `refine=True` but the evaluator's `analyze_graph` flagged NOTHING
         (`GraphResult.refine` empty) also never runs it -- running refine
         over zero files is pure waste.
    Only reached once the base result was assembled; a prior compile/build/
    analyze failure already returned via `_graph_error_result`.
    """
    if not refine:
        return {"refine_status": "not_requested", "refine_findings": []}
    if not analysis_result.get("ok"):
        return {"refine_status": "skipped_analysis_failed", "refine_findings": []}
    refine_symbols = analysis_result.get("refine") or []
    if not refine_symbols:
        return {"refine_status": "skipped_empty_refine_set", "refine_findings": []}
    files_to_refine = _narrow_refine_symbols_to_driver_matched_files(
        refine_symbols, file_paths
    )
    if not files_to_refine:
        return {
            "refine_status": "skipped_no_matching_files",
            "refine_findings": [],
        }
    return _graph_refine_phase(executor, so_path, files_to_refine)


def run_graph_pipeline(
    executor: GraphStepExecutor,
    *,
    refine: bool = False,
    file_paths: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """Compile ONCE, then build-graph -> analyze-graph ->
    `build_graph_analysis_result` -- the whole graph-mode pipeline -- with
    every step run by *executor*.

    Bug #1909: once the base result is assembled, `_graph_maybe_refine`
    decides -- and, when it decides to, runs -- the optional `--refine`
    step, merging its outcome into the SAME response dict (new
    `refine_status`/`refine_findings` keys only, existing keys untouched --
    the response envelope stays backward compatible). Every known failure
    is a `_graph_error_result`, never an exception (Bug #1612); anything the
    executor itself raises propagates to the caller.
    """
    so_path, compile_info, compile_error = _graph_compile_phase(
        executor, executor.remaining_seconds("compile")
    )
    if compile_error is not None:
        return _graph_error_result("CompileError", compile_error)
    assert so_path is not None

    build_pair, build_error_result = _graph_build_phase(executor, so_path)
    if build_error_result is not None:
        return build_error_result
    assert build_pair is not None
    build_status, build_result = build_pair

    analyze_output, analyze_error_result = _graph_analyze_phase(
        executor, so_path, build_status
    )
    if analyze_error_result is not None:
        return analyze_error_result
    assert analyze_output is not None

    result = build_graph_analysis_result(
        analyze_output, build_status, build_result, compile_info
    )
    result.update(
        _graph_maybe_refine(executor, result, refine, file_paths or [], so_path)
    )
    return result


class LocalXrayExecutionBackend:
    """In-pod X-Ray execution: compiles and runs the evaluator via xray-cli.

    Validates Rust evaluator code, compiles to a dynamic library,
    and runs the Rust xray scanner for parallel AST evaluation. The node-wide
    compile semaphore, compile rlimits, ``xray_subprocess_env()`` allowlist and
    the local + Postgres compile caches all live here, behind the
    ``XrayExecutionBackend`` seam (``xray/execution_backend.py``).
    """

    def __init__(
        self,
        xray_cache_backend: Optional[XrayCacheBackend] = None,
        identity_cache: Optional[SharedIdentityCache] = None,
        confine_to_repo_root: bool = True,
    ) -> None:
        """Initialise backend with default xray-cli path and optional cluster cache.

        Args:
            xray_cache_backend: Optional cluster cache backend (XrayCacheBackend).
                Pass None (default) for solo-mode deployments.
            identity_cache: Optional externally-owned SharedIdentityCache
                (Bug #1784 review MAJOR-2) that OUTLIVES this instance --
                e.g. one shared across every cell of an xray_search_batch
                operation, so the identity subprocess is invoked once per
                unique evaluator source across the WHOLE batch instead of
                once per cell. None (default) creates a private cache sized
                to _IDENTITY_CACHE_MAX_ENTRIES -- unchanged single-repo
                xray_search behaviour.
            confine_to_repo_root: True (default, server context) reads a
                finding's line content only when the file resolves inside
                the repository root; False (local ``cidx xray``) reads it
                wherever a symlink points.
        """
        self._confine_to_repo_root = confine_to_repo_root
        # Resolved per instance, not read from the import-time constant, so a
        # CIDX_XRAY_CLI_PATH exported after this module was first imported
        # still takes effect (see resolve_xray_cli_path for the full order).
        self._xray_cli_path: Path = resolve_xray_cli_path()
        self._xray_cache: Optional[XrayCacheBackend] = xray_cache_backend
        # Side-channel populated by run_batch() — debug_log() messages from xray-cli JSON.
        # Read by XRaySearchEngine.run() to surface in result dict as debug_output[].
        self._last_debug_messages: List[str] = []
        self._identity_cache: SharedIdentityCache = (
            identity_cache
            or SharedIdentityCache(max_entries=_IDENTITY_CACHE_MAX_ENTRIES)
        )

    def _get_cache_identity_info(
        self,
        rust_code: str,
        deadline_seconds: Optional[float] = None,
        graph_mode: bool = False,
    ) -> Optional[CacheIdentityInfo]:
        """Bug #1784: get the composite cache identity for `rust_code`.

        Serves from the bounded identity cache when available (identity is
        a pure function of source+ABI+rustc) -- private to this instance by
        default, or a batch-shared SharedIdentityCache when one was passed
        to __init__. Otherwise shells out to `xray-cli --print-cache-identity`
        -- the SOLE implementation of the identity formula -- with the
        subprocess timeout clamped to `deadline_seconds` (review MAJOR-2:
        never block longer than the caller's remaining operation budget; an
        already-expired deadline skips the subprocess entirely). Returns
        None (never raises) on any failure and records a
        cidx.xray.cache_identity_failures metric.

        H9 (consolidated review, Issue #1811/Bug #1812): `graph_mode` must
        be True for a graph-mode evaluator (fn collect_facts + fn
        analyze_graph) -- it selects the graph-mode-aware identity formula
        (`cache_identity_info_graph` on the Rust side, via `--graph-mode`),
        which is the ONLY identity that matches what `compile_evaluator`
        actually uses as the real `.so` filename for that evaluator. The
        cache key is mode-qualified (`"graph:" + rust_code` vs plain
        `rust_code`) so a legacy and a graph-mode call sharing one
        SharedIdentityCache instance can never collide on identical source
        text mapping to two different real identities.
        """
        cache_key = f"graph:{rust_code}" if graph_mode else rust_code
        cached = self._identity_cache.get(cache_key)
        if cached is not None:
            return cached
        if deadline_seconds is not None and deadline_seconds <= 0:
            logger.warning(
                "XrayCache: skipping --print-cache-identity -- operation "
                "deadline already exhausted"
            )
            _record_identity_failure_metric("deadline_exhausted")
            return None
        info = self._fetch_identity_via_subprocess(
            rust_code, deadline_seconds, graph_mode=graph_mode
        )
        if info is not None:
            self._identity_cache.put(cache_key, info)
        return info

    def _fetch_identity_via_subprocess(
        self,
        rust_code: str,
        deadline_seconds: Optional[float],
        graph_mode: bool = False,
    ) -> Optional[CacheIdentityInfo]:
        """Run `xray-cli --print-cache-identity`, parse, classify failures.

        Never raises. Records a cidx.xray.cache_identity_failures metric on
        every failure path (nonzero exit, incomplete output, exception).
        """
        try:
            timeout = self._resolve_identity_timeout(deadline_seconds)
            result = self._run_cache_identity_subprocess(
                rust_code, timeout, graph_mode=graph_mode
            )
            if result.returncode != 0:
                logger.warning(
                    "XrayCache: --print-cache-identity exited %d; stderr=%r",
                    result.returncode,
                    result.stderr[:_RUSTC_STDERR_LOG_LIMIT],
                )
                _record_identity_failure_metric("nonzero_exit")
                return None
            info = self._parse_cache_identity_output(result.stdout)
            if info is None:
                _record_identity_failure_metric("incomplete_output")
            return info
        except Exception as exc:
            logger.warning("XrayCache: cache identity computation failed: %s", exc)
            _record_identity_failure_metric("exception")
            return None

    @staticmethod
    def _resolve_identity_timeout(deadline_seconds: Optional[float]) -> float:
        """Clamp the identity subprocess timeout to the caller's remaining
        deadline (review MAJOR-2). No floor above `deadline_seconds` -- a
        floor would itself violate "never block longer than the caller has
        left". Callers with an already-expired deadline never reach this
        (see _get_cache_identity_info's early-return)."""
        if deadline_seconds is None:
            return _CACHE_IDENTITY_TIMEOUT_SECS
        return min(_CACHE_IDENTITY_TIMEOUT_SECS, deadline_seconds)

    def _run_cache_identity_subprocess(
        self,
        rust_code: str,
        timeout_seconds: float = _CACHE_IDENTITY_TIMEOUT_SECS,
        graph_mode: bool = False,
    ) -> "subprocess.CompletedProcess[str]":
        cmd = [str(self._xray_cli_path), "--print-cache-identity"]
        if graph_mode:
            cmd.append("--graph-mode")
        return subprocess.run(
            cmd,
            input=rust_code,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            env=xray_subprocess_env(),
        )

    @staticmethod
    def _parse_cache_identity_output(stdout: str) -> Optional[CacheIdentityInfo]:
        return parse_cache_identity_output(stdout)

    @staticmethod
    def _get_cache_dir() -> Path:
        """Return the local xray cache directory.

        CIDX_DATA_DIR is a server-level administrative config (same env var used
        in Bug #879 for IPC path alignment). It is set by the system operator or
        the auto-updater — NOT user-supplied input. Validation rejects non-absolute
        paths (after expanduser) to prevent directory traversal.

        Falls back to ~/.cidx-server/xray-cache when unset or invalid.
        Both branches are normalized via resolve().

        MUST produce the same path as Rust's get_cache_dir() in cache.rs so that
        Python-written pre-fill .so files are visible to the Rust compiler cache.
        """
        import os  # noqa: PLC0415 — stdlib, lazy import to keep startup clean

        raw = os.environ.get(_XRAY_CACHE_DIR_ENV, "").strip()
        if raw:
            expanded = Path(raw).expanduser()
            if expanded.is_absolute():
                return expanded.resolve() / _XRAY_CACHE_DIR_NAME
            logger.warning(
                "RustNativeBackend: %s=%r is not absolute after expanduser; using default",
                _XRAY_CACHE_DIR_ENV,
                raw,
            )
        return (Path.home() / _CIDX_SERVER_DIR_NAME / _XRAY_CACHE_DIR_NAME).resolve()

    def execute_scan(self, request: XrayScanRequest) -> XrayScanOutcome:
        """Run one evaluator over one batch of candidate files in this pod.

        See ``XrayScanRequest`` for the inputs. ``evaluator_code`` must contain
        ``fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>``;
        ``timeout_seconds`` bounds the xray-cli subprocess.

        Returns:
            The (matches, errors, meta) tuples, one per file spec, and the
            evaluator's debug_log() lines when xray-cli produced output.
        """
        evaluator_code = request.evaluator_code
        file_specs = request.file_specs
        timeout_seconds = request.timeout_seconds
        on_process_spawned = request.on_process_spawned
        repo_path = request.repo_path
        if not file_specs:
            return XrayScanOutcome([])

        rust_code, validation_error = self._validate_rust_code(evaluator_code)
        if validation_error is not None:
            return XrayScanOutcome(
                _error_all(file_specs, "ValidationError", validation_error)
            )

        if not self._xray_cli_path.exists():
            msg = (
                f"xray-cli binary not found at {self._xray_cli_path}. "
                "Build it with: cd rust && cargo build --release -p xray-cli, "
                f"or point {_XRAY_CLI_PATH_ENV_VAR} at a prebuilt binary."
            )
            logger.error("RustNativeBackend: %s", msg)
            # ONE deduplicated tuple carrying file_path=="", NOT one tuple per
            # file spec. A missing binary is a whole-batch failure in exactly
            # the same sense as a compile error or an unparseable xray-cli
            # response: not a single file was opened, parsed or evaluated.
            # file_path=="" is this module's established structural
            # discriminator for that (see the invoke_error / parse_error /
            # cli_error branches below, and the Bug #1827 M-2 comment in
            # search_engine.py which reads it to decide whether a batch item
            # counts as a processed file).
            #
            # Emitting the real per-file paths -- what this branch used to do
            # -- made XRaySearchEngine.run() count every one of them as
            # successfully processed, so a scan with no binary at all
            # reported files_processed == files_total, matches: [], no
            # partial and no timeout: indistinguishable from "scanned
            # everything, found nothing". That is the single most dangerous
            # shape this API can return, because the caller's natural
            # conclusion ("the pattern has no hits in this repo") is the
            # exact opposite of the truth ("nothing was scanned").
            return XrayScanOutcome(
                [_error_tuple("", "BinaryNotFound", _sanitize_error_message(msg))]
            )

        base = Path(repo_path) if repo_path else Path.cwd()
        abs_paths = [str(base / spec.get("file_path", "")) for spec in file_specs]
        # Resolved ONCE for the whole batch -- threaded through to the
        # line_content-enrichment read as a defense-in-depth containment
        # check, alongside candidate-collection filtering upstream. None
        # (no check) only for the local CLI, which follows symlinks.
        resolved_root = base.resolve() if self._confine_to_repo_root else None

        # Bug #1784 review MAJOR-2: track ONE operation deadline across this
        # whole call so every internal identity-helper invocation (pre-fill
        # AND post-fill) is bounded by what actually remains of the
        # caller's own timeout_seconds, instead of each independently
        # defaulting to the fixed _CACHE_IDENTITY_TIMEOUT_SECS ceiling.
        operation_deadline = time.monotonic() + timeout_seconds

        stdout, invoke_error = self._invoke_xray_cli(
            rust_code,
            abs_paths,
            timeout_seconds,
            on_process_spawned,
            deadline_seconds=self._remaining_seconds(operation_deadline),
        )
        if invoke_error is not None:
            # Compilation/invocation errors are per-evaluator, not per-file.
            # Return a single deduplicated error entry instead of one per file.
            return XrayScanOutcome([_error_tuple("", "XRayCliError", invoke_error)])

        output, parse_error = self._parse_json_output(stdout)
        if parse_error is not None:
            # JSON parse failure is a per-evaluator error — deduplicate to one entry.
            return XrayScanOutcome(
                [_error_tuple("", "XRayCliError", _sanitize_error_message(parse_error))]
            )

        cli_error = output.get("error")
        if cli_error:
            # Top-level CLI error (e.g. compilation failed) — deduplicate to
            # one entry, typed by xray-cli's error_kind (scan_error_type).
            error_type = scan_error_type(output)
            logger.warning("RustNativeBackend: xray-cli error: %s", cli_error)
            return XrayScanOutcome(
                [_error_tuple("", error_type, _sanitize_error_message(cli_error))]
            )

        # Cluster post-fill: upload a freshly compiled .so to PG so other nodes
        # can skip compilation. Only fires when: cache is configured, the compile
        # was NOT a cache hit (cached=false), and the compile took real time.
        if (
            self._xray_cache is not None
            and output.get("cached") is False
            and output.get("compile_ms", 0) > 0
        ):
            self._try_post_fill(
                rust_code,
                output.get("compile_ms", 0),
                deadline_seconds=self._remaining_seconds(operation_deadline),
            )

        findings = output.get("findings", [])
        # xray-cli emits each finding's line_content from the source it parsed.
        # An older binary does not; re-reading the file to fill it is a local
        # repository read, allowed only where the repository IS local. Under a
        # delegated content backend that is an error, never a blank column.
        if _content_is_delegated() and any(
            "line_content" not in finding for finding in findings
        ):
            msg = (
                "xray-cli did not emit line_content and the repository is "
                "behind a delegated content backend, so it cannot be re-read "
                "locally. Upgrade xray-cli."
            )
            logger.error("RustNativeBackend: %s", msg)
            return XrayScanOutcome([_error_tuple("", "XRayCliError", msg)])
        # debug_log() messages travel beside the results; RustNativeBackend
        # stores them on _last_debug_messages, which XRaySearchEngine.run()
        # surfaces as debug_output[].
        return XrayScanOutcome(
            self._build_results(file_specs, abs_paths, findings, resolved_root),
            debug_messages=output.get("debug_messages", []),
        )

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _remaining_seconds(deadline: float) -> float:
        """Seconds left until `deadline` (a time.monotonic()-based instant).

        Bug #1784 review MAJOR-2: shared by every call site that needs to
        pass the CALLER's remaining operation budget into an identity-helper
        call. Never floors at zero -- a negative/zero value is meaningful
        (deadline already exhausted) and _get_cache_identity_info handles it
        by skipping the subprocess entirely rather than spawning one.
        """
        return deadline - time.monotonic()

    def _try_post_fill(
        self,
        rust_code: str,
        compile_ms: int,
        deadline_seconds: Optional[float] = None,
        graph_mode: bool = False,
    ) -> None:
        """Upload freshly compiled .so to cluster cache after a successful compile.

        Reads the local .so (written by Rust's compile_evaluator, named by
        the SAME composite identity — Bug #1784) and calls cache.store(). All
        exceptions are caught and logged at WARNING — the .so exists locally
        so functionality is unimpaired even if upload fails.

        Args:
            deadline_seconds: Remaining operation budget (review MAJOR-2),
                forwarded to the identity helper's subprocess timeout. None
                preserves the previous fixed-timeout behaviour for direct/
                isolated callers.
            graph_mode: H9 (consolidated review, Issue #1811/Bug #1812) --
                must be True when `rust_code` is a graph-mode evaluator, so
                the identity computed here (used to locate the local .so to
                upload) matches the one `compile_evaluator` actually used
                (`_compile_for_graph_mode` always passes True).
        """
        try:
            if self._xray_cache is None:
                logger.warning(
                    "XrayCache: post-fill called with no cache backend — skipping"
                )
                return
            info = self._get_cache_identity_info(
                rust_code, deadline_seconds=deadline_seconds, graph_mode=graph_mode
            )
            if info is None:
                logger.warning(
                    "XrayCache: post-fill skipped — could not compute cache identity"
                )
                return
            cache_dir = self._get_cache_dir()
            local_so = cache_dir / f"{info.identity}.so"
            if not local_so.exists():
                logger.warning(
                    "XrayCache: post-fill skipped — .so not found at %s", local_so
                )
                return
            so_bytes = local_so.read_bytes()
            self._xray_cache.store(
                info.identity,
                info.rustc_version,
                wrap_so_for_cluster_cache(info.identity, info.rustc_version, so_bytes),
                compile_ms,
            )
            logger.info(
                "XrayCache: uploaded %s to cluster cache (%d bytes)",
                info.identity[:12],
                len(so_bytes),
            )
        except Exception as exc:
            logger.warning("XrayCache: post-fill failed: %s", exc)

    def _validate_rust_code(self, evaluator_code: str) -> Tuple[str, Optional[str]]:
        """Validate Rust evaluator code. Returns (rust_code, error_msg).

        The evaluator_code is expected to already be valid Rust.  This method
        checks for required signature and forbidden constructs without any
        transformation — if valid, returns the code unchanged.
        """
        return validate_rust_code(evaluator_code)

    def _try_pre_fill(
        self,
        rust_code: str,
        deadline_seconds: Optional[float] = None,
        graph_mode: bool = False,
    ) -> None:
        """Fetch .so from cluster cache and write locally so Rust skips recompile.

        Bug #1784: keyed on the composite cache identity (not a raw
        sha256(user_code)), so a pre-filled artifact is written under
        EXACTLY the filename Rust's compile_evaluator() will look up. All
        exceptions are logged at WARNING — a failed pre-fill is non-fatal;
        Rust will simply compile from scratch.

        Args:
            deadline_seconds: Remaining operation budget (review MAJOR-2),
                forwarded to the identity helper's subprocess timeout. None
                preserves the previous fixed-timeout behaviour for direct/
                isolated callers.
            graph_mode: H9 (consolidated review, Issue #1811/Bug #1812) --
                must be True when `rust_code` is a graph-mode evaluator, so
                the identity computed here matches the one
                `compile_evaluator` actually uses (`_compile_for_graph_mode`
                always passes True).

        Early-returns when BOTH .so and .meta already exist for the identity.
        """
        assert self._xray_cache is not None, (
            "_try_pre_fill requires non-None _xray_cache"
        )
        try:
            info = self._get_cache_identity_info(
                rust_code, deadline_seconds=deadline_seconds, graph_mode=graph_mode
            )
            if info is None:
                return  # cannot determine identity -- Rust will compile fresh
            cache_dir = self._get_cache_dir()
            local_so = cache_dir / f"{info.identity}.so"
            meta_path = cache_dir / f"{info.identity}.meta"
            if local_so.exists() and meta_path.exists():
                return  # both artifacts present — no fetch needed
            blob = self._xray_cache.fetch(info.identity, info.rustc_version)
            if blob is None:
                return  # cluster miss — Rust will compile fresh
            so_bytes = verified_so_from_cluster_blob(
                info.identity, info.rustc_version, blob
            )
            if so_bytes is None:
                # The bytes in the shared table are not the artifact this
                # cluster compiled for this key (tampered, truncated, stored
                # under a different key, or written before the envelope
                # existed). Writing them would hand Rust a cache hit it
                # dlopens unverified, so refuse and let the local compile
                # path run — a refusal costs one recompile, after which
                # _try_post_fill rewrites the row in the verifiable format.
                logger.warning(
                    "XrayCache: REFUSED cluster blob for %s (%d bytes) — "
                    "integrity verification failed; compiling locally instead",
                    info.identity[:12],
                    len(blob) if blob is not None else 0,
                )
                _record_cache_integrity_failure("verification_failed")
                return
            cache_dir.mkdir(parents=True, exist_ok=True)
            self._write_prefilled_artifact(info, meta_path, local_so, so_bytes)
            logger.info(
                "XrayCache: pre-filled %s from cluster cache", info.identity[:12]
            )
        except Exception as exc:
            logger.warning("XrayCache: pre-fill failed: %s", exc)

    @staticmethod
    def _write_prefilled_artifact(
        info: CacheIdentityInfo, meta_path: Path, local_so: Path, blob: bytes
    ) -> None:
        """Write .meta then .so atomically for a cluster-cache pre-fill hit.

        Writes .meta BEFORE .so to avoid partial state: if meta write fails,
        .so is never written. On .so write failure, .meta is deleted to
        prevent orphan metadata. The epoch format '{epoch}s-since-epoch'
        matches Rust's is_fresh() contract; abi_version matches Rust's
        CacheMetadata field (Bug #1784) so the freshness check on the Rust
        side accepts this pre-filled artifact as valid.

        Bug #1784 review MINOR-5: temp .so path comes from tempfile.mkstemp()
        (collision-safe across concurrent threads sharing one PID), not the
        old f"{name}.tmp.{os.getpid()}" scheme. Cleanup on failure is
        delegated to _cleanup_prefill_failure so none of its steps can mask
        the original exception, which always re-raises via the bare `raise`.
        """
        import os  # noqa: PLC0415 — stdlib, lazy import
        import time  # noqa: PLC0415 — stdlib, lazy import

        epoch = int(time.time())
        meta_path.write_text(
            f"source_hash={info.source_hash}\n"
            f"rustc_version={info.rustc_version}\n"
            f"abi_version={info.abi_version}\n"
            f"compiled_at={epoch}s-since-epoch\n"
            f"compile_ms=0\n"
        )
        fd: Optional[int] = None
        tmp_so: Optional[Path] = None
        try:
            fd, tmp_so_name = tempfile.mkstemp(
                dir=str(local_so.parent), prefix=f"{local_so.name}.tmp."
            )
            tmp_so = Path(tmp_so_name)
            with os.fdopen(fd, "wb") as f:
                fd = None  # ownership now with `f`; its __exit__ closes it
                f.write(blob)
            tmp_so.rename(local_so)
        except Exception:
            RustNativeBackend._cleanup_prefill_failure(fd, tmp_so, meta_path)
            raise

    @staticmethod
    def _cleanup_prefill_failure(
        fd: Optional[int], tmp_so: Optional[Path], meta_path: Path
    ) -> None:
        """Best-effort cleanup after a _write_prefilled_artifact failure.

        Every step is independently wrapped so a cleanup-step failure can
        never mask the caller's original, re-raised exception (Bug #1784
        review).
        """
        import os  # noqa: PLC0415 — stdlib, lazy import

        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if tmp_so is not None:
            try:
                tmp_so.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            meta_path.unlink(missing_ok=True)
        except OSError:
            pass

    def _run_xray_cli_process(
        self,
        cmd: List[str],
        timeout_seconds: int,
        on_process_spawned: Optional[Callable],
        acquire_compile_slot: bool,
        apply_compile_limits: bool = False,
    ) -> Tuple[str, Optional[str]]:
        """Spawn xray-cli, wait for completion, return (stdout, error_msg).

        R2-3 (Codex re-review): spawned in its OWN process group
        (`start_new_session=True`) so that on timeout the ENTIRE group --
        including any grandchild the direct child spawned (e.g. rustc
        launched by xray-cli) -- is killed via `os.killpg`, never just the
        direct child via a bare `proc.kill()`. SIGKILL cannot be caught or
        forwarded by the process it kills, so a bare `proc.kill()` leaves
        any grandchild reparented to init, running FOREVER as an orphan
        that keeps consuming CPU/RAM.

        R2-4 (Codex re-review): `acquire_compile_slot` has NO DEFAULT --
        every call site must state whether this invocation can trigger a
        fresh rustc compile (the legacy scan path: True) or only runs an
        already-compiled dylib (--build-graph/--analyze-graph: False), so
        a future call site can never silently inherit an unthrottled
        default. When True, a process-wide compile slot is acquired
        (deadline-aware, bounded by `timeout_seconds`) BEFORE spawning the
        subprocess -- a saturated compile queue returns a clear timeout
        error instead of an unbounded Nth concurrent rustc process, and
        the slot is released in `finally` regardless of outcome.
        """
        import os  # noqa: PLC0415 — stdlib, lazy import to keep startup clean
        import signal  # noqa: PLC0415 — stdlib, lazy import to keep startup clean

        if acquire_compile_slot and not _acquire_compile_slot(
            timeout_seconds=float(timeout_seconds)
        ):
            msg = (
                f"xray-cli compile queue full: timed out waiting "
                f"{timeout_seconds}s for a compile slot"
            )
            logger.warning("RustNativeBackend: %s", msg)
            return "", _sanitize_error_message(msg)

        try:
            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                start_new_session=True,
                env=xray_subprocess_env(),
                # Same gate as the compile slot, deliberately: only an
                # invocation that can trigger a rustc compile gets the
                # per-compile resource bounds. --build-graph/--analyze-graph
                # run an already-compiled dylib across a whole repository and
                # their memory profile has never been measured against these
                # numbers, so capping a compile must never silently cap them.
                preexec_fn=(
                    _apply_compile_resource_limits if apply_compile_limits else None
                ),
            )

            if on_process_spawned is not None:
                on_process_spawned(proc)

            try:
                stdout, stderr = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                # `start_new_session=True` makes proc.pid both the process
                # ID AND the process group ID (new session/group leader),
                # so `os.killpg(proc.pid, ...)` reaches the direct child
                # AND every descendant it spawned in one signal.
                # ProcessLookupError means the whole group already exited
                # between TimeoutExpired firing and this line (a benign
                # race, not an error). PermissionError is a second, rarer
                # benign race: the group leader already exited and its
                # PGID was reused by an unrelated process this session
                # cannot signal. Neither must prevent proc.wait() below
                # from reaping the direct child.
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
                proc.wait()
                msg = xray_cli_timeout_message(timeout_seconds)
                logger.warning("RustNativeBackend: %s", msg)
                return "", _sanitize_error_message(msg)

            # H12 (consolidated review, Issue #1811/Bug #1812, Codex):
            # treat EVERY non-zero return code as failure, including
            # stderr in the message -- a crashed subprocess that happened
            # to emit plausible-looking (partial/stale) JSON to stdout
            # before exiting non-zero must never be silently parsed and
            # accepted as a real result just because stdout was non-empty.
            if proc.returncode != 0:
                raw_msg = xray_cli_exit_message(proc.returncode, stderr)
                logger.warning("RustNativeBackend: %s", raw_msg)
                return "", _sanitize_error_message(raw_msg)
            return stdout or "", None
        finally:
            if acquire_compile_slot:
                _release_compile_slot()

    def _invoke_xray_cli(
        self,
        rust_code: str,
        abs_paths: List[str],
        timeout_seconds: int,
        on_process_spawned: Optional[Callable],
        deadline_seconds: Optional[float] = None,
        tmp_dir: Optional[Path] = None,
    ) -> Tuple[str, Optional[str]]:
        """Write temp files (evaluator + candidate list), invoke xray-cli.

        Returns (stdout, error_msg).

        Bug #1612: the candidate file list is written to a temp file and
        handed to xray-cli via --files-from instead of individual --files
        argv elements. Passing tens of thousands of candidate paths as argv
        overflows ARG_MAX (OSError: [Errno 7] Argument list too long) once a
        repo's candidate set is large enough -- the temp-file handoff has no
        such ceiling.

        Args:
            deadline_seconds: Remaining operation budget (Bug #1784 review
                MAJOR-2), forwarded to the pre-fill identity call so it
                never blocks longer than the caller has left.
            tmp_dir: Directory the two per-invocation temp files (evaluator
                .rs, candidate-list .txt) are written into (Bug #1796).
                Defaults to _get_xray_tmp_dir() -- a CIDX-owned directory
                under CIDX_DATA_DIR or ~/.tmp -- NEVER the process-wide
                system temp directory. Exposed as an injectable seam so
                callers/tests can point it at an isolated directory and
                assert both where artifacts land and that cleanup happens.
        """
        resolved_tmp_dir = tmp_dir if tmp_dir is not None else _get_xray_tmp_dir()
        tmp_file: Optional[Any] = None
        files_tmp: Optional[Any] = None
        try:
            tmp_file, files_tmp = self._write_invoke_temp_files(
                rust_code, abs_paths, resolved_tmp_dir
            )

            # Cluster pre-fill: if PG has a fresh blob, write it locally so Rust
            # sees a local cache hit and skips compilation entirely.
            if self._xray_cache is not None:
                self._try_pre_fill(rust_code, deadline_seconds=deadline_seconds)

            cmd = [
                str(self._xray_cli_path),
                "--dynlib",
                tmp_file.name,
                "--files-from",
                files_tmp.name,
                "--json",
            ]
            return self._run_xray_cli_process(
                cmd,
                timeout_seconds,
                on_process_spawned,
                acquire_compile_slot=True,
                # NOT apply_compile_limits: this spawn compiles AND scans in one
                # process, and the scan is rayon-parallel. RLIMIT_CPU is
                # process-wide CPU time summed across threads, so a bound sized
                # for rustc (180s) is ~22s of wall clock on 8 cores -- a
                # large-repo query with a warm cache compiles nothing and would
                # still be SIGKILLed. RLIMIT_DATA was likewise measured against
                # rustc, never against a scan's ASTs and findings.
                #
                # The compile reached through THIS path is still heap-bounded:
                # xray_subprocess_env() hands xray-cli the compile limit
                # (_COMPILE_RLIMIT_DATA_ENV) and xray-cli sets RLIMIT_DATA on
                # the rustc child it spawns, never on itself -- so the scan
                # keeps its unmeasured, repository-sized memory profile.
                # Capping the user's scan with a number measured for the
                # compiler would convert a hypothetical into a certain
                # regression. Time is bounded by the 120s wall timeout and the
                # process-group kill.
                apply_compile_limits=False,
            )
        except OSError as exc:
            # Broadened from FileNotFoundError (Bug #1612): ANY OS-level
            # failure -- creating the temp files or spawning xray-cli,
            # including a residual E2BIG ("Argument list too long") -- must
            # be caught here and surfaced as a structured error tuple, never
            # propagate unhandled up through run_batch() into the MCP layer
            # as a raw -32603 internal error.
            msg = f"xray-cli could not be executed: {exc}"
            logger.error("RustNativeBackend: %s", msg)
            return "", _sanitize_error_message(msg)
        finally:
            if tmp_file is not None:
                Path(tmp_file.name).unlink(missing_ok=True)
            if files_tmp is not None:
                Path(files_tmp.name).unlink(missing_ok=True)

    @staticmethod
    def _write_invoke_temp_files(
        rust_code: str, abs_paths: List[str], directory: Path
    ) -> Tuple[Any, Any]:
        """Writes the evaluator .rs and candidate-list .txt temp files for
        one _invoke_xray_cli call (Bug #1796 tmp_dir contract unchanged).
        Leak-safe: if writing the SECOND file fails, the first is cleaned
        up HERE before re-raising, so a partial failure can never leak the
        first file regardless of how the caller unpacks the return value.
        """
        tmp_file = _write_temp_file(
            rust_code, suffix=".rs", prefix="xray_eval_", directory=directory
        )
        try:
            files_tmp = _write_temp_file(
                "\n".join(abs_paths),
                suffix=".txt",
                prefix="xray_files_",
                directory=directory,
            )
        except Exception:
            Path(tmp_file.name).unlink(missing_ok=True)
            raise
        return tmp_file, files_tmp

    def _parse_json_output(self, stdout: str) -> Tuple[Dict[str, Any], Optional[str]]:
        """Parse JSON from xray-cli stdout. Returns (output_dict, error_msg)."""
        return parse_json_output(stdout)

    def _build_results(
        self,
        file_specs: List[Dict[str, Any]],
        abs_paths: List[str],
        findings: List[Dict[str, Any]],
        resolved_root: Optional[Path] = None,
    ) -> _BatchResult:
        """Group findings by file and build result tuples per spec."""
        return _group_scan_results(
            file_specs,
            abs_paths,
            findings,
            lambda spec, abs_path, spec_findings: _build_matches(
                spec, spec_findings, abs_path=abs_path, resolved_root=resolved_root
            ),
        )

    # ------------------------------------------------------------------
    # Story #1811 (S5, AC2): graph-mode driver
    # ------------------------------------------------------------------

    def _compile_for_graph_mode(
        self,
        rust_code: str,
        eval_path: str,
        deadline_seconds: Optional[float],
    ) -> Tuple[Optional[str], Dict[str, Any], Optional[str]]:
        """Compile `rust_code` (already written to `eval_path`) to a `.so`
        via `xray-cli --compile-only`, reusing the EXISTING cluster-cache
        pre-fill/post-fill machinery unchanged (Bug #1784: identity comes
        ONLY from `--print-cache-identity`, never re-derived here).

        The graph pipeline's compile phase (`_graph_compile_phase`) run
        against this pod's step executor. Returns `(so_path, {"cached":
        bool, "compile_ms": int}, error_message)`; `so_path` is `None` iff
        `error_message` is not `None`.
        """
        return _graph_compile_phase(
            _LocalGraphStepExecutor(self, rust_code, eval_path=eval_path),
            deadline_seconds,
        )

    def _spawn_compile_only(
        self, eval_path: str, timeout_seconds: int
    ) -> Tuple[str, Optional[str]]:
        """Spawn `xray-cli --compile-only --dynlib <eval_path>`; return its
        raw `(stdout, error_msg)` from `_run_xray_cli_process`."""
        cmd = [str(self._xray_cli_path), "--compile-only", "--dynlib", eval_path]
        return self._run_xray_cli_process(
            cmd,
            timeout_seconds,
            None,
            acquire_compile_slot=True,
            # This process compiles and does nothing else, so the bound is
            # measured against exactly what runs in it.
            apply_compile_limits=True,
        )

    def _run_compile_only_subprocess(
        self, eval_path: str, timeout_seconds: int
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """`_spawn_compile_only`, with its JSON `CompileOnlyOutput` parsed.
        Returns `(output_dict, error_msg)` -- `error_msg` is `None` on a
        successful subprocess run (the COMPILE itself may still have failed;
        check `output_dict["error"]`).
        """
        stdout, error = self._spawn_compile_only(eval_path, timeout_seconds)
        if error is not None:
            return {}, error
        return self._parse_json_output(stdout)

    def _spawn_graph_subcommand(
        self, args: List[str], timeout_seconds: int
    ) -> Tuple[str, Optional[str]]:
        """Spawn `xray-cli <args...>` -- shared by `--build-graph`,
        `--analyze-graph` and `--refine` (Rule 4). They run an ALREADY
        compiled dylib, so they never take a compile slot.
        """
        cmd = [str(self._xray_cli_path)] + args
        return self._run_xray_cli_process(
            cmd, timeout_seconds, None, acquire_compile_slot=False
        )

    def _run_graph_subcommand(
        self, args: List[str], timeout_seconds: int
    ) -> Tuple[Dict[str, Any], Optional[str]]:
        """`_spawn_graph_subcommand`, with its JSON report parsed."""
        stdout, error = self._spawn_graph_subcommand(args, timeout_seconds)
        if error is not None:
            return {}, error
        return self._parse_json_output(stdout)

    @staticmethod
    def _build_graph_analysis_result(
        analyze_output: Dict[str, Any],
        build_status: str,
        build_result: Dict[str, Any],
        compile_info: Dict[str, Any],
    ) -> Dict[str, Any]:
        """See the module-level `build_graph_analysis_result`."""
        return build_graph_analysis_result(
            analyze_output, build_status, build_result, compile_info
        )

    def _compile_build_and_analyze(
        self,
        rust_code: str,
        repo_root: str,
        eval_path: str,
        files_from_path: str,
        graph_out: Path,
        facts_out: Path,
        deadline: float,
        *,
        refine: bool = False,
        file_paths: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """The whole graph-mode pipeline (`run_graph_pipeline`) run in this
        pod: every step is an xray-cli subprocess over the temp files the
        caller wrote, bounded by `deadline` (a time.monotonic() instant).
        """
        executor = _LocalGraphStepExecutor(
            self,
            rust_code,
            eval_path=eval_path,
            deadline=deadline,
            repo_root=repo_root,
            files_from_path=files_from_path,
            graph_out=graph_out,
            facts_out=facts_out,
        )
        return run_graph_pipeline(executor, refine=refine, file_paths=file_paths)

    def execute_graph_analysis(self, request: XrayGraphRequest) -> Dict[str, Any]:
        """Story #1811 (S5, AC2): compile the evaluator ONCE (reusing the
        existing compile/cache machinery, Bug #1784), then --build-graph ->
        --analyze-graph. Every KNOWN failure returns a structured
        `_graph_error_result`; the temp-file+subprocess flow is wrapped in
        `except OSError` (mirrors `_invoke_xray_cli` above) -- the MCP front
        door is the final backstop for anything truly unexpected (Bug #1612).

        Bug #1909: `refine=True` additionally invokes S3's `--refine` phase
        (see `_maybe_run_refine`) when analyze_graph's `GraphResult.refine`
        came back non-empty -- opt-in, never the default.
        """
        evaluator_code = request.evaluator_code
        repo_root = request.repo_root
        file_paths = request.file_paths
        timeout_seconds = request.timeout_seconds
        refine = request.refine
        if timeout_seconds <= 0 or not repo_root or not file_paths:
            return _graph_error_result(
                "InvalidArgument", "invalid timeout_seconds/repo_root/file_paths"
            )

        rust_code, validation_error = self._validate_rust_code(evaluator_code)
        if validation_error is not None:
            return _graph_error_result("ValidationError", validation_error)
        if not self._xray_cli_path.exists():
            msg = (
                f"xray-cli binary not found at {self._xray_cli_path}. "
                f"Set {_XRAY_CLI_PATH_ENV_VAR} to a prebuilt binary, or build "
                "it with: cd rust && cargo build --release -p xray-cli."
            )
            logger.error("RustNativeBackend: %s", msg)
            return _graph_error_result("BinaryNotFound", msg)

        tmp_eval = tmp_files_from = graph_out = facts_out = None
        try:
            deadline = time.monotonic() + timeout_seconds
            tmp_dir = _get_xray_tmp_dir()
            tmp_dir.mkdir(parents=True, exist_ok=True)
            graph_out = tmp_dir / f"xray_graph_out_{uuid.uuid4().hex}.bin"
            facts_out = tmp_dir / f"xray_graph_facts_{uuid.uuid4().hex}.json"
            tmp_eval = _write_temp_file(rust_code, ".rs", "xray_graph_eval_", tmp_dir)
            tmp_files_from = _write_temp_file(
                "\n".join(file_paths), ".txt", "xray_graph_files_", tmp_dir
            )
            return self._compile_build_and_analyze(
                rust_code,
                repo_root,
                tmp_eval.name,
                tmp_files_from.name,
                graph_out,
                facts_out,
                deadline,
                refine=refine,
                file_paths=file_paths,
            )
        except OSError as exc:
            msg = f"xray-cli graph analysis could not be executed: {exc}"
            logger.error("RustNativeBackend: %s", msg)
            return _graph_error_result("XRayCliError", msg)
        finally:
            eval_name = tmp_eval.name if tmp_eval is not None else None
            files_name = tmp_files_from.name if tmp_files_from is not None else None
            _safe_unlink_graph_temp_path(eval_name)
            _safe_unlink_graph_temp_path(files_name)
            _safe_unlink_graph_temp_path(graph_out)
            _safe_unlink_graph_temp_path(facts_out)


class _LocalGraphStepExecutor(GraphStepExecutor):
    """The graph pipeline's steps as xray-cli subprocesses in this pod.

    Every step goes through the backend's `_run_xray_cli_process` (process
    group kill, env allowlist, compile slot + rlimits for the compile), and
    the remaining budget through its `_remaining_seconds`, so the in-pod
    behaviour is exactly the backend's. The compile hooks are the cluster
    cache pre-fill/post-fill, keyed on the graph-mode identity (H9).
    """

    def __init__(
        self,
        backend: LocalXrayExecutionBackend,
        rust_code: str,
        *,
        eval_path: str,
        deadline: Optional[float] = None,
        repo_root: str = "",
        files_from_path: str = "",
        graph_out: Optional[Path] = None,
        facts_out: Optional[Path] = None,
    ) -> None:
        self._backend = backend
        self._rust_code = rust_code
        self._eval_path = eval_path
        self._deadline = deadline
        self._repo_root = repo_root
        self._files_from_path = files_from_path
        self._graph_out = graph_out
        self._facts_out = facts_out

    def remaining_seconds(self, step: str) -> float:
        if self._deadline is None:
            raise RuntimeError(f"no deadline was given for graph step {step!r}")
        return self._backend._remaining_seconds(self._deadline)

    def before_compile(self, deadline_seconds: Optional[float]) -> None:
        if self._backend._xray_cache is not None:
            self._backend._try_pre_fill(
                self._rust_code, deadline_seconds=deadline_seconds, graph_mode=True
            )

    def after_compile(
        self, compile_info: Dict[str, Any], deadline_seconds: Optional[float]
    ) -> None:
        if (
            self._backend._xray_cache is not None
            and not compile_info["cached"]
            and compile_info["compile_ms"] > 0
        ):
            self._backend._try_post_fill(
                self._rust_code,
                compile_info["compile_ms"],
                deadline_seconds=deadline_seconds,
                graph_mode=True,
            )

    def run_step(self, step: GraphStep) -> Tuple[str, Optional[str]]:
        if step.name == "compile":
            return self._backend._spawn_compile_only(
                self._eval_path, step.timeout_seconds
            )
        if step.so_path is None:
            raise ValueError(f"graph step {step.name!r} needs a compiled so_path")
        if step.name == "build":
            return self._backend._spawn_graph_subcommand(
                [
                    "--build-graph",
                    "--repo-root",
                    self._repo_root,
                    "--files-from",
                    self._files_from_path,
                    "--dylib",
                    step.so_path,
                    "--graph-out",
                    str(self._graph_out),
                    "--facts-out",
                    str(self._facts_out),
                ],
                step.timeout_seconds,
            )
        if step.name == "analyze":
            return self._backend._spawn_graph_subcommand(
                [
                    "--analyze-graph",
                    "--graph-in",
                    str(self._graph_out),
                    "--dylib",
                    step.so_path,
                    "--facts-in",
                    str(self._facts_out),
                ],
                step.timeout_seconds,
            )
        if step.name == "refine":
            return self._run_refine(step.so_path, step)
        raise ValueError(f"unknown graph step {step.name!r}")

    def _run_refine(self, so_path: str, step: GraphStep) -> Tuple[str, Optional[str]]:
        """`--refine` reads its narrowed file list from a temp file, written
        for this step only and removed whatever the outcome."""
        tmp_dir = _get_xray_tmp_dir()
        files_tmp: Optional[Any] = None
        try:
            files_tmp = _write_temp_file(
                "\n".join(step.refine_files or []),
                ".txt",
                "xray_refine_files_",
                tmp_dir,
            )
            return self._backend._spawn_graph_subcommand(
                [
                    "--refine",
                    "--graph-in",
                    str(self._graph_out),
                    "--dylib",
                    so_path,
                    "--repo-root",
                    self._repo_root,
                    "--files-from",
                    files_tmp.name,
                    "--facts-in",
                    str(self._facts_out),
                ],
                step.timeout_seconds,
            )
        finally:
            _safe_unlink_graph_temp_path(
                files_tmp.name if files_tmp is not None else None
            )


class RustNativeBackend(LocalXrayExecutionBackend):
    """The X-Ray evaluator backend the search engine and handlers construct.

    Keeps the ``run_batch`` / ``run_graph_analysis`` signatures every caller
    uses and hands each call, as ONE request, to the configured
    ``XrayExecutionBackend``: an injected one, else the one the running server
    built from ``xray_backend``, else this instance's own in-pod execution
    (``LocalXrayExecutionBackend``) -- which is what ``xray_backend=local``,
    the CLI and tests get, unchanged.
    """

    def __init__(
        self,
        xray_cache_backend: Optional[XrayCacheBackend] = None,
        identity_cache: Optional[SharedIdentityCache] = None,
        confine_to_repo_root: bool = True,
        execution_backend: Optional[XrayExecutionBackend] = None,
    ) -> None:
        """See ``LocalXrayExecutionBackend.__init__`` for the first three.

        Args:
            execution_backend: Where to execute; None selects as described in
                the class docstring.
        """
        super().__init__(
            xray_cache_backend=xray_cache_backend,
            identity_cache=identity_cache,
            confine_to_repo_root=confine_to_repo_root,
        )
        if execution_backend is None:
            from code_indexer.server.startup.xray_backend_wiring import (  # noqa: PLC0415
                server_xray_execution_backend,
            )

            execution_backend = server_xray_execution_backend()
        self._execution_backend: XrayExecutionBackend = execution_backend or self

    def run_batch(
        self,
        *,
        evaluator_code: str,
        file_specs: List[Dict[str, Any]],
        worker_threads: int = 4,
        timeout_seconds: int = 60,
        on_process_spawned: Optional[Callable] = None,
        repo_path: Optional[str] = None,
    ) -> _BatchResult:
        """Drop-in replacement for PythonEvaluatorSandbox.run_batch().

        Args:
            evaluator_code: Rust evaluator source code containing
                ``fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding>`` function.
            file_specs: List of dicts with file_path, source, lang,
                match_positions.
            worker_threads: Ignored — Rust uses rayon auto-threading.
            timeout_seconds: Timeout for the xray-cli subprocess.
            on_process_spawned: Optional callback when subprocess starts.
            repo_path: Base path for resolving relative file paths.

        Returns:
            List of (matches, errors, meta) tuples, one per file spec.
        """
        outcome = self._execution_backend.execute_scan(
            XrayScanRequest(
                evaluator_code=evaluator_code,
                file_specs=file_specs,
                repo_path=repo_path,
                timeout_seconds=timeout_seconds,
                on_process_spawned=on_process_spawned,
            )
        )
        # Side-channel read by XRaySearchEngine.run() for debug_output[].
        if outcome.debug_messages is not None:
            self._last_debug_messages = outcome.debug_messages
        return outcome.results

    def run_graph_analysis(
        self,
        *,
        evaluator_code: str,
        repo_root: str,
        file_paths: List[str],
        timeout_seconds: int = 60,
        refine: bool = False,
    ) -> Dict[str, Any]:
        """Compile once, then build-graph -> analyze-graph (-> refine).

        See ``LocalXrayExecutionBackend.execute_graph_analysis``.
        """
        return self._execution_backend.execute_graph_analysis(
            XrayGraphRequest(
                evaluator_code=evaluator_code,
                repo_root=repo_root,
                file_paths=file_paths,
                timeout_seconds=timeout_seconds,
                refine=refine,
            )
        )


# Where a match's line_content comes from: called once per xray-cli finding.
LineContentSource = Callable[[Dict[str, Any]], str]

# Builds one spec's matches from (spec, the path its findings are reported
# under, those findings).
_SpecMatchBuilder = Callable[
    [Dict[str, Any], str, List[Dict[str, Any]]], List[Dict[str, Any]]
]


def build_matches(
    spec: Dict[str, Any],
    spec_findings: List[Dict[str, Any]],
    *,
    line_content: LineContentSource,
) -> List[Dict[str, Any]]:
    """Convert xray-cli findings to match dicts for one file spec.

    The ONE place a scan match is shaped. ``line_content`` supplies each
    match's ``line_content`` and is the only source of it: this function
    reads nothing, so a caller without the repository on disk (a delegated
    execution) passes the lines it was handed and no local read can happen.
    """
    lang = spec.get("lang", "")
    rel_path = spec.get("file_path", "")
    return [
        {
            "line_number": finding.get("line", 0),
            "file_path": rel_path,
            "language": lang,
            "pattern": finding.get("pattern", ""),
            "snippet": finding.get("snippet", ""),
            "line_content": line_content(finding),
        }
        for finding in spec_findings
    ]


def _group_scan_results(
    file_specs: List[Dict[str, Any]],
    finding_paths: List[str],
    findings: List[Dict[str, Any]],
    build_spec_matches: _SpecMatchBuilder,
) -> BatchResult:
    """One (matches, [], None) tuple per spec, in spec order.

    Findings are grouped by the exact path string xray-cli reported
    (``finding["file"]``), which is the path it was given for that spec:
    ``finding_paths[i]`` for ``file_specs[i]``. A spec with no findings gets
    ``([], [], None)`` without calling ``build_spec_matches``.
    """
    findings_by_path: Dict[str, List[Dict[str, Any]]] = {}
    for finding in findings:
        findings_by_path.setdefault(finding.get("file", ""), []).append(finding)

    results: BatchResult = []
    for spec, path in zip(file_specs, finding_paths):
        spec_findings = findings_by_path.get(path, [])
        if not spec_findings:
            results.append(([], [], None))
            continue
        results.append((build_spec_matches(spec, path, spec_findings), [], None))
    return results


def build_scan_results(
    file_specs: List[Dict[str, Any]],
    finding_paths: List[str],
    findings: List[Dict[str, Any]],
    *,
    line_content_for: Callable[[Dict[str, Any], str], LineContentSource],
) -> BatchResult:
    """A scan's per-spec results from xray-cli's findings, with no disk read.

    ``finding_paths[i]`` is the path xray-cli was given for ``file_specs[i]``
    (findings are grouped by it, as the in-pod scan groups them);
    ``line_content_for(spec, path)`` returns that spec's line source. Every
    match is shaped by :func:`build_matches`.
    """
    return _group_scan_results(
        file_specs,
        finding_paths,
        findings,
        lambda spec, path, spec_findings: build_matches(
            spec, spec_findings, line_content=line_content_for(spec, path)
        ),
    )


def _build_matches(
    spec: Dict[str, Any],
    spec_findings: List[Dict[str, Any]],
    abs_path: str = "",
    resolved_root: Optional[Path] = None,
) -> List[Dict[str, Any]]:
    """Convert xray-cli findings to match dicts for one file spec, in this pod.

    ``line_content`` is the one xray-cli emitted from the source it parsed.
    Only a finding WITHOUT it (an older binary) falls back to reading
    ``abs_path`` here -- ``run_batch`` refuses that case under a delegated
    content backend, so this read only ever happens on a local repository.

    ``resolved_root``, when given, must be an already-resolved directory
    (resolved ONCE by the caller for the whole batch, not per file).
    ``line_content`` is reported only when ``abs_path`` resolves inside that
    root -- defense in depth alongside the candidate-collection filtering
    upstream, for the last point before a finding's content leaves this
    function.
    """
    contained = bool(abs_path) and (
        resolved_root is None or is_resolved_within_root(Path(abs_path), resolved_root)
    )
    source_lines: List[str] = []
    if contained and any("line_content" not in f for f in spec_findings):
        try:
            source = Path(abs_path).read_bytes().decode("utf-8", errors="replace")
            source_lines = source.splitlines()
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to read %s for line_content enrichment: %s", abs_path, exc
            )

    def _line_content(finding: Dict[str, Any]) -> str:
        if not contained:
            return ""
        if "line_content" in finding:
            emitted: str = finding["line_content"]
            return emitted
        idx = finding.get("line", 0) - 1
        return source_lines[idx] if 0 <= idx < len(source_lines) else ""

    return build_matches(spec, spec_findings, line_content=_line_content)


def _content_is_delegated() -> bool:
    """True when the running server reads repositories through a NON-local
    content backend. False outside a server (CLI, tests) -- read lazily so a
    CLI process never imports the server's wiring for nothing."""
    from code_indexer.server.startup.content_backend_wiring import (
        is_content_seam_delegated,
    )

    return is_content_seam_delegated()
