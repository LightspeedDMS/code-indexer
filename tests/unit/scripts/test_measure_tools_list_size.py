"""
Unit tests for scripts/mcp/measure_tools_list_size.py

Story #986: Baseline measurement harness (per-role bytes plus tiktoken)

Tests verify (TDD RED phase - script does not exist yet):
- test_output_has_top_level_keys: generated_at, git_sha, roles keys present
- test_all_four_roles_present: anonymous, normal_user, power_user, admin in roles
- test_per_role_stable_keys: each role entry has byte_size, tiktoken_count, tool_count
- test_byte_size_positive: byte_size > 0 for all roles
- test_tiktoken_count_positive: tiktoken_count > 0 for all roles
- test_tool_count_positive_or_zero: tool_count >= 0 for all roles
- test_anonymous_has_fewer_tools_than_normal_user: anonymous subset of normal_user
- test_tool_count_hierarchy: admin >= power_user >= normal_user >= anonymous
- test_output_directory_created: reports/mcp/ dir is created if missing
- test_output_file_written: baseline_<timestamp>.json written to reports/mcp/
- test_output_is_valid_json: written file is valid JSON
- test_git_sha_is_hex_string: git_sha is a 7-40 char hex string
- test_generated_at_is_iso8601: generated_at parses as ISO8601 datetime
- test_measure_role_returns_correct_keys: measure_role() returns dict with expected keys
- test_build_role_permission_sets: permission sets match expected role hierarchy
"""

import json
import re
import sys
from datetime import datetime
from pathlib import Path

import pytest

# Ensure src/ is on sys.path for imports
_PROJECT_ROOT = Path(__file__).parents[3]
if str(_PROJECT_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT / "src"))

# The script under test
_SCRIPT_PATH = _PROJECT_ROOT / "scripts" / "mcp" / "measure_tools_list_size.py"


def _import_script():
    """Import the measurement script as a module."""
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "measure_tools_list_size", _SCRIPT_PATH
    )
    mod = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Basic import / existence test
# ---------------------------------------------------------------------------


def test_script_file_exists():
    """The measurement script must exist at the expected path."""
    assert _SCRIPT_PATH.exists(), (
        f"Script not found at {_SCRIPT_PATH}. "
        "Create scripts/mcp/measure_tools_list_size.py first."
    )


# ---------------------------------------------------------------------------
# Permission set / role mapping tests
# ---------------------------------------------------------------------------


def test_build_role_permission_sets():
    """
    ROLE_PERMISSIONS constant must define the four expected roles with
    correct permission inheritance:
    - anonymous: only 'public'
    - normal_user: query_repos, repository:read, activate_repos, public
    - power_user: normal_user + repository:write, delegate_open
    - admin: power_user + manage_users, manage_golden_repos, repository:admin
    """
    mod = _import_script()
    perms = mod.ROLE_PERMISSIONS  # type: ignore[attr-defined]

    assert "anonymous" in perms, "ROLE_PERMISSIONS must have 'anonymous' key"
    assert "normal_user" in perms, "ROLE_PERMISSIONS must have 'normal_user' key"
    assert "power_user" in perms, "ROLE_PERMISSIONS must have 'power_user' key"
    assert "admin" in perms, "ROLE_PERMISSIONS must have 'admin' key"

    # anonymous: only public tools visible
    assert "public" in perms["anonymous"], "anonymous must include 'public'"
    # normal_user has base permissions
    for p in ("public", "query_repos", "repository:read", "activate_repos"):
        assert p in perms["normal_user"], f"normal_user must have permission '{p}'"
    # power_user is superset of normal_user
    for p in perms["normal_user"]:
        assert p in perms["power_user"], (
            f"power_user must inherit normal_user permission '{p}'"
        )
    # admin is superset of power_user
    for p in perms["power_user"]:
        assert p in perms["admin"], f"admin must inherit power_user permission '{p}'"
    # admin has admin-specific permissions
    for p in ("manage_users", "manage_golden_repos", "repository:admin"):
        assert p in perms["admin"], f"admin must have permission '{p}'"


# ---------------------------------------------------------------------------
# measure_role() function tests
# ---------------------------------------------------------------------------


def test_measure_role_returns_correct_keys():
    """measure_role() must return a dict with byte_size, tiktoken_count, tool_count."""
    mod = _import_script()
    result = mod.measure_role("normal_user")  # type: ignore[attr-defined]

    assert isinstance(result, dict), "measure_role() must return a dict"
    assert "byte_size" in result, "Result must contain 'byte_size'"
    assert "tiktoken_count" in result, "Result must contain 'tiktoken_count'"
    assert "tool_count" in result, "Result must contain 'tool_count'"


