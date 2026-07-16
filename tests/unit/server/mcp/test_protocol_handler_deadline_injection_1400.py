"""Story #1400 CRITICAL 5 dynamic half: protocol.py deadline injection.

_invoke_handler's SYNC-dispatch branch (run_in_executor + asyncio.wait_for)
must inject `handler_deadline_monotonic = time.monotonic() + timeout_seconds`
into any handler whose signature declares that parameter -- mirroring the
existing optional session_key/session_state injection pattern
(inspect.signature-based, additive, never breaks a handler that doesn't
declare it).

This lets a sync handler (search_code) compute its own internal deadlines
(e.g. a temporal foreground waiter's response_deadline) relative to the
SAME outer timeout the protocol layer is about to enforce via
asyncio.wait_for, so the handler can always return before the outer
timeout fires with NO job_id (the exact failure mode #1400 exists to
eliminate).

The async-dispatch branch (await handler(...), no timeout wrapper at all
per the documented CLAUDE.md sync/async distinction) does NOT receive this
injection -- there is no meaningful "deadline" to compute since there is
no enforced timeout on that path.
"""

import inspect
import time
from datetime import datetime
from typing import Any, Dict, Optional

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.protocol import _invoke_handler

_DUMMY_HASH = "$2b$12$dummyhashfortest000000000000000000000000000000000000000"
_TEST_TIMEOUT_SECONDS = 30.0


def _make_user() -> User:
    return User(
        username="admin",
        password_hash=_DUMMY_HASH,
        role=UserRole.ADMIN,
        created_at=datetime.now(),
    )


class TestSyncHandlerReceivesDeadline:
    @pytest.mark.asyncio
    async def test_sync_handler_declaring_deadline_param_receives_monotonic_deadline(
        self,
    ) -> None:
        received: Dict[str, Any] = {}

        def handler_with_deadline(
            args: Dict, user: User, handler_deadline_monotonic: Optional[float] = None
        ) -> Dict:
            received["handler_deadline_monotonic"] = handler_deadline_monotonic
            return {"ok": True}

        sig = inspect.signature(handler_with_deadline)
        before = time.monotonic()
        await _invoke_handler(
            handler=handler_with_deadline,
            arguments={},
            user=_make_user(),
            session_state=None,
            sig=sig,
            is_async=False,
            timeout_seconds=_TEST_TIMEOUT_SECONDS,
        )
        after = time.monotonic()

        deadline = received["handler_deadline_monotonic"]
        assert deadline is not None
        # deadline == call-time monotonic() + timeout_seconds, bracketed by
        # the before/after monotonic() calls around the _invoke_handler call.
        assert (
            before + _TEST_TIMEOUT_SECONDS <= deadline <= after + _TEST_TIMEOUT_SECONDS
        )

    @pytest.mark.asyncio
    async def test_plain_handler_without_deadline_param_receives_nothing(self) -> None:
        """A handler that does NOT declare handler_deadline_monotonic must
        not receive it -- no TypeError, no unexpected kwarg."""

        def plain_handler(args: Dict, user: User) -> Dict:
            return {"ok": True}

        sig = inspect.signature(plain_handler)
        result = await _invoke_handler(
            handler=plain_handler,
            arguments={},
            user=_make_user(),
            session_state=None,
            sig=sig,
            is_async=False,
            timeout_seconds=_TEST_TIMEOUT_SECONDS,
        )
        assert result == {"ok": True}


class TestAsyncHandlerDoesNotReceiveDeadline:
    @pytest.mark.asyncio
    async def test_async_handler_declaring_deadline_param_gets_none(self) -> None:
        """The async-dispatch branch has no enforced outer timeout (per the
        documented sync/async distinction), so it must NOT receive a
        computed deadline even if it declares the parameter."""
        received: Dict[str, Any] = {}

        async def async_handler_with_deadline(
            args: Dict, user: User, handler_deadline_monotonic: Optional[float] = None
        ) -> Dict:
            received["handler_deadline_monotonic"] = handler_deadline_monotonic
            return {"ok": True}

        sig = inspect.signature(async_handler_with_deadline)
        await _invoke_handler(
            handler=async_handler_with_deadline,
            arguments={},
            user=_make_user(),
            session_state=None,
            sig=sig,
            is_async=True,
            timeout_seconds=_TEST_TIMEOUT_SECONDS,
        )

        assert received.get("handler_deadline_monotonic") is None


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
