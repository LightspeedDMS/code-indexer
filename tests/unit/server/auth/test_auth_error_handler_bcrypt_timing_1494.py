"""Story #1494 AC4: failed-auth timing-equalization loop uses real bcrypt.

Finding C8 (GIL-blocking analysis report): `perform_dummy_password_work`
faked bcrypt timing with a 5000-iteration pure-Python hashlib.sha256 loop.
Real bcrypt releases the GIL during its work; the fake pure-Python loop does
not, so a credential-stuffing burst produced GIL-held CPU exactly where the
success path (real bcrypt.checkpw) would not. Fixed by calling a real
bcrypt.checkpw against a static dummy hash instead.

Wiring verification (`test_calls_real_bcrypt_checkpw`) wraps the real
`bcrypt.checkpw` with `wraps=...` purely to assert the call arguments -- the
real implementation still executes underneath. A previous GIL-release test
measured wall-clock thread scaling, but that is not a stable unit-test
invariant: a loaded host can have no spare core, making correctly
GIL-releasing bcrypt slower than the serial baseline. The real-bcrypt wiring
and valid cost-factor tests below provide deterministic coverage without a
scheduling assumption.
"""

from __future__ import annotations

from unittest.mock import patch

import bcrypt
import pytest

from code_indexer.server.auth.auth_error_handler import (
    AuthErrorHandler,
    _DUMMY_BCRYPT_HASH,
)


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
