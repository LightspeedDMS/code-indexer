"""Bug #1461 (Epic #1454 Story #1461 salvage item #6 [MIN]): the Bug #642
NULL-temporal_options max_commits safety net must consult the golden-owned
sister location, not just the in-repo tree.

`_read_max_commits_from_temporal_meta()` (refresh_scheduler.py) only scans
the golden repo's OWN `.code-indexer/index/` tree for the legacy
`temporal_meta.json` artifact. Story #1457 can relocate a quarter shard's
data to the golden-owned sister location
(`{golden_repos_dir}/.versioned/{bare_alias}-temporal-{slug}[-{quarter}]/
v_*/`, published via an alias pointer under `{golden_repos_dir}/aliases/`),
and Story #1458's fleet-migration bootstrap can reclaim (delete) the
in-repo tree entirely once migrated. The new consolidated per-version build
never writes `temporal_meta.json` at all -- it writes `temporal_progress.
json` (`TemporalProgressiveMetadata`, `completed_commits` list) instead.
Once both of those happen, the old in-repo-only scan silently returns None
forever, and the Bug #642 fallback -- itself only reachable when
temporal_options is NULL -- goes dark: no `--max-commits` bound is ever
appended again for that repo.

Fix: when the in-repo scan misses, `_index_source` additionally consults
`get_temporal_repo_status()` (Story #1457/#1459's existing resolver-based
sister/in-repo union primitive, already reused by this file's Bug #1461
salvage item #1 fix, `_fixed_root_temporal_data_exists`) to find the best
resolved shard, then reads that shard's `temporal_progress.json` and uses
`len(completed_commits)` as the SAME "conservative upper bound" fallback
semantics the legacy `total_commits` field provided. Fails open (returns
None, no --max-commits appended -- identical to the pre-existing miss
behavior) on any error or absence, exactly like the in-repo scan it
supplements.

This test uses a REAL AliasManager + real marker/progress files for the
sister-location fixture (mirroring
test_refresh_scheduler_sister_temporal_reconcile_1461.py's recipe) and
calls the REAL `_index_source` method (only subprocess.run and
run_with_popen_progress are mocked, matching
test_refresh_scheduler_temporal_options_split_brain_1414.py's established
pattern).
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_server_paths import (
    server_temporal_index_root,
)


# ---------------------------------------------------------------------------
# Helpers (mirroring test_refresh_scheduler_temporal_options_split_brain_1414.py)
# ---------------------------------------------------------------------------


def _make_scheduler(registry, golden_meta, golden_repos_dir=None):
    """Build a lightweight RefreshScheduler with just enough state to call
    _index_source. golden_repos_dir is optional (None mirrors every
    pre-existing bare object.__new__() test instance in this suite that
    never set it -- the fallback must be a no-op guard, not a crash, in
    that case)."""
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler

    scheduler = object.__new__(RefreshScheduler)
    scheduler.registry = registry
    scheduler.golden_repo_metadata = golden_meta
    if golden_repos_dir is not None:
        scheduler.golden_repos_dir = Path(golden_repos_dir)
    return scheduler


def _make_registry(
    alias_name,
    enable_temporal=True,
    temporal_options=None,
    repo_url="git@github.com:org/repo.git",
    enable_scip=False,
):
    registry = MagicMock()
    repo_info = {
        "alias_name": alias_name,
        "repo_url": repo_url,
        "enable_temporal": enable_temporal,
        "temporal_options": temporal_options,
        "enable_scip": enable_scip,
    }
    registry.get_global_repo.return_value = repo_info
    return registry


def _make_golden_meta(temporal_options):
    golden_meta = MagicMock()
    golden_meta.get_repo.return_value = {"temporal_options": temporal_options}
    return golden_meta


def _capture_subprocess_cmds(scheduler, alias_name, source_path):
    captured = []

    def recording_run(cmd, **kwargs):
        captured.append(list(cmd))
        result = MagicMock()
        result.returncode = 0
        result.stdout = "ok"
        result.stderr = ""
        return result

    def recording_popen_progress(
        command,
        phase_name,
        allocator,
        progress_callback,
        all_stdout,
        all_stderr,
        cwd,
        error_label=None,
        env=None,
    ):
        captured.append(list(command))

    import code_indexer.services.progress_subprocess_runner as psr_mod

    with (
        patch("subprocess.run", side_effect=recording_run),
        patch.object(
            psr_mod, "run_with_popen_progress", side_effect=recording_popen_progress
        ),
    ):
        scheduler._index_source(alias_name, str(source_path))

    return captured


def _make_sister_temporal_fixture(
    golden_repos_dir: Path,
    bare_alias: str,
    *,
    embedder_slug: str = "voyage_code_3",
    quarter: str = "2024Q1",
    completed_commits=None,
) -> Path:
    """Build a real temporal shard at the FIXED server-owned root.

    Bug #1529: temporal data for a golden repo lives at
    ``{golden_repos_dir}/.temporal/{alias}/`` -- a fixed, deterministic path --
    rather than a ``.versioned/`` snapshot published behind an AliasManager
    pointer. The behavior under test is UNCHANGED: a real hnsw_index.bin
    marker (queryable) plus a real temporal_progress.json (the file the
    consolidated build actually writes -- never temporal_meta.json) living
    OUTSIDE the repo tree must still supply the max_commits bound.
    """
    shard_dir: Path = (
        server_temporal_index_root(golden_repos_dir, bare_alias)
        / f"code-indexer-temporal-{embedder_slug}-{quarter}"
    )
    shard_dir.mkdir(parents=True)
    (shard_dir / "hnsw_index.bin").write_bytes(b"fake-hnsw")
    # A real committed row: discovery keys off DATA presence, not merely the
    # presence of an index file (row-existence-is-not-queryability).
    (shard_dir / "vector_aaaa1111.json").write_text(
        json.dumps({"id": "proj:commit:aaaaaaaa:0"})
    )
    if completed_commits is not None:
        (shard_dir / "temporal_progress.json").write_text(
            json.dumps({"completed_commits": completed_commits})
        )
    return shard_dir


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestSisterTemporalMaxCommitsFallback:
    def test_sister_version_progress_supplies_max_commits_when_in_repo_meta_absent(
        self, tmp_path
    ):
        """In-repo temporal_meta.json is absent everywhere (relocated and
        reclaimed) and temporal_options is NULL (Bug #642 fallback path) --
        the sister version's temporal_progress.json completed_commits count
        must supply --max-commits."""
        alias = "my-repo-global"
        bare_alias = "my-repo"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        golden_meta = _make_golden_meta(None)

        golden_repos_dir = tmp_path / "golden-repos"
        source_path = golden_repos_dir / bare_alias
        (source_path / ".code-indexer" / "index").mkdir(parents=True)
        # In-repo temporal tree deliberately empty -- no temporal_meta.json
        # anywhere under source_path (the relocated-and-reclaimed scenario).

        commit_hashes = [f"c{i:03d}" for i in range(7)]
        _make_sister_temporal_fixture(
            golden_repos_dir, bare_alias, completed_commits=commit_hashes
        )

        scheduler = _make_scheduler(
            registry, golden_meta, golden_repos_dir=golden_repos_dir
        )

        cmds = _capture_subprocess_cmds(scheduler, alias, source_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd, (
            "Bug #1461: sister-location temporal_progress.json must supply "
            f"a max_commits fallback bound when the in-repo scan misses. "
            f"Got: {temporal_cmd}"
        )
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "7", (
            f"Expected max_commits=7 (len(completed_commits)) sourced from "
            f"the sister version's temporal_progress.json. Got: {temporal_cmd}"
        )

    def test_sister_data_present_but_no_progress_file_still_omits_flag(self, tmp_path):
        """Regression guard: a sister version that exists but carries no
        temporal_progress.json (e.g. built before this fallback existed)
        must not crash -- it must simply omit --max-commits, identical to
        the pre-existing miss behavior."""
        alias = "my-repo-global"
        bare_alias = "my-repo"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        golden_meta = _make_golden_meta(None)

        golden_repos_dir = tmp_path / "golden-repos"
        source_path = golden_repos_dir / bare_alias
        (source_path / ".code-indexer" / "index").mkdir(parents=True)

        _make_sister_temporal_fixture(
            golden_repos_dir, bare_alias, completed_commits=None
        )

        scheduler = _make_scheduler(
            registry, golden_meta, golden_repos_dir=golden_repos_dir
        )

        cmds = _capture_subprocess_cmds(scheduler, alias, source_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        assert "--max-commits" not in temporal_cmds[0], (
            f"No temporal_progress.json exists at the sister version -- "
            f"--max-commits must not be fabricated. Got: {temporal_cmds[0]}"
        )

    def test_no_golden_repos_dir_attribute_does_not_crash(self, tmp_path):
        """Regression guard: a bare-instantiated scheduler (no
        golden_repos_dir attribute at all -- mirrors every pre-existing
        object.__new__() test instance in
        test_refresh_scheduler_temporal_options_split_brain_1414.py) must
        not crash when the in-repo scan misses; it must just omit
        --max-commits, exactly like the pre-existing behavior before this
        fix."""
        alias = "my-repo-global"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        golden_meta = _make_golden_meta(None)
        scheduler = _make_scheduler(registry, golden_meta, golden_repos_dir=None)

        source_path = tmp_path
        (source_path / ".code-indexer" / "index").mkdir(parents=True)

        cmds = _capture_subprocess_cmds(scheduler, alias, source_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        assert "--max-commits" not in temporal_cmds[0], (
            f"No golden_repos_dir wired -- fallback must be a no-op, never "
            f"a crash. Got: {temporal_cmds[0]}"
        )

    def test_sister_multi_quarter_progress_sums_to_repo_wide_total(self, tmp_path):
        """#1461-6 review: the legacy temporal_meta.json.total_commits field
        this fallback replaces was REPO-WIDE, but reading only the single
        best-resolved quarter shard undercounts a repo with multiple
        quarters. The fix must union completed-commit hashes across EVERY
        resolved quarter of the embedder, not just one."""
        alias = "my-repo-global"
        bare_alias = "my-repo"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        golden_meta = _make_golden_meta(None)

        golden_repos_dir = tmp_path / "golden-repos"
        source_path = golden_repos_dir / bare_alias
        (source_path / ".code-indexer" / "index").mkdir(parents=True)

        q1_commits = [f"q1-c{i:03d}" for i in range(5)]
        q2_commits = [f"q2-c{i:03d}" for i in range(3)]
        _make_sister_temporal_fixture(
            golden_repos_dir,
            bare_alias,
            quarter="2024Q1",
            completed_commits=q1_commits,
        )
        _make_sister_temporal_fixture(
            golden_repos_dir,
            bare_alias,
            quarter="2024Q2",
            completed_commits=q2_commits,
        )

        scheduler = _make_scheduler(
            registry, golden_meta, golden_repos_dir=golden_repos_dir
        )

        cmds = _capture_subprocess_cmds(scheduler, alias, source_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        temporal_cmd = temporal_cmds[0]
        assert "--max-commits" in temporal_cmd, (
            f"Multi-quarter sister data must still supply a max_commits "
            f"bound. Got: {temporal_cmd}"
        )
        idx = temporal_cmd.index("--max-commits")
        assert temporal_cmd[idx + 1] == "8", (
            f"Expected the repo-wide total (5 + 3 = 8) summed/unioned "
            f"across BOTH quarters, not just one quarter's count. "
            f"Got: {temporal_cmd}"
        )

    def test_sister_multi_quarter_missing_progress_on_one_quarter_omits_flag(
        self, tmp_path
    ):
        """#1461-6 review: if one resolved quarter has real data but no
        temporal_progress.json, the repo-wide total cannot be reliably
        computed -- the flag must be OMITTED entirely (fail-open to
        unbounded) rather than emit a total that silently excludes that
        quarter's commits (an under-cap, worse than no bound at all)."""
        alias = "my-repo-global"
        bare_alias = "my-repo"
        registry = _make_registry(alias, enable_temporal=True, temporal_options=None)
        golden_meta = _make_golden_meta(None)

        golden_repos_dir = tmp_path / "golden-repos"
        source_path = golden_repos_dir / bare_alias
        (source_path / ".code-indexer" / "index").mkdir(parents=True)

        q1_commits = [f"q1-c{i:03d}" for i in range(5)]
        _make_sister_temporal_fixture(
            golden_repos_dir,
            bare_alias,
            quarter="2024Q1",
            completed_commits=q1_commits,
        )
        # Second quarter has real data (hnsw_index.bin) but NO
        # temporal_progress.json -- an unreliable-total scenario.
        _make_sister_temporal_fixture(
            golden_repos_dir,
            bare_alias,
            quarter="2024Q2",
            completed_commits=None,
        )

        scheduler = _make_scheduler(
            registry, golden_meta, golden_repos_dir=golden_repos_dir
        )

        cmds = _capture_subprocess_cmds(scheduler, alias, source_path)

        temporal_cmds = [c for c in cmds if "--index-commits" in c]
        assert temporal_cmds, f"No temporal command issued. Commands: {cmds}"
        assert "--max-commits" not in temporal_cmds[0], (
            f"One quarter's total is unknowable -- must omit the flag "
            f"rather than under-cap. Got: {temporal_cmds[0]}"
        )
