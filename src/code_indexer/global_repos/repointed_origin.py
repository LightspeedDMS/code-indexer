"""issue-64616: decide whether a base clone whose ``origin`` was repointed can be
healed, reading only through the git seam.

A writer sharing the golden clone directory (the the content service golden-repo
seeder, before the content service a prior change) ran ``git remote set-url origin <other>``
on a clone of another project with the same name. The refresh refuses to
fetch such a clone (``RefreshScheduler._ensure_origin_is_registered``). It
heals it -- origin set back to the registered URL -- only when BOTH hold:

1. the clone was cloned FROM the registered repository: the oldest entry of
   HEAD's reflog is ``clone: from <url>`` with the registered identity;
2. its content did not follow the repointed origin since: after fetching the
   clone's branch from that origin, HEAD is neither that branch's tip nor an
   ancestor of it (``rev-list --count origin/<branch>..HEAD`` > 0). A clone
   that was reset or pulled to the other project after the repoint holds
   that project now, whatever it was cloned from.

Anything else -- no readable clone entry, a clone of another repository, a
content check that cannot run -- keeps the stop. Every read goes through the
scheduler's ``GitBackend``: local git, or the git service under delegated git, never
this pod's disk.

the git service: the reflog is read with ``git_exec`` (``log --walk-reflogs
--format=%gs HEAD``), which the git service allows from a prior change on. An older the git service
refuses the option; the refusal reads as "source unknown" and the stop is
kept, so this works before and after a prior change deploys with no change here. The
probe and the repair are typed the git service tools (``git_symbolic_ref``,
``git_fetch`` with ``ref``, ``git_rev_list_count``, ``git_set_remote``).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from code_indexer.server.storage.shared.git_backend import (
    GitBackend,
    GitCommandRunner,
    head_branch,
)
from code_indexer.utils.subprocess_diagnostics import redact_url_credentials

#: HEAD's reflog subjects, newest first; the oldest is the clone's.
REFLOG_ARGS = ("log", "--walk-reflogs", "--format=%gs", "HEAD")
_CLONE_ENTRY = "clone: from "

#: Seconds for the local reads, and for the one probe fetch of the branch.
READ_TIMEOUT_SECONDS = 30
PROBE_FETCH_TIMEOUT_SECONDS = 300


@dataclass(frozen=True)
class CloneSource:
    """Where the clone was cloned from (credentials as recorded), or why
    that is unknown."""

    url: Optional[str]
    unknown_reason: str = ""


def read_clone_source(backend: GitBackend, repo_path: str) -> CloneSource:
    """The ``clone: from <url>`` entry of HEAD's reflog, through the seam.

    Unknown when the backend cannot run git's local subcommands, git refuses
    or fails the read (a the git service older than a prior change refuses ``--walk-reflogs``),
    the reflog is empty, or its oldest entry is
    not the clone's (expired by ``gc.reflogExpire``, or never written).
    Transport errors raise."""
    if not isinstance(backend, GitCommandRunner):
        return CloneSource(None, "the git backend cannot read the reflog")
    out = backend.run(repo_path, list(REFLOG_ARGS), timeout=READ_TIMEOUT_SECONDS)
    if out.returncode != 0:
        detail = redact_url_credentials(str(out.stderr or "").strip())
        return CloneSource(
            None,
            f"reading HEAD's reflog failed (exit {out.returncode}"
            f"{': ' + detail if detail else ''})",
        )
    entries = [line.strip() for line in str(out.stdout).splitlines() if line.strip()]
    if not entries:
        return CloneSource(None, "the clone has no reflog")
    oldest = entries[-1]
    url = oldest[len(_CLONE_ENTRY) :].strip()
    if not oldest.startswith(_CLONE_ENTRY) or not url:
        return CloneSource(None, "the oldest reflog entry is not the clone's")
    return CloneSource(url)


def content_followed_origin(
    backend: GitBackend, repo_path: str, credentials_url: str
) -> bool:
    """True when the clone's HEAD is on the repointed origin's history.

    Fetches HEAD's branch from ``origin`` into ``refs/remotes/origin/<b>``
    (forced; the healed refresh's own fetch overwrites it again), then counts
    HEAD's commits that tip does not contain. *credentials_url* (the
    registered URL) is supplied by a local backend only for its own host;
    a backend that owns its credentials ignores it.

    Raises ValueError when HEAD is detached (no branch to compare), and
    whatever the fetch or the count raise: undetermined, never healed."""
    branch = head_branch(backend, repo_path, timeout=READ_TIMEOUT_SECONDS)
    if not branch:
        raise ValueError("HEAD is detached")
    backend.fetch(
        repo_path,
        "origin",
        timeout=PROBE_FETCH_TIMEOUT_SECONDS,
        credentials_url=credentials_url,
        ref=branch,
    )
    only_here = backend.rev_list_count(
        repo_path, f"refs/remotes/origin/{branch}..HEAD", timeout=READ_TIMEOUT_SECONDS
    )
    return int(only_here) == 0
