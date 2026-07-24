"""read_legacy_shard_rows() -- side-effect-free full row reader for a
temporal shard's on-disk hash-sharded vector_*.json files (Story #1457 AC1
relocation trigger).

Sibling to temporal_row_existence.py's temporal_shard_has_committed_rows()
(same rglob("vector_*.json") scan target), but reads FULL row content
instead of short-circuiting on existence. This is the reader AC6's Branch
B-bootstrap needs to pass as legacy_row_reader to
execute_temporal_refresh_branch() -- the primitive an eventual AC11
bootstrap implementation should REUSE rather than reimplement.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any, Dict, Iterator

logger = logging.getLogger(__name__)


def read_legacy_shard_rows(
    shard_dir: Path, *, fail_on_corrupt: bool = False
) -> Iterator[Dict[str, Any]]:
    """Yield every committed row (parsed JSON dict) under shard_dir.

    Side-effect-free (never writes anything). Scans the SAME 4-level
    hash-sharded vector_*.json layout temporal_shard_has_committed_rows()
    scans, but yields full row content instead of short-circuiting.

    A malformed/corrupt row file, or a file that parses as valid JSON but
    is not a dict (row shape contract violation):
      - fail_on_corrupt=False (default): skipped with a logged WARNING
        rather than aborting the whole scan -- matches the established
        precedent FilesystemVectorStore._rebuild_path_index_from_disk
        already uses for this same vector_*.json scan target. Correct for
        tolerant/introspective reuse of this generic scan primitive.
      - fail_on_corrupt=True: raises RuntimeError naming the offending
        file instead of skipping (Story #1457 CRITICAL #4, 2026-07-23
        code review). The PUBLISH path (AC6 Branch B-bootstrap / a future
        AC11 bootstrap) MUST use this -- publishing an incomplete result
        because a corrupt row was silently dropped is a correctness bug,
        not a graceful degradation.

    Args:
        shard_dir: Path to a temporal quarter-shard or monolith collection
            directory.
        fail_on_corrupt: When True, raise instead of skip-and-warn on any
            unreadable/invalid row (see above).

    Returns:
        Empty iterator if shard_dir does not exist or is not a directory.

    Raises:
        RuntimeError: fail_on_corrupt=True and a row file is malformed,
            unreadable, or does not parse as a dict.
    """
    shard_dir = Path(shard_dir)
    if not shard_dir.is_dir():
        return

    for vector_file in shard_dir.rglob("vector_*.json"):
        try:
            with open(str(vector_file), "r") as fh:
                row = json.load(fh)
        except (json.JSONDecodeError, OSError) as exc:
            if fail_on_corrupt:
                raise RuntimeError(
                    f"Refusing to build a temporal version from an "
                    f"incomplete row set: malformed/unreadable row file "
                    f"{vector_file}: {exc}"
                ) from exc
            logger.warning(
                "Skipping malformed row file %s during legacy shard scan: %s",
                vector_file,
                exc,
            )
            continue

        if not isinstance(row, dict):
            if fail_on_corrupt:
                raise RuntimeError(
                    f"Refusing to build a temporal version from an "
                    f"incomplete row set: row file {vector_file} parsed "
                    f"as a {type(row).__name__}, expected a dict"
                )
            logger.warning(
                "Skipping row file %s during legacy shard scan: parsed "
                "JSON is a %s, expected a dict",
                vector_file,
                type(row).__name__,
            )
            continue

        yield row
