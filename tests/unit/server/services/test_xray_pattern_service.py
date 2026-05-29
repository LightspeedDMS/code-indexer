"""Unit tests for XrayPatternService — Story #1031.

Tests acceptance criteria AC1-AC10 and AC12 for the persistent xray evaluator
pattern library. AC11 (MCP tool documentation) is verified by verify_tool_docs.py.
Real filesystem used via tmp_path — no mocks of core functionality.
Git operations are mocked because they require a real git repo.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest
import yaml


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

MINIMAL_EVALUATOR = "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }"

MINIMAL_PATTERN_YAML = f"""\
name: my-pattern
description: "Test pattern"
language: java
evaluator_code: |
  {MINIMAL_EVALUATOR}
"""

PATTERN_WITH_PARAMS_YAML = """\
name: deep-nesting
description: "Finds deep nesting"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
      let depth = DEPTH_THRESHOLD;
      let _ = depth;
      vec![]
  }
parameters:
  - name: DEPTH_THRESHOLD
    type: usize
    default: 4
    description: "Minimum nesting depth to flag"
  - name: SNIPPET_MAX
    type: usize
    default: 80
    description: "Max snippet length"
"""

PATTERN_WITH_STR_PARAM_YAML = """\
name: str-pattern
description: "Pattern with str param"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
parameters:
  - name: LABEL
    type: str
    default: hello
    description: "A label"
"""

PATTERN_WITH_BOOL_PARAM_YAML = """\
name: bool-pattern
description: "Pattern with bool param"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
parameters:
  - name: VERBOSE
    type: bool
    default: true
    description: "Verbose output"
"""


def _make_cidx_meta(tmp_path: Path) -> Path:
    """Create a minimal cidx-meta directory structure."""
    cidx_meta = tmp_path / "data" / "golden-repos" / "cidx-meta"
    cidx_meta.mkdir(parents=True, exist_ok=True)
    return cidx_meta


def _import_service():
    from code_indexer.server.services.xray_pattern_service import XrayPatternService

    return XrayPatternService


# ---------------------------------------------------------------------------
# AC1 — Pattern Storage: valid YAML stored to __any__/{name}.yaml
# ---------------------------------------------------------------------------


class TestPatternStorage:
    """AC1: store_xray_pattern saves pattern to cidx-meta/xray-patterns/__any__/{name}.yaml."""

    def test_store_pattern_creates_yaml_file(self, tmp_path: Path) -> None:
        """AC1: Valid pattern stored to expected path."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            result = service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=False,
            )

        assert result["success"] is True
        pattern_file = cidx_meta / "xray-patterns" / "__any__" / "my-pattern.yaml"
        assert pattern_file.exists()

    def test_store_pattern_creates_parent_dirs(self, tmp_path: Path) -> None:
        """AC1: Parent directories created if they don't exist."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=False,
            )

        assert (cidx_meta / "xray-patterns" / "__any__").is_dir()

    def test_store_pattern_content_is_valid_yaml(self, tmp_path: Path) -> None:
        """AC1: Stored file is valid YAML with all fields preserved."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=False,
            )

        pattern_file = cidx_meta / "xray-patterns" / "__any__" / "my-pattern.yaml"
        loaded = yaml.safe_load(pattern_file.read_text())
        assert loaded["name"] == "my-pattern"
        assert loaded["description"] == "Test pattern"
        assert loaded["language"] == "java"
        assert "evaluate_node" in loaded["evaluator_code"]

    def test_store_pattern_with_repo_specific_scope(self, tmp_path: Path) -> None:
        """AC2-related: Repo-specific scope stores to {scope}/{name}.yaml."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="my-repo",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=False,
            )

        pattern_file = cidx_meta / "xray-patterns" / "my-repo" / "my-pattern.yaml"
        assert pattern_file.exists()


# ---------------------------------------------------------------------------
# AC2 — Cross-Repo Patterns: __any__ scope found for any repo
# ---------------------------------------------------------------------------


class TestCrossRepoPatterns:
    """AC2: Patterns in __any__/ are found for any repo alias."""

    def test_any_scope_pattern_resolved_for_unknown_repo(self, tmp_path: Path) -> None:
        """AC2: __any__ pattern found for any repo alias."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=False,
            )

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="some-other-repo",
            pattern_name="my-pattern",
        )
        assert "evaluate_node" in code


