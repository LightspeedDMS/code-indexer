"""Every test of a fleet-concurrency invariant must actually run in CI.

Both CI jobs are curated ALLOWLISTS -- ``smoke`` names files, and
``build:image``'s in-image step runs ``tests/unit/xray/`` plus six MCP files.
Anything not named is never *collected*, so unlike a gated test it does not even
appear as a skip. There is no number anywhere that goes down when coverage is
lost, which is why it went unnoticed: the background-work audit's five changes
all merged on green pipelines with none of their tests ever collected.

Adding the files to the list fixes the instance. This fixes the class, the only
way a list can be kept honest: **derive the requirement instead of maintaining
it**. A test that imports one of the fleet-invariant seams below is, by that
fact, a test CI must run -- nobody has to remember to add it here.

Deliberately narrow. This is not "all tests must run in CI" (smoke would take an
hour); it is "the handful of seams where being wrong costs a fleet-wide
incident, and where the failure is silent rather than loud".
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Set

import pytest

_REPO = Path(__file__).resolve().parents[3]
_CI = _REPO / ".gitlab-ci.yml"
_TESTS = _REPO / "tests" / "unit"

# Importing any of these means the test covers a mechanism that decides WHERE
# background work runs across the fleet. Each one, wrong, is a multi-pod
# incident: duplicated destructive sweeps, N-times-oversubscribed pools, or work
# that silently runs nowhere.
_FLEET_INVARIANT_SEAMS = (
    "leader_gate",  # M1: who may run fleet-wide work
    "worker_budget",  # R4: node budget vs per-process budget
    "POD_PULL_OPS",  # M2: which ops are work-stealable
    "DistributedJobClaimer",  # M2: the claim itself
    "register_job_if_no_conflict",  # M4: the race-to-claim single-flight
)

# Jobs whose pytest invocation counts as "CI runs this".
_SMOKE_JOB = "smoke"
_IMAGE_JOB = "build:image"
# Directory arguments: a file under one of these is covered by that directory
# token appearing as a pytest argument. Only build:image passes one today.
_DIRECTORY_ARG_SUFFIX = "/"
# pytest options whose VALUE is a test path but means the opposite of "run it".
_NEGATING_OPTIONS = ("--deselect", "--ignore")


def _pytest_test_tokens(job: str) -> Set[str]:
    """Test-path arguments actually passed to pytest by `job`.

    Parsed from the YAML and shlex-split, NOT substring-matched against the raw
    file. The first version of this did ``rel in ci_file_text`` and review showed
    it failing open in precisely the way this module's docstring forbids: the
    commit that added the guard also added a .gitlab-ci.yml COMMENT naming the
    guard's own path, so the guard's membership check passed on that comment.
    Removing it from the pytest args would have stopped it running in CI with
    nothing reporting the loss -- a guard that cannot notice its own removal.

    Values of --deselect/--ignore are dropped: they are test paths that mean the
    opposite of "run this".
    """
    import shlex

    import yaml

    config = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    entries = (config.get(job) or {}).get("script") or []
    tokens: Set[str] = set()
    for entry in entries:
        if "pytest" not in entry:
            continue
        parts = shlex.split(entry)
        skip_next = False
        for part in parts:
            if skip_next:
                skip_next = False
                continue
            if part in _NEGATING_OPTIONS:
                skip_next = True
                continue
            if part.startswith("--") and "=" in part:
                opt = part.split("=", 1)[0]
                if opt in _NEGATING_OPTIONS:
                    continue
            if part.startswith("tests/"):
                tokens.add(part)
    return tokens


def _covered_tokens() -> Set[str]:
    return _pytest_test_tokens(_SMOKE_JOB) | _pytest_test_tokens(_IMAGE_JOB)


def _named_in_ci(rel: str) -> bool:
    """Whether some CI job passes `rel` (or a directory containing it) to pytest.

    NOTE on directory coverage: the only directory argument today is
    ``tests/unit/xray/`` in ``build:image``, and that job runs on master/web
    ONLY -- not on merge-request pipelines. So a file covered solely by that
    directory is not checked before merge. That is weaker than it looks, which
    is why the one derived file in that position is ALSO named in ``smoke``
    rather than left to the directory. Accepting directory coverage here keeps
    the rule honest if a future job runs a whole tree pre-merge.
    """
    tokens = _covered_tokens()
    if rel in tokens:
        return True
    return any(t.endswith(_DIRECTORY_ARG_SUFFIX) and rel.startswith(t) for t in tokens)


def _tests_touching_invariants() -> Set[str]:
    found = set()
    for path in sorted(_TESTS.rglob("test_*.py")):
        try:
            body = path.read_text(encoding="utf-8")
        except OSError:  # pragma: no cover -- unreadable file is not our business
            continue
        # Match the seam as a whole word so a passing mention in prose does not
        # drag an unrelated test into the required set.
        if any(re.search(rf"\b{re.escape(s)}\b", body) for s in _FLEET_INVARIANT_SEAMS):
            found.add(str(path.relative_to(_REPO)))
    return found


def test_the_ci_file_is_where_we_think_it_is() -> None:
    """Fail loudly rather than vacuously if the layout moves.

    Every assertion below asks "is this path a pytest argument". If the file
    were missing, unparseable, or the job renamed, the answer would be "no" for
    everything and the suite would fail loudly -- but a scan that returned an
    EMPTY required set would pass by doing nothing, which is the shape this
    suite exists to catch elsewhere. Hence this and the next test.
    """
    assert _CI.is_file(), f"{_CI} not found; the checks below would pass vacuously"
    tokens = _covered_tokens()
    assert len(tokens) >= 20, (
        f"only {len(tokens)} pytest test-path arguments found across "
        f"{_SMOKE_JOB!r} and {_IMAGE_JOB!r}; the YAML shape probably changed and "
        f"every membership check below would fail or pass by accident"
    )


def test_membership_requires_a_real_pytest_argument() -> None:
    """A path mentioned only in a COMMENT must not count as covered.

    This is the finding that prompted the rewrite: the original check was
    ``rel in ci_file_text``, and the very commit adding this guard also added a
    comment naming the guard's own file -- so the guard could be dropped from
    the pytest args and still report itself as covered. A guard that cannot
    notice its own removal is decoration.
    """
    import shlex

    import yaml

    config = yaml.safe_load(_CI.read_text(encoding="utf-8"))
    entry = next(e for e in config[_SMOKE_JOB]["script"] if "pytest" in e)
    args = set(shlex.split(entry))
    # The guard's own file must be a real argument, not merely mentioned.
    me = "tests/unit/ci/test_smoke_covers_fleet_invariants.py"
    assert me in args, (
        f"{me} is not a pytest argument of the {_SMOKE_JOB!r} job. If it only "
        f"appears in a comment, this guard does not run in CI and cannot "
        f"report anything -- including its own absence."
    )


def test_a_deselected_path_does_not_count_as_covered() -> None:
    """--deselect/--ignore name test paths that mean the OPPOSITE of 'run it'."""
    import textwrap

    fake = textwrap.dedent(
        """
        smoke:
          script:
            - >
              python -m pytest tests/unit/a.py
              --deselect tests/unit/b.py
              --ignore=tests/unit/c.py
        """
    )
    import shlex

    import yaml

    cfg = yaml.safe_load(fake)
    entry = next(e for e in cfg["smoke"]["script"] if "pytest" in e)
    parts = shlex.split(entry)
    skip = False
    got = set()
    for part in parts:
        if skip:
            skip = False
            continue
        if part in _NEGATING_OPTIONS:
            skip = True
            continue
        if part.startswith("--") and "=" in part:
            if part.split("=", 1)[0] in _NEGATING_OPTIONS:
                continue
        if part.startswith("tests/"):
            got.add(part)
    assert got == {"tests/unit/a.py"}, (
        f"deselect/ignore values leaked into the covered set: {sorted(got)}"
    )


def test_the_invariant_scan_finds_something() -> None:
    """Same guard, one level up: an empty required set passes everything."""
    found = _tests_touching_invariants()
    assert len(found) >= 5, (
        f"only {len(found)} tests matched the fleet-invariant seams "
        f"{_FLEET_INVARIANT_SEAMS}; the scan is probably broken, and a broken "
        f"scan silently requires nothing"
    )


@pytest.mark.parametrize("rel", sorted(_tests_touching_invariants()))
def test_a_fleet_invariant_test_is_named_in_ci(rel: str) -> None:
    assert _named_in_ci(rel), (
        f"{rel} covers a fleet-concurrency invariant but no CI job collects it.\n"
        f"Both CI jobs are allowlists, so an unlisted file does not skip -- it "
        f"is never collected, and nothing reports a smaller number.\n"
        f"Add it to the `smoke` job's pytest arguments in .gitlab-ci.yml, or, if "
        f"it genuinely does not belong there, stop importing "
        f"{' / '.join(_FLEET_INVARIANT_SEAMS)}."
    )


def test_derived_files_are_covered_by_a_pre_merge_job() -> None:
    """Directory coverage via build:image is master-only, so it is not a
    pre-merge check. Everything derived must be in `smoke`, which runs on every
    merge request -- otherwise a regression is caught only after it lands.
    """
    smoke = _pytest_test_tokens(_SMOKE_JOB)
    missing = sorted(r for r in _tests_touching_invariants() if r not in smoke)
    assert not missing, (
        "covered only by a master-only job, so not checked before merge: "
        + ", ".join(missing)
    )
