"""Self-tests for the runner's helpers."""

import re
import subprocess

import pytest

from run_repro import REPO_ROOT, archive_tree, node_for_run, parse_node_sequence


def _committed_version() -> str:
    text = subprocess.run(
        ["git", "-C", str(REPO_ROOT), "show", "HEAD:src/code_indexer/__init__.py"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    match = re.search(r'__version__ = "([^"]+)"', text)
    assert match is not None
    return match.group(1)


def test_archive_tree_extracts_a_ref_once(tmp_path):
    src = archive_tree("HEAD", tmp_path)
    init = src / "code_indexer" / "__init__.py"
    assert f'__version__ = "{_committed_version()}"' in init.read_text()
    marker_mtime = init.stat().st_mtime_ns
    assert archive_tree("HEAD", tmp_path) == src
    assert init.stat().st_mtime_ns == marker_mtime


def test_archive_tree_is_safe_for_concurrent_runs(tmp_path):
    from concurrent.futures import ThreadPoolExecutor

    with ThreadPoolExecutor(max_workers=4) as pool:
        trees = list(pool.map(lambda _: archive_tree("HEAD", tmp_path), range(4)))
    assert len(set(trees)) == 1
    src = trees[0]
    assert (src.parent / ".extracted").exists()
    assert (src / "code_indexer" / "__init__.py").exists()
    assert [p.name for p in tmp_path.iterdir()] == [src.parent.name]


def test_archive_tree_replaces_a_stale_partial_extraction(tmp_path):
    first = archive_tree("HEAD", tmp_path)
    (first.parent / ".extracted").unlink()
    (first / "code_indexer" / "__init__.py").unlink()
    again = archive_tree("HEAD", tmp_path)
    assert again == first
    assert (again / "code_indexer" / "__init__.py").exists()
    assert (again.parent / ".extracted").exists()


def test_archive_tree_quarantines_a_stale_target_instead_of_deleting_it_first(tmp_path):
    import os
    import stat

    first = archive_tree("HEAD", tmp_path)
    target = first.parent
    (target / ".extracted").unlink()  # stale: no marker
    locked = target / "locked"
    locked.mkdir()
    (locked / "keep").write_text("x")
    locked.chmod(stat.S_IRUSR | stat.S_IXUSR)  # contents cannot be deleted
    try:
        again = archive_tree("HEAD", tmp_path)
        assert again == first
        assert (target / ".extracted").exists()
        assert (again / "code_indexer" / "__init__.py").exists()
        assert not (target / "locked").exists()  # the stale tree was moved aside
    finally:
        for root, dirs, _files in os.walk(tmp_path):
            for name in dirs:
                os.chmod(os.path.join(root, name), stat.S_IRWXU)


def test_archive_tree_rejects_unknown_ref(tmp_path):
    with pytest.raises(RuntimeError, match="no-such-ref"):
        archive_tree("no-such-ref", tmp_path)


def test_sequence_entries_apply_in_order_and_the_last_repeats():
    seq = parse_node_sequence("0,0,1")
    assert [node_for_run(i, seq, rotate=False) for i in range(5)] == [0, 0, 1, 1, 1]


def test_rotate_gives_every_run_its_own_node():
    assert [node_for_run(i, [], rotate=True) for i in range(3)] == [0, 1, 2]


def test_default_is_a_single_node():
    assert [node_for_run(i, [], rotate=False) for i in range(3)] == [0, 0, 0]


def test_scenario_presets_apply_and_explicit_options_win():
    from run_repro import parse_args

    git = parse_args(["--scenario", "git", "--files", "3000"])
    assert (git.git, git.files, git.random_cycles, git.node_sequence) == (
        True,
        3000,
        20,
        [0],
    )
    assert git.initial_src is None and git.old_src_runs == 0
    dup = parse_args(["--scenario", "dup", "--dup-groups", "7"])
    assert (dup.dup_groups, dup.sync_dup_fraction) == (7, 0.3)
    assert parse_args(["--scenario", "soak"]).files == 199000


def test_default_cycles_end_with_the_yield_probe():
    from run_repro import parse_args
    from scenarios import YIELD_PROBE

    args = parse_args(["--old-ref", "none"])
    assert args.cycles.split(",")[-1] == YIELD_PROBE and args.random_cycles == 0


def test_files_all_processed_reads_the_progress_tail(tmp_path):
    import json

    from run_repro import files_all_processed

    out = tmp_path / "run.out"
    assert files_all_processed(out) is False
    out.write_text(json.dumps({"info": "3/5 files (60%) | x"}) + "\n")
    assert files_all_processed(out) is False
    with open(out, "a") as handle:
        handle.write(
            "x" * 20000 + "\n" + json.dumps({"info": "5/5 files (100%) | x"}) + "\n"
        )
    assert files_all_processed(out) is True
    out.write_text(json.dumps({"info": "0/0 files"}) + "\n")
    assert files_all_processed(out) is False


def test_durability_facts_classify_in_flight_and_held_keys():
    from run_repro import durability_facts

    facts = durability_facts(
        sent_counts={"stored": 1, "pending": 1, "lost": 2, "old": 1},
        durable_before={"old"},
        durable_after={"stored", "pending", "old"},
        interrupted=True,
        held_keys={"pending", "lost"},
        pending_after={"pending"},
    )
    assert facts["sent_durable_before"] == frozenset({"old"})
    assert facts["inflight_keys"] == frozenset({"lost"})
    assert facts["held_keys"] == frozenset({"pending", "lost"})
    assert facts["held_in_pending_after"] == frozenset({"pending"})
    calm = durability_facts(
        {"a": 1}, set(), set(), interrupted=False, held_keys=set(), pending_after=set()
    )
    assert calm["inflight_keys"] == frozenset()


@pytest.mark.parametrize("bad", ["../x", "/abs", "a/b", ".", "..", "", "a b"])
def test_name_must_be_one_safe_path_component(bad):
    from run_repro import parse_args

    with pytest.raises(SystemExit):
        parse_args(["--old-ref", "none", "--name", bad])


def test_safe_name_is_accepted():
    from run_repro import parse_args

    assert (
        parse_args(["--old-ref", "none", "--name", "s0-dup.v2_1"]).name == "s0-dup.v2_1"
    )


def test_work_dir_must_resolve_directly_under_the_scratch_root(tmp_path):
    from cli_args import work_dir_for

    root = tmp_path / "scratch"
    root.mkdir()
    assert work_dir_for("run-1", root) == root / "run-1"
    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (root / "escape").symlink_to(outside)
    with pytest.raises(RuntimeError, match="outside"):
        work_dir_for("escape", root)


def test_split_handshake_extracts_only_the_handshake_option():
    from run_repro import split_handshake

    assert split_handshake(["--files", "5"]) == (None, ["--files", "5"])
    assert split_handshake(["--sandbox-handshake", "7:ab", "--files", "5"]) == (
        "7:ab",
        ["--files", "5"],
    )


def test_main_refuses_a_forged_handshake_even_with_the_old_env_marker(monkeypatch):
    from run_repro import main

    monkeypatch.setenv("REEMBED_SANDBOXED", "1")
    argv = ["--sandbox-handshake", "999:" + "0" * 64, "--old-ref", "none"]
    assert main(argv + ["--name", "never-created"]) == 2


def test_inner_main_refuses_a_namespace_that_is_not_isolated():
    from run_repro import inner_main, parse_args

    args = parse_args(["--old-ref", "none", "--name", "never-created"])
    assert inner_main(args) == 2  # this test runs on the host network namespace


def test_unindexed_counts_new_paths_and_stale_stored_paths():
    from run_facts import unindexed_at_start

    disk = {"a.md", "b.md", "c.md", "new.md", "dup-copy.md"}
    stored = {"a.md", "b.md", "c.md"}
    # b.md was edited (its stored content is stale); dup-copy.md is a new path
    # whose content is stored under another path (still allowed to be planned).
    assert unindexed_at_start(disk, stored, stale={"b.md"}) == 3


def _two_runs(recovery_planned, unindexed, **recovery):
    from invariant import FinalState, RunRecord

    initial = RunRecord(
        "initial",
        "initial",
        False,
        0,
        3,
        3,
        3,
        owed_keys=3,
        sent_counts={"a": 1, "b": 1, "c": 1},
    )
    rec = RunRecord(
        "cycle-1", "recovery", False, 0, 0, recovery_planned, unindexed, **recovery
    )
    final = FinalState(status="completed", content_missing=0, content_files=3)
    return [initial, rec], final


def test_recovery_after_an_edit_planning_new_plus_edited_files_passes_r_a():
    from invariant import evaluate
    from run_facts import unindexed_at_start

    unindexed = unindexed_at_start({"a", "b", "c", "new"}, {"a", "b", "c"}, {"b"})
    runs, final = _two_runs(2, unindexed, owed_keys=0)  # planned: new + edited b
    assert evaluate(runs, final).check("R-a").status == "PASS"
    runs, final = _two_runs(3, unindexed, owed_keys=0)  # one more than owed
    assert evaluate(runs, final).check("R-a").status == "FAIL"


def test_a_key_lost_at_an_earlier_kill_is_owed_again_and_passes_r_a_content():
    from dataclasses import replace

    from invariant import evaluate
    from run_facts import content_bound

    disk = {"a", "e"}
    # Run 1 sends a and e and is killed; only a became durable.
    owed1, not_owed1 = content_bound({"a": 1, "e": 1}, disk, durable_before=set())
    # Run 2 starts with a durable, so e (lost in flight) is owed again.
    owed2, not_owed2 = content_bound({"e": 1}, disk, durable_before={"a"})
    assert (owed1, not_owed1, owed2, not_owed2) == (2, frozenset(), 1, frozenset())
    runs, final = _two_runs(
        1, 1, owed_keys=owed2, sent_not_owed=not_owed2, sent_counts={"e": 1}
    )
    runs[1] = replace(runs[1], inputs_embedded=1)
    runs[0] = replace(
        runs[0],
        interrupted=True,
        exit_code=-9,
        owed_keys=owed1,
        sent_counts={"a": 1, "e": 1},
        inputs_embedded=2,
    )
    assert evaluate(runs, final).check("R-a content").status == "PASS"


def test_trusted_exit_accepts_a_verdict_only_when_the_report_confirms_it(tmp_path):
    import json

    from run_repro import trusted_exit

    report = tmp_path / "report.json"
    assert trusted_exit(1, report) == 2  # crash with exit 1 and no report
    report.write_text("{not json")
    assert trusted_exit(1, report) == 2
    for code in (0, 1, 3):
        report.write_text(json.dumps({"verdict": {"exit_code": code}}))
        assert trusted_exit(code, report) == code
    report.write_text(json.dumps({"verdict": {"exit_code": 0}}))
    assert trusted_exit(1, report) == 2  # disagreement
    assert trusted_exit(2, report) == 2
    assert trusted_exit(-9, report) == 2


def test_import_location_check_is_path_aware(tmp_path):
    from run_facts import imports_from

    src = tmp_path / "src"
    sibling = tmp_path / "src2"
    for d in (src / "code_indexer", sibling / "code_indexer"):
        d.mkdir(parents=True)
        (d / "__init__.py").write_text("")
    assert imports_from(str(src / "code_indexer" / "__init__.py"), src) is True
    # A plain string-prefix match would wrongly accept the sibling tree.
    assert imports_from(str(sibling / "code_indexer" / "__init__.py"), src) is False
    assert imports_from(str(tmp_path / "elsewhere.py"), src) is False


def test_content_bound_owes_only_disk_keys_not_durable():
    from run_facts import content_bound

    owed, not_owed = content_bound(
        sent_counts={"new": 3, "stored": 1, "gone": 1},
        disk_keys={"new", "stored", "other"},
        durable_before={"stored"},
    )
    assert owed == 2  # "new" and "other" are on disk and not durable
    assert not_owed == frozenset({"stored", "gone"})


@pytest.mark.parametrize("bad", ["-1", "a", "0,,1"])
def test_parse_node_sequence_rejects_bad_entries(bad):
    with pytest.raises(ValueError):
        parse_node_sequence(bad)