# ---------------------------------------------------------------------------
# AC3 — Pattern Resolution Order: repo-specific takes priority over __any__
# ---------------------------------------------------------------------------


class TestPatternResolutionOrder:
    """AC3: Repo-specific pattern takes priority over __any__ pattern."""

    def test_repo_specific_wins_over_any(self, tmp_path: Path) -> None:
        """AC3: Same name in repo-specific and __any__: repo-specific used."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        repo_specific_yaml = """\
name: my-pattern
description: "Repo-specific version"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
      // repo-specific
      vec![]
  }
"""
        any_yaml = """\
name: my-pattern
description: "Any-scope version"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {
      // any scope
      vec![]
  }
"""

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(scope="my-repo", pattern_yaml=repo_specific_yaml)
            service.store_xray_pattern(scope="__any__", pattern_yaml=any_yaml)

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="my-repo",
            pattern_name="my-pattern",
        )
        assert "repo-specific" in code

    def test_any_scope_used_when_no_repo_specific(self, tmp_path: Path) -> None:
        """AC3: Falls back to __any__ when no repo-specific match."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__", pattern_yaml=MINIMAL_PATTERN_YAML
            )

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="other-repo",
            pattern_name="my-pattern",
        )
        assert "evaluate_node" in code


# ---------------------------------------------------------------------------
# AC4 — Overwrite Protection
# ---------------------------------------------------------------------------


class TestOverwriteProtection:
    """AC4: Overwrite=false (default) prevents replacing existing pattern."""

    def test_overwrite_false_raises_on_existing(self, tmp_path: Path) -> None:
        """AC4: Error pattern_already_exists when overwrite=false."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__", pattern_yaml=MINIMAL_PATTERN_YAML
            )
            result = service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=MINIMAL_PATTERN_YAML,
                overwrite=False,
            )

        assert result["error"] == "pattern_already_exists"

    def test_overwrite_true_replaces_existing(self, tmp_path: Path) -> None:
        """AC4: overwrite=true replaces existing pattern."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        updated_yaml = """\
name: my-pattern
description: "Updated description"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__", pattern_yaml=MINIMAL_PATTERN_YAML
            )
            result = service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=updated_yaml,
                overwrite=True,
            )

        assert result["success"] is True
        pattern_file = cidx_meta / "xray-patterns" / "__any__" / "my-pattern.yaml"
        loaded = yaml.safe_load(pattern_file.read_text())
        assert loaded["description"] == "Updated description"


# ---------------------------------------------------------------------------
# AC5 — Pattern Name in Search (mutual exclusivity)
# ---------------------------------------------------------------------------


