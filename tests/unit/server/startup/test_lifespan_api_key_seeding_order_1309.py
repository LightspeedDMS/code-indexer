"""Regression guard: API key seeding must run AFTER the ConfigService PG pool switch,
and BEFORE the LLM lease lifecycle starts.

Bug #1309: In server cluster mode (storage_mode=postgres), seed_api_keys_on_startup()
was called during lifespan startup BEFORE ConfigService switched from the empty
local-SQLite config to the shared PostgreSQL config pool ("APP-GENERAL-052: ConfigService
PG pool set early" block). It therefore read the empty local-SQLite config, found no
VoyageAI/Cohere key, and never seeded VOYAGE_API_KEY / CO_API_KEY into os.environ.
Consequence: on every freshly-installed cluster node, all indexing subprocesses failed
with "VOYAGE_API_KEY environment variable is required".

Fix (round 1): relocate the seed_api_keys_on_startup(config_service) call (and its
surrounding try/except + logging block) to run AFTER the early PG pool switch block,
so that in postgres/cluster mode ConfigService already returns the PG-backed config
(with the keys) when seeding reads it. The call remains UNCONDITIONAL (not
postgres-gated) so solo/SQLite mode -- where the PG block is skipped entirely -- is
unaffected.

Regression found in code review (round 2): moving seeding after the PG pool switch
placed it AFTER `LlmLeaseLifecycleService.start()` in subscription mode. That service
sets `os.environ["ANTHROPIC_API_KEY"]` from its leased credential. But
seed_api_keys_on_startup(), when config's `anthropic_api_key` is blank (the normal
case in subscription mode -- Anthropic auth is lease-managed, not config-managed),
unconditionally pops `ANTHROPIC_API_KEY` from os.environ (api_key_seeding.py:~67).
Net effect: seeding-after-lease clobbered the lease-managed Anthropic key immediately
after the lease set it.

Fix (round 2): move the ENTIRE ordering block (PG pool switch + seeding) to run
BEFORE the "--- LLM Lease Lifecycle ---" block, so the final order is:
[ConfigService PG pool switch] -> [seed_api_keys_on_startup] -> [LlmLeaseLifecycleService.start()].
This satisfies both constraints: seeding reads the PG-backed config (VoyageAI/Cohere
keys seed correctly), and the lease -- which runs last -- has its ANTHROPIC_API_KEY
write survive uncontested.

Tests (source-order checks, following the established pattern from
test_lifespan_config_service_early_pool_bug.py / test_lifespan_clone_backend_wiring_bug1044.py):
  1. Seeding call still present in lifespan.py source.
  2. Seeding call appears exactly once (no leftover duplicate from the relocation).
  3. Seeding call appears AFTER the early ConfigService PG pool switch marker.
  4. Seeding call is UNCONDITIONAL -- it sits at the same top-level indentation as the
     postgres guard's `if` line, i.e. it is NOT nested inside
     `if storage_mode == "postgres" and backend_registry is not None:`.
  5. Seeding call appears BEFORE the LLM lease lifecycle `.start()` call, so a
     lease-managed ANTHROPIC_API_KEY set is never clobbered by seeding's unconditional
     pop of a blank-config Anthropic key.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

# The API key seeding call we require to be relocated. It calls get_config_service()
# fresh (rather than reusing a pre-switch local variable) so it unambiguously reads
# the same singleton instance that just had its PG connection pool set.
_SEEDING_CALL_MARKER = "seeding_result = seed_api_keys_on_startup(get_config_service())"

# The early PG pool switch call (Bug #1309 fix must come after this)
_EARLY_POOL_MARKER = "get_config_service().set_connection_pool(_early_config_pool)"

# The postgres guard that wraps the early pool switch -- seeding must NOT be nested in it
_POSTGRES_GUARD_LINE = 'if storage_mode == "postgres" and backend_registry is not None:'

# The LLM lease lifecycle start call -- seeding must come BEFORE this (round-2 regression)
_LLM_LEASE_START_MARKER = "llm_lifecycle_service.start("


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


def _line_indent(source: str, marker: str) -> int:
    """Return the leading-whitespace width of the line containing `marker`."""
    pos = source.find(marker)
    assert pos != -1, f"Marker not found in source: {marker!r}"
    line_start = source.rfind("\n", 0, pos) + 1
    line = source[line_start:pos]
    return len(line) - len(line.lstrip(" "))


def _nearest_preceding_try_indent(source: str, marker_pos: int) -> int:
    """Find the indentation of the nearest `try:` statement enclosing `marker_pos`.

    Scans backward from marker_pos for the last line that is exactly `try:`
    (ignoring leading whitespace) -- this is the try-block that wraps the
    seeding call. Its indentation reveals whether the whole seeding block sits
    at the function's top level or is nested inside another conditional.
    """
    prefix = source[:marker_pos]
    lines = prefix.splitlines()
    for line in reversed(lines):
        stripped = line.strip()
        if stripped == "try:":
            return len(line) - len(line.lstrip(" "))
    raise AssertionError(
        f"No enclosing 'try:' statement found before position {marker_pos}"
    )


class TestApiKeySeedingSourceOrderBug1309:
    def test_seeding_call_present_in_lifespan_source(self):
        source = _source()
        assert _SEEDING_CALL_MARKER in source, (
            f"lifespan.py must still contain the API key seeding call.\n"
            f"Expected to find: {_SEEDING_CALL_MARKER!r}"
        )

    def test_seeding_call_appears_exactly_once(self):
        source = _source()
        occurrences = source.count(_SEEDING_CALL_MARKER)
        assert occurrences == 1, (
            f"Expected exactly 1 occurrence of the seeding call marker, found "
            f"{occurrences}. Relocation must MOVE the call, not duplicate it."
        )

    def test_seeding_call_occurs_after_early_config_pool_switch(self):
        """Bug #1309 fix: seeding must read config AFTER the PG pool is set.

        Fails before the fix (seeding at ~line 998, early pool switch at ~line 1153 --
        seeding comes first); passes after the fix (seeding relocated after the switch).
        """
        source = _source()

        seeding_pos = source.find(_SEEDING_CALL_MARKER)
        assert seeding_pos != -1, f"Marker not found: {_SEEDING_CALL_MARKER!r}"

        early_pool_pos = source.find(_EARLY_POOL_MARKER)
        assert early_pool_pos != -1, f"Marker not found: {_EARLY_POOL_MARKER!r}"

        assert seeding_pos > early_pool_pos, (
            f"Source-order violation (Bug #1309): API key seeding call (pos "
            f"{seeding_pos}) appears BEFORE the early ConfigService PG pool switch "
            f"(pos {early_pool_pos}).\n"
            "In postgres/cluster mode this means seeding reads the empty local-SQLite "
            "config instead of the PG-backed config, so VOYAGE_API_KEY/CO_API_KEY are "
            "never set. Move the seeding call (and its try/except block) to AFTER the "
            "'APP-GENERAL-052: ConfigService PG pool set early' block."
        )

    def test_seeding_call_is_unconditional_not_nested_in_postgres_guard(self):
        """Seeding must run in BOTH solo and cluster mode -- it must not be nested
        inside the `if storage_mode == "postgres" and backend_registry is not None:`
        guard that wraps the early pool switch. We verify this via indentation: the
        `try:` statement enclosing the seeding call must be at the SAME indentation
        level as the guard's `if` line itself (both top-level statements in the
        lifespan function body), not deeper (which would mean the seeding try-block
        is nested inside the guard).
        """
        source = _source()

        guard_pos = source.find(_POSTGRES_GUARD_LINE)
        assert guard_pos != -1, f"Marker not found: {_POSTGRES_GUARD_LINE!r}"

        seeding_pos = source.find(_SEEDING_CALL_MARKER)
        assert seeding_pos != -1, f"Marker not found: {_SEEDING_CALL_MARKER!r}"

        guard_indent = _line_indent(source, _POSTGRES_GUARD_LINE)
        seeding_try_indent = _nearest_preceding_try_indent(source, seeding_pos)

        assert seeding_try_indent == guard_indent, (
            f"API key seeding call must be unconditional (its enclosing `try:` at "
            f"the same indentation as the postgres guard's `if` line, "
            f"indent={guard_indent}), but found the enclosing try-block at "
            f"indent={seeding_try_indent} -- it appears to be nested inside a "
            "conditional block. Seeding must run in solo mode too."
        )

    def test_seeding_call_occurs_before_llm_lease_lifecycle_start(self):
        """Round-2 regression guard: seeding must run BEFORE the LLM lease starts.

        `LlmLeaseLifecycleService.start()` sets os.environ["ANTHROPIC_API_KEY"] from
        its leased credential (subscription mode). seed_api_keys_on_startup(), when
        config's anthropic_api_key is blank (the normal case in subscription mode --
        Anthropic auth is lease-managed, not config-managed), unconditionally pops
        ANTHROPIC_API_KEY from os.environ. If seeding runs AFTER the lease start, it
        clobbers the lease-managed key immediately after the lease sets it.

        Fails while seeding is positioned after llm_lifecycle_service.start() (the
        round-2 regression introduced by the round-1 relocation); passes once the
        entire PG-pool-switch + seeding block is moved before the LLM lease block.
        """
        source = _source()

        seeding_pos = source.find(_SEEDING_CALL_MARKER)
        assert seeding_pos != -1, f"Marker not found: {_SEEDING_CALL_MARKER!r}"

        lease_start_pos = source.find(_LLM_LEASE_START_MARKER)
        assert lease_start_pos != -1, f"Marker not found: {_LLM_LEASE_START_MARKER!r}"

        assert seeding_pos < lease_start_pos, (
            f"Source-order violation (Bug #1309 round 2): API key seeding call (pos "
            f"{seeding_pos}) appears AFTER the LLM lease lifecycle start (pos "
            f"{lease_start_pos}).\n"
            "In subscription mode, LlmLeaseLifecycleService.start() sets "
            "ANTHROPIC_API_KEY from the leased credential. Seeding running afterward "
            "unconditionally pops ANTHROPIC_API_KEY when config's key is blank, "
            "clobbering the lease-managed key. Move the PG-pool-switch + seeding "
            "block to run BEFORE the '--- LLM Lease Lifecycle ---' block."
        )

    def test_stale_api_key_seeding_comment_removed_from_claude_cli_init(self):
        """The ClaudeCliManager init comment claiming config 'may have been updated
        by API key seeding' is factually wrong: seeding mutates os.environ, never
        the config object, and it no longer runs anywhere near this code (it now
        runs much earlier, before the LLM lease block). The stale comment must be
        removed or corrected so it stops misleading future maintainers.
        """
        source = _source()
        stale_comment = "# Get fresh config (may have been updated by API key seeding)"
        assert stale_comment not in source, (
            f"Stale/incorrect comment still present: {stale_comment!r}\n"
            "API key seeding mutates os.environ, not the config object, and now "
            "runs before the LLM lease block -- this comment must be corrected "
            "or removed."
        )

    def test_early_pool_none_case_logs_loud_warning(self):
        """MEDIUM finding: if storage_mode == postgres but backend_registry has no
        connection pool available, the early pool switch must log LOUDLY (not
        silently skip) -- otherwise seeding below silently reads pre-switch
        (bootstrap SQLite) config with no indication why.

        Asserts on the distinctive warning MESSAGE text (not just the
        "APP-GENERAL-054" error code) because that code is also legitimately
        logged elsewhere in this file (~line 1595) and in branch_service.py --
        a bare code-substring check would still pass if this specific
        no-pool-available warning were deleted from the early pool-switch
        block.
        """
        source = _source()
        no_pool_warning_message = "storage_mode=postgres but no connection pool is "
        assert no_pool_warning_message in source, (
            "Expected a loud WARNING log (APP-GENERAL-054) with the message "
            f"{no_pool_warning_message!r} for the case where "
            "storage_mode == 'postgres' but _early_config_pool is None -- this "
            "failure mode must never be silent."
        )

    def test_seeding_success_log_includes_cohere_seeded(self):
        """LOW finding: the seeding-completed success log condition must include
        cohere_seeded, otherwise a Cohere-only seed wrongly logs
        'No keys needed seeding'.
        """
        source = _source()
        assert 'seeding_result["cohere_seeded"]' in source, (
            "The API key auto-seeding completion condition must check "
            'seeding_result["cohere_seeded"] in addition to anthropic_seeded and '
            "voyageai_seeded, or a cohere-only seed is misreported as a no-op."
        )
