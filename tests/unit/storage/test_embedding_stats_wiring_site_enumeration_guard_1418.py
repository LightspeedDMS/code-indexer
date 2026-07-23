"""Story #1418: enumeration guard for `cidx index` child-subprocess launch
sites that must wire CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR.

Mirrors Bug #1313's test_temporal_pg_env_wiring_site_enumeration_guard_1313.py
approach (source-text scan) but with a DIFFERENT invariant: unlike temporal
(which only wires `--index-commits` launch sites, conditional on postgres
mode), embedding-stats wiring fires UNCONDITIONALLY for BOTH storage modes
and must cover every `cidx index` spawn that can trigger a real embedding-
provider HTTP call (semantic and/or temporal), not just temporal.

Investigation established that each of these 5 files funnels ALL of its
`cidx index` (semantic + temporal, and in golden_repo_manager.py's second
workflow also its fts-rebuild-only branch, harmlessly) child-subprocess
launches through exactly ONE (or, for golden_repo_manager.py, TWO --
independent post-clone and add-indexes workflows) shared per-function env-
resolution closure/call site. Wiring `build_embedding_stats_child_env`
ONCE per closure therefore covers every embedding-triggering spawn in that
file without needing to touch each individual command-construction call
site separately.
"""

from __future__ import annotations

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_SRC_ROOT = _REPO_ROOT / "src" / "code_indexer"

_WIRING_MARKER = "build_embedding_stats_child_env"

# The complete, deliberately-reviewed set of server-side production files
# that spawn a `cidx index` child subprocess capable of making a real
# embedding-provider HTTP call, together with the shared closure/function
# name each file funnels those spawns through. If you add a NEW site,
# reference build_embedding_stats_child_env at (or above) that same shared
# closure AND add it to this map.
_KNOWN_LAUNCH_SITES = {
    "server/repositories/golden_repo_manager.py": 2,  # _run_popen + _run_with_popen_progress
    "server/services/activated_repo_index_manager.py": 1,  # _run_subprocess_with_telemetry
    "global_repos/refresh_scheduler.py": 1,  # _run_popen_c
    "server/mcp/handlers/repos.py": 1,  # _run_provider_subprocess
    "server/services/claude_cli_manager.py": 1,  # _commit_and_reindex (direct call)
}


class TestKnownLaunchSitesReferenceWiringMarker:
    def test_each_known_site_references_build_embedding_stats_child_env(self):
        for rel_path, expected_min_count in _KNOWN_LAUNCH_SITES.items():
            source = (_SRC_ROOT / rel_path).read_text()
            actual_count = source.count(_WIRING_MARKER)
            assert actual_count >= expected_min_count, (
                f"{rel_path}: expected at least {expected_min_count} "
                f"reference(s) of {_WIRING_MARKER}, found {actual_count}. "
                f"This file spawns a `cidx index` child subprocess but is "
                f"not (fully) wired for embedding-stats bootstrap "
                f"(Story #1418)."
            )
