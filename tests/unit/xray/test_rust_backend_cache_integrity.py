"""Integrity verification for `.so` blobs fetched from the cluster cache.

The cluster cache stores compiled `.so` bytes in PostgreSQL;
`RustNativeBackend._try_pre_fill` writes them straight to the local xray
cache directory, where Rust finds them as a cache hit and `dlopen`s them
WITHOUT compiling. Before this module's fix the only load-time gate was
`dynlib.rs` byte-comparing an embedded rustc-version string plus an ABI
export -- neither of which says anything about whether the bytes are the
ones this cluster compiled. `so_bytes` was never checked against the key
it was stored under, so a row rewritten in place under a legitimate key
was `dlopen`ed unverified on every pod that queried it.

The fix is an integrity envelope: `_try_post_fill` wraps the compiled
`.so` in a digest bound to (cache identity, rustc version) before
handing it to `cache.store()`, and `_try_pre_fill` refuses to write
anything it cannot verify. Refusal is NEVER an error -- the local
compile path is the fallback, so an unverifiable row costs one
recompile, not a failed query.

Every test here reverts to RED if the specific behaviour it names is
removed from rust_backend.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, List, Optional, Tuple
from unittest.mock import MagicMock, patch


from code_indexer.xray import rust_backend

_IDENTITY = "a" * 64
_OTHER_IDENTITY = "b" * 64
_SOURCE_HASH = "c" * 64
_RUSTC = "rustc 1.98.0 (abcdef 2025-01-01)"
_OTHER_RUSTC = "rustc 1.91.0 (111111 2024-01-01)"
_SO_BYTES = b"\x7fELF" + b"fake compiled evaluator payload" * 8
_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { Vec::new() }"
_HMAC_ENV = "CIDX_XRAY_CACHE_HMAC_KEY"


class _FakeClusterCache:
    """Minimal XrayCacheBackend double that records what it is asked for."""

    def __init__(self, blob: Optional[bytes]) -> None:
        self.blob = blob
        self.fetch_calls: List[Tuple[str, str]] = []
        self.stored: List[Tuple[str, str, bytes, int]] = []

    def fetch(self, source_hash: str, rustc_version: str) -> Optional[bytes]:
        self.fetch_calls.append((source_hash, rustc_version))
        return self.blob

    def store(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compile_ms: int = 0,
    ) -> None:
        self.stored.append((source_hash, rustc_version, so_bytes, compile_ms))


def _identity_stdout(identity: str = _IDENTITY, rustc: str = _RUSTC) -> str:
    return (
        f"identity={identity}\n"
        f"source_hash={_SOURCE_HASH}\n"
        f"abi_version=2\n"
        f"rustc_version={rustc}\n"
    )


def _run_pre_fill(cache: Any, cache_dir: Path, identity_stdout: str) -> None:
    backend = rust_backend.RustNativeBackend(xray_cache_backend=cache)
    identity_result = MagicMock(returncode=0, stdout=identity_stdout, stderr="")
    with patch.object(backend, "_get_cache_dir", return_value=cache_dir):
        with patch("subprocess.run", return_value=identity_result):
            backend._try_pre_fill(_EVALUATOR)


def _run_post_fill(cache: Any, cache_dir: Path, so_bytes: bytes) -> None:
    backend = rust_backend.RustNativeBackend(xray_cache_backend=cache)
    (cache_dir / f"{_IDENTITY}.so").write_bytes(so_bytes)
    identity_result = MagicMock(returncode=0, stdout=_identity_stdout(), stderr="")
    with patch.object(backend, "_get_cache_dir", return_value=cache_dir):
        with patch("subprocess.run", return_value=identity_result):
            backend._try_post_fill(_EVALUATOR, compile_ms=42)


# ---------------------------------------------------------------------------
# The envelope itself
# ---------------------------------------------------------------------------


def test_wrapped_blob_round_trips_back_to_the_original_so_bytes() -> None:
    """Wrapping then verifying under the SAME identity/rustc returns the
    exact compiled bytes -- the envelope must be lossless, or a verified
    cache hit would `dlopen` something that is not the artifact."""
    blob = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    assert blob != _SO_BYTES, "the stored blob must carry integrity material"
    assert (
        rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, blob) == _SO_BYTES
    )


def test_payload_tampered_after_storage_is_refused() -> None:
    """Flipping ONE byte of the compiled payload inside an otherwise
    well-formed envelope must fail verification. This is the finding:
    a row rewritten in place under a legitimate key."""
    blob = bytearray(
        rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    )
    blob[-1] ^= 0xFF
    assert (
        rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, bytes(blob))
        is None
    )


def test_digest_tampered_to_match_a_replaced_payload_is_refused() -> None:
    """An attacker who rewrites the payload AND recomputes a BARE content
    hash of it must still fail: the digest is bound to the cache identity
    and rustc version, not computed over the payload alone."""
    evil = b"\x7fELF" + b"attacker payload" * 16
    forged = (
        rust_backend._SO_ENVELOPE_MAGIC
        + rust_backend._SO_ENVELOPE_TAG_SHA256
        + hashlib.sha256(evil).digest()
        + evil
    )
    assert rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, forged) is None


def test_envelope_is_bound_to_the_cache_identity() -> None:
    """A blob legitimately compiled and stored under identity A must not
    verify under identity B -- otherwise an attacker with DB write could
    move an old/known-bad artifact under a hot key instead of forging one."""
    blob = rust_backend.wrap_so_for_cluster_cache(_OTHER_IDENTITY, _RUSTC, _SO_BYTES)
    assert rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, blob) is None
    assert (
        rust_backend.verified_so_from_cluster_blob(_OTHER_IDENTITY, _RUSTC, blob)
        == _SO_BYTES
    )


def test_envelope_is_bound_to_the_rustc_version() -> None:
    """The rustc version is the other half of the row's key (fetch filters
    on it); the envelope must bind it too."""
    blob = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    assert (
        rust_backend.verified_so_from_cluster_blob(_IDENTITY, _OTHER_RUSTC, blob)
        is None
    )


def test_legacy_unwrapped_blob_is_refused() -> None:
    """Rows written before this change carry a bare `.so` with no envelope.
    They are unverifiable, so they must be refused (costing one local
    recompile, after which post-fill rewrites the row wrapped) rather than
    trusted -- a "bare bytes means legacy, trust it" fallback would be the
    whole hole, re-opened by an attacker simply not writing an envelope."""
    assert (
        rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, _SO_BYTES) is None
    )


def test_truncated_envelope_is_refused_without_raising() -> None:
    """A short/garbage blob must verify as None, never raise -- fetch()
    returns whatever is in the column."""
    blob = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    for cut in (0, 1, 8, 9, 20, len(rust_backend._SO_ENVELOPE_MAGIC) + 32):
        assert (
            rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, blob[:cut])
            is None
        )


def test_digest_comparison_is_constant_time() -> None:
    """Secret-bearing comparison must not leak via timing (CLAUDE.md:
    constant-time comparison for secrets, never `==`). With the HMAC key
    configured the stored digest IS a secret-derived value."""
    import inspect

    source = inspect.getsource(rust_backend.verified_so_from_cluster_blob)
    assert (
        "compare_digest" in source
    ), "digest comparison must use hmac.compare_digest, not == / !="


# ---------------------------------------------------------------------------
# The optional cluster HMAC key: defence against an attacker with DB write
# ---------------------------------------------------------------------------


def test_hmac_key_rejects_an_envelope_forged_without_the_key(monkeypatch) -> None:
    """A plain sha256 envelope is forgeable by anyone who can write the
    row. With CIDX_XRAY_CACHE_HMAC_KEY set, a node must refuse any blob
    that is not authenticated with that key."""
    monkeypatch.delenv(_HMAC_ENV, raising=False)
    forgeable = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)

    monkeypatch.setenv(_HMAC_ENV, "cluster-shared-secret")
    assert (
        rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, forgeable) is None
    )


def test_hmac_key_rejects_an_envelope_made_with_a_different_key(monkeypatch) -> None:
    monkeypatch.setenv(_HMAC_ENV, "key-one")
    blob = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    monkeypatch.setenv(_HMAC_ENV, "key-two")
    assert rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, blob) is None


def test_hmac_key_accepts_its_own_envelope(monkeypatch) -> None:
    monkeypatch.setenv(_HMAC_ENV, "cluster-shared-secret")
    blob = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    assert (
        rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, blob) == _SO_BYTES
    )


def test_node_without_the_key_refuses_an_hmac_envelope(monkeypatch) -> None:
    """Asymmetric by design: a node that lost its key must not silently
    fall back to trusting the unauthenticated half of the format."""
    monkeypatch.setenv(_HMAC_ENV, "cluster-shared-secret")
    blob = rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    monkeypatch.delenv(_HMAC_ENV, raising=False)
    assert rust_backend.verified_so_from_cluster_blob(_IDENTITY, _RUSTC, blob) is None


# ---------------------------------------------------------------------------
# End to end through _try_pre_fill / _try_post_fill
# ---------------------------------------------------------------------------


def test_pre_fill_writes_the_so_when_the_envelope_verifies(tmp_path) -> None:
    cache = _FakeClusterCache(
        rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    )
    _run_pre_fill(cache, tmp_path, _identity_stdout())

    assert (tmp_path / f"{_IDENTITY}.so").read_bytes() == _SO_BYTES
    assert (tmp_path / f"{_IDENTITY}.meta").exists()


def test_pre_fill_never_writes_a_so_it_could_not_verify(tmp_path) -> None:
    """THE finding. A tampered row must not reach the local cache
    directory, because anything that lands there is `dlopen`ed by Rust
    on the next query with no further check."""
    tampered = bytearray(
        rust_backend.wrap_so_for_cluster_cache(_IDENTITY, _RUSTC, _SO_BYTES)
    )
    tampered[-5] ^= 0xFF
    cache = _FakeClusterCache(bytes(tampered))

    _run_pre_fill(cache, tmp_path, _identity_stdout())

    assert not (
        tmp_path / f"{_IDENTITY}.so"
    ).exists(), "an unverifiable .so must never be written to the dlopen'd cache dir"
    assert not (tmp_path / f"{_IDENTITY}.meta").exists(), (
        "a .meta without its .so would make Rust's freshness check see a "
        "half-written cache entry"
    )


def test_pre_fill_refusal_is_not_an_error(tmp_path) -> None:
    """Degrade safely: refusing a row means "recompile locally", so
    _try_pre_fill must return normally. A raise here would turn a poisoned
    cache row into a failed user query on every pod."""
    cache = _FakeClusterCache(b"\x7fELF not an envelope at all")
    _run_pre_fill(cache, tmp_path, _identity_stdout())  # must not raise
    assert not (tmp_path / f"{_IDENTITY}.so").exists()


def test_pre_fill_leaves_an_existing_local_so_untouched_on_refusal(tmp_path) -> None:
    """A refused cluster blob must not clobber or delete a .so this node
    compiled itself."""
    local_so = tmp_path / f"{_IDENTITY}.so"
    local_meta = tmp_path / f"{_IDENTITY}.meta"
    local_so.write_bytes(b"\x7fELF locally compiled")
    cache = _FakeClusterCache(b"garbage")

    _run_pre_fill(cache, tmp_path, _identity_stdout())

    assert local_so.read_bytes() == b"\x7fELF locally compiled"
    assert not local_meta.exists()


def test_post_fill_stores_a_blob_that_pre_fill_can_verify(tmp_path) -> None:
    """The two halves must agree: whatever post-fill writes to the cluster
    cache must be exactly what pre-fill accepts under the same key."""
    cache = _FakeClusterCache(None)
    _run_post_fill(cache, tmp_path, _SO_BYTES)

    assert len(cache.stored) == 1
    source_hash, rustc_version, stored_blob, compile_ms = cache.stored[0]
    assert (source_hash, rustc_version, compile_ms) == (_IDENTITY, _RUSTC, 42)
    assert stored_blob != _SO_BYTES, "post-fill must store the wrapped blob"
    assert (
        rust_backend.verified_so_from_cluster_blob(
            source_hash, rustc_version, stored_blob
        )
        == _SO_BYTES
    )


def test_post_fill_then_pre_fill_round_trips_through_the_cache(tmp_path) -> None:
    """Full loop on one node: compile -> store -> (another pod) fetch ->
    pre-fill writes the identical bytes."""
    uploader = _FakeClusterCache(None)
    _run_post_fill(uploader, tmp_path, _SO_BYTES)
    stored_blob = uploader.stored[0][2]

    downloader_dir = tmp_path / "other-pod"
    downloader_dir.mkdir()
    _run_pre_fill(_FakeClusterCache(stored_blob), downloader_dir, _identity_stdout())

    assert (downloader_dir / f"{_IDENTITY}.so").read_bytes() == _SO_BYTES


def test_refusal_metric_never_raises_without_a_telemetry_stack() -> None:
    """The refusal path runs inside _try_pre_fill's own try/except, but a
    metric helper that raised would turn every refusal into a logged
    pre-fill failure with a misleading message."""
    rust_backend._record_cache_integrity_failure("verification_failed")


def test_a_refusal_is_logged_loudly(tmp_path, caplog) -> None:
    """A refused blob is a security-relevant event. It must not be silent."""
    import logging

    cache = _FakeClusterCache(b"\x7fELF not an envelope")
    with caplog.at_level(logging.WARNING, logger="code_indexer.xray.rust_backend"):
        _run_pre_fill(cache, tmp_path, _identity_stdout())

    assert any(
        "REFUSED" in record.message and _IDENTITY[:12] in record.getMessage()
        for record in caplog.records
    ), f"expected a WARNING naming the refused identity; got {caplog.records!r}"
