"""
Bug fix: LLM Lease Lifecycle late-initialization on cluster secondary nodes.

Secondary nodes skip the early LLM lease lifecycle init (lines 687-727 in
lifespan.py) because their local SQLite config lacks claude_auth_mode:
subscription.  The shared PG config is only available after the cluster pool
connects.

This test file verifies that a late-initialization block mirrors the OIDC
late-init pattern (Bug #998) inserted immediately after the OIDC late-init
block and before the Bug #587 block.

All assertions are source-text / structural -- no imports of FastAPI app or
network connections required.

Helpers:
  _lifespan_source()           -- reads lifespan.py text
  _oidc_late_init_marker_pos() -- offset of 'OIDC late-initialized from PG config'
  _llm_late_init_tail()        -- source text from 'llm_lifecycle_service is None' onward

Test classes:
  TestLlmLeaseLifecycleLateInitGuard   -- verifies structural presence and
      content of the late-init block.

  TestLlmLeaseLifecycleLateInitOrdering -- verifies the block appears at the
      correct position relative to OIDC late-init and Bug #587.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT_DEPTH = 4  # test_file -> startup -> server -> unit -> tests -> repo
_REPO_ROOT = Path(__file__).resolve().parents[_REPO_ROOT_DEPTH]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _lifespan_source() -> str:
    """Return full text of lifespan.py."""
    return _LIFESPAN_PATH.read_text()


def _oidc_late_init_marker_pos(source: str) -> int:
    """Return character offset of the OIDC late-init log message string in source.

    Locates 'OIDC late-initialized from PG config' -- the final log statement
    inside the OIDC late-init block.  The LLM late-init guard must appear
    AFTER this position.
    """
    marker = "OIDC late-initialized from PG config"
    pos = source.find(marker)
    assert pos != -1, f"OIDC late-init log marker not found in lifespan.py: {marker!r}"
    return pos


def _llm_late_init_tail(source: str) -> str:
    """Return source text starting from the 'llm_lifecycle_service is None' guard.

    Raises AssertionError with a clear message if the guard is absent.
    """
    guard = "llm_lifecycle_service is None"
    pos = source.find(guard)
    assert pos != -1, (
        f"'{guard}' guard not found in lifespan.py. "
        "The late-init block for cluster secondary nodes is absent."
    )
    return source[pos:]


class TestLlmLeaseLifecycleLateInitGuard:
    """Structural tests: late-init guard and its contents must be present in lifespan.py."""

    def test_llm_lifecycle_none_guard_exists(self):
        """lifespan.py must contain 'llm_lifecycle_service is None' guard."""
        _llm_late_init_tail(_lifespan_source())  # raises if absent

    def test_llm_late_init_checks_subscription_mode(self):
        """Late-init block must verify claude_auth_mode == 'subscription' from PG config."""
        tail = _llm_late_init_tail(_lifespan_source())
        assert "claude_auth_mode" in tail, (
            "Late-init block must reference claude_auth_mode. "
            "Not found after 'llm_lifecycle_service is None'."
        )
        assert '"subscription"' in tail, (
            "Late-init block must check for 'subscription' mode. "
            "Not found after 'llm_lifecycle_service is None'."
        )

    def test_llm_late_init_checks_provider_url_and_api_key(self):
        """Late-init block must check both provider URL and API key before proceeding."""
        tail = _llm_late_init_tail(_lifespan_source())
        assert "llm_creds_provider_url" in tail, (
            "Late-init block must reference llm_creds_provider_url. "
            "Not found after 'llm_lifecycle_service is None'."
        )
        assert "llm_creds_provider_api_key" in tail, (
            "Late-init block must reference llm_creds_provider_api_key. "
            "Not found after 'llm_lifecycle_service is None'."
        )

    def test_llm_late_init_creates_lifecycle_service(self):
        """Late-init block must instantiate LlmLeaseLifecycleService."""
        tail = _llm_late_init_tail(_lifespan_source())
        assert "LlmLeaseLifecycleService(" in tail, (
            "Late-init block must create LlmLeaseLifecycleService. "
            "Not found after 'llm_lifecycle_service is None'."
        )

    def test_llm_late_init_calls_start_with_consumer_id(self):
        """Late-init block must call .start() and pass llm_creds_provider_consumer_id."""
        tail = _llm_late_init_tail(_lifespan_source())
        assert ".start(" in tail, (
            "Late-init block must call .start() on the lifecycle service. "
            "Not found after 'llm_lifecycle_service is None'."
        )
        assert "llm_creds_provider_consumer_id" in tail, (
            "Late-init block must pass llm_creds_provider_consumer_id to .start(). "
            "Not found after 'llm_lifecycle_service is None'."
        )

    def test_llm_late_init_stores_in_app_state(self):
        """Late-init block must store the service in app.state.llm_lifecycle_service."""
        tail = _llm_late_init_tail(_lifespan_source())
        assert "app.state.llm_lifecycle_service" in tail, (
            "Late-init block must store service in app.state.llm_lifecycle_service. "
            "Not found after 'llm_lifecycle_service is None'."
        )

    def test_llm_late_init_logs_late_initialized_from_pg_config(self):
        """Late-init block must log a message containing 'late-initialized from PG config'."""
        tail = _llm_late_init_tail(_lifespan_source())
        assert "late-initialized from PG config" in tail, (
            "Late-init block must log 'late-initialized from PG config'. "
            "Not found after 'llm_lifecycle_service is None'."
        )

    def test_llm_late_init_calls_load_config_before_get_config(self):
        """Late-init block must call load_config() before get_config() to force PG reload."""
        tail = _llm_late_init_tail(_lifespan_source())
        load_pos = tail.find("load_config()")
        get_pos = tail.find("get_config()")
        assert load_pos != -1, (
            "Late-init block must call load_config() to force PG config reload. "
            "Not found after 'llm_lifecycle_service is None'."
        )
        assert load_pos < get_pos, (
            f"load_config() (pos {load_pos}) must appear BEFORE get_config() (pos {get_pos}) "
            "in the late-init block to ensure fresh PG config is loaded."
        )


class TestLlmLeaseLifecycleLateInitOrdering:
    """Source-order tests: late-init must appear at the correct position in lifespan.py."""

    def test_llm_lifecycle_none_guard_appears_after_oidc_late_init(self):
        """'llm_lifecycle_service is None' guard must appear AFTER the OIDC late-init log marker.

        The OIDC late-init log message is the final statement in the OIDC block.
        The LLM late-init must follow to ensure PG config is already loaded.
        """
        source = _lifespan_source()
        oidc_pos = _oidc_late_init_marker_pos(source)
        guard = "llm_lifecycle_service is None"
        guard_pos = source.find(guard)
        assert guard_pos != -1, f"'{guard}' not found in lifespan.py"
        assert guard_pos > oidc_pos, (
            f"'llm_lifecycle_service is None' guard (pos {guard_pos}) must appear "
            f"AFTER the OIDC late-init log message (pos {oidc_pos}). "
            f"Cluster nodes need PG config available before LLM lease late-init runs."
        )

    def test_late_init_appears_before_bug_587_block(self):
        """LLM late-init guard must appear BEFORE the Bug #587 ActivatedRepoManager block."""
        source = _lifespan_source()
        guard = "llm_lifecycle_service is None"
        guard_pos = source.find(guard)
        assert guard_pos != -1, f"'{guard}' not found in lifespan.py"
        bug587_pos = source.find("# Bug #587")
        assert bug587_pos != -1, "'# Bug #587' marker not found in lifespan.py"
        assert guard_pos < bug587_pos, (
            f"LLM late-init guard (pos {guard_pos}) must appear BEFORE "
            f"# Bug #587 block (pos {bug587_pos}). "
            f"Insert the block between OIDC late-init end and Bug #587."
        )

    def test_early_llm_init_block_header_still_exists(self):
        """The early LLM init block header must still exist -- late-init is additive."""
        source = _lifespan_source()
        early_marker = "# --- LLM Lease Lifecycle (subscription credential mode) ---"
        assert early_marker in source, (
            "Early LLM Lease Lifecycle init block header missing from lifespan.py. "
            "The late-init must be additive, not a replacement."
        )