def test_measure_role_byte_size_positive():
    """byte_size must be > 0 for any role that has at least one visible tool."""
    mod = _import_script()
    result = mod.measure_role("normal_user")  # type: ignore[attr-defined]
    assert result["byte_size"] > 0, (
        f"byte_size must be positive for normal_user. Got: {result['byte_size']}"
    )


def test_measure_role_tiktoken_count_positive():
    """tiktoken_count must be > 0 for any role with visible tools."""
    mod = _import_script()
    result = mod.measure_role("normal_user")  # type: ignore[attr-defined]
    assert result["tiktoken_count"] > 0, (
        f"tiktoken_count must be positive for normal_user. Got: {result['tiktoken_count']}"
    )


def test_measure_role_tool_count_non_negative():
    """tool_count must be >= 0 (anonymous may have exactly 1 public tool)."""
    mod = _import_script()
    result = mod.measure_role("anonymous")  # type: ignore[attr-defined]
    assert result["tool_count"] >= 0, (
        f"tool_count must be non-negative. Got: {result['tool_count']}"
    )


def test_measure_anonymous_has_at_least_one_tool():
    """anonymous role must see at least the 'authenticate' public tool."""
    mod = _import_script()
    result = mod.measure_role("anonymous")  # type: ignore[attr-defined]
    assert result["tool_count"] >= 1, (
        f"anonymous must have at least 1 tool (authenticate). Got: {result['tool_count']}"
    )


# ---------------------------------------------------------------------------
# Tool count hierarchy tests
# ---------------------------------------------------------------------------


def test_anonymous_has_fewer_tools_than_normal_user():
    """anonymous must see fewer tools than normal_user (subset relationship)."""
    mod = _import_script()
    anon = mod.measure_role("anonymous")  # type: ignore[attr-defined]
    normal = mod.measure_role("normal_user")  # type: ignore[attr-defined]
    assert anon["tool_count"] < normal["tool_count"], (
        f"anonymous tool_count ({anon['tool_count']}) must be < "
        f"normal_user tool_count ({normal['tool_count']})"
    )


def test_tool_count_hierarchy():
    """Tool count must respect: admin >= power_user >= normal_user >= anonymous."""
    mod = _import_script()
    anon = mod.measure_role("anonymous")["tool_count"]  # type: ignore[attr-defined]
    normal = mod.measure_role("normal_user")["tool_count"]  # type: ignore[attr-defined]
    power = mod.measure_role("power_user")["tool_count"]  # type: ignore[attr-defined]
    admin = mod.measure_role("admin")["tool_count"]  # type: ignore[attr-defined]

    assert normal >= anon, (
        f"normal_user ({normal}) must have >= anonymous ({anon}) tools"
    )
    assert power >= normal, (
        f"power_user ({power}) must have >= normal_user ({normal}) tools"
    )
    assert admin >= power, f"admin ({admin}) must have >= power_user ({power}) tools"


# ---------------------------------------------------------------------------
# collect_measurements() function tests
# ---------------------------------------------------------------------------


def test_collect_measurements_returns_all_roles():
    """collect_measurements() must return a dict with all 4 role keys."""
    mod = _import_script()
    result = mod.collect_measurements()  # type: ignore[attr-defined]

    for role in ("anonymous", "normal_user", "power_user", "admin"):
        assert role in result, f"collect_measurements() must include role '{role}'"


def test_collect_measurements_each_role_has_stable_keys():
    """Each role in collect_measurements() output must have byte_size, tiktoken_count, tool_count."""
    mod = _import_script()
    result = mod.collect_measurements()  # type: ignore[attr-defined]

    for role, entry in result.items():
        assert "byte_size" in entry, f"Role '{role}' missing 'byte_size'"
        assert "tiktoken_count" in entry, f"Role '{role}' missing 'tiktoken_count'"
        assert "tool_count" in entry, f"Role '{role}' missing 'tool_count'"


# ---------------------------------------------------------------------------
# JSON output shape tests
# ---------------------------------------------------------------------------


def test_build_report_has_top_level_keys():
    """build_report() must return dict with generated_at, git_sha, and roles keys."""
    mod = _import_script()
    report = mod.build_report()  # type: ignore[attr-defined]

    assert "generated_at" in report, "Report must contain 'generated_at'"
    assert "git_sha" in report, "Report must contain 'git_sha'"
    assert "roles" in report, "Report must contain 'roles'"


def test_build_report_roles_has_all_four_roles():
    """build_report()['roles'] must contain all four role keys."""
    mod = _import_script()
    report = mod.build_report()  # type: ignore[attr-defined]

    for role in ("anonymous", "normal_user", "power_user", "admin"):
        assert role in report["roles"], (
            f"report['roles'] must contain '{role}'. Got keys: {list(report['roles'].keys())}"
        )


