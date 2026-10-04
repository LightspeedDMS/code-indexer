"""Migrated SQLite database templates for tests.

Initializing a server database from scratch runs every DDL statement as its
own durable transaction: ~40 fdatasyncs for ``DatabaseSchema``. Under real
disk-sync latency (another process writing to the same volume) that alone
costs seconds per test. A template is built ONCE per process per key by the
caller's real production initializer and copied to each test's path; callers
then re-run the same idempotent initializer on the copy, which only reads
(no schema write, so no sync).
"""

from __future__ import annotations

import atexit
import re
import shutil
import tempfile
import threading
from pathlib import Path
from typing import Callable, Dict, Optional

_SIDECAR_SUFFIXES = ("-wal", "-journal", "-shm")
#: A key prefixes one directory under the templates root: a plain slug only.
_KEY_PATTERN = re.compile(r"[a-z0-9][a-z0-9-]{0,63}")

_lock = threading.Lock()
_templates: Dict[str, Path] = {}
_root: Optional[Path] = None


def _templates_root() -> Path:
    global _root
    if _root is None:
        _root = Path(tempfile.mkdtemp(prefix="cidx-sqlite-templates-"))
        atexit.register(shutil.rmtree, str(_root), True)
    return _root


def _build_template(key: str, build: Callable[[Path], None]) -> Path:
    # A fresh directory per attempt: a failed build never blocks a retry.
    template_dir = Path(tempfile.mkdtemp(prefix=f"{key}-", dir=_templates_root()))
    template = template_dir / "template.db"
    try:
        build(template)
        if not template.is_file():
            raise RuntimeError(f"template build for {key!r} created no {template}")
        leftovers = [
            str(template) + suffix
            for suffix in _SIDECAR_SUFFIXES
            if Path(str(template) + suffix).exists()
        ]
        if leftovers:
            # A copy of the main file alone would silently lose these pages.
            raise RuntimeError(
                f"template build for {key!r} left uncheckpointed sidecars "
                f"{leftovers}; close every connection before returning"
            )
    except BaseException:
        shutil.rmtree(template_dir, ignore_errors=True)
        raise
    return template


def _template_for(key: str, build: Callable[[Path], None]) -> Path:
    if not isinstance(key, str) or not _KEY_PATTERN.fullmatch(key):
        raise ValueError(f"template key must be a lowercase slug, got {key!r}")
    with _lock:
        template = _templates.get(key)
        if template is None:
            template = _build_template(key, build)
            _templates[key] = template
        return template


def copy_migrated_sqlite_db(
    key: str, build: Callable[[Path], None], dest: Path
) -> None:
    """Copy the ``key`` template (built by ``build`` on first use in this
    process) to ``dest``, creating ``dest``'s parent directory. ``dest`` is
    created exclusively: an existing database is never replaced."""
    template = _template_for(key, build)
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(template, "rb") as src, open(dest, "xb") as out:
        shutil.copyfileobj(src, out)
