"""Story #1494 AC4: failed-auth timing-equalization loop uses real bcrypt.

Finding C8 (GIL-blocking analysis report): `perform_dummy_password_work`
faked bcrypt timing with a 5000-iteration pure-Python hashlib.sha256 loop.
Real bcrypt releases the GIL during its work; the fake pure-Python loop does
not, so a credential-stuffing burst produced GIL-held CPU exactly where the
success path (real bcrypt.checkpw) would not. Fixed by calling a real
bcrypt.checkpw against a static dummy hash instead.

Wiring verification (`test_calls_real_bcrypt_checkpw`) wraps the real
`bcrypt.checkpw` with `wraps=...` purely to assert the call arguments -- the
real implementation still executes underneath. The GIL-release evidence
(`TestPerformDummyPasswordWorkReleasesGil`) performs zero mocking of any
kind: it drives the real, unpatched `perform_dummy_password_work` end to
end and measures actual wall-clock thread scaling, mirroring the source
report's own stated method ("workload run serially vs in 2 threads; >1.5x
speedup = GIL released"). The pass threshold there is intentionally a bit
looser (1.3x) than the report's 1.5x to absorb thread-scheduling/CI
contention noise while still clearly discriminating GIL-held (~1.0x, as the
report's own pure-Python control measured) from GIL-released (bcrypt
measured 2.23x in the report).
"""

from __future__ import annotations

import os
import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import bcrypt
import pytest

from code_indexer.server.auth.auth_error_handler import (
    AuthErrorHandler,
    _DUMMY_BCRYPT_HASH,
)

# Real bcrypt.checkpw is ~200-350ms per call on this hardware (matches the
# report's measured 334ms figure) -- keep the sample size small so this
# test completes in a few seconds, not tens of seconds.
_THREAD_SCALING_SAMPLE_SIZE = 4
_GIL_RELEASE_SPEEDUP_THRESHOLD = 1.3


@pytest.fixture
def error_handler() -> AuthErrorHandler:
    return AuthErrorHandler(minimum_response_time_ms=0)


class TestPerformDummyPasswordWorkUsesRealBcrypt:
    """The pure-Python 5000-iteration sha256 loop is replaced by a real
    bcrypt.checkpw call against a static dummy hash."""

    def test_calls_real_bcrypt_checkpw(self, error_handler: AuthErrorHandler) -> None:
        """Wiring check: wraps (does not replace behavior of) bcrypt.checkpw
        purely to observe the call arguments; the real hash comparison still
        executes underneath via wraps=bcrypt.checkpw."""
        with patch(
            "code_indexer.server.auth.auth_error_handler.bcrypt.checkpw",
            wraps=bcrypt.checkpw,
        ) as spy_checkpw:
            error_handler.perform_dummy_password_work()

        spy_checkpw.assert_called_once()
        password_arg, hash_arg = spy_checkpw.call_args.args
        assert isinstance(password_arg, bytes)
        assert isinstance(hash_arg, bytes)
        assert hash_arg == _DUMMY_BCRYPT_HASH

    def test_dummy_hash_is_a_valid_bcrypt_hash_with_comparable_cost(self) -> None:
        """The static dummy hash must be a genuine bcrypt hash whose work
        factor matches real credential hashes (cost 12, per
        PasswordManager's BcryptHasher default) so timing equalization
        against the real verify-password path actually holds."""
        assert _DUMMY_BCRYPT_HASH.startswith(b"$2b$12$")
        # Only the "does not raise" behavior is under test here (a
        # malformed/incompatible hash raises ValueError); the match/no-match
        # outcome is irrelevant to timing equalization, so the return value
        # is deliberately unused.
        _ = bcrypt.checkpw(b"anything", _DUMMY_BCRYPT_HASH)

    def test_no_longer_uses_pure_python_hash_loop(
        self, error_handler: AuthErrorHandler
    ) -> None:
        """hashlib.sha256 must not be invoked by the dummy-work path anymore
        -- the old 5000-iteration fake-timing loop is gone."""
        with patch(
            "code_indexer.server.auth.auth_error_handler.hashlib.sha256"
        ) as mock_sha256:
            error_handler.perform_dummy_password_work()

        mock_sha256.assert_not_called()


class TestPerformDummyPasswordWorkReleasesGil:
    """Real concurrent-thread evidence (zero mocking of any kind) that the
    dummy-work path releases the GIL, matching the report's own measurement
    methodology."""

    @pytest.mark.skipif(
        (os.cpu_count() or 1) < 2, reason="needs >=2 CPUs to observe GIL release"
    )
    def test_thread_scaling_shows_gil_release(
        self, error_handler: AuthErrorHandler
    ) -> None:
        n = _THREAD_SCALING_SAMPLE_SIZE

        serial_start = time.perf_counter()
        for _ in range(n):
            error_handler.perform_dummy_password_work()
        serial_elapsed = time.perf_counter() - serial_start

        # Matches the source report's own methodology: 2 concurrent threads.
        with ThreadPoolExecutor(max_workers=2) as pool:
            threaded_start = time.perf_counter()
            futures = [
                pool.submit(error_handler.perform_dummy_password_work) for _ in range(n)
            ]
            for fut in futures:
                fut.result()
            threaded_elapsed = time.perf_counter() - threaded_start

        speedup = serial_elapsed / threaded_elapsed
        assert speedup > _GIL_RELEASE_SPEEDUP_THRESHOLD, (
            f"Expected >{_GIL_RELEASE_SPEEDUP_THRESHOLD}x speedup from "
            f"GIL-releasing bcrypt work under threading, got {speedup:.2f}x "
            f"(serial={serial_elapsed:.3f}s, threaded={threaded_elapsed:.3f}s)"
        )