class TestPatternNameResolution:
    """AC5: pattern_name loads stored code; missing pattern raises error."""

    def test_pattern_not_found_error(self, tmp_path: Path) -> None:
        """AC5: pattern_not_found when pattern_name doesn't exist."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with pytest.raises(ValueError, match="pattern_not_found"):
            service.resolve_and_prepare_pattern(
                repo_alias="some-repo",
                pattern_name="nonexistent",
            )


# ---------------------------------------------------------------------------
# AC6 — Seed Patterns
# ---------------------------------------------------------------------------


class TestSeedPatterns:
    """AC6: ensure_seed_patterns creates catch-rethrow.yaml and deep-nesting.yaml in __any__/."""

    def test_seed_patterns_created_on_first_access(self, tmp_path: Path) -> None:
        """AC6: Seed patterns exist in __any__/ after ensure_seed_patterns()."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.ensure_seed_patterns()

        any_dir = cidx_meta / "xray-patterns" / "__any__"
        assert (any_dir / "catch-rethrow.yaml").exists()
        assert (any_dir / "deep-nesting.yaml").exists()

    def test_seed_pattern_catch_rethrow_is_valid_yaml(self, tmp_path: Path) -> None:
        """AC6: catch-rethrow.yaml has required fields."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.ensure_seed_patterns()

        ct = yaml.safe_load(
            (cidx_meta / "xray-patterns" / "__any__" / "catch-rethrow.yaml").read_text()
        )
        assert ct["name"] == "catch-rethrow"
        assert "evaluate_node" in ct["evaluator_code"]
        assert isinstance(ct.get("parameters", []), list)

    def test_seed_pattern_deep_nesting_has_params(self, tmp_path: Path) -> None:
        """AC6: deep-nesting.yaml has DEPTH_THRESHOLD parameter."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.ensure_seed_patterns()

        dn = yaml.safe_load(
            (cidx_meta / "xray-patterns" / "__any__" / "deep-nesting.yaml").read_text()
        )
        param_names = [p["name"] for p in dn.get("parameters", [])]
        assert "DEPTH_THRESHOLD" in param_names

    def test_seed_patterns_not_overwritten_if_exist(self, tmp_path: Path) -> None:
        """AC6: ensure_seed_patterns is idempotent — won't overwrite existing files."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        any_dir = cidx_meta / "xray-patterns" / "__any__"
        any_dir.mkdir(parents=True, exist_ok=True)
        custom = any_dir / "catch-rethrow.yaml"
        custom.write_text("name: catch-rethrow\ncustom: true\n")

        with patch.object(service, "_git_commit"):
            service.ensure_seed_patterns()

        loaded = yaml.safe_load(custom.read_text())
        assert loaded.get("custom") is True


# ---------------------------------------------------------------------------
# AC7 — Parametrized Pattern Defaults
# ---------------------------------------------------------------------------


class TestParametrizedPatternDefaults:
    """AC7: Pattern with parameters prepends const declarations with defaults."""

    def test_default_params_prepended_as_consts(self, tmp_path: Path) -> None:
        """AC7: Default parameter values become const lines before evaluator code."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="deep-nesting",
            pattern_params={},
        )
        assert "const DEPTH_THRESHOLD: usize = 4;" in code
        assert "const SNIPPET_MAX: usize = 80;" in code

    def test_consts_appear_before_evaluator_code(self, tmp_path: Path) -> None:
        """AC7: Const declarations are prepended before the evaluator fn."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="deep-nesting",
        )
        const_pos = code.index("const DEPTH_THRESHOLD")
        fn_pos = code.index("fn evaluate_node")
        assert const_pos < fn_pos


# ---------------------------------------------------------------------------
# AC8 — Parameter Override
# ---------------------------------------------------------------------------


class TestParameterOverride:
    """AC8: pattern_params overrides default values."""

    def test_override_replaces_default_value(self, tmp_path: Path) -> None:
        """AC8: pattern_params={"DEPTH_THRESHOLD": 6} produces usize = 6."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="deep-nesting",
            pattern_params={"DEPTH_THRESHOLD": 6},
        )
        assert "const DEPTH_THRESHOLD: usize = 6;" in code

    def test_unoverridden_param_uses_default(self, tmp_path: Path) -> None:
        """AC8: Only specified param is overridden; others keep defaults."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        code, _params = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="deep-nesting",
            pattern_params={"DEPTH_THRESHOLD": 6},
        )
        assert "const SNIPPET_MAX: usize = 80;" in code


# ---------------------------------------------------------------------------
# AC9 — Invalid Parameter
# ---------------------------------------------------------------------------


class TestInvalidParameter:
    """AC9: Unknown parameter name raises unknown_parameter error."""

    def test_unknown_param_raises_error(self, tmp_path: Path) -> None:
        """AC9: pattern_params with unknown name raises ValueError unknown_parameter."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        with pytest.raises(ValueError, match="unknown_parameter"):
            service.resolve_and_prepare_pattern(
                repo_alias="any-repo",
                pattern_name="deep-nesting",
                pattern_params={"NONEXISTENT_PARAM": 99},
            )


