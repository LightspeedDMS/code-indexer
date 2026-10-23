"""Persistent Xray Evaluator Pattern Library — Story #1031.

Provides storage, retrieval, and parametrization of reusable Rust evaluator
patterns for the xray_search and xray_explore MCP tools.

Pattern storage layout (inside cidx-meta):
    xray-patterns/
        __any__/          # Cross-repo patterns (found for any repo alias)
            catch-rethrow.yaml
            deep-nesting.yaml
            ...
        {repo-alias}/     # Repo-specific patterns (take priority over __any__)
            my-pattern.yaml
            ...

Pattern YAML schema:
    name: str             (required, used as filename stem)
    description: str      (required)
    language: str         (required)
    tags: list[str]       (optional)
    author: str           (optional)
    created_at: str       (optional, ISO date)
    evaluator_code: str   (required, Rust fn evaluate_node code; when
                           execution_mode is "graph", holds a
                           collect_facts + analyze_graph callback pair
                           instead)
    execution_mode: str   (optional: "legacy" | "graph". Missing means
                           "legacy" -- ADR-001, Story #1859. All 39
                           patterns stored before this field existed are
                           legacy source and resolve as "legacy".)
    parameters: list      (optional, list of ParameterDecl dicts)

ParameterDecl:
    name: str       (required, UPPER_SNAKE)
    type: str       (required, one of: usize, i64, f64, bool, str)
    default: any    (required, compatible with type)
    description: str (optional)
"""

from __future__ import annotations

import logging
import subprocess
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

import yaml

from code_indexer.global_repos.catalog_lock_holder import (
    CatalogLockedExclusively,
    require_publishing_holder,
)
from code_indexer.server.git.git_subprocess_env import build_non_interactive_git_env
from code_indexer.server.services.audit_outcome import (
    conforming_details,
    record_outcome,
)
from code_indexer.server.services.cidx_meta_backup.paths import (
    cidx_meta_is_git_repository,
)
from code_indexer.server.services.xray_pattern_params import (
    build_const_lines,
    resolve_params,
    validate_parameter_declarations,
)
from code_indexer.server.services.xray_pattern_seeds import (
    SEED_CATCH_RETHROW as _SEED_CATCH_RETHROW,
    SEED_DEEP_NESTING as _SEED_DEEP_NESTING,
)
from code_indexer.xray.sandbox import (
    detect_evaluator_entry_points,
    validate_rust_evaluator,
)

logger = logging.getLogger(__name__)


class PatternCommitError(RuntimeError):
    """git add/commit of a pattern change in cidx-meta failed."""


def _record_pattern_store(
    actor: str, name: Optional[str], outcome: str, details: Dict[str, Any]
) -> None:
    """Record one ``xray_pattern_stored`` row (never raises)."""
    record_outcome(
        actor=actor,
        action_type="xray_pattern_stored",
        target_type="xray_pattern",
        target_id=name,
        outcome=outcome,
        details=details,
    )


def record_refused_pattern_store(actor: str, scope: str) -> None:
    """Record a store refused before the service ran (e.g. an ungranted
    scope): one failure row, the pattern itself not yet resolved."""
    is_global = scope == XrayPatternService.ANY_SCOPE
    details = conforming_details(
        "xray_pattern_stored", global_scope=is_global, repo=None if is_global else scope
    )
    _record_pattern_store(actor, None, "failure", details)


# Required top-level fields in a pattern YAML
_REQUIRED_FIELDS = ("name", "description", "language", "evaluator_code")

# Valid values for the optional execution_mode field (ADR-001, Story #1859).
# Absence is interpreted as "legacy" everywhere this is checked -- the field
# was added after all 39 pre-existing stored patterns were already legacy
# evaluate_node source, and no caller written before this field existed
# ever supplies it.
_ALLOWED_EXECUTION_MODES = frozenset({"legacy", "graph"})
DEFAULT_EXECUTION_MODE = "legacy"

