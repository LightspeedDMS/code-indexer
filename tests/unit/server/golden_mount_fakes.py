"""A cidx pod WITHOUT the golden-repos volume, for mount-removal tests.

With every repository backend and the IndexStore delegated, a pod's
``<data>/golden-repos`` holds its own index state only -- what it built or
checked out of the IndexStore -- and no repository content. Locally (the
default) it holds the golden clones themselves.

:func:`forbid_golden_content_io` makes any local access to golden-repos
CONTENT raise :class:`LocalIOForbidden`: a repository's directory may be
created, stat'ed and used as a cwd for index state, but never listed, and
nothing under it outside cidx's index state may be touched. cidx's own server
state is NOT allowed there either (``.cache``, ``.scratch``): under
all-delegated backends it lives on the pod's data directory.

Allowed under golden-repos (pod state or control-plane metadata):

* ``aliases/``, ``.write_mode/`` -- the legacy control-plane files (the
  registry in production; the files when no registry is installed);
* ``<repo>/.code-indexer`` and the IndexStore checkout markers beside it;
* ``.versioned/<repo>/v_*/`` -- the same, for a snapshot;
* ``.temporal/``, ``.integrity-baseline/`` -- IndexStore checkouts / refresh
  scratch of index state;
* ``.<repo>.checkout.lock`` / ``.<repo>.readers.lock`` and other per-target
  lock files the checkout takes beside a target.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Callable, Iterator, List

import contextlib

import pytest

from code_indexer.server.services import index_materializer
from tests.unit.server.activated_seam_fakes import (
    LocalIOForbidden,
    install_local_io_guards,
)

INDEX_DIR = ".code-indexer"

#: Entries directly under a repository's directory that are pod index state.
_REPO_STATE_NAMES = frozenset(
    {
        INDEX_DIR,
        index_materializer.CHECKED_OUT_MARKER,
        index_materializer.IN_PROGRESS_MARKER,
        index_materializer.INCOMING_DIRNAME,
        index_materializer.RETIRED_DIRNAME,
        ".cidx-index-last-served",
    }
)

#: Top-level control-plane / index-state directories under golden-repos.
_ROOT_STATE_DIRS = frozenset(
    {"aliases", ".write_mode", ".temporal", ".integrity-baseline"}
)

_LOCK_SUFFIXES = (".checkout.lock", ".readers.lock", ".lock")


def golden_content_forbidden(golden_repos_dir: Path) -> Callable[[Any], bool]:
    """Predicate: True for a path under *golden_repos_dir* that is neither
    pod index state nor control-plane metadata (see the module docstring)."""
    root = os.path.abspath(os.fspath(golden_repos_dir))

    def forbidden(value: Any) -> bool:
        try:
            text = os.fspath(value)
        except TypeError:
            return False
        if isinstance(text, bytes):
            text = text.decode(errors="replace")
        full = os.path.abspath(text)
        if full != root and not full.startswith(root + os.sep):
            return False
        rel = Path(full).relative_to(root).parts
        if not rel:
            return False
        head = rel[0]
        if head in _ROOT_STATE_DIRS:
            return False
        if len(rel) == 1 and head.startswith(".") and head.endswith(_LOCK_SUFFIXES):
            return False
        if head == ".versioned":
            inner = rel[3:]  # .versioned/<repo>/v_<ts>/...
            return bool(inner) and inner[0] not in _REPO_STATE_NAMES
        if head.startswith("."):
            return True  # .cache, .scratch, ...: cidx state on the volume
        inner = rel[1:]
        return bool(inner) and inner[0] not in _REPO_STATE_NAMES

    return forbidden


def _repository_dir(golden_repos_dir: Path, value: Any) -> bool:
    """True when *value* IS a repository directory (or snapshot) itself."""
    try:
        full = os.path.abspath(os.fspath(value))
    except TypeError:
        return False
    root = os.path.abspath(os.fspath(golden_repos_dir))
    if not full.startswith(root + os.sep):
        return False
    rel = Path(full).relative_to(root).parts
    if rel[0] == ".versioned":
        return len(rel) == 3
    return (
        len(rel) == 1
        and not rel[0].startswith(".")
        and rel[0] not in (_ROOT_STATE_DIRS)
    )


def _install_listing_guards(
    patcher: pytest.MonkeyPatch, golden_repos_dir: Path
) -> None:
    """Listing a repository's directory enumerates its content: refuse it."""

    def guard(owner: Any, name: str) -> None:
        original = getattr(owner, name)

        def wrapper(*args: Any, **kwargs: Any) -> Any:
            target = args[0] if args else kwargs.get("path", ".")
            if _repository_dir(golden_repos_dir, target):
                raise LocalIOForbidden(f"{name}({target!r}) lists repository content")
            return original(*args, **kwargs)

        patcher.setattr(owner, name, wrapper)

    for name in ("listdir", "scandir", "walk"):
        guard(os, name)
    for name in ("iterdir", "glob", "rglob"):
        guard(Path, name)


@contextlib.contextmanager
def forbid_golden_content_io(golden_repos_dir: Path) -> Iterator[None]:
    """Inside the block, golden-repos content access raises LocalIOForbidden."""
    with pytest.MonkeyPatch.context() as patcher:
        install_local_io_guards(patcher, golden_content_forbidden(golden_repos_dir))
        _install_listing_guards(patcher, golden_repos_dir)
        yield


def guard_is_live(golden_repos_dir: Path, repo: str) -> List[str]:
    """Probe the guard; the names of probes that did NOT raise (want [])."""
    probes = {
        "read content": lambda: (golden_repos_dir / repo / "README.md").read_text(),
        "list repo": lambda: os.listdir(golden_repos_dir / repo),
        "payload cache": lambda: (golden_repos_dir / ".cache").exists(),
        "leases": lambda: (golden_repos_dir / ".scratch").exists(),
    }
    missed = []
    with forbid_golden_content_io(golden_repos_dir):
        for name, probe in probes.items():
            try:
                probe()
            except LocalIOForbidden:
                continue
            except OSError:
                pass
            missed.append(name)
    return missed


def remove_pod_repository(golden_repos_dir: Path, repo: str) -> None:
    """The pod path absent: nothing of *repo* on this pod."""
    shutil.rmtree(golden_repos_dir / repo, ignore_errors=True)
