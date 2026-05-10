"""
Unit tests for Story #988 MCP Tool Surface Compression infrastructure scripts.

Scripts under test (all in scripts/mcp/):
  - snapshot_tool_doc_corpus.py
  - verify_slim_coverage.py
  - verify_inputschema_preserved.py
  - generate_inputschema_fingerprint.py
  - apply_slim_descriptions.py

Tests use tmp_path fixtures for fully isolated, self-contained test runs.
No network, no database, no external dependencies beyond yaml (project dep).
"""

import importlib.util
import json
import re
from pathlib import Path
from typing import Any, Dict

import yaml

# Project root so we can locate script files
_PROJECT_ROOT = Path(__file__).parents[3]
_SCRIPTS_MCP = _PROJECT_ROOT / "scripts" / "mcp"

# ---------------------------------------------------------------------------
# Import helpers
# ---------------------------------------------------------------------------


def _import_script(name: str) -> Any:
    """Import a script from scripts/mcp/ by stem name."""
    script_path = _SCRIPTS_MCP / f"{name}.py"
    assert script_path.exists(), f"Script not found: {script_path}"
    spec = importlib.util.spec_from_file_location(name, script_path)
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Fixture helpers - build synthetic tool_docs trees in tmp_path
# ---------------------------------------------------------------------------

_SIMPLE_FRONTMATTER = """\
---
name: {name}
category: {category}
required_permission: query_repos
tl_dr: A test tool for {name}.
inputSchema:
  type: object
  properties:
    query:
      type: string
  required: []
---
This is the body for {name}.
"""

_GUIDE_FRONTMATTER = """\
---
name: {name}
category: guides
required_permission: query_repos
tl_dr: A guide doc for {name}.
---
Guide body for {name}.
"""

_SLIM_FRONTMATTER = """\
---
name: {name}
category: {category}
required_permission: query_repos
tl_dr: A test tool for {name}.
slim_description: "Slim text for {name}."
inputSchema:
  type: object
  properties:
    query:
      type: string
  required: []
---
Body for {name}.
"""


def _make_tool_doc(
    root: Path,
    category: str,
    name: str,
    *,
    include_input_schema: bool = True,
    include_slim: bool = False,
) -> Path:
    """Create a synthetic tool doc file under root/category/name.md."""
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    path = cat_dir / f"{name}.md"
    if include_slim:
        content = _SLIM_FRONTMATTER.format(name=name, category=category)
    elif include_input_schema:
        content = _SIMPLE_FRONTMATTER.format(name=name, category=category)
    else:
        content = _GUIDE_FRONTMATTER.format(name=name, category=category)
    path.write_text(content, encoding="utf-8")
    return path


def _make_excluded_file(root: Path, category: str, name: str) -> Path:
    """Create a _ prefixed file that must be excluded from walks."""
    cat_dir = root / category
    cat_dir.mkdir(parents=True, exist_ok=True)
    path = cat_dir / f"_{name}.md"
    path.write_text(
        "---\nname: _excluded\ncategory: test\n---\nshould be excluded\n",
        encoding="utf-8",
    )
    return path


def _build_snapshot_json(tool_docs_root: Path, branch_sha: str = "abc1234") -> Dict:
    """Build a minimal manifest dict matching snapshot_tool_doc_corpus output format."""
    tools = []
    for md_file in sorted(tool_docs_root.rglob("*.md")):
        if md_file.name.startswith("_"):
            continue
        raw = md_file.read_text(encoding="utf-8")
        parts = raw.split("---", 2)
        if len(parts) < 3:
            continue
        fm = yaml.safe_load(parts[1])
        if not fm or "name" not in fm:
            continue
        tools.append(
            {
                "name": fm["name"],
                "category": fm.get("category", ""),
                "path": str(md_file),
            }
        )
    tools.sort(key=lambda t: t["name"])
    return {
        "branch_cut_sha": branch_sha,
        "tool_count": len(tools),
        "tools": tools,
    }


# ===========================================================================
# 1. snapshot_tool_doc_corpus tests
# ===========================================================================