# Coarse write-lock alias for the shared cidx-meta git repo (Bug #1037).
# Must match the alias used by MemoryStoreService and RefreshScheduler.
_COARSE_ALIAS = "cidx-meta"
#: Every X-Ray pattern writer's catalog-lock owner name starts with this.
XRAY_PATTERN_LOCK_OWNER_PREFIX = "xray-pattern:"


# ---------------------------------------------------------------------------
# XrayPatternService
# ---------------------------------------------------------------------------


class XrayPatternService:
    """Service for storing, retrieving, and parametrizing xray evaluator patterns.

    All patterns are stored in the cidx-meta directory under xray-patterns/.
    Patterns in __any__/ scope are cross-repo (found for any repo alias).
    Patterns in {repo-alias}/ scope are repo-specific (take priority over __any__).

    Args:
        cidx_meta_path: The mutable cidx-meta base path (from get_cidx_meta_path()).
        refresh_scheduler: Optional RefreshScheduler instance. When provided,
            store_xray_pattern_audited() acquires the
            cidx-meta coarse write lock (mirroring
            MemoryStoreService._run_with_coarse_lock) before touching the shared
            git index; when the lock is already held it piggybacks (write the
            file, skip the commit, which the holder's refresh picks up). When
            None (default) there is no coarse lock: writes and commits run
            directly.
    """

    PATTERNS_DIR = "xray-patterns"
    ANY_SCOPE = "__any__"
    SEED_PATTERN_NAMES = ("catch-rethrow", "deep-nesting")

    def __init__(
        self,
        cidx_meta_path: Path,
        refresh_scheduler: Any = None,
    ) -> None:
        self._cidx_meta = Path(cidx_meta_path)
        self._scheduler = refresh_scheduler

    @property
    def _patterns_root(self) -> Path:
        return self._cidx_meta / self.PATTERNS_DIR

    def store_xray_pattern_audited(
        self,
        scope: str,
        pattern_yaml: str,
        overwrite: bool = False,
        *,
        actor: str,
        may_replace_global: bool,
    ) -> Dict[str, Any]:
        """Store a pattern to cidx-meta/xray-patterns/{scope}/{name}.yaml on
        behalf of *actor*.

        Records exactly one ``xray_pattern_stored`` row per attempt (success,
        or failure for every refusal, validation error or failed commit) and
        names *actor* in the cidx-meta commit. Replacing an existing
        ``__any__`` pattern is refused unless *may_replace_global*.

        Returns:
            Dict with {"success": True} on success, or
            {"error": "<code>", "message": "<detail>"} on failure.
        """
        facts: Dict[str, Any] = {}
        try:
            result = self._store_pattern(
                scope,
                pattern_yaml,
                overwrite,
                actor=actor,
                may_replace_global=may_replace_global,
                facts=facts,
            )
        except Exception:
            _record_pattern_store(
                actor, facts.get("name"), "failure", facts.get("details", {})
            )
            raise
        outcome = "success" if result.get("success") is True else "failure"
        _record_pattern_store(
            actor, facts.get("name"), outcome, facts.get("details", {})
        )
        return result

    def _store_pattern(
        self,
        scope: str,
        pattern_yaml: str,
        overwrite: bool,
        *,
        actor: str,
        may_replace_global: bool,
        facts: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Body of :meth:`store_xray_pattern_audited`; fills *facts* with the
        pattern name and audit details as soon as they are known."""
        is_global = scope == self.ANY_SCOPE
        facts["details"] = conforming_details(
            "xray_pattern_stored",
            global_scope=is_global,
            repo=None if is_global else scope,
        )
        # 0. Validate scope for path traversal sequences (before any filesystem access)
        if "/" in scope or "\\" in scope or ".." in scope:
            return {
                "error": "path_traversal_rejected",
                "message": (f"scope '{scope}' contains path traversal sequences"),
            }

        # 1. Parse YAML
        try:
            spec = yaml.safe_load(pattern_yaml)
        except yaml.YAMLError as exc:
            return {"error": "invalid_yaml", "message": str(exc)}

        if not isinstance(spec, dict):
            return {
                "error": "invalid_yaml",
                "message": "Pattern YAML must be a mapping",
            }

        # 2. Validate required fields
        for field in _REQUIRED_FIELDS:
            if not spec.get(field):
                return {
                    "error": "missing_required_field",
                    "message": f"Pattern YAML must contain '{field}'",
                }

        name: str = spec["name"]
        facts["name"] = name
        evaluator_code: str = spec["evaluator_code"]

        # 2a. Validate name for path traversal sequences
        if "/" in name or "\\" in name or ".." in name:
            return {
                "error": "invalid_pattern_name",
                "message": (f"name '{name}' contains path traversal sequences"),
            }

        # 3. Validate evaluator_code via Rust whitelist
        validation = validate_rust_evaluator(evaluator_code)
        if not validation.ok:
            return {
                "error": "xray_evaluator_validation_failed",
                "error_code": validation.error_code,
                "offending_construct": validation.offending_construct,
                "offending_line": validation.offending_line,
                "message": validation.reason,
            }

        # 4. Validate parameters if present
        params: List[Dict[str, Any]] = spec.get("parameters") or []
        param_validation = validate_parameter_declarations(params, new_store=True)
        if param_validation is not None:
            return param_validation

        # 4a. Validate execution_mode if present (ADR-001, Story #1859).
        # Absence is not an error -- it means "legacy" (see DEFAULT_EXECUTION_MODE).
        execution_mode = spec.get("execution_mode")
        if (
            execution_mode is not None
            and execution_mode not in _ALLOWED_EXECUTION_MODES
        ):
            return {
                "error": "invalid_execution_mode",
                "message": (
                    f"execution_mode {execution_mode!r} is not allowed. "
                    f"Allowed values: {sorted(_ALLOWED_EXECUTION_MODES)}"
                ),
            }

        # 4b. Cross-check the declared (or defaulted) execution_mode against
        # the callback family ACTUALLY present in evaluator_code (C4,
        # #1858-#1861 remediation). `validate_rust_evaluator` above only
        # confirms evaluator_code satisfies AT LEAST ONE mode's entry
        # points -- it never checks that mode against the DECLARED
        # execution_mode, so `execution_mode: graph` paired with
        # legacy-only code (or vice versa) previously stored successfully
        # and only failed much later at Rust compile/load with an
        # unrelated, opaque error. ADR-001 names an artifact whose exports
        # do not match its declaration as a hard error -- reject it here,
        # at store time, reusing the SAME detection `validate_rust_
        # evaluator` already ran (Rule 4 anti-duplication) rather than
        # re-deriving it.
        resolved_mode = execution_mode or DEFAULT_EXECUTION_MODE
        has_legacy_entry_point, has_graph_entry_point = detect_evaluator_entry_points(
            evaluator_code
        )
        if resolved_mode == "graph" and not has_graph_entry_point:
            return {
                "error": "execution_mode_mismatch",
                "message": (
                    "execution_mode is 'graph' but evaluator_code does not "
                    "define both 'fn collect_facts' and 'fn analyze_graph'"
                ),
            }
        if resolved_mode == "legacy" and not has_legacy_entry_point:
            return {
                "error": "execution_mode_mismatch",
                "message": (
                    "execution_mode is 'legacy' (declared or defaulted) but "
                    "evaluator_code does not define 'fn evaluate_node'"
                ),
            }

        # 5. Resolve target path
        target_dir = self._patterns_root / scope
        target_path = target_dir / f"{name}.yaml"

        # 6. Overwrite protection: an early answer here, decided again inside
        # the write operation right before writing (which holds the coarse
        # write lock unless it piggybacks on another holder's lock).
        result_path = str(target_path.relative_to(self._cidx_meta))
        unchanged = {"success": True, "path": result_path}
        if self._holds(target_path, pattern_yaml):
            return unchanged
        refusal = self._replace_refusal(
            target_path, scope, name, overwrite, may_replace_global
        )
        if refusal is not None:
            return refusal

        # 7. Write YAML and git-commit under coarse lock.
        owner = f"{XRAY_PATTERN_LOCK_OWNER_PREFIX}store:{name}"

        def _write_and_commit(piggyback: bool) -> Optional[Dict[str, Any]]:
            if self._holds(target_path, pattern_yaml):
                return unchanged
            locked_refusal = self._replace_refusal(
                target_path, scope, name, overwrite, may_replace_global
            )
            if locked_refusal is not None:
                return locked_refusal
            previous = target_path.read_text("utf-8") if target_path.exists() else None
            facts["details"]["overwrite"] = previous is not None
            action = "add" if previous is None else "update"
            self._put_catalog_file(target_path, pattern_yaml)
            if piggyback:
                return None
            try:
                self._git_commit(
                    files=[target_path],
                    message=f"xray-patterns: {action} {scope}/{name} by {actor}",
                )
            except PatternCommitError:
                if previous is None:
                    target_path.unlink(missing_ok=True)
                else:
                    self._put_catalog_file(target_path, previous)
                self._unstage(target_path)
                raise
            return None

        try:
            locked_result, lock_still_held = self._run_with_coarse_lock(
                owner, _write_and_commit
            )
        except PatternCommitError as exc:
            return {"success": False, "error": "commit_failed", "message": str(exc)}
        except CatalogLockedExclusively as exc:
            return {"success": False, "error": "catalog_locked", "message": str(exc)}
        if locked_result is not None:
            return dict(locked_result)
        self._request_refresh(lock_still_held)
        return {"success": True, "path": result_path}

    def _request_refresh(self, lock_still_held: bool) -> None:
        """Owe the catalog refresh a completed pattern write needs. A refresh
        starting under a held lock skips and owes nothing: after our own
        lock is released the refresh is requested; while the lock is still
        held (another owner's, or ours after a failed release) the refresh
        is owed to the debouncer. No-op without a scheduler."""
        if self._scheduler is None:
            return
        from code_indexer.global_repos.meta_description_hook import (
            refresh_after_catalog_write,
        )

        refresh_after_catalog_write(self._scheduler, lock_still_held)

    def _put_catalog_file(self, target: Path, content: str) -> None:
        """Put *target* in place atomically, staged outside the catalog."""
        from code_indexer.global_repos.catalog_staged_write import (
            SHARED_CATALOG_FILE_MODE,
            write_catalog_file,
        )

        if not write_catalog_file(
            self._cidx_meta, target, content, mode=SHARED_CATALOG_FILE_MODE
        ):
            logger.warning("X-Ray pattern %s written without a directory fsync", target)

    @staticmethod
    def _holds(target_path: Path, pattern_yaml: str) -> bool:
        """True when the pattern file already has exactly this content."""
        return target_path.exists() and target_path.read_text("utf-8") == pattern_yaml

    def _unstage(self, target_path: Path) -> None:
        """Reset the index entry of *target_path* to HEAD after a failed
        commit, so the abandoned change is never picked up later."""
        relative = str(target_path.relative_to(self._cidx_meta))
        result = subprocess.run(
            ["git", "reset", "-q", "--", relative],
            cwd=str(self._cidx_meta),
            env=build_non_interactive_git_env(),
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            logger.error(
                "xray_pattern_service: could not unstage %s after a failed commit; "
                "the index may still hold the abandoned change: %s",
                relative,
                result.stderr.strip(),
            )

    def _replace_refusal(
        self,
        target_path: Path,
        scope: str,
        name: str,
        overwrite: bool,
        may_replace_global: bool,
    ) -> Optional[Dict[str, Any]]:
        """The refusal to replace an existing pattern, or None to proceed."""
        if not target_path.exists():
            return None
        if not overwrite:
            return {
                "error": "pattern_already_exists",
                "message": (
                    f"Pattern '{name}' already exists in scope '{scope}'. "
                    "Use overwrite=true to replace it."
                ),
            }
        if scope == self.ANY_SCOPE and not may_replace_global:
            return {
                "success": False,
                "error": "global_pattern_overwrite_forbidden",
                "message": (
                    f"Replacing the shared pattern '{name}' in scope "
                    f"'{scope}' requires the admin role."
                ),
            }
        return None

    def resolve_and_prepare_pattern(
        self,
        repo_alias: str,
        pattern_name: str,
        pattern_params: Optional[Dict[str, Any]] = None,
    ) -> Tuple[str, Dict[str, Any]]:
        """Load a pattern and inject parameter consts into the evaluator code.

        Resolution order:
            1. cidx_meta/xray-patterns/{repo_alias}/{pattern_name}.yaml
            2. cidx_meta/xray-patterns/__any__/{pattern_name}.yaml

        Args:
            repo_alias: Repository alias for resolution priority.
            pattern_name: Name of the pattern (filename stem without .yaml).
            pattern_params: Optional overrides for declared parameters.

        Returns:
            Tuple of (prepared_evaluator_code, resolved_param_values).
            The prepared code has const declarations prepended.

        Raises:
            ValueError: "pattern_not_found" if pattern doesn't exist in either scope.
            ValueError: "unknown_parameter" if pattern_params contains unknown key.
            ValueError: "parameter_type_mismatch" if param value has wrong type.
        """
        if pattern_params is None:
            pattern_params = {}

        # Consolidated review finding H3 (Issue #1811/Bug #1812): a
        # non-dict pattern_params (e.g. a list or bare string, both
        # iterable) must be rejected HERE, before _resolve_params ever
        # iterates it -- `for key in overrides` silently iterates a list's
        # elements (or a string's characters) as if they were dict keys,
        # and the subsequent `overrides.get(name, default)` then raises an
        # unhandled AttributeError (list) or produces a nonsensical
        # unknown_parameter/parameter_type_mismatch report keyed off single
        # characters (string). Validating the TYPE here, at the one shared
        # choke point both the REST route (xray_routes.py) and the MCP
        # handler (handlers/xray.py::_resolve_evaluator_code) funnel
        # through, means the fix covers both callers without duplicating
        # the check in each.
        if not isinstance(pattern_params, dict):
            raise ValueError(
                f"invalid_pattern_params: pattern_params must be a dict, "
                f"got {type(pattern_params).__name__}"
            )

        # Bug #1423 (defense in depth): repo_alias must be a str before it
        # reaches the path-traversal check below or _load_pattern's Path
        # division. A list (even single-element -- the omni multi-repo
        # repository_alias form) passes the "/" in field_value membership
        # check silently (it's a valid, False, list-containment test) and
        # would otherwise crash _load_pattern with a raw
        # "unsupported operand type(s) for /: 'PosixPath' and 'list'"
        # TypeError. Reject loudly here instead.
        if not isinstance(repo_alias, str):
            raise ValueError(
                f"invalid_repo_alias_type: repo_alias must be a str, got "
                f"{type(repo_alias).__name__} ({repo_alias!r})"
            )

        # Validate repo_alias and pattern_name for path traversal sequences
        for field_name, field_value in [
            ("repo_alias", repo_alias),
            ("pattern_name", pattern_name),
        ]:
            if "/" in field_value or "\\" in field_value or ".." in field_value:
                raise ValueError(
                    f"path_traversal_rejected: {field_name} '{field_value}' "
                    "contains path traversal sequences"
                )

        spec = self._load_pattern(repo_alias, pattern_name)
        if spec is None:
            raise ValueError(
                f"pattern_not_found: pattern '{pattern_name}' not found in "
                f"scope '{repo_alias}' or '__any__'"
            )

        params_decls: List[Dict[str, Any]] = spec.get("parameters") or []
        evaluator_code: str = spec["evaluator_code"]

        # Validate + resolve parameter values
        resolved = resolve_params(params_decls, pattern_params)

        # Build const lines and prepend before evaluator code
        const_lines = build_const_lines(params_decls, resolved)
        if const_lines:
            prepared_code = const_lines + "\n" + evaluator_code
        else:
            prepared_code = evaluator_code

        return prepared_code, resolved

    def get_pattern_execution_mode(self, repo_alias: str, pattern_name: str) -> str:
        """Return the declared execution_mode ("legacy" or "graph") for a
        stored pattern, without preparing/parametrizing its evaluator code.

        Resolution order matches resolve_and_prepare_pattern (repo-specific
        scope first, then __any__) -- both go through the same _load_pattern
        lookup, so a repo-specific override and its declared mode are never
        resolved from two different files.

        Missing execution_mode metadata resolves to DEFAULT_EXECUTION_MODE
        ("legacy") per ADR-001: the field was added after all 39 existing
        patterns were already stored as legacy evaluate_node source, and
        none of them declares it.

        Args:
            repo_alias: Repository alias for resolution priority.
            pattern_name: Name of the pattern (filename stem without .yaml).

        Returns:
            "legacy" or "graph".

        Raises:
            ValueError: "pattern_not_found" if pattern doesn't exist in
                either scope.
            ValueError: "path_traversal_rejected" for a repo_alias or
                pattern_name containing path traversal sequences.
        """
        for field_name, field_value in [
            ("repo_alias", repo_alias),
            ("pattern_name", pattern_name),
        ]:
            if "/" in field_value or "\\" in field_value or ".." in field_value:
                raise ValueError(
                    f"path_traversal_rejected: {field_name} '{field_value}' "
                    "contains path traversal sequences"
                )

        spec = self._load_pattern(repo_alias, pattern_name)
        if spec is None:
            raise ValueError(
                f"pattern_not_found: pattern '{pattern_name}' not found in "
                f"scope '{repo_alias}' or '__any__'"
            )
        return str(spec.get("execution_mode") or DEFAULT_EXECUTION_MODE)

    def ensure_seed_patterns(self) -> bool:
        """Create the seed patterns (catch-rethrow, deep-nesting) in __any__/
        that do not exist yet, under the catalog write lock as an X-Ray
        pattern writer (piggyback rules apply; a piggybacked write is left
        for the holder's refresh to commit). Idempotent.

        Returns True when every seed is in place; False when an exclusive
        holder refused the write (logged; nothing written): the caller
        retries on its next call."""
        any_dir = self._patterns_root / self.ANY_SCOPE
        seeds = [
            (any_dir / f"{stem}.yaml", content)
            for stem, content in zip(
                self.SEED_PATTERN_NAMES, (_SEED_CATCH_RETHROW, _SEED_DEEP_NESTING)
            )
        ]
        if all(target.exists() for target, _content in seeds):
            return True

        def _write_and_commit(piggyback: bool) -> None:
            created = [target for target, _content in seeds if not target.exists()]
            for target, content in seeds:
                if target in created:
                    self._put_catalog_file(target, content)
            if created and not piggyback:
                self._git_commit_tolerating_failure(
                    files=created,
                    message="xray-patterns: add seed patterns to __any__/",
                )

        try:
            _result, lock_still_held = self._run_with_coarse_lock(
                f"{XRAY_PATTERN_LOCK_OWNER_PREFIX}seed", _write_and_commit
            )
        except CatalogLockedExclusively as exc:
            logger.warning(
                "X-Ray seed patterns not written (%s); retried at the next xray_search",
                exc,
            )
            return False
        self._request_refresh(lock_still_held)
        return True

    # ------------------------------------------------------------------
    # Coarse write-lock coordination (Bug #1037)
    # Mirrors MemoryStoreService._coarse_piggyback_or_acquire /
    # _release_coarse_lock / _run_with_coarse_lock.
    # ------------------------------------------------------------------

    def _coarse_piggyback_or_acquire(self, owner: str) -> bool:
        """Return True if piggybacking (caller must skip acquire/release).

        Piggybacks only on a lock held by a refresh, which publishes the
        write; any other holder (write mode, a restore, another writer)
        holds it exclusively: CatalogLockedExclusively is raised before
        anything is written. When no scheduler is injected, always returns
        False (caller owns lock conceptually and will call _git_commit
        directly).
        """
        if self._scheduler is None:
            return False
        if self._scheduler.acquire_write_lock(_COARSE_ALIAS, owner):
            return False
        require_publishing_holder(self._scheduler)
        return True

    def _release_coarse_lock(self, owner: str) -> bool:
        """Release the coarse write lock; False when it could not be removed.
        No-op (True) when no scheduler is injected."""
        if self._scheduler is None:
            return True
        return bool(self._scheduler.release_write_lock(_COARSE_ALIAS, owner))

    def _run_with_coarse_lock(
        self,
        owner: str,
        operation: Callable[[bool], Any],
    ) -> Tuple[Any, bool]:
        """Acquire coarse lock (or piggyback), run operation, release lock.

        The operation receives a single ``piggyback`` bool argument.  When
        True the operation must skip any git commit (the lock-holder's refresh
        will pick up filesystem changes via ``git add -A``).

        Returns ``(operation result, lock_still_held)``: True when the write
        ran under another owner's lock, or when releasing our own failed --
        a refresh requested now would skip on that lock.
        Guarantees coarse lock release via try/finally when we hold it.
        Operation exceptions propagate; the coarse lock is still released.
        """
        piggyback = self._coarse_piggyback_or_acquire(owner)
        released = True
        try:
            result = operation(piggyback)
        finally:
            if not piggyback:
                released = self._release_coarse_lock(owner)
        if not released:
            logger.warning(
                "xray_pattern_service: cidx-meta write lock release failed for "
                "owner=%r; the refresh is owed to the debouncer",
                owner,
            )
        return result, piggyback or not released

    def _git_commit_tolerating_failure(self, files: List[Path], message: str) -> None:
        """:meth:`_git_commit`, logging a failed commit as a WARNING
        (the seed path, where a missing commit is tolerated)."""
        try:
            self._git_commit(files, message)
        except PatternCommitError as exc:
            logger.warning("xray_pattern_service: git commit failed: %s", exc)

    def _git_commit(self, files: List[Path], message: str) -> None:
        """Add files and create a local git commit in cidx-meta.

        Uses canonical cidx-meta-backup author/committer identity. The commit
        is never pushed here: the catalog refresh the store requests runs the
        backup sync under the exclusive backup lock, the one writer of the
        backup mirror. A cidx-meta that is not a git repository keeps plain
        files: nothing is committed and nothing fails.

        Raises:
            PatternCommitError: git add or git commit failed.
        """
        if not cidx_meta_is_git_repository(self._cidx_meta):
            return
        env = build_non_interactive_git_env()
        env["GIT_AUTHOR_NAME"] = "cidx-meta-backup"
        env["GIT_AUTHOR_EMAIL"] = "cidx-meta-backup@example.invalid"
        env["GIT_COMMITTER_NAME"] = env["GIT_AUTHOR_NAME"]
        env["GIT_COMMITTER_EMAIL"] = env["GIT_AUTHOR_EMAIL"]

        try:
            relative_files = [str(f.relative_to(self._cidx_meta)) for f in files]
            subprocess.run(
                ["git", "add"] + relative_files,
                cwd=str(self._cidx_meta),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(self._cidx_meta),
                env=env,
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            raise PatternCommitError(f"git commit in cidx-meta failed: {exc}") from exc

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_pattern(
        self, repo_alias: str, pattern_name: str
    ) -> Optional[Dict[str, Any]]:
        """Try repo-specific path then __any__ path. Returns parsed spec or None.

        Raises:
            ValueError: "pattern_parse_error" if a candidate file EXISTS but
                is unreadable, fails YAML parsing, or fails schema
                validation (consolidated review, Issue #1811/Bug #1812,
                new finding #5 + R2-5 re-review). A malformed
                repo-specific pattern must surface loudly here rather than
                silently falling through to the __any__ global pattern of
                the same name -- an operator-authored typo in a
                repo-specific override would otherwise be masked by a
                same-named global pattern quietly taking over, with only a
                WARNING log to notice. R2-5 widened this from "YAML syntax
                errors only" to a full schema check: an EMPTY file
                (yaml.safe_load returns None) was previously
                indistinguishable from "file doesn't exist" and silently
                fell through to __any__ as pattern_not_found; a
                list/scalar-shaped YAML document crashed downstream with a
                raw AttributeError; a missing 'evaluator_code' key crashed
                with a raw KeyError; and a wrong-typed 'evaluator_code'
                (e.g. an int) silently proceeded as a bogus evaluator
                value. All four collapse into ONE explicit
                pattern_parse_error here instead.
        """
        candidates = [
            self._patterns_root / repo_alias / f"{pattern_name}.yaml",
            self._patterns_root / self.ANY_SCOPE / f"{pattern_name}.yaml",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError) as exc:
                    raise ValueError(
                        f"pattern_parse_error: pattern file '{path}' exists "
                        f"but could not be read: {exc}"
                    ) from exc
                try:
                    spec = yaml.safe_load(text)
                except yaml.YAMLError as exc:
                    raise ValueError(
                        f"pattern_parse_error: pattern file '{path}' exists "
                        f"but failed to parse as YAML: {exc}"
                    ) from exc
                self._validate_pattern_schema(spec, path)
                return spec  # type: ignore[no-any-return]
        return None

    def _validate_pattern_schema(self, spec: Any, path: Path) -> None:
        """Validate a freshly-parsed pattern spec's structural shape.

        Raises:
            ValueError: "pattern_parse_error" for ANY of: not a YAML
                mapping (list/scalar/None -- including an EMPTY file,
                which yaml.safe_load parses as None), a missing or
                wrong-typed required key (name, evaluator_code -- both
                already required at WRITE time by store_xray_pattern's
                own validation; requiring them again here keeps
                read/write validation symmetric), or a malformed
                'parameters' declaration list (non-list entry, or an
                entry failing _validate_parameter_declarations).
                Collapses every schema-invalid shape into ONE explicit,
                non-fall-through error instead of a raw
                AttributeError/KeyError or a silently-accepted bad value.
        """
        if not isinstance(spec, dict):
            raise ValueError(
                f"pattern_parse_error: pattern file '{path}' does not "
                f"contain a YAML mapping (got {type(spec).__name__})"
            )
        name = spec.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError(
                f"pattern_parse_error: pattern file '{path}' is missing a "
                "non-empty 'name' string"
            )
        evaluator_code = spec.get("evaluator_code")
        if not isinstance(evaluator_code, str) or not evaluator_code:
            raise ValueError(
                f"pattern_parse_error: pattern file '{path}' is missing a "
                "non-empty 'evaluator_code' string"
            )
        params = spec.get("parameters")
        if params is not None:
            if not isinstance(params, list) or any(
                not isinstance(decl, dict) for decl in params
            ):
                raise ValueError(
                    f"pattern_parse_error: pattern file '{path}' has a "
                    "'parameters' field that is not a list of mappings"
                )
            error = validate_parameter_declarations(params, new_store=False)
            if error is not None:
                raise ValueError(
                    f"pattern_parse_error: pattern file '{path}' has an "
                    f"invalid parameter declaration: {error.get('message', error)}"
                )
        execution_mode = spec.get("execution_mode")
        if (
            execution_mode is not None
            and execution_mode not in _ALLOWED_EXECUTION_MODES
        ):
            raise ValueError(
                f"pattern_parse_error: pattern file '{path}' has an invalid "
                f"'execution_mode' value {execution_mode!r} "
                "(must be 'legacy' or 'graph')"
            )
