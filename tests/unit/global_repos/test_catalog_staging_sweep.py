"""The catalog staging sweep removes what crashed writers left behind and
nothing a live writer still uses.

Staged temp files (``.*.tmp``) are stale after STALE_STAGING_SECONDS (15
minutes). A description fact-check input (``.<alias>.verify.<random>.md``,
written by the description hook) lives as long as one verification pass,
so it is stale only after 24 hours: a crash leaves it for at most a day,
and a live verification -- even one far longer than the default pass
timeout -- is never swept.

Real staged writer on the real filesystem; file ages are set with utime.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from code_indexer.global_repos import catalog_staged_write as staged

CATALOG = "cidx-meta"
HOUR = 60 * 60


def _staging(tmp_path: Path) -> Path:
    (tmp_path / CATALOG).mkdir()
    staging = tmp_path / staged.STAGING_DIR_NAME
    staging.mkdir()
    return staging


def _aged(path: Path, age_seconds: float) -> Path:
    path.write_text("leftover\n")
    when = time.time() - age_seconds
    os.utime(path, (when, when))
    return path


def _sweep_by_writing(tmp_path: Path) -> None:
    catalog = tmp_path / CATALOG
    staged.write_catalog_file(catalog, catalog / "example-repo.md", "x\n", mode=0o644)


def test_a_crashed_fact_check_input_is_swept_once_stale(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    stale = _aged(staging / ".example-repo.verify.abc123.md", 25 * HOUR)

    _sweep_by_writing(tmp_path)

    assert not stale.exists(), "a crashed verification's input stays forever"


def test_a_live_fact_check_input_is_never_swept(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    live = _aged(staging / ".example-repo.verify.def456.md", 6 * HOUR)

    _sweep_by_writing(tmp_path)

    assert live.exists(), "a verification still running lost its input"


def test_staged_temp_files_keep_their_own_stale_age(tmp_path: Path) -> None:
    staging = _staging(tmp_path)
    stale = _aged(
        staging / ".example-repo.md.a1.tmp", staged.STALE_STAGING_SECONDS + 60
    )
    fresh = _aged(staging / ".example-repo.md.b2.tmp", 60)

    _sweep_by_writing(tmp_path)

    assert not stale.exists()
    assert fresh.exists()
