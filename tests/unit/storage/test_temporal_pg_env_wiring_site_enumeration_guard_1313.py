"""Bug #1313 round-4: enumeration guard for server-side temporal-index
subprocess launch sites.

Codex round-4 review found TWO previously-unwired launch sites beyond the
two fixed in round-3 (golden_repo_manager.py::add_indexes_to_golden_repo
and mcp/handlers/repos.py::_provider_temporal_index_job); a further
exhaustive sweep during this fix found a FIFTH
(activated_repo_index_manager.py::_execute_temporal_indexing). Each site
independently remembering to call build_temporal_child_env is a
human-diligence hope, not a guarantee -- this test makes "every
server-side `cidx index --index-commits` launch site is PG-bootstrap-aware"
a TEST-ENFORCED invariant: if a future 6th site is added without wiring,
this test fails immediately instead of silently reintroducing the
NFS-backed SQLite-WAL bottleneck.

Approach: source-text scan (mirrors the other Bug #1313 guard tests, e.g.
test_temporal_metadata_layering_guard_1313.py) rather than executing the
huge dependency graphs of every caller.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src" / "code_indexer"

# The complete, deliberately-reviewed set of server-side production files
# that construct a `cidx index --index-commits` command list and spawn it
# as a child subprocess (Popen or subprocess.run), together with the exact
# number of "--index-commits" list-literal occurrences expected in each
# (golden_repo_manager.py has TWO: _execute_post_clone_workflow and
# add_indexes_to_golden_repo). If you are adding a NEW site, you must BOTH
# wire it (call build_temporal_child_env and pass env= to the subprocess
# call) AND update this map -- updating the map alone without wiring will
# not make this test pass, because the second assertion below checks that
# build_temporal_child_env is referenced too.
_KNOWN_TEMPORAL_LAUNCH_SITES = {
    "server/repositories/golden_repo_manager.py": 2,
    "global_repos/refresh_scheduler.py": 1,
    "server/mcp/handlers/repos.py": 1,
    "server/services/activated_repo_index_manager.py": 1,
}

# Files that legitimately contain the "--index-commits" string literal but
# are NOT parent-side launch sites requiring build_temporal_child_env:
#   - cli.py: the CHILD entrypoint itself (reads CIDX_TEMPORAL_PG_BOOTSTRAP_DIR
#     via install_postgres_temporal_backend_from_bootstrap -- a DIFFERENT
#     function, the child-side counterpart to build_temporal_child_env).
#   - cli_daemon_fast.py: client-side flag PARSING only
#     (`"--index-commits" in args`) for daemon-delegation dispatch -- never
#     constructs or spawns a `["cidx", ..., "--index-commits", ...]` command
#     list itself.
#   - cli_fast_entry.py (Bug #1417): same client-side flag PARSING pattern as
#     cli_daemon_fast.py above -- `is_delegatable_command` checks
#     `"--index-commits" in args` to force temporal indexing onto the
#     standalone/full-CLI dispatch path (never daemon-delegated) so the
#     CIDX_TEMPORAL_PG_BOOTSTRAP_DIR fail-loud wiring in cli.py's standalone
#     branch is always exercised. Never constructs or spawns a
#     `["cidx", ..., "--index-commits", ...]` command list itself.
_NON_LAUNCH_SITE_EXCEPTIONS = frozenset(
    {"cli.py", "cli_daemon_fast.py", "cli_fast_entry.py"}
)

_LITERAL = '"--index-commits"'
_WIRING_MARKER = "build_temporal_child_env"


def _all_python_files_containing_literal() -> dict:
    """Return {relative_path: source_text} for every .py file under
    src/code_indexer/ containing the exact '--index-commits' string
    literal, excluding test fixtures."""
    found = {}
    for path in _SRC_ROOT.rglob("*.py"):
        text = path.read_text()
        if _LITERAL in text:
            rel = str(path.relative_to(_SRC_ROOT))
            found[rel] = text
    return found


class TestKnownLaunchSitesAreWired:
    def test_each_known_site_references_build_temporal_child_env(self):
        for rel_path, expected_count in _KNOWN_TEMPORAL_LAUNCH_SITES.items():
            source = (_SRC_ROOT / rel_path).read_text()
            actual_count = source.count(_LITERAL)
            assert actual_count == expected_count, (
                f"{rel_path}: expected {expected_count} occurrence(s) of "
                f"{_LITERAL}, found {actual_count}. If you added/removed a "
                f"temporal-index launch site in this file, update "
                f"_KNOWN_TEMPORAL_LAUNCH_SITES AND confirm it calls "
                f"{_WIRING_MARKER} (Bug #1313)."
            )
            assert _WIRING_MARKER in source, (
                f"{rel_path} constructs a '--index-commits' command but does "
                f"not reference {_WIRING_MARKER} -- this launch site is "
                f"NOT wired for cluster/postgres mode (Bug #1313). It will "
                f"silently fall back to the NFS-backed SQLite-WAL backend."
            )


class TestNoUnwiredSixthSite:
    def test_no_other_file_constructs_index_commits_without_wiring(self):
        """Fails loudly if a NEW file (not in the known set, not in the
        explicit non-launch-site exception list) contains the
        '--index-commits' literal without also referencing
        build_temporal_child_env in the same file."""
        found = _all_python_files_containing_literal()

        known_or_excepted = set(_KNOWN_TEMPORAL_LAUNCH_SITES) | {
            path for path in found if Path(path).name in _NON_LAUNCH_SITE_EXCEPTIONS
        }

        unexpected = set(found) - known_or_excepted
        assert not unexpected, (
            f"Found NEW file(s) constructing '--index-commits' that are "
            f"neither in the known-wired set nor the explicit "
            f"non-launch-site exceptions: {sorted(unexpected)}. Bug #1313: "
            f"any new server-side temporal-index launch site MUST call "
            f"build_temporal_child_env(get_config_service().get_config()) "
            f"and pass the result as env= to the subprocess call, then be "
            f"added to _KNOWN_TEMPORAL_LAUNCH_SITES in this test."
        )

        # Belt-and-suspenders: every file in the known set must ALSO show up
        # in the live scan (catches a known site being accidentally deleted
        # without updating this guard).
        missing = set(_KNOWN_TEMPORAL_LAUNCH_SITES) - set(found)
        assert not missing, (
            f"Known temporal launch site(s) no longer contain "
            f"'--index-commits': {sorted(missing)}. If removed "
            f"intentionally, update _KNOWN_TEMPORAL_LAUNCH_SITES."
        )
