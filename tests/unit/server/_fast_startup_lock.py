"""Zero-bound startup acquire of the primary-instance lock for unit tests (#1995).

``initialize_services`` calls ``acquire_primary_instance_lock(server_data_dir)``
with the production bound (5 s, Bug #1549), meant to ride out a predecessor
PROCESS that is still exiting.  In this suite the holder is nearly always an
earlier ``create_app()`` in the SAME process against the same data directory,
which never releases, so every second app slept the full 5 s before being
refused.  ``tests/unit/server/conftest.py`` installs this wrapper for startup
under the gate's ``CIDX_TEST_FAST_SQLITE=1`` flag: the default bound becomes 0,
explicit timeouts pass through unchanged, and the refusal (False) is the same.
"""

from __future__ import annotations

from code_indexer.server.utils import primary_instance_lock

_real_acquire = primary_instance_lock.acquire_primary_instance_lock


def acquire_without_startup_wait(server_data_dir: str, timeout: float = 0.0) -> bool:
    """``acquire_primary_instance_lock`` with a zero default bound."""
    return _real_acquire(server_data_dir, timeout)