# ---------------------------------------------------------------------------
# AC10 — Type Validation
# ---------------------------------------------------------------------------


class TestTypeValidation:
    """AC10: Type mismatch raises parameter_type_mismatch error."""

    def test_type_mismatch_raises_error(self, tmp_path: Path) -> None:
        """AC10: usize param given string raises ValueError parameter_type_mismatch."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        with pytest.raises(ValueError, match="parameter_type_mismatch"):
            service.resolve_and_prepare_pattern(
                repo_alias="any-repo",
                pattern_name="deep-nesting",
                pattern_params={"DEPTH_THRESHOLD": "not_a_number"},
            )

    def test_bool_param_type_validation(self, tmp_path: Path) -> None:
        """AC10: bool param given non-bool raises parameter_type_mismatch."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_BOOL_PARAM_YAML,
            )

        with pytest.raises(ValueError, match="parameter_type_mismatch"):
            service.resolve_and_prepare_pattern(
                repo_alias="any-repo",
                pattern_name="bool-pattern",
                pattern_params={"VERBOSE": "yes"},
            )


# ---------------------------------------------------------------------------
# AC12 — Cache Key Includes Parameters (SHA-256 of prepared code differs)
# ---------------------------------------------------------------------------


class TestCacheKeyIncludesParameters:
    """AC12: Different pattern_params produce different prepared code (different SHA-256)."""

    def test_different_params_produce_different_code(self, tmp_path: Path) -> None:
        """AC12: SHA-256 differs when params differ."""
        import hashlib

        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_PARAMS_YAML,
            )

        code4, _ = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="deep-nesting",
            pattern_params={"DEPTH_THRESHOLD": 4},
        )
        code6, _ = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="deep-nesting",
            pattern_params={"DEPTH_THRESHOLD": 6},
        )

        sha4 = hashlib.sha256(code4.encode()).hexdigest()
        sha6 = hashlib.sha256(code6.encode()).hexdigest()
        assert sha4 != sha6


# ---------------------------------------------------------------------------
# Validation: Missing required fields
# ---------------------------------------------------------------------------


class TestPatternValidation:
    """Pattern YAML validation: missing required fields rejected."""

    def test_missing_name_rejected(self, tmp_path: Path) -> None:
        """Validation: pattern without 'name' field returns error."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        bad_yaml = (
            "description: test\n"
            "language: java\n"
            "evaluator_code: |\n"
            "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
        )
        result = service.store_xray_pattern(scope="__any__", pattern_yaml=bad_yaml)
        assert result.get("error") is not None

    def test_missing_evaluator_code_rejected(self, tmp_path: Path) -> None:
        """Validation: pattern without 'evaluator_code' field returns error."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        bad_yaml = "name: test\ndescription: test\nlanguage: java\n"
        result = service.store_xray_pattern(scope="__any__", pattern_yaml=bad_yaml)
        assert result.get("error") is not None

    def test_invalid_evaluator_code_rejected(self, tmp_path: Path) -> None:
        """Validation: evaluator_code failing Rust validation returns error."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        bad_yaml = (
            "name: bad-pattern\n"
            "description: Uses unsafe\n"
            "language: java\n"
            "evaluator_code: |\n"
            "  unsafe fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
        )
        result = service.store_xray_pattern(scope="__any__", pattern_yaml=bad_yaml)
        assert result.get("error") is not None

    def test_invalid_parameter_type_rejected(self, tmp_path: Path) -> None:
        """Validation: parameter with unknown type is rejected."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        bad_yaml = (
            "name: bad-params\n"
            "description: Bad param type\n"
            "language: java\n"
            "evaluator_code: |\n"
            "  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }\n"
            "parameters:\n"
            "  - name: MY_PARAM\n"
            "    type: invalid_type\n"
            "    default: 42\n"
        )
        result = service.store_xray_pattern(scope="__any__", pattern_yaml=bad_yaml)
        assert result.get("error") is not None


# ---------------------------------------------------------------------------
# Const generation for different parameter types
# ---------------------------------------------------------------------------


