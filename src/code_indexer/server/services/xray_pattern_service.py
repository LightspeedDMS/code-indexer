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
    evaluator_code: str   (required, Rust fn evaluate_node code)
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
from typing import Any, Dict, List, Optional, Tuple

import yaml

from code_indexer.xray.sandbox import validate_rust_evaluator

logger = logging.getLogger(__name__)

# Allowed parameter types
_ALLOWED_PARAM_TYPES = frozenset({"usize", "i64", "f64", "bool", "str"})

# Required top-level fields in a pattern YAML
_REQUIRED_FIELDS = ("name", "description", "language", "evaluator_code")


# ---------------------------------------------------------------------------
# Seed pattern content
# ---------------------------------------------------------------------------

_SEED_CATCH_RETHROW = """\
name: catch-rethrow
description: "Detects empty catch blocks that just rethrow the exception"
language: java
tags: [error-handling, anti-pattern]
author: cidx
created_at: "2026-05-28"
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
      let mut findings = Vec::new();
      find_catch_blocks(node, &mut findings);
      findings
  }

  fn find_catch_blocks(node: &OwnedNode, findings: &mut Vec<EvalFinding>) {
      let is_catch = matches!(
          node.kind.as_str(),
          "catch_clause" | "catch_block" | "except_clause"
      );
      if is_catch {
          classify_catch(node, findings);
      }
      for child in &node.children {
          find_catch_blocks(child, findings);
      }
  }

  fn classify_catch(node: &OwnedNode, findings: &mut Vec<EvalFinding>) {
      let body = node.child_by_kind("block")
          .or_else(|| node.child_by_kind("catch_body"));
      let body = match body {
          Some(b) => b,
          None => {
              findings.push(EvalFinding {
                  pattern: "empty-catch".to_string(),
                  line: node.start_line,
                  snippet: truncate_snippet(node.text(), SNIPPET_MAX),
              });
              return;
          }
      };
      let stmts: Vec<&OwnedNode> = body.named_children()
          .into_iter()
          .filter(|c| c.kind != "comment" && c.kind != "line_comment"
                    && c.kind != "block_comment")
          .collect();
      if stmts.is_empty() {
          findings.push(EvalFinding {
              pattern: "empty-catch".to_string(),
              line: node.start_line,
              snippet: truncate_snippet(node.text(), SNIPPET_MAX),
          });
          return;
      }
      let has_throw = body.has_descendant_of_kind("throw_statement")
          || body.has_descendant_of_kind("throw_expression");
      if has_throw && stmts.len() == 1 {
          findings.push(EvalFinding {
              pattern: "catch-rethrow".to_string(),
              line: node.start_line,
              snippet: truncate_snippet(node.text(), SNIPPET_MAX),
          });
      } else if has_throw && stmts.len() <= 3 {
          let body_text = body.text();
          let has_log_call = body_text.contains(".log")
              || body_text.contains(".warn")
              || body_text.contains(".error")
              || body_text.contains("LOG.")
              || body_text.contains("logger.");
          if has_log_call {
              findings.push(EvalFinding {
                  pattern: "catch-log-rethrow".to_string(),
                  line: node.start_line,
                  snippet: truncate_snippet(node.text(), SNIPPET_MAX),
              });
          }
      }
  }
parameters:
  - name: SNIPPET_MAX
    type: usize
    default: 80
    description: "Maximum character length for code snippets in findings"
"""

