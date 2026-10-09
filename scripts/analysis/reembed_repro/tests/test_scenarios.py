"""Self-tests for scenario presets and seeded random interrupt cycles."""

from argparse import Namespace

from child_runner import InterruptSpec
from scenarios import SCENARIOS, YIELD_PROBE, build_cycles, random_cycles


def test_random_cycles_are_seeded_and_parse():
    a = random_cycles(40, seed=3, kinds=("SIGTERM", "SIGKILL"), max_chunks=300)
    assert a == random_cycles(40, seed=3, kinds=("SIGTERM", "SIGKILL"), max_chunks=300)
    assert a != random_cycles(40, seed=4, kinds=("SIGTERM", "SIGKILL"), max_chunks=300)
    assert len(a) == 40
    specs = [InterruptSpec.parse(e) for e in a if e != "none"]
    assert {s.name for s in specs} == {"SIGTERM", "SIGKILL"}
    assert {s.trigger for s in specs} >= {"early", "planned", "chunks", "final"}
    assert all(1 <= s.value <= 300 for s in specs if s.trigger == "chunks")


def test_random_cycles_honour_the_kinds():
    entries = random_cycles(25, seed=1, kinds=("CANCEL",), max_chunks=50)
    assert {InterruptSpec.parse(e).name for e in entries if e != "none"} == {"CANCEL"}


def test_build_cycles_uses_explicit_list_or_random_plus_yield_probe():
    explicit = Namespace(cycles="SIGTERM@chunks:5,none", random_cycles=0)
    assert build_cycles(explicit) == ["SIGTERM@chunks:5", "none"]
    rnd = Namespace(
        cycles="ignored",
        random_cycles=20,
        cycle_seed=7,
        random_kinds="SIGTERM,SIGKILL",
        max_chunks_trigger=100,
    )
    entries = build_cycles(rnd)
    assert len(entries) == 21 and entries[-1] == YIELD_PROBE
    assert entries[:20] == random_cycles(20, 7, ("SIGTERM", "SIGKILL"), 100)


def test_random_presets_wait_past_the_incremental_safety_buffer_before_recovery():
    for name, preset in SCENARIOS.items():
        if preset.get("random_cycles"):
            assert preset["recovery_delay_seconds"] > 60, name
    assert SCENARIOS["default"]["recovery_delay_seconds"] == 0


def test_recovery_delay_option_takes_the_preset_and_explicit_values_win():
    from cli_args import parse_args

    assert parse_args(["--scenario", "dup"]).recovery_delay_seconds > 60
    explicit = parse_args(["--scenario", "dup", "--recovery-delay-seconds", "0"])
    assert explicit.recovery_delay_seconds == 0


def test_recovery_delay_must_be_finite_and_within_0_to_600():
    from cli_args import parse_args

    base = ["--scenario", "dup", "--recovery-delay-seconds"]
    for bad in ("-1", "600.5", "nan", "inf", "abc"):
        try:
            parse_args(base + [bad])
        except SystemExit:
            continue
        raise AssertionError(f"--recovery-delay-seconds {bad!r} was accepted")
    for good in ("0", "600"):
        assert parse_args(base + [good]).recovery_delay_seconds == float(good)


def test_presets_exercise_the_version_agnostic_checks():
    for name, preset in SCENARIOS.items():
        if preset.get("random_cycles"):
            assert preset["random_cycles"] >= 20, name
        else:
            assert preset["cycles"].split(",")[-1] == YIELD_PROBE, name
    assert SCENARIOS["soak"]["files"] == 199000
    assert SCENARIOS["cancel"]["random_kinds"] == "CANCEL"
    assert SCENARIOS["restart"]["random_kinds"] == "RESTART"
    assert SCENARIOS["git"]["git"] is True
    assert (
        SCENARIOS["dup"]["dup_groups"] > 0 and SCENARIOS["dup"]["sync_dup_fraction"] > 0
    )