class TestConstGeneration:
    """Const lines are generated correctly for all parameter types."""

    def test_str_param_generates_str_const(self, tmp_path: Path) -> None:
        """str param generates: const NAME: &str = "value";"""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_STR_PARAM_YAML,
            )

        code, _ = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="str-pattern",
        )
        assert 'const LABEL: &str = "hello";' in code

    def test_bool_param_generates_bool_const(self, tmp_path: Path) -> None:
        """bool param generates: const NAME: bool = true/false;"""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        with patch.object(service, "_git_commit"):
            service.store_xray_pattern(
                scope="__any__",
                pattern_yaml=PATTERN_WITH_BOOL_PARAM_YAML,
            )

        code, _ = service.resolve_and_prepare_pattern(
            repo_alias="any-repo",
            pattern_name="bool-pattern",
        )


# ---------------------------------------------------------------------------
# Security: Path Traversal Rejection
# ---------------------------------------------------------------------------


class TestPathTraversal:
    """Finding 1: Path traversal in scope/name must be rejected before any
    filesystem access."""

    def test_scope_with_dot_dot_rejected(self, tmp_path: Path) -> None:
        """Scope containing '..' is rejected with path_traversal_rejected."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        result = service.store_xray_pattern(
            scope="../../..",
            pattern_yaml=MINIMAL_PATTERN_YAML,
        )
        assert result.get("error") == "path_traversal_rejected"

    def test_scope_with_forward_slash_rejected(self, tmp_path: Path) -> None:
        """Scope containing '/' is rejected with path_traversal_rejected."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        result = service.store_xray_pattern(
            scope="valid/scope",
            pattern_yaml=MINIMAL_PATTERN_YAML,
        )
        assert result.get("error") == "path_traversal_rejected"

    def test_scope_with_backslash_rejected(self, tmp_path: Path) -> None:
        """Scope containing backslash is rejected with path_traversal_rejected."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        result = service.store_xray_pattern(
            scope="scope\\evil",
            pattern_yaml=MINIMAL_PATTERN_YAML,
        )
        assert result.get("error") == "path_traversal_rejected"

    def test_name_with_dot_dot_rejected(self, tmp_path: Path) -> None:
        """Pattern name containing '..' is rejected with invalid_pattern_name."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        traversal_yaml = """\
name: ../../../etc/passwd
description: "Evil"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""
        result = service.store_xray_pattern(
            scope="__any__",
            pattern_yaml=traversal_yaml,
        )
        assert result.get("error") == "invalid_pattern_name"

    def test_name_with_slash_rejected(self, tmp_path: Path) -> None:
        """Pattern name containing '/' is rejected with invalid_pattern_name."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        traversal_yaml = """\
name: sub/evil-name
description: "Evil slash in name"
language: java
evaluator_code: |
  fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }
"""
        result = service.store_xray_pattern(
            scope="__any__",
            pattern_yaml=traversal_yaml,
        )
        assert result.get("error") == "invalid_pattern_name"

    def test_resolve_and_prepare_rejects_traversal_alias(self, tmp_path: Path) -> None:
        """resolve_and_prepare_pattern raises ValueError when repo_alias
        contains path traversal sequences."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        import pytest as _pytest

        with _pytest.raises(ValueError, match="path_traversal_rejected"):
            service.resolve_and_prepare_pattern(
                repo_alias="../../etc",
                pattern_name="catch-rethrow",
            )

    def test_resolve_and_prepare_rejects_traversal_pattern_name(
        self, tmp_path: Path
    ) -> None:
        """resolve_and_prepare_pattern raises ValueError when pattern_name
        contains path traversal sequences."""
        XrayPatternService = _import_service()
        cidx_meta = _make_cidx_meta(tmp_path)
        service = XrayPatternService(cidx_meta)

        import pytest as _pytest

        with _pytest.raises(ValueError, match="path_traversal_rejected"):
            service.resolve_and_prepare_pattern(
                repo_alias="my-repo",
                pattern_name="../../../etc/passwd",
            )
