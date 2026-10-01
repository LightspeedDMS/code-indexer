"""Issue #1548 round-8: WriteLockManager owner_token ownership-identity tests.

Two Codex-reproduced exploits are covered here against the REAL
``WriteLockManager`` (not a fake):

- Issue 2: ``owner_name`` alone is not a unique per-acquisition ownership
  token. Two callers sharing the same ``owner_name`` (e.g. two migration
  passes) previously meant a STALE holder -- whose lock already expired
  and was legitimately re-acquired by a FRESH holder -- could still
  successfully ``renew()``/``release()`` what is now the fresh holder's
  lock, because the check only compared ``owner_name``.
- Issue 3: a renewal call that is slow/blocked building its temp file
  must not blindly recreate/renew a lock that was released (or
  re-acquired by a different holder) while it was busy -- the fix
  re-validates the lock's CURRENT on-disk state immediately before the
  atomic write. The Issue-3 tests below use REAL threads and a delay
  injected at the ``pathlib.Path.write_text`` I/O boundary (never the
  SUT's own methods) to reproduce a genuinely concurrent "renewal blocked
  mid-write" race.
"""

import json
import threading
from pathlib import Path
from typing import Any, Dict, cast

import pytest

from code_indexer.global_repos.write_lock_manager import WriteLockManager


@pytest.fixture
def manager(tmp_path):
    lock_dir = tmp_path / "golden-repos"
    lock_dir.mkdir(parents=True)
    return WriteLockManager(golden_repos_dir=lock_dir)


def _lock_file_content(manager: WriteLockManager, alias: str) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(manager._lock_file(alias).read_text()))


# ---------------------------------------------------------------------------
# Issue 2: stale-holder-same-owner-name replacement
# ---------------------------------------------------------------------------


def test_acquire_stores_owner_token_when_provided(manager):
    assert manager.acquire("demo", owner_name="migration", owner_token="token-1")
    assert _lock_file_content(manager, "demo")["owner_token"] == "token-1"


def test_acquire_without_owner_token_omits_it_from_metadata(manager):
    """Byte-identical behavior for every pre-#1548-round-8 caller."""
    assert manager.acquire("demo", owner_name="migration")
    assert "owner_token" not in _lock_file_content(manager, "demo")


def test_renew_with_stale_token_after_same_owner_name_reacquire_is_refused(manager):
    """Codex's exact same-owner-name replacement scenario: instance A
    acquires, its lock expires/gets released, instance B (SAME owner_name)
    acquires fresh with a NEW token. Instance A's still-running heartbeat
    must NOT be able to renew what is now instance B's lock.
    """
    assert manager.acquire("demo", owner_name="migration", owner_token="token-A")
    # Instance A's lock is released (e.g. TTL expiry + eviction, or a
    # legitimate release by A itself) and instance B re-acquires under the
    # SAME owner_name with a fresh token.
    assert manager.release("demo", owner_name="migration", owner_token="token-A")
    assert manager.acquire("demo", owner_name="migration", owner_token="token-B")

    # Instance A's heartbeat, unaware of the handover, tries to renew with
    # its OWN (now stale) token.
    renewed = manager.renew(
        "demo", owner_name="migration", ttl_seconds=999, owner_token="token-A"
    )

    assert renewed is False
    # Instance B's lock must be completely untouched by A's attempt.
    assert _lock_file_content(manager, "demo")["owner_token"] == "token-B"


def test_release_with_stale_token_after_same_owner_name_reacquire_is_refused(manager):
    assert manager.acquire("demo", owner_name="migration", owner_token="token-A")
    assert manager.release("demo", owner_name="migration", owner_token="token-A")
    assert manager.acquire("demo", owner_name="migration", owner_token="token-B")

    released = manager.release("demo", owner_name="migration", owner_token="token-A")

    assert released is False
    # Instance B's lock must still exist, untouched.
    assert _lock_file_content(manager, "demo")["owner_token"] == "token-B"


def test_renew_without_a_token_is_unaffected_by_token_mismatch_logic(manager):
    """A caller that never opts into tokens (owner_token=None throughout)
    keeps its pre-#1548-round-8 behavior exactly -- renewal is governed
    by owner_name alone.
    """
    assert manager.acquire("demo", owner_name="migration")
    assert manager.renew("demo", owner_name="migration", ttl_seconds=999)


def test_renew_with_correct_token_succeeds(manager):
    assert manager.acquire("demo", owner_name="migration", owner_token="token-1")
    assert manager.renew(
        "demo", owner_name="migration", ttl_seconds=999, owner_token="token-1"
    )
    assert _lock_file_content(manager, "demo")["ttl_seconds"] == 999


# ---------------------------------------------------------------------------
# Issue 3: renewal blocked mid-write, races a concurrent release()/re-acquire()
# ---------------------------------------------------------------------------

_WAIT_TIMEOUT_SECONDS = 5.0