class TestSnapshotToolDocCorpus:
    """Tests for scripts/mcp/snapshot_tool_doc_corpus.py"""

    def test_script_file_exists(self):
        """The script must exist at scripts/mcp/snapshot_tool_doc_corpus.py"""
        script = _SCRIPTS_MCP / "snapshot_tool_doc_corpus.py"
        assert script.exists(), f"Script not found at {script}"

    def test_produces_valid_json_with_required_keys(self, tmp_path):
        """snapshot_corpus() must return dict with branch_cut_sha, tool_count, tools."""
        _make_tool_doc(tmp_path, "search", "tool_alpha")
        _make_tool_doc(tmp_path, "search", "tool_beta")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        assert isinstance(result, dict), "Must return a dict"
        assert "branch_cut_sha" in result, "Must have 'branch_cut_sha' key"
        assert "tool_count" in result, "Must have 'tool_count' key"
        assert "tools" in result, "Must have 'tools' key"

    def test_tool_count_matches_tools_list(self, tmp_path):
        """tool_count must equal len(tools)."""
        _make_tool_doc(tmp_path, "search", "alpha_tool")
        _make_tool_doc(tmp_path, "search", "beta_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        assert result["tool_count"] == len(result["tools"])

    def test_tools_sorted_alphabetically_by_name(self, tmp_path):
        """Tools in the output must be sorted alphabetically by name."""
        _make_tool_doc(tmp_path, "search", "zebra_tool")
        _make_tool_doc(tmp_path, "search", "alpha_tool")
        _make_tool_doc(tmp_path, "search", "mango_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        names = [t["name"] for t in result["tools"]]
        assert names == sorted(names), f"Tools not sorted alphabetically: {names}"

    def test_excludes_underscore_prefixed_files(self, tmp_path):
        """Files starting with _ must be excluded from the corpus."""
        _make_tool_doc(tmp_path, "search", "visible_tool")
        _make_excluded_file(tmp_path, "search", "hidden_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        names = [t["name"] for t in result["tools"]]
        assert "visible_tool" in names, "visible_tool must be included"
        # The excluded file should NOT produce a tool entry named '_excluded' or 'hidden_tool'
        assert "_excluded" not in names, "_ prefixed file must be excluded"
        assert len(names) == 1, f"Only 1 visible tool expected, got: {names}"

    def test_each_tool_entry_has_required_fields(self, tmp_path):
        """Each tool entry must have name, category, path fields."""
        _make_tool_doc(tmp_path, "search", "test_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        assert len(result["tools"]) == 1
        tool = result["tools"][0]
        assert "name" in tool, "Tool entry must have 'name'"
        assert "category" in tool, "Tool entry must have 'category'"
        assert "path" in tool, "Tool entry must have 'path'"

    def test_branch_cut_sha_is_string(self, tmp_path):
        """branch_cut_sha must be a non-empty string."""
        _make_tool_doc(tmp_path, "search", "any_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        sha = result["branch_cut_sha"]
        assert isinstance(sha, str), "branch_cut_sha must be a string"
        assert len(sha) > 0, "branch_cut_sha must be non-empty"

    def test_main_prints_json_to_stdout(self, tmp_path, capsys):
        """main() must print valid JSON to stdout."""
        _make_tool_doc(tmp_path, "search", "stdout_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        # Call main with a custom tool_docs_root so it uses tmp_path
        mod.main(tool_docs_root=tmp_path)
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert "branch_cut_sha" in parsed
        assert "tool_count" in parsed
        assert "tools" in parsed

    def test_multi_category_discovery(self, tmp_path):
        """Tools in multiple subdirectories must all be discovered."""
        _make_tool_doc(tmp_path, "search", "search_tool")
        _make_tool_doc(tmp_path, "admin", "admin_tool")
        _make_tool_doc(tmp_path, "git", "git_tool")
        mod = _import_script("snapshot_tool_doc_corpus")
        result = mod.snapshot_corpus(tool_docs_root=tmp_path)
        names = [t["name"] for t in result["tools"]]
        assert "search_tool" in names
        assert "admin_tool" in names
        assert "git_tool" in names


# ===========================================================================
# 2. verify_slim_coverage tests
# ===========================================================================


class TestVerifySlimCoverage:
    """Tests for scripts/mcp/verify_slim_coverage.py"""

    def test_script_file_exists(self):
        """The script must exist at scripts/mcp/verify_slim_coverage.py"""
        script = _SCRIPTS_MCP / "verify_slim_coverage.py"
        assert script.exists(), f"Script not found at {script}"

    def _write_manifest(self, tmp_path: Path, tools: list) -> Path:
        """Write a manifest JSON file and return its path."""
        manifest_path = tmp_path / "slim_manifest_pre_s3.json"
        manifest = {
            "branch_cut_sha": "abc1234",
            "tool_count": len(tools),
            "tools": tools,
        }
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
        return manifest_path

    def test_returns_zero_when_all_have_slim_description(self, tmp_path):
        """Exit code 0 when all tools in manifest have slim_description."""
        # Create tool doc files with slim_description
        doc_path = _make_tool_doc(tmp_path, "search", "tool_one", include_slim=True)
        tools = [
            {"name": "tool_one", "category": "search", "path": str(doc_path)},
        ]
        manifest_path = self._write_manifest(tmp_path, tools)
        mod = _import_script("verify_slim_coverage")
        exit_code = mod.verify_coverage(manifest_path=manifest_path)
        assert exit_code == 0, f"Expected exit code 0 but got {exit_code}"

    def test_returns_one_when_some_missing_slim_description(self, tmp_path):
        """Exit code 1 when any tool is missing slim_description."""
        # tool_one has slim, tool_two does not
        doc_one = _make_tool_doc(tmp_path, "search", "tool_one", include_slim=True)
        doc_two = _make_tool_doc(tmp_path, "search", "tool_two", include_slim=False)
        tools = [
            {"name": "tool_one", "category": "search", "path": str(doc_one)},
            {"name": "tool_two", "category": "search", "path": str(doc_two)},
        ]
        manifest_path = self._write_manifest(tmp_path, tools)
        mod = _import_script("verify_slim_coverage")
        exit_code = mod.verify_coverage(manifest_path=manifest_path)
        assert exit_code == 1, f"Expected exit code 1 but got {exit_code}"

    def test_handles_empty_manifest(self, tmp_path):
        """Empty manifest (zero tools) should return exit code 0 (vacuous pass)."""
        manifest_path = self._write_manifest(tmp_path, [])
        mod = _import_script("verify_slim_coverage")
        exit_code = mod.verify_coverage(manifest_path=manifest_path)
        assert exit_code == 0, "Empty manifest must return exit 0 (vacuous truth)"

    def test_prints_summary_with_pass(self, tmp_path, capsys):
        """Prints 'N/M tools have slim_description. PASS' when 100% coverage."""
        doc_path = _make_tool_doc(tmp_path, "search", "pass_tool", include_slim=True)
        tools = [{"name": "pass_tool", "category": "search", "path": str(doc_path)}]
        manifest_path = self._write_manifest(tmp_path, tools)
        mod = _import_script("verify_slim_coverage")
        mod.verify_coverage(manifest_path=manifest_path)
        captured = capsys.readouterr()
        assert "PASS" in captured.out, f"Expected PASS in output: {captured.out!r}"

    def test_prints_summary_with_fail_and_missing_names(self, tmp_path, capsys):
        """Prints FAIL and lists missing tool names when coverage is incomplete."""
        doc_one = _make_tool_doc(tmp_path, "search", "ok_tool", include_slim=True)
        doc_two = _make_tool_doc(tmp_path, "search", "missing_tool", include_slim=False)
        tools = [
            {"name": "ok_tool", "category": "search", "path": str(doc_one)},
            {"name": "missing_tool", "category": "search", "path": str(doc_two)},
        ]
        manifest_path = self._write_manifest(tmp_path, tools)
        mod = _import_script("verify_slim_coverage")
        mod.verify_coverage(manifest_path=manifest_path)
        captured = capsys.readouterr()
        assert "FAIL" in captured.out, f"Expected FAIL in output: {captured.out!r}"
        assert "missing_tool" in captured.out, (
            f"Missing tool name not listed: {captured.out!r}"
        )

    def test_all_missing_returns_one(self, tmp_path):
        """All tools missing slim_description must return exit code 1."""
        doc_a = _make_tool_doc(tmp_path, "search", "tool_a", include_slim=False)
        doc_b = _make_tool_doc(tmp_path, "search", "tool_b", include_slim=False)
        tools = [
            {"name": "tool_a", "category": "search", "path": str(doc_a)},
            {"name": "tool_b", "category": "search", "path": str(doc_b)},
        ]
        manifest_path = self._write_manifest(tmp_path, tools)
        mod = _import_script("verify_slim_coverage")
        exit_code = mod.verify_coverage(manifest_path=manifest_path)
        assert exit_code == 1


# ===========================================================================
# 3. verify_inputschema_preserved tests
# ===========================================================================


class TestVerifyInputSchemaPreserved:
    """Tests for scripts/mcp/verify_inputschema_preserved.py"""

    def test_script_file_exists(self):
        """The script must exist at scripts/mcp/verify_inputschema_preserved.py"""
        script = _SCRIPTS_MCP / "verify_inputschema_preserved.py"
        assert script.exists(), f"Script not found at {script}"

    def test_returns_zero_when_all_inputschemas_present(self, tmp_path):
        """Exit 0 when every tool doc with inputSchema has a valid non-null dict."""
        _make_tool_doc(tmp_path, "search", "tool_ok")
        mod = _import_script("verify_inputschema_preserved")
        exit_code = mod.verify_inputschemas(tool_docs_root=tmp_path)
        assert exit_code == 0, f"Expected 0 but got {exit_code}"

    def test_returns_zero_for_guides_without_inputschema(self, tmp_path):
        """Exit 0 when guide docs lack inputSchema (expected/allowed)."""
        _make_tool_doc(tmp_path, "guides", "guide_one", include_input_schema=False)
        mod = _import_script("verify_inputschema_preserved")
        exit_code = mod.verify_inputschemas(tool_docs_root=tmp_path)
        assert exit_code == 0, (
            f"Expected 0 for guide without inputSchema, got {exit_code}"
        )

    def test_returns_one_when_inputschema_is_null(self, tmp_path):
        """Exit 1 when a tool doc has inputSchema: null (corrupted)."""
        cat_dir = tmp_path / "search"
        cat_dir.mkdir()
        broken_md = cat_dir / "broken_tool.md"
        broken_md.write_text(
            "---\nname: broken_tool\ncategory: search\ntl_dr: broken\n"
            "inputSchema: null\n---\nBody.\n",
            encoding="utf-8",
        )
        mod = _import_script("verify_inputschema_preserved")
        exit_code = mod.verify_inputschemas(tool_docs_root=tmp_path)
        assert exit_code == 1, f"Expected 1 for null inputSchema, got {exit_code}"

    def test_excludes_underscore_prefixed_files(self, tmp_path):
        """_ prefixed files must be skipped during verification."""
        _make_excluded_file(tmp_path, "search", "excluded")
        _make_tool_doc(tmp_path, "search", "valid_tool")
        mod = _import_script("verify_inputschema_preserved")
        exit_code = mod.verify_inputschemas(tool_docs_root=tmp_path)
        assert exit_code == 0, f"Excluded file should not cause failure: {exit_code}"

    def test_prints_summary_with_pass(self, tmp_path, capsys):
        """Prints summary including PASS when all schemas valid."""
        _make_tool_doc(tmp_path, "search", "good_tool")
        mod = _import_script("verify_inputschema_preserved")
        mod.verify_inputschemas(tool_docs_root=tmp_path)
        captured = capsys.readouterr()
        assert "PASS" in captured.out, f"Expected PASS in output: {captured.out!r}"

    def test_prints_summary_with_fail(self, tmp_path, capsys):
        """Prints FAIL when a tool has null inputSchema."""
        cat_dir = tmp_path / "search"
        cat_dir.mkdir()
        broken_md = cat_dir / "broken_tool.md"
        broken_md.write_text(
            "---\nname: broken_tool\ncategory: search\ntl_dr: t\n"
            "inputSchema: null\n---\nBody.\n",
            encoding="utf-8",
        )
        mod = _import_script("verify_inputschema_preserved")
        mod.verify_inputschemas(tool_docs_root=tmp_path)
        captured = capsys.readouterr()
        assert "FAIL" in captured.out, f"Expected FAIL in output: {captured.out!r}"

    def test_mixed_valid_and_guide(self, tmp_path):
        """Exit 0 when mix of tools with schema and guides without."""
        _make_tool_doc(tmp_path, "search", "with_schema")
        _make_tool_doc(tmp_path, "guides", "a_guide", include_input_schema=False)
        mod = _import_script("verify_inputschema_preserved")
        exit_code = mod.verify_inputschemas(tool_docs_root=tmp_path)
        assert exit_code == 0


# ===========================================================================
# 4. generate_inputschema_fingerprint tests
# ===========================================================================


class TestGenerateInputSchemaFingerprint:
    """Tests for scripts/mcp/generate_inputschema_fingerprint.py"""

    def test_script_file_exists(self):
        """The script must exist at scripts/mcp/generate_inputschema_fingerprint.py"""
        script = _SCRIPTS_MCP / "generate_inputschema_fingerprint.py"
        assert script.exists(), f"Script not found at {script}"

    def test_produces_dict_of_tool_name_to_sha256(self, tmp_path):
        """generate_fingerprints() must return dict mapping tool name to sha256 hex."""
        _make_tool_doc(tmp_path, "search", "fp_tool")
        mod = _import_script("generate_inputschema_fingerprint")
        result = mod.generate_fingerprints(tool_docs_root=tmp_path)
        assert isinstance(result, dict), "Must return a dict"
        assert "fp_tool" in result, "Must have entry for 'fp_tool'"
        sha = result["fp_tool"]
        assert isinstance(sha, str), "SHA value must be a string"
        assert re.match(r"^[0-9a-f]{64}$", sha), (
            f"SHA must be 64-char hex string, got: {sha!r}"
        )

    def test_deterministic_sha256(self, tmp_path):
        """Calling generate_fingerprints twice on same files must produce identical hashes."""
        _make_tool_doc(tmp_path, "search", "stable_tool")
        mod = _import_script("generate_inputschema_fingerprint")
        result1 = mod.generate_fingerprints(tool_docs_root=tmp_path)
        result2 = mod.generate_fingerprints(tool_docs_root=tmp_path)
        assert result1 == result2, "Fingerprints must be deterministic"

    def test_different_schemas_different_hashes(self, tmp_path):
        """Two tools with different inputSchema must produce different hashes."""
        cat_dir = tmp_path / "search"
        cat_dir.mkdir()
        # Tool A - one property
        (cat_dir / "tool_a.md").write_text(
            "---\nname: tool_a\ncategory: search\ntl_dr: A\n"
            "inputSchema:\n  type: object\n  properties:\n    a:\n      type: string\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        # Tool B - different property
        (cat_dir / "tool_b.md").write_text(
            "---\nname: tool_b\ncategory: search\ntl_dr: B\n"
            "inputSchema:\n  type: object\n  properties:\n    b:\n      type: integer\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        mod = _import_script("generate_inputschema_fingerprint")
        result = mod.generate_fingerprints(tool_docs_root=tmp_path)
        assert result["tool_a"] != result["tool_b"], (
            "Different schemas must produce different hashes"
        )

    def test_guides_without_inputschema_excluded_from_fingerprints(self, tmp_path):
        """Guide docs without inputSchema must NOT appear in fingerprint output."""
        _make_tool_doc(tmp_path, "guides", "a_guide", include_input_schema=False)
        _make_tool_doc(tmp_path, "search", "real_tool")
        mod = _import_script("generate_inputschema_fingerprint")
        result = mod.generate_fingerprints(tool_docs_root=tmp_path)
        assert "a_guide" not in result, "Guide without inputSchema must be excluded"
        assert "real_tool" in result, "Tool with inputSchema must be included"

    def test_compare_exits_zero_on_identical(self, tmp_path):
        """compare_fingerprints() returns 0 when current matches baseline."""
        _make_tool_doc(tmp_path, "search", "same_tool")
        mod = _import_script("generate_inputschema_fingerprint")
        current = mod.generate_fingerprints(tool_docs_root=tmp_path)
        # Baseline is identical to current
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(current), encoding="utf-8")
        exit_code = mod.compare_fingerprints(
            current=current, baseline_path=baseline_path
        )
        assert exit_code == 0, f"Expected 0 on identical fingerprints, got {exit_code}"

    def test_compare_exits_one_on_drift(self, tmp_path):
        """compare_fingerprints() returns 1 when schema has changed."""
        cat_dir = tmp_path / "search"
        cat_dir.mkdir()
        tool_md = cat_dir / "drifted_tool.md"
        tool_md.write_text(
            "---\nname: drifted_tool\ncategory: search\ntl_dr: T\n"
            "inputSchema:\n  type: object\n  properties:\n    old:\n      type: string\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        mod = _import_script("generate_inputschema_fingerprint")
        old_fingerprints = mod.generate_fingerprints(tool_docs_root=tmp_path)
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(old_fingerprints), encoding="utf-8")

        # Now modify the tool to simulate drift
        tool_md.write_text(
            "---\nname: drifted_tool\ncategory: search\ntl_dr: T\n"
            "inputSchema:\n  type: object\n  properties:\n    new:\n      type: integer\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        new_fingerprints = mod.generate_fingerprints(tool_docs_root=tmp_path)
        exit_code = mod.compare_fingerprints(
            current=new_fingerprints, baseline_path=baseline_path
        )
        assert exit_code == 1, f"Expected 1 on drift, got {exit_code}"

    def test_compare_prints_drift_details(self, tmp_path, capsys):
        """compare_fingerprints() must print which tools drifted."""
        cat_dir = tmp_path / "search"
        cat_dir.mkdir()
        tool_md = cat_dir / "drifted_tool.md"
        tool_md.write_text(
            "---\nname: drifted_tool\ncategory: search\ntl_dr: T\n"
            "inputSchema:\n  type: object\n  properties:\n    old:\n      type: string\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        mod = _import_script("generate_inputschema_fingerprint")
        old_fp = mod.generate_fingerprints(tool_docs_root=tmp_path)
        baseline_path = tmp_path / "baseline.json"
        baseline_path.write_text(json.dumps(old_fp), encoding="utf-8")

        tool_md.write_text(
            "---\nname: drifted_tool\ncategory: search\ntl_dr: T\n"
            "inputSchema:\n  type: object\n  properties:\n    new:\n      type: integer\n"
            "---\nBody.\n",
            encoding="utf-8",
        )
        new_fp = mod.generate_fingerprints(tool_docs_root=tmp_path)
        mod.compare_fingerprints(current=new_fp, baseline_path=baseline_path)
        captured = capsys.readouterr()
        assert "drifted_tool" in captured.out, (
            f"Drifted tool name must appear in output: {captured.out!r}"
        )

    def test_main_prints_json_to_stdout(self, tmp_path, capsys):
        """main() without --compare must print valid JSON fingerprint dict to stdout."""
        _make_tool_doc(tmp_path, "search", "main_tool")
        mod = _import_script("generate_inputschema_fingerprint")
        mod.main(tool_docs_root=tmp_path, args=[])
        captured = capsys.readouterr()
        parsed = json.loads(captured.out)
        assert isinstance(parsed, dict), "stdout must be valid JSON object"
        assert "main_tool" in parsed


# ===========================================================================
# 5. apply_slim_descriptions tests
# ===========================================================================


class TestApplySlimDescriptions:
    """Tests for scripts/mcp/apply_slim_descriptions.py"""

    def test_script_file_exists(self):
        """The script must exist at scripts/mcp/apply_slim_descriptions.py"""
        script = _SCRIPTS_MCP / "apply_slim_descriptions.py"
        assert script.exists(), f"Script not found at {script}"

    def _make_mapping_file(self, tmp_path: Path, mapping: Dict[str, str]) -> Path:
        """Write a slim mapping JSON file."""
        mapping_path = tmp_path / "slim_mapping.json"
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")
        return mapping_path

    def test_adds_slim_description_after_tl_dr(self, tmp_path):
        """apply_slim() inserts slim_description line after tl_dr in frontmatter."""
        doc = _make_tool_doc(tmp_path, "search", "my_tool")
        original = doc.read_text(encoding="utf-8")
        assert "slim_description" not in original, "Pre-condition: no slim_description"

        mapping_file = self._make_mapping_file(tmp_path, {"my_tool": "Quick search."})
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)

        updated = doc.read_text(encoding="utf-8")
        assert "slim_description" in updated, "slim_description must be added"
        assert "Quick search." in updated, "Slim text must appear in file"

    def test_slim_description_placed_after_tl_dr(self, tmp_path):
        """slim_description must appear immediately after tl_dr in frontmatter."""
        doc = _make_tool_doc(tmp_path, "search", "order_tool")
        mapping_file = self._make_mapping_file(tmp_path, {"order_tool": "Short desc."})
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)

        updated = doc.read_text(encoding="utf-8")
        lines = updated.split("\n")
        tl_dr_idx = next(i for i, line in enumerate(lines) if line.startswith("tl_dr:"))
        slim_idx = next(
            i for i, line in enumerate(lines) if line.startswith("slim_description:")
        )
        assert slim_idx == tl_dr_idx + 1, (
            f"slim_description must be on line after tl_dr. "
            f"tl_dr at {tl_dr_idx}, slim at {slim_idx}"
        )

    def test_preserves_inputschema_exactly(self, tmp_path):
        """inputSchema block must be byte-for-byte identical after apply_slim()."""
        doc = _make_tool_doc(tmp_path, "search", "schema_tool")
        original_text = doc.read_text(encoding="utf-8")
        # Extract the inputSchema portion
        parts = original_text.split("---", 2)
        fm_data = yaml.safe_load(parts[1])
        original_schema = fm_data.get("inputSchema")

        mapping_file = self._make_mapping_file(tmp_path, {"schema_tool": "Brief desc."})
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)

        updated_text = doc.read_text(encoding="utf-8")
        parts_after = updated_text.split("---", 2)
        fm_after = yaml.safe_load(parts_after[1])
        updated_schema = fm_after.get("inputSchema")
        assert updated_schema == original_schema, (
            "inputSchema must be preserved identically after applying slim_description"
        )

    def test_preserves_body_content_exactly(self, tmp_path):
        """The markdown body (after second ---) must be unchanged."""
        doc = _make_tool_doc(tmp_path, "search", "body_tool")
        original_text = doc.read_text(encoding="utf-8")
        original_body = original_text.split("---", 2)[2]

        mapping_file = self._make_mapping_file(tmp_path, {"body_tool": "Slim."})
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)

        updated_text = doc.read_text(encoding="utf-8")
        updated_body = updated_text.split("---", 2)[2]
        assert updated_body == original_body, (
            "Body content must be unchanged after applying slim_description"
        )

    def test_updates_existing_slim_description(self, tmp_path):
        """If slim_description already exists, it must be replaced not duplicated."""
        doc = _make_tool_doc(tmp_path, "search", "update_tool", include_slim=True)
        original_text = doc.read_text(encoding="utf-8")
        assert "Slim text for update_tool." in original_text, "Pre-condition: has slim"

        mapping_file = self._make_mapping_file(
            tmp_path, {"update_tool": "New slim description."}
        )
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)

        updated_text = doc.read_text(encoding="utf-8")
        slim_count = updated_text.count("slim_description:")
        assert slim_count == 1, (
            f"slim_description must appear exactly once, found {slim_count}"
        )
        assert "New slim description." in updated_text, "Updated value must be present"
        assert "Slim text for update_tool." not in updated_text, (
            "Old value must be gone"
        )

    def test_handles_yaml_special_chars_in_slim_text(self, tmp_path):
        """slim_description with colons and special chars must be properly escaped."""
        doc = _make_tool_doc(tmp_path, "search", "special_tool")
        slim_text = "Search: fast, accurate, reliable"
        mapping_file = self._make_mapping_file(tmp_path, {"special_tool": slim_text})
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)

        updated_text = doc.read_text(encoding="utf-8")
        # The raw text must be parseable as valid YAML frontmatter
        parts = updated_text.split("---", 2)
        fm = yaml.safe_load(parts[1])
        assert fm.get("slim_description") == slim_text, (
            f"YAML must parse slim_description correctly. Got: {fm.get('slim_description')!r}"
        )

    def test_prints_summary_of_applied_count(self, tmp_path, capsys):
        """main() must print summary with count of applied entries."""
        _make_tool_doc(tmp_path, "search", "count_tool_a")
        _make_tool_doc(tmp_path, "search", "count_tool_b")
        mapping_file = self._make_mapping_file(
            tmp_path, {"count_tool_a": "Desc A.", "count_tool_b": "Desc B."}
        )
        mod = _import_script("apply_slim_descriptions")
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)
        captured = capsys.readouterr()
        # Should mention 2 tools applied
        assert "2" in captured.out, (
            f"Summary must mention 2 applied entries. Got: {captured.out!r}"
        )

    def test_skips_tools_not_found_in_docs(self, tmp_path, capsys):
        """Tools in mapping that have no matching doc file must be silently skipped."""
        _make_tool_doc(tmp_path, "search", "existing_tool")
        mapping_file = self._make_mapping_file(
            tmp_path,
            {"existing_tool": "Found.", "ghost_tool": "Not found."},
        )
        mod = _import_script("apply_slim_descriptions")
        # Must not raise, must complete without error
        mod.apply_slim(mapping_path=mapping_file, tool_docs_root=tmp_path)
        doc = tmp_path / "search" / "existing_tool.md"
        assert "Found." in doc.read_text(encoding="utf-8"), (
            "existing_tool must be updated"
        )
