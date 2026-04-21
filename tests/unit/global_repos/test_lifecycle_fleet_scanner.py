"""
Unit tests for LifecycleFleetScanner — Story #876.

Tests:
  1. Parametrized: missing file, no lifecycle block, outdated schema version,
     confidence=unknown poison, malformed frontmatter → alias flagged;
     valid complete metadata → NOT flagged
  2. Empty alias list → returns empty list
  3. 'cidx-meta' self-alias → skipped regardless of file presence
"""

from pathlib import Path
from typing import Dict, Optional

import pytest
import yaml

from code_indexer.global_repos.lifecycle_batch_runner import (
    CURRENT_LIFECYCLE_SCHEMA_VERSION,
    LifecycleFleetScanner,
)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_VALID_LIFECYCLE: Dict[str, str] = {
    "ci_system": "github-actions",
    "deployment_target": "kubernetes",
    "language_ecosystem": "python/poetry",
    "build_system": "poetry",
    "testing_framework": "pytest",
    "confidence": "high",
}

_VALID_FRONTMATTER: Dict = {
    "lifecycle": _VALID_LIFECYCLE,
    "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def golden_repos_dir(tmp_path: Path) -> Path:
    """Temp golden_repos_dir with cidx-meta/ subdirectory."""
    (tmp_path / "cidx-meta").mkdir(parents=True)
    return tmp_path


# ---------------------------------------------------------------------------
# 1. Parametrized broken/valid cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("frontmatter", "raw_override", "should_be_flagged"),
    [
        # Missing .md file
        (None, None, True),
        # Frontmatter with no lifecycle block
        ({"lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION}, None, True),
        # Outdated schema version
        ({"lifecycle": _VALID_LIFECYCLE, "lifecycle_schema_version": 0}, None, True),
        # confidence=unknown poison
        (
            {
                "lifecycle": {**_VALID_LIFECYCLE, "confidence": "unknown"},
                "lifecycle_schema_version": CURRENT_LIFECYCLE_SCHEMA_VERSION,
            },
            None,
            True,
        ),
        # Malformed frontmatter (split_frontmatter_and_body returns {})
        (None, "---\nbad: yaml: [\nno closing delimiter\n", True),
        # Valid complete metadata → NOT flagged
        (_VALID_FRONTMATTER, None, False),
    ],
    ids=[
        "missing_file",
        "no_lifecycle_block",
        "outdated_schema_version",
        "confidence_unknown_poison",
        "malformed_frontmatter",
        "valid_repo_not_flagged",
    ],
)
def test_find_broken_or_missing_broken_cases(
    golden_repos_dir: Path,
    frontmatter: Optional[Dict],
    raw_override: Optional[str],
    should_be_flagged: bool,
) -> None:
    """Parametrized: broken conditions → alias in result; valid → alias absent."""
    alias = "test-global"
    cidx_meta_dir = golden_repos_dir / "cidx-meta"
    meta_path = cidx_meta_dir / f"{alias}.md"

    if raw_override is not None:
        meta_path.write_text(raw_override, encoding="utf-8")
    elif frontmatter is not None:
        fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
        meta_path.write_text(
            f"---\n{fm_yaml}---\n\nSome description.\n", encoding="utf-8"
        )
    # else: leave file absent (simulates missing file case)

    scanner = LifecycleFleetScanner(
        golden_repos_dir=golden_repos_dir,
        repo_aliases=[alias],
    )
    broken = scanner.find_broken_or_missing()

    if should_be_flagged:
        assert alias in broken, f"Expected {alias!r} to be flagged as broken"
    else:
        assert alias not in broken, (
            f"Expected {alias!r} NOT to be flagged (valid metadata)"
        )


# ---------------------------------------------------------------------------
# 2. Empty alias list → returns empty list
# ---------------------------------------------------------------------------


def test_find_broken_or_missing_returns_empty_for_empty_input(
    golden_repos_dir: Path,
) -> None:
    """Empty alias list → empty result with no crash."""
    scanner = LifecycleFleetScanner(golden_repos_dir=golden_repos_dir, repo_aliases=[])
    assert scanner.find_broken_or_missing() == []


# ---------------------------------------------------------------------------
# 3. 'cidx-meta' self-alias → skipped
# ---------------------------------------------------------------------------


def test_find_broken_or_missing_skips_cidx_meta_self_alias(
    golden_repos_dir: Path,
) -> None:
    """'cidx-meta' self-alias is never flagged, even with no .md file present."""
    cidx_meta_dir = golden_repos_dir / "cidx-meta"
    # Write a valid file for real-global to confirm regular aliases ARE processed
    fm_yaml = yaml.dump(
        _VALID_FRONTMATTER, default_flow_style=False, allow_unicode=True
    )
    (cidx_meta_dir / "real-global.md").write_text(
        f"---\n{fm_yaml}---\n\nSome description.\n", encoding="utf-8"
    )
    scanner = LifecycleFleetScanner(
        golden_repos_dir=golden_repos_dir,
        repo_aliases=["cidx-meta", "real-global"],
    )
    broken = scanner.find_broken_or_missing()
    assert "cidx-meta" not in broken
    assert "real-global" not in broken


# ---------------------------------------------------------------------------
# 4. v2 schema flagged as broken under v3 (AC-V3-11)
# ---------------------------------------------------------------------------


def test_fleet_scanner_flags_v2_as_broken_under_v3(
    golden_repos_dir: Path,
) -> None:
    """
    A .md file with lifecycle_schema_version: 2 must be flagged as broken
    when CURRENT_LIFECYCLE_SCHEMA_VERSION == 3 (Schema v3 amendment, Story #876).

    This test verifies that the import fix (removing the stale local constant=2
    in lifecycle_batch_runner.py and importing 3 from unified_response_parser)
    also propagates correctly into LifecycleFleetScanner's comparison.
    """
    alias = "legacy-v2-global"
    cidx_meta_dir = golden_repos_dir / "cidx-meta"

    v2_frontmatter = {
        "lifecycle": _VALID_LIFECYCLE,
        "lifecycle_schema_version": 2,
    }
    fm_yaml = yaml.dump(v2_frontmatter, default_flow_style=False, allow_unicode=True)
    (cidx_meta_dir / f"{alias}.md").write_text(
        f"---\n{fm_yaml}---\n\nA legacy v2 repo description.\n", encoding="utf-8"
    )

    scanner = LifecycleFleetScanner(
        golden_repos_dir=golden_repos_dir,
        repo_aliases=[alias],
    )
    broken = scanner.find_broken_or_missing()

    assert alias in broken, (
        f"Expected {alias!r} to be flagged as broken: "
        f"lifecycle_schema_version=2 is stale under CURRENT={CURRENT_LIFECYCLE_SCHEMA_VERSION}"
    )