_SEED_DEEP_NESTING = """\
name: deep-nesting
description: "Finds control flow nested N+ levels deep (if/for/while/switch/when)"
language: java
tags: [complexity, code-quality]
author: cidx
created_at: "2026-05-28"
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
      let mut findings = Vec::new();
      walk_nesting(node, 0, &mut findings);
      findings
  }

  fn is_control_flow(kind: &str) -> bool {
      matches!(
          kind,
          "if_statement"
              | "if_expression"
              | "for_statement"
              | "enhanced_for_statement"
              | "while_statement"
              | "do_statement"
              | "switch_expression"
              | "switch_statement"
              | "when_expression"
              | "for_in_statement"
              | "for_of_statement"
      )
  }

  fn walk_nesting(node: &OwnedNode, depth: usize, findings: &mut Vec<EvalFinding>) {
      let new_depth = if is_control_flow(&node.kind) {
          depth + 1
      } else {
          depth
      };
      if new_depth >= DEPTH_THRESHOLD && is_control_flow(&node.kind) {
          let first_line = node.text().lines().next().unwrap_or("");
          findings.push(EvalFinding {
              pattern: "deep-nesting".to_string(),
              line: node.start_line,
              snippet: truncate_snippet(
                  &format!("[depth {}] {}", new_depth, first_line),
                  SNIPPET_MAX,
              ),
          });
      }
      for child in &node.children {
          walk_nesting(child, new_depth, findings);
      }
  }
parameters:
  - name: DEPTH_THRESHOLD
    type: usize
    default: 4
    description: "Minimum nesting depth to flag"
  - name: SNIPPET_MAX
    type: usize
    default: 80
    description: "Maximum character length for code snippets in findings"
"""


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
    """

    PATTERNS_DIR = "xray-patterns"
    ANY_SCOPE = "__any__"

    def __init__(self, cidx_meta_path: Path) -> None:
        self._cidx_meta = Path(cidx_meta_path)

    @property
    def _patterns_root(self) -> Path:
        return self._cidx_meta / self.PATTERNS_DIR

    def store_xray_pattern(
        self,
        scope: str,
        pattern_yaml: str,
        overwrite: bool = False,
    ) -> Dict[str, Any]:
        """Store a pattern to cidx-meta/xray-patterns/{scope}/{name}.yaml.

        Args:
            scope: Target scope — "__any__" for cross-repo or a repo alias.
            pattern_yaml: YAML string conforming to the PatternSpec schema.
            overwrite: When False (default), returns error if pattern exists.

        Returns:
            Dict with {"success": True} on success, or
            {"error": "<code>", "message": "<detail>"} on failure.
        """
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
        param_validation = self._validate_parameter_declarations(params)
        if param_validation is not None:
            return param_validation

        # 5. Resolve target path
        target_dir = self._patterns_root / scope
        target_path = target_dir / f"{name}.yaml"

        # 6. Capture existence BEFORE writing, then check overwrite protection
        is_update = target_path.exists()
        if is_update and not overwrite:
            return {
                "error": "pattern_already_exists",
                "message": (
                    f"Pattern '{name}' already exists in scope '{scope}'. "
                    "Use overwrite=true to replace it."
                ),
            }

        # 7. Write YAML
        target_dir.mkdir(parents=True, exist_ok=True)
        target_path.write_text(pattern_yaml, encoding="utf-8")

        # 8. Git commit
        action = "update" if is_update else "add"
        self._git_commit(
            files=[target_path],
            message=f"xray-patterns: {action} {scope}/{name}",
        )

        return {"success": True, "path": str(target_path)}

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
        resolved = self._resolve_params(params_decls, pattern_params)

        # Build const lines and prepend before evaluator code
        const_lines = self._build_const_lines(params_decls, resolved)
        if const_lines:
            prepared_code = const_lines + "\n" + evaluator_code
        else:
            prepared_code = evaluator_code

        return prepared_code, resolved

    def ensure_seed_patterns(self) -> None:
        """Create seed patterns in __any__/ if they don't already exist.

        Creates:
            - catch-rethrow.yaml
            - deep-nesting.yaml

        Idempotent: skips files that already exist.
        """
        any_dir = self._patterns_root / self.ANY_SCOPE
        any_dir.mkdir(parents=True, exist_ok=True)

        created: List[Path] = []

        for stem, content in (
            ("catch-rethrow", _SEED_CATCH_RETHROW),
            ("deep-nesting", _SEED_DEEP_NESTING),
        ):
            target = any_dir / f"{stem}.yaml"
            if not target.exists():
                target.write_text(content, encoding="utf-8")
                created.append(target)

        if created:
            self._git_commit(
                files=created,
                message="xray-patterns: add seed patterns to __any__/",
            )

    def _git_commit(self, files: List[Path], message: str) -> None:
        """Add files and create a git commit in cidx-meta.

        Args:
            files: List of absolute paths to git-add.
            message: Commit message.
        """
        try:
            relative_files = [str(f.relative_to(self._cidx_meta)) for f in files]
            subprocess.run(
                ["git", "add"] + relative_files,
                cwd=str(self._cidx_meta),
                check=True,
                capture_output=True,
                text=True,
            )
            subprocess.run(
                ["git", "commit", "-m", message],
                cwd=str(self._cidx_meta),
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as exc:
            logger.warning("xray_pattern_service: git commit failed: %s", exc)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _load_pattern(
        self, repo_alias: str, pattern_name: str
    ) -> Optional[Dict[str, Any]]:
        """Try repo-specific path then __any__ path. Returns parsed spec or None."""
        candidates = [
            self._patterns_root / repo_alias / f"{pattern_name}.yaml",
            self._patterns_root / self.ANY_SCOPE / f"{pattern_name}.yaml",
        ]
        for path in candidates:
            if path.is_file():
                try:
                    return yaml.safe_load(path.read_text(encoding="utf-8"))  # type: ignore[return-value]
                except yaml.YAMLError:
                    logger.warning("xray_pattern_service: failed to parse %s", path)
        return None

    def _validate_parameter_declarations(
        self, params: List[Dict[str, Any]]
    ) -> Optional[Dict[str, Any]]:
        """Validate parameter declarations. Returns error dict or None if valid."""
        for decl in params:
            if not decl.get("name"):
                return {
                    "error": "invalid_parameter",
                    "message": "Each parameter must have a 'name' field",
                }
            param_type = decl.get("type", "")
            if param_type not in _ALLOWED_PARAM_TYPES:
                return {
                    "error": "invalid_parameter_type",
                    "message": (
                        f"Parameter type '{param_type}' is not allowed. "
                        f"Allowed types: {sorted(_ALLOWED_PARAM_TYPES)}"
                    ),
                }
            if "default" not in decl:
                return {
                    "error": "invalid_parameter",
                    "message": f"Parameter '{decl['name']}' must have a 'default' value",
                }
        return None

    def _resolve_params(
        self,
        decls: List[Dict[str, Any]],
        overrides: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Merge declared defaults with caller overrides, validating types.

        Raises:
            ValueError: "unknown_parameter" for unrecognized override keys.
            ValueError: "parameter_type_mismatch" for type violations.
        """
        declared_names = {d["name"] for d in decls}

        for key in overrides:
            if key not in declared_names:
                raise ValueError(
                    f"unknown_parameter: '{key}' is not a declared parameter. "
                    f"Declared parameters: {sorted(declared_names)}"
                )

        resolved: Dict[str, Any] = {}
        for decl in decls:
            name = decl["name"]
            param_type = decl["type"]
            default = decl["default"]
            value = overrides.get(name, default)
            _validate_param_type(name, param_type, value)
            resolved[name] = value

        return resolved

    def _build_const_lines(
        self,
        decls: List[Dict[str, Any]],
        resolved: Dict[str, Any],
    ) -> str:
        """Generate Rust const declarations from resolved parameter values.

        Returns empty string when there are no parameters.
        """
        if not decls:
            return ""

        lines: List[str] = []
        for decl in decls:
            name = decl["name"]
            param_type = decl["type"]
            value = resolved[name]
            lines.append(_format_const_line(name, param_type, value))

        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _validate_param_type(name: str, param_type: str, value: Any) -> None:
    """Raise ValueError with 'parameter_type_mismatch' if value doesn't match type.

    Raises:
        ValueError: With "parameter_type_mismatch" prefix.
    """
    if param_type == "bool":
        if not isinstance(value, bool):
            raise ValueError(
                f"parameter_type_mismatch: parameter '{name}' expects bool, "
                f"got {type(value).__name__} ({value!r})"
            )
    elif param_type == "str":
        if not isinstance(value, str):
            raise ValueError(
                f"parameter_type_mismatch: parameter '{name}' expects str, "
                f"got {type(value).__name__} ({value!r})"
            )
    elif param_type in ("usize", "i64"):
        # bool is a subclass of int — reject it explicitly
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"parameter_type_mismatch: parameter '{name}' expects {param_type} (integer), "
                f"got {type(value).__name__} ({value!r})"
            )
    elif param_type == "f64":
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(
                f"parameter_type_mismatch: parameter '{name}' expects f64 (number), "
                f"got {type(value).__name__} ({value!r})"
            )


def _format_const_line(name: str, param_type: str, value: Any) -> str:
    """Format a single Rust const declaration line.

    Returns e.g. "const DEPTH_THRESHOLD: usize = 4;"
    """
    rust_type = "&str" if param_type == "str" else param_type

    if param_type == "str":
        rust_value = f'"{value}"'
    elif param_type == "bool":
        rust_value = "true" if value else "false"
    else:
        rust_value = str(value)

    return f"const {name}: {rust_type} = {rust_value};"
