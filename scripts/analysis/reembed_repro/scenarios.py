"""Scenario presets and seeded random interrupt cycles (design 15.1, story S0).

``--scenario NAME`` sets defaults for the options below; any option given on
the command line still wins. Every scenario exercises R-d: the upgrade-like
default ends its explicit cycle list with the yield probe, and the random
scenarios append it after their seeded cycles.

* ``default`` -- upgrade-like and cluster-like: an older release (12.82.0)
  builds the index and runs the first refreshes, then the tree under test
  runs on another node;
* ``dup``     -- duplicate-content groups (2-50 copies) plus copies brought by
  later syncs;
* ``git``     -- git repository: sync commits, uncommitted edits, renames and
  branch switches;
* ``cancel``  -- every interrupt is a server job cancel (SIGTERM, SIGKILL 2 s);
* ``restart`` -- every interrupt is a manual ``systemctl restart`` (SIGTERM,
  SIGKILL 90 s);
* ``soak``    -- large-repo soak: 199,000 files with seeded SIGTERM/SIGKILL
  cycles.

The random scenarios run only the tree under test, all on one node (so a
recovery meets its own sealed resume state, harness finding a).
"""

from __future__ import annotations

import argparse
import random
from typing import Any, Dict, List, Sequence

YIELD_PROBE = "SIGTERM@inflight:4"
DEFAULT_CYCLES = (
    "SIGTERM@planned:0.5,SIGKILL@chunks:3000,SIGTERM@chunks:3000,SIGKILL@chunks:3000,"
    "SIGTERM@chunks:3000," + YIELD_PROBE
)
_POINTS = ("early", "planned", "chunks", "final")
_NONE_PROBABILITY = 0.1

# Preset values are argparse defaults of mixed types (str, int, float, bool,
# list) handed to ArgumentParser.set_defaults(**preset); a TypedDict would
# duplicate the parser's whole option list, so the values are typed Any.
#: Pause between the last cycle and the final recovery, as when the recovery
#: runs on a later scheduler cycle. It exceeds the indexer's 60 s incremental
#: safety buffer, so a resumed run that ignores files synced while it was
#: pending finishes too late for the next incremental to see them (finding c).
RECOVERY_DELAY_SECONDS = 65

_RANDOM_BASE: Dict[str, Any] = {
    "old_ref": "none",
    "node_sequence": [0],
    "sync_files": 200,
    "random_cycles": 20,
    "random_kinds": "SIGTERM,SIGKILL",
    "max_chunks_trigger": 400,
    "recovery_delay_seconds": RECOVERY_DELAY_SECONDS,
}

SCENARIOS: Dict[str, Dict[str, Any]] = {
    "default": {
        "cycles": DEFAULT_CYCLES,
        "random_cycles": 0,
        "recovery_delay_seconds": 0,
    },
    "dup": {**_RANDOM_BASE, "dup_groups": 100, "sync_dup_fraction": 0.3},
    "git": {**_RANDOM_BASE, "git": True},
    "cancel": {**_RANDOM_BASE, "random_kinds": "CANCEL"},
    "restart": {**_RANDOM_BASE, "random_kinds": "RESTART"},
    "soak": {
        **_RANDOM_BASE,
        "files": 199000,
        "sync_files": 2000,
        "max_chunks_trigger": 3000,
    },
}


def random_cycles(
    n: int, seed: int, kinds: Sequence[str], max_chunks: int
) -> List[str]:
    """``n`` seeded cycle entries: an interrupt at the hash pass (early /
    planned), after ``k`` inputs, or during finalization; sometimes none."""
    if n < 0 or max_chunks < 1 or not kinds:
        raise ValueError("need n >= 0, max_chunks >= 1 and at least one kind")
    rng = random.Random(seed)
    entries = []
    for _ in range(n):
        if rng.random() < _NONE_PROBABILITY:
            entries.append("none")
            continue
        kind = rng.choice(list(kinds))
        point = rng.choice(_POINTS)
        if point == "chunks":
            value: object = rng.randint(1, max_chunks)
        elif point == "early":
            value = round(rng.uniform(0.5, 3.0), 1)
        else:
            value = round(rng.uniform(0.0, 1.0), 1)
        entries.append(f"{kind}@{point}:{value}")
    return entries


def build_cycles(args: argparse.Namespace) -> List[str]:
    """The refreshes to run, one entry each."""
    if args.random_cycles:
        kinds = tuple(k.strip() for k in args.random_kinds.split(","))
        return random_cycles(
            args.random_cycles, args.cycle_seed, kinds, args.max_chunks_trigger
        ) + [YIELD_PROBE]
    return [entry.strip() for entry in args.cycles.split(",")]