def test_build_report_generated_at_is_iso8601():
    """generated_at value must be parseable as an ISO8601 datetime."""
    mod = _import_script()
    report = mod.build_report()  # type: ignore[attr-defined]

    generated_at = report["generated_at"]
    # Must be parseable as ISO8601 - will raise ValueError if not
    try:
        parsed = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
        assert parsed is not None
    except (ValueError, AttributeError) as e:
        pytest.fail(f"generated_at '{generated_at}' is not valid ISO8601: {e}")


def test_build_report_git_sha_is_hex_string():
    """git_sha must be a 7-40 character hexadecimal string or 'unknown'."""
    mod = _import_script()
    report = mod.build_report()  # type: ignore[attr-defined]

    git_sha = report["git_sha"]
    assert isinstance(git_sha, str), f"git_sha must be a string. Got: {type(git_sha)}"
    # Either 'unknown' or a valid hex SHA (7-40 chars)
    if git_sha != "unknown":
        assert re.match(r"^[0-9a-f]{7,40}$", git_sha), (
            f"git_sha '{git_sha}' must be 7-40 hex chars or 'unknown'"
        )


# ---------------------------------------------------------------------------
# File output tests
# ---------------------------------------------------------------------------


def test_write_report_creates_output_directory(tmp_path):
    """write_report() must create the output directory if it does not exist."""
    mod = _import_script()
    output_dir = tmp_path / "reports" / "mcp"
    assert not output_dir.exists(), "Pre-condition: output_dir must not exist"

    report = mod.build_report()  # type: ignore[attr-defined]
    written_path = mod.write_report(report, output_dir=output_dir)  # type: ignore[attr-defined]

    assert output_dir.exists(), (
        f"write_report() must create output directory '{output_dir}'"
    )
    assert written_path.exists(), (
        f"write_report() must create the output file. Expected at: {written_path}"
    )


def test_write_report_filename_has_timestamp(tmp_path):
    """Output filename must match baseline_<timestamp>.json pattern."""
    mod = _import_script()
    output_dir = tmp_path / "reports" / "mcp"

    report = mod.build_report()  # type: ignore[attr-defined]
    written_path = mod.write_report(report, output_dir=output_dir)  # type: ignore[attr-defined]

    filename = written_path.name
    assert filename.startswith("baseline_"), (
        f"Output filename must start with 'baseline_'. Got: '{filename}'"
    )
    assert filename.endswith(".json"), (
        f"Output filename must end with '.json'. Got: '{filename}'"
    )


def test_write_report_produces_valid_json(tmp_path):
    """The written file must be valid JSON."""
    mod = _import_script()
    output_dir = tmp_path / "reports" / "mcp"

    report = mod.build_report()  # type: ignore[attr-defined]
    written_path = mod.write_report(report, output_dir=output_dir)  # type: ignore[attr-defined]

    content = written_path.read_text(encoding="utf-8")
    try:
        parsed = json.loads(content)
        assert isinstance(parsed, dict), "Written file must contain a JSON object"
    except json.JSONDecodeError as e:
        pytest.fail(f"Written file is not valid JSON: {e}")


def test_write_report_json_contains_all_keys(tmp_path):
    """The written JSON must contain generated_at, git_sha, and roles keys."""
    mod = _import_script()
    output_dir = tmp_path / "reports" / "mcp"

    report = mod.build_report()  # type: ignore[attr-defined]
    written_path = mod.write_report(report, output_dir=output_dir)  # type: ignore[attr-defined]

    parsed = json.loads(written_path.read_text(encoding="utf-8"))
    for key in ("generated_at", "git_sha", "roles"):
        assert key in parsed, f"Written JSON missing key '{key}'"


def test_write_report_byte_sizes_are_integers(tmp_path):
    """byte_size values in written JSON must be integers."""
    mod = _import_script()
    output_dir = tmp_path / "reports" / "mcp"

    report = mod.build_report()  # type: ignore[attr-defined]
    written_path = mod.write_report(report, output_dir=output_dir)  # type: ignore[attr-defined]

    parsed = json.loads(written_path.read_text(encoding="utf-8"))
    for role, entry in parsed["roles"].items():
        assert isinstance(entry["byte_size"], int), (
            f"Role '{role}' byte_size must be int. Got: {type(entry['byte_size'])}"
        )


def test_write_report_returns_path_object(tmp_path):
    """write_report() must return a Path object pointing to the written file."""
    mod = _import_script()
    output_dir = tmp_path / "reports" / "mcp"

    report = mod.build_report()  # type: ignore[attr-defined]
    written_path = mod.write_report(report, output_dir=output_dir)  # type: ignore[attr-defined]

    assert isinstance(written_path, Path), (
        f"write_report() must return a Path. Got: {type(written_path)}"
    )