def _install_write_text_delay(monkeypatch):
    """Delay the SPECIFIC ``Path.write_text`` call ``_write_renewed_lock_
    content`` makes for its own ``.tmp-<uuid>`` temp file -- identified
    purely by the temp-file naming convention the SUT already uses, never
    by patching any WriteLockManager method itself. ``acquire()``/
    ``release()`` do not call ``Path.write_text`` at all (acquire uses a
    raw file descriptor; release only reads + unlinks), so this delay is
    scoped to exactly the renewal write path under test.

    Returns (write_started, release_done) events: the caller signals
    ``release_done`` once its concurrent release()/re-acquire() has run,
    letting the blocked renewal proceed into its real
    ``_current_lock_state_matches`` recheck and (if that recheck fails,
    as it now must) refuse to write.
    """
    write_started = threading.Event()
    release_done = threading.Event()
    real_write_text = Path.write_text

    def _delayed_write_text(self, *args, **kwargs):
        if ".tmp-" in self.name:
            write_started.set()
            assert release_done.wait(timeout=_WAIT_TIMEOUT_SECONDS), (
                "test deadlocked waiting for the concurrent "
                "release()/re-acquire() to signal completion"
            )
        return real_write_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", _delayed_write_text)
    return write_started, release_done


def test_renewal_blocked_mid_write_does_not_recreate_lock_after_release(
    manager, monkeypatch
):
    """Codex's exact scenario: a renewal call is slow/blocked building its
    temp file. WHILE it is blocked, the lock is legitimately released.
    When the renewal finally wakes up and reaches its write step, it must
    refuse to write (recreating the lock file) rather than blindly
    completing.
    """
    assert manager.acquire("demo", owner_name="migration", owner_token="token-1")
    write_started, release_done = _install_write_text_delay(monkeypatch)

    result: dict = {}

    def _do_renew():
        result["renewed"] = manager.renew(
            "demo", owner_name="migration", ttl_seconds=999, owner_token="token-1"
        )

    renewal_thread = threading.Thread(target=_do_renew)
    renewal_thread.start()
    assert write_started.wait(timeout=_WAIT_TIMEOUT_SECONDS), (
        "renewal thread never reached its temp-file write"
    )

    # Concurrently, while the renewal is genuinely blocked inside its own
    # write step, the lock is released.
    released = manager.release("demo", owner_name="migration", owner_token="token-1")
    assert released, "release() should succeed while renewal is blocked"
    release_done.set()

    renewal_thread.join(timeout=_WAIT_TIMEOUT_SECONDS)
    assert not renewal_thread.is_alive(), "renewal thread did not finish"

    assert result["renewed"] is False
    # The critical assertion: the lock file must NOT have been recreated
    # by the renewal that woke up after release() already ran.
    assert not manager._lock_file("demo").exists()


def test_renewal_blocked_mid_write_races_a_fresh_reacquire_and_loses(
    manager, monkeypatch
):
    """Variant of the round-8 race: instead of a bare release(), a
    DIFFERENT holder re-acquires the alias (same owner_name, new token)
    while the first renewal is blocked. The blocked renewal must refuse
    to overwrite the fresh holder's lock.

    The fresh re-acquire deliberately goes through a SEPARATE
    ``WriteLockManager`` instance pointed at the same lock directory --
    simulating a genuinely different process (each instance owns its own
    in-memory ``_intra_process_guards``, so the two never contend on the
    same intra-process ``threading.Lock``). Reusing the SAME manager
    instance for both sides would instead exercise that manager's own
    (real, separate) intra-process mutual exclusion between ``acquire()``
    and an in-flight ``renew()`` -- a different protection than the
    round-8 cross-process token fix this test targets.
    """
    assert manager.acquire("demo", owner_name="migration", owner_token="token-1")
    write_started, release_done = _install_write_text_delay(monkeypatch)

    other_process_manager = WriteLockManager(golden_repos_dir=manager._golden_repos_dir)

    result: dict = {}

    def _do_renew():
        result["renewed"] = manager.renew(
            "demo", owner_name="migration", ttl_seconds=999, owner_token="token-1"
        )

    renewal_thread = threading.Thread(target=_do_renew)
    renewal_thread.start()
    assert write_started.wait(timeout=_WAIT_TIMEOUT_SECONDS)

    released = manager.release("demo", owner_name="migration", owner_token="token-1")
    assert released
    acquired = other_process_manager.acquire(
        "demo", owner_name="migration", owner_token="token-2"
    )
    assert acquired
    release_done.set()

    renewal_thread.join(timeout=_WAIT_TIMEOUT_SECONDS)
    assert not renewal_thread.is_alive()

    assert result["renewed"] is False
    # The fresh holder's lock (token-2) must be completely untouched --
    # NOT renewed/overwritten by the stale holder's blocked renewal.
    assert _lock_file_content(manager, "demo")["owner_token"] == "token-2"
