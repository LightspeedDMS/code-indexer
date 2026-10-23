"""Age a source tree's files, as in a catalog nothing has written to for a
while: a file written moments before a refresh lists it is racily clean
(local_source_manifest) and counts as changed at the next check."""

from __future__ import annotations

import os
import time
from pathlib import Path

QUIET_SECONDS = 3600


def age_source_files(root: Path, seconds: int = QUIET_SECONDS) -> None:
    """Set the mtime of every non-hidden file under *root* *seconds* back."""
    old = time.time() - seconds
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if any(part.startswith(".") for part in relative.parts):
            continue
        if path.is_file() and not path.is_symlink():
            os.utime(path, (old, old))
