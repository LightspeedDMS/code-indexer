"""
Isolation-policy helpers shared by every server-side Claude/Codex CLI
invocation that analyzes a golden repository (lifecycle description/metadata
generation, dependency-map passes, and the legacy repo-analyzer path).

Invariant: the CLI runs from a neutral working directory, so it does not
auto-load a repository's own CLAUDE.md/AGENTS.md as trusted configuration —
the repository is still fully reachable (read AND write) via --add-dir. The
CLI's MCP server list is always explicit (--strict-mcp-config), so the
account's other globally-registered MCP servers are never pulled into the
session, whether or not a cidx-local credential could be obtained for this
call. Every invoker builds its command line through these functions instead
of re-deriving the same flags independently.

By owner decision, this module does NOT restrict which built-in tools
(Bash/Read/Write/Edit/Glob/Grep) the agent may use — every analysis flow
keeps the same command capability it had before this module existed
(including running git and writing the files its own prompt asks it to
write), via --dangerously-skip-permissions. The isolation controls here are
strictly: working directory, settings-source loading, and MCP server list.
Restricting tool access and the MCP credential's own privileges are a
separate, later change (least-privilege service credential), not this one.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import stat
import tempfile
import time
from typing import Dict, List, Optional

from code_indexer.server.services.config_service import get_config_service

logger = logging.getLogger(__name__)

# Base directory for every scratch path this module creates. Never the
# system temp directory directly (project convention keeps ad hoc runtime
# files under the user's own ~/.tmp, not shared /tmp).
CIDX_TMP_ROOT = os.path.expanduser("~/.tmp")

# Prefix for the per-invocation private directory that holds only the
# standalone MCP config file — deliberately never the neutral cwd and never
# a directory passed via --add-dir, so it is not merely "not the default
# location" but outside every path the agent's file tools can reach by name.
MCP_CONFIG_DIR_PREFIX = "cidx-mcp-config-"

# Flows that read the server's own log database directly (via Bash/sqlite3)
# rather than golden-repo content, and so keep the pre-existing invocation
# shape unchanged by this module (working directory as given, no --add-dir,
# no --strict-mcp-config). This is not a claim that the data involved is
# free of request-supplied values — log rows can contain values taken from
# incoming requests — only that this flow's isolation treatment is handled
# separately (tracked as its own follow-up).
_ISOLATION_EXEMPT_FLOWS: frozenset = frozenset({"self_monitoring_scan"})


def is_isolation_exempt_flow(flow: str) -> bool:
    """Return True for the narrow set of flows this module leaves untouched."""
    return flow in _ISOLATION_EXEMPT_FLOWS


def _ensure_cidx_tmp_root() -> str:
    """Create (if needed) and return the base directory for scratch paths."""
    os.makedirs(CIDX_TMP_ROOT, exist_ok=True)
    return CIDX_TMP_ROOT


# Directory name (under CIDX_TMP_ROOT) holding one stable subdirectory per
# target, used by prepare_stable_neutral_cwd().
STABLE_CWD_ROOT_NAME = "cidx-agent-cwd"

# An existing entry in a stable neutral cwd younger than this is left alone
# by prepare_stable_neutral_cwd() rather than removed. This buys no
# isolation/security on its own -- a CLAUDE.md left in the stable cwd is
# never auto-loaded under --setting-sources user regardless of its age --
# it exists purely to bound long-run disk accumulation (an agent's Bash
# tool can write scratch files directly into its own cwd; real repo
# content lives behind --add-dir, not here). 24 hours is comfortably past
# every real invocation's timeout (the longest configured CLI timeouts in
# this codebase are measured in minutes, not hours), so nothing genuinely
# in flight is ever this old; anything older is safe to reclaim.
_STABLE_CWD_MIN_ENTRY_AGE_SECONDS = 24 * 60 * 60.0

# Bounds for _newest_mtime_in_subtree's walk. This scratch cwd is expected
# to hold at most a handful of small files an agent's Bash tool wrote into
# its OWN cwd, so these are generous headroom, not an expected ceiling.
_MTIME_SCAN_MAX_DEPTH = 50
_MTIME_SCAN_MAX_ENTRIES = 20000


def _stable_cwd_key(target: str) -> str:
    """Return a short, filesystem-safe key that is STABLE for a given target.

    The same `target` string (an absolute repo path for lifecycle flows, or
    the golden-repos root for dependency-map flows) always hashes to the
    same key, which is exactly what makes prepare_stable_neutral_cwd()
    return the same directory across calls for that target -- a SHA-256
    hash sidesteps every filesystem-unsafe character a raw path could
    contain, so no separate sanitization step is needed.
    """
    if not isinstance(target, str) or not target:
        raise ValueError(f"target must be a non-empty string, got {target!r}")
    return hashlib.sha256(target.encode("utf-8")).hexdigest()[:20]


def _newest_mtime_in_subtree(path: str) -> float:
    """Return the newest mtime found anywhere in path's subtree, including
    path itself.

    A directory's own mtime only changes when a DIRECT child is added or
    removed -- writing a file several levels below it never touches it.
    Judging staleness from a top-level entry's own mtime alone can
    therefore misjudge a directory that is still being actively written to
    several levels deep as long-abandoned. Walking the whole subtree for
    its newest mtime avoids that.

    Never follows symlinks (a symlink's own mtime is used, not its
    target's). Bounded by _MTIME_SCAN_MAX_DEPTH and
    _MTIME_SCAN_MAX_ENTRIES; hitting either bound returns the current time,
    so the caller treats the entry as too complex to safely judge in this
    pass and leaves it alone rather than risk deleting something still in
    use -- a later call gets another chance once things have quieted down.
    Never raises: any stat/listdir failure along the way is skipped, using
    whatever mtime was already found.
    """
    entries_remaining = [_MTIME_SCAN_MAX_ENTRIES]

    def _walk(current: str, depth: int) -> float:
        try:
            newest = os.lstat(current).st_mtime
        except OSError:
            return 0.0
        if depth >= _MTIME_SCAN_MAX_DEPTH:
            return newest
        try:
            with os.scandir(current) as it:
                for entry in it:
                    entries_remaining[0] -= 1
                    if entries_remaining[0] <= 0:
                        return time.time()
                    try:
                        if not entry.is_symlink() and entry.is_dir(
                            follow_symlinks=False
                        ):
                            child_mtime = _walk(entry.path, depth + 1)
                        else:
                            child_mtime = entry.stat(follow_symlinks=False).st_mtime
                    except OSError:
                        continue
                    if child_mtime > newest:
                        newest = child_mtime
        except OSError:
            pass
        return newest

    return _walk(path, 0)


def _empty_dir_contents_safely(dir_path: str, *, min_age_seconds: float) -> None:
    """Remove every entry in dir_path whose NEWEST mtime anywhere in its
    subtree is at least min_age_seconds old, leaving younger entries (and
    the directory itself) untouched. See _newest_mtime_in_subtree for why
    the whole subtree, not just the top-level entry, is examined.

    Never raises: a listing or removal failure is logged and skipped, since
    this is a best-effort cleanup step, not the invocation's real result.
    """
    now = time.time()
    try:
        entry_names = os.listdir(dir_path)
    except OSError as exc:
        logger.debug(
            "agent_cli_isolation: could not list %s for cleanup: %s", dir_path, exc
        )
        return
    for name in entry_names:
        entry_path = os.path.join(dir_path, name)
        age_seconds = now - _newest_mtime_in_subtree(entry_path)
        if age_seconds < min_age_seconds:
            continue
        try:
            if os.path.isdir(entry_path) and not os.path.islink(entry_path):
                shutil.rmtree(entry_path)
            else:
                os.remove(entry_path)
        except OSError as exc:
            logger.debug(
                "agent_cli_isolation: could not remove stale entry %s: %s",
                entry_path,
                exc,
            )


def prepare_stable_neutral_cwd(target: str) -> str:
    """Return the stable neutral cwd for `target`, emptied of stale leftovers.

    Returns the SAME directory across every call for the same `target` (an
    absolute repo path for lifecycle flows, or the shared golden-repos root
    for dependency-map flows), rather than a fresh, unique directory per
    call. That stability is the fix: a unique cwd per call means the claude
    CLI's own per-cwd session-transcript folder under
    ~/.claude/projects/<escaped-cwd>/ is created once PER CALL instead of
    once per repo, growing unboundedly at fleet scale (~900 repos) since
    the deployment executor consolidates every such folder to NFS.
    Restoring one stable directory per target restores the pre-existing
    one-folder-per-repo transcript behaviour.

    The directory is never deleted by this function -- only entries left
    behind by a previous run in it, and only ones whose newest mtime
    anywhere in their own subtree is at least _STABLE_CWD_MIN_ENTRY_AGE_
    SECONDS old (see _newest_mtime_in_subtree), so a directory a
    concurrent run against the same target is still actively writing to,
    even several levels deep, is not swept out from under it. Concurrent
    runs against DIFFERENT targets always get different directories, so
    clearing one never affects another.
    """
    if not isinstance(target, str) or not target:
        raise ValueError(f"target must be a non-empty string, got {target!r}")
    stable_root = os.path.join(_ensure_cidx_tmp_root(), STABLE_CWD_ROOT_NAME)
    stable_dir = os.path.join(stable_root, _stable_cwd_key(target))
    os.makedirs(stable_dir, exist_ok=True)
    _empty_dir_contents_safely(
        stable_dir, min_age_seconds=_STABLE_CWD_MIN_ENTRY_AGE_SECONDS
    )
    return stable_dir


def create_private_mcp_config_dir() -> str:
    """Create and return a fresh directory reserved for the MCP config file.

    Distinct from the stable per-target scratch cwd (see
    prepare_stable_neutral_cwd): this directory is never the subprocess
    cwd and never passed via --add-dir, so it sits outside every
    path the agent's file tools reach by name during a normal invocation —
    though with full Bash access, the agent could still locate and read it
    by absolute path if it discovered the path, since nothing enforces
    directory confinement in this design. That residual is accepted here;
    the durable fix is a least-privilege credential (tracked separately),
    not hiding the file's location.
    """
    return tempfile.mkdtemp(prefix=MCP_CONFIG_DIR_PREFIX, dir=_ensure_cidx_tmp_root())


def build_mcp_config_document(port: int, auth_header_value: str) -> Dict:
    """Return the JSON-serializable document for a --mcp-config file.

    Names only the cidx-local HTTP MCP server — never the account's other
    globally-registered servers — mirroring what
    ``claude mcp add --transport http --header ... cidx-local http://localhost:<port>/mcp``
    registers globally, but scoped to a single, explicit, per-invocation file.
    """
    if not isinstance(port, int) or isinstance(port, bool) or port <= 0:
        raise ValueError(f"port must be a positive int, got {port!r}")
    if not isinstance(auth_header_value, str) or not auth_header_value:
        raise ValueError("auth_header_value must be a non-empty string")
    return {
        "mcpServers": {
            "cidx-local": {
                "type": "http",
                "url": f"http://localhost:{port}/mcp",
                "headers": {"Authorization": auth_header_value},
            }
        }
    }


def write_mcp_config_file(port: int, auth_header_value: str, dest_dir: str) -> str:
    """Write a private (0600) standalone MCP config file and return its path."""
    document = build_mcp_config_document(port, auth_header_value)
    fd, path = tempfile.mkstemp(prefix="cidx-mcp-config-", suffix=".json", dir=dest_dir)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(document, fh)
    finally:
        os.chmod(path, stat.S_IRUSR | stat.S_IWUSR)
    return path


def remove_mcp_config_file(path: Optional[str]) -> None:
    """Best-effort removal of a file written by try_build_mcp_config_file,
    including its private parent directory. Never raises."""
    if not path:
        return
    try:
        os.remove(path)
    except OSError as exc:
        logger.debug(
            "agent_cli_isolation: could not remove mcp config %s: %s", path, exc
        )
    try:
        os.rmdir(os.path.dirname(path))
    except OSError as exc:
        logger.debug(
            "agent_cli_isolation: could not remove mcp config dir for %s: %s", path, exc
        )


def try_build_mcp_config_file() -> Optional[str]:
    """
    Best-effort standalone --mcp-config file naming only cidx-local, written
    to its own private directory (see create_private_mcp_config_dir).

    Reuses the existing MCPSelfRegistrationService singleton (wired once at
    server startup) purely to read the port and Authorization header value
    it already holds — this does not change which credential the MCP call
    authenticates as. Prefers the value already cached in this process over
    triggering a fresh registration subprocess, so a normal invocation never
    shells out to `claude mcp add` on its own account. Three steps, same
    order as build_codex_mcp_auth_header_provider:
      1. The header already cached in this process.
      2. build_auth_header_from_creds() — runs ensure_registered(), which
         only populates the cache when it performs a fresh registration.
         When cidx-local is already registered (true for every process
         after the very first one), ensure_registered() takes its
         already-registered fast path and never calls
         register_in_claude_code(), so the cache stays empty here even
         though registration itself succeeded.
      3. build_header_from_stored_credentials() — reads the persisted
         client_id/client_secret directly and builds the header from them,
         covering exactly the case step 2 misses.
    Returns None (no MCP access for this invocation) when the singleton or
    all three steps come up empty; never raises. Shared by every invoker so
    there is exactly one place that knows how to obtain these values.
    """
    try:
        from code_indexer.server.services.mcp_self_registration_service import (
            MCPSelfRegistrationService,
        )

        svc = MCPSelfRegistrationService.get_instance()
        if svc is None:
            return None
        auth_header = svc.get_cached_auth_header_value()
        if not auth_header:
            auth_header = svc.build_auth_header_from_creds()
        if not auth_header:
            auth_header = svc.build_header_from_stored_credentials()
        if not auth_header:
            return None
        port = get_config_service().get_config().port
        dest_dir = create_private_mcp_config_dir()
        return write_mcp_config_file(port, auth_header, dest_dir)
    except Exception as exc:
        logger.warning(
            "agent_cli_isolation: could not build standalone MCP config (%s); "
            "proceeding without cidx-local MCP access for this invocation",
            exc,
        )
        return None


def build_claude_isolation_args(
    analysis_dir: str,
    mcp_config_path: Optional[str],
) -> List[str]:
    """Build the isolation-related argv fragment for a `claude` invocation.

    ``analysis_dir`` is the directory the agent needs to read AND write (a
    single repo clone for lifecycle flows, the golden-repos root for
    dependency-map flows) — passed via --add-dir, never as the subprocess
    cwd, so a repository's own CLAUDE.md/AGENTS.md is not auto-loaded as CLI
    project configuration. --setting-sources 'user' loads ONLY the service
    account's own user-level settings.json (pace-maker hook registration,
    model/telemetry/env config) exactly as it did before the neutral-cwd
    change, while never loading a project/local settings.json -- those live
    in a repository's own working tree, and the neutral scratch cwd never
    contains one (the analyzed repository is reachable only via --add-dir,
    never as cwd), so repo-controlled settings stay excluded without this
    flag having to say so explicitly. Always includes --strict-mcp-config so
    the account's other globally-registered MCP servers are never pulled
    in; --mcp-config is added only when the caller could build a private,
    cidx-local-only config file, in which case zero other servers are ever
    visible in this session. --dangerously-skip-permissions is unconditional
    here — every non-exempt flow keeps the full command capability (Bash,
    Write, Edit, git) it had before this module existed; that flag is what
    lets those commands run non-interactively.
    """
    if not isinstance(analysis_dir, str) or not analysis_dir:
        raise ValueError("analysis_dir must be a non-empty string")
    args: List[str] = [
        "--setting-sources",
        "user",
        "--add-dir",
        analysis_dir,
        "--strict-mcp-config",
    ]
    if mcp_config_path:
        args += ["--mcp-config", mcp_config_path]
    args.append("--dangerously-skip-permissions")
    return args
