"""Story #932 regression: RefreshScheduler must structurally satisfy RefreshSchedulerProtocol.

Catches method-name typos in the Protocol vs concrete class — the exact failure mode
that shipped #932 to production. Pre-fix this test would have failed loudly:
RefreshSchedulerProtocol.is_write_lock_held does not match RefreshScheduler.is_write_locked.
"""

from __future__ import annotations

import inspect
import typing

import pytest

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.server.services.memory_store_service import (
    RefreshSchedulerProtocol,
)

# All methods that MemoryStoreService._coarse_piggyback_or_acquire and related
# helpers call on the scheduler.  Each method must exist on BOTH the Protocol
# and the real class so that typing and runtime behaviour agree.
REQUIRED_METHODS = (
    "acquire_write_lock",
    "release_write_lock",
    "is_write_locked",
    "trigger_refresh_for_repo",
)


class TestRefreshSchedulerProtocolConformance:
    def test_real_class_has_all_protocol_methods(self):
        """RefreshScheduler must implement every method declared by the Protocol.

        Covers all four methods used by MemoryStoreService call sites so any
        future Protocol/class mismatch is caught immediately rather than at runtime.
        """
        for method_name in REQUIRED_METHODS:
            assert hasattr(RefreshScheduler, method_name), (
                f"RefreshScheduler missing Protocol method: {method_name}"
            )

    def test_protocol_declares_all_required_methods(self):
        """RefreshSchedulerProtocol must declare every method the call sites need.

        Covers all four methods so adding a new call site without updating the
        Protocol is caught at test-time rather than in production.
        """
        for method_name in REQUIRED_METHODS:
            assert hasattr(RefreshSchedulerProtocol, method_name), (
                f"RefreshSchedulerProtocol missing method: {method_name}"
            )

    @pytest.mark.parametrize(
        "method_name",
        [
            "acquire_write_lock",
            "release_write_lock",
            "is_write_locked",
            "trigger_refresh_for_repo",
        ],
    )
    def test_signature_matches_protocol(self, method_name: str) -> None:
        """Story #932 Codex review: catch parameter-name + return-type drift across ALL
        Protocol methods.

        Replaces the earlier single-method test that only checked acquire_write_lock.
        Would have caught Issue 1 (release_write_lock: Protocol -> bool, real -> None)
        and Issue 2 (trigger_refresh_for_repo: Protocol param repo_alias, real alias_name)
        at test-time rather than in production.
        """
        real_method = getattr(RefreshScheduler, method_name)
        proto_method = getattr(RefreshSchedulerProtocol, method_name)

        real_sig = inspect.signature(real_method)
        proto_sig = inspect.signature(proto_method)

        # Parameter names must match (already string-comparable, no resolution needed)
        real_params = set(real_sig.parameters.keys())
        proto_params = set(proto_sig.parameters.keys())
        assert real_params == proto_params, (
            f"{method_name} param mismatch:\n"
            f"  Protocol: {sorted(proto_params)}\n"
            f"  Real:     {sorted(real_params)}"
        )

        # Return annotation must match — but Protocol uses `from __future__ import annotations`
        # (PEP 563), so its annotations are string literals. Use typing.get_type_hints() to
        # resolve forward references on BOTH sides to actual type objects before comparing.
        # No fallback: if resolution fails, the test fails loudly so annotation drift is caught.
        real_hints = typing.get_type_hints(real_method)
        proto_hints = typing.get_type_hints(proto_method)

        real_return = real_hints.get("return")
        proto_return = proto_hints.get("return")

        assert real_return == proto_return, (
            f"{method_name} return type mismatch (after resolving annotations):\n"
            f"  Protocol: {proto_return!r}\n"
            f"  Real:     {real_return!r}"
        )

    def test_no_old_misspelled_method_remains(self):
        """The pre-#932 typo must not be reintroduced anywhere.

        This test documents the exact failure mode: commit 6514a8d6 introduced
        'is_write_lock_held' in RefreshSchedulerProtocol while the real class
        implements 'is_write_locked'. MagicMock synthesised the misspelled name
        silently, so all 174 unit tests passed while the runtime exploded with
        AttributeError on every create_memory / edit_memory / delete_memory call.
        """
        assert not hasattr(RefreshScheduler, "is_write_lock_held"), (
            "RefreshScheduler must NOT have is_write_lock_held — that was the #932 typo"
        )
        assert not hasattr(RefreshSchedulerProtocol, "is_write_lock_held"), (
            "RefreshSchedulerProtocol must NOT have is_write_lock_held — that was the #932 typo"
        )
