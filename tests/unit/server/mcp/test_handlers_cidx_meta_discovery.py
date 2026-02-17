"""
Unit tests for cidx-meta-global discovery documentation (Story #210).

Tests verify that:
1. AC2: quick_reference handler includes discovery section with required fields
2. AC3: first_time_user_guide includes discovery as step 3 with proper content
3. AC3: first_time_user_guide has 9 steps total (original 8 + new discovery step)
4. AC3: first_time_user_guide quick_start_summary has 9 entries
5. AC4: first_time_user_guide includes cidx-meta-global fallback error

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import pytest
from unittest.mock import patch, MagicMock
from datetime import datetime

from code_indexer.server.mcp.handlers import quick_reference, first_time_user_guide
from code_indexer.server.auth.user_manager import User, UserRole


def _extract_mcp_data(mcp_response: dict) -> dict:
    """Extract the JSON data from MCP-compliant content array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}


class TestQuickReferenceDiscovery:
    """Test suite for quick_reference discovery section (Story #210 AC2)."""

    @pytest.fixture
    def test_user(self):
        """Create a test user with query permissions."""
        return User(
            username="test",
            password_hash="hashed_password",
            role=UserRole.POWER_USER,
            created_at=datetime.now(),
        )

    def test_quick_reference_includes_discovery_section(self, test_user):
        """AC2: Quick reference should include discovery section with required fields."""
        mock_config = MagicMock()
        mock_config.service_display_name = "Neo"

        with patch(
            "code_indexer.server.mcp.handlers.get_config_service"
        ) as mock_get_service:
            mock_service = MagicMock()
            mock_service.get_config.return_value = mock_config
            mock_get_service.return_value = mock_service

            mcp_response = quick_reference({}, test_user)

        result = _extract_mcp_data(mcp_response)
        assert result["success"] is True
        assert "discovery" in result, "discovery section must be present in response"

        discovery = result["discovery"]

        # Verify required fields exist
        assert "meta_repo" in discovery
        assert discovery["meta_repo"] == "cidx-meta-global"

        assert "what_it_contains" in discovery
        assert "Synthetic repository" in discovery["what_it_contains"]
        assert "AI-generated markdown descriptions" in discovery["what_it_contains"]

        assert "workflow" in discovery
        assert isinstance(discovery["workflow"], list)
        assert len(discovery["workflow"]) == 5, "Should have 5-step workflow"

        # Verify workflow steps mention key concepts
        workflow_text = " ".join(discovery["workflow"])
        assert "list_global_repos()" in workflow_text
        assert "cidx-meta-global" in workflow_text
        assert "dependency-map/" in workflow_text

        assert "result_mapping" in discovery
        assert "Strip .md extension" in discovery["result_mapping"]
        assert "append '-global'" in discovery["result_mapping"]

        assert "fallback" in discovery
        assert "cidx-meta-global not found" in discovery["fallback"] or "If cidx-meta-global" in discovery["fallback"]
        assert "list_global_repos()" in discovery["fallback"]


class TestFirstTimeUserGuideDiscovery:
    """Test suite for first_time_user_guide discovery changes (Story #210 AC3, AC4)."""

    @pytest.fixture
    def test_user(self):
        """Create a test user with query permissions."""
        return User(
            username="test",
            password_hash="hashed_password",
            role=UserRole.POWER_USER,
            created_at=datetime.now(),
        )

    def test_first_time_guide_has_9_steps(self, test_user):
        """AC3: first_time_user_guide should have 9 steps after adding discovery."""
        mcp_response = first_time_user_guide({}, test_user)
        result = _extract_mcp_data(mcp_response)

        assert result["success"] is True
        assert "guide" in result

        guide = result["guide"]
        assert "steps" in guide
        assert len(guide["steps"]) == 9, "Should have 9 steps (8 original + 1 new discovery)"

    def test_first_time_guide_step_3_is_discovery(self, test_user):
        """AC3: Step 3 should be discovery with proper content."""
        mcp_response = first_time_user_guide({}, test_user)
        result = _extract_mcp_data(mcp_response)

        guide = result["guide"]
        steps = guide["steps"]

        # Find step 3
        step_3 = None
        for step in steps:
            if step["step_number"] == 3:
                step_3 = step
                break

        assert step_3 is not None, "Step 3 must exist"
        assert "step_number" in step_3
        assert step_3["step_number"] == 3

        assert "title" in step_3
        assert "discover" in step_3["title"].lower() or "repository" in step_3["title"].lower()

        assert "description" in step_3
        assert "cidx-meta-global" in step_3["description"]
        assert ".md" in step_3["description"]
        assert "-global" in step_3["description"]

        assert "example_call" in step_3
        assert "search_code" in step_3["example_call"]
        assert "cidx-meta-global" in step_3["example_call"]

        assert "expected_result" in step_3
        assert "file_path=" in step_3["expected_result"]
        assert ".md" in step_3["expected_result"]

    def test_first_time_guide_steps_renumbered_correctly(self, test_user):
        """AC3: Original steps 3-8 should become 4-9 after discovery insertion."""
        mcp_response = first_time_user_guide({}, test_user)
        result = _extract_mcp_data(mcp_response)

        guide = result["guide"]
        steps = guide["steps"]

        # Verify step numbers are sequential 1-9
        step_numbers = [step["step_number"] for step in steps]
        assert step_numbers == list(range(1, 10)), "Step numbers should be 1-9 in sequence"

        # Verify step 4 is what used to be step 3 (Check repository capabilities)
        step_4 = [s for s in steps if s["step_number"] == 4][0]
        assert "capabilities" in step_4["title"].lower() or "global_repo_status" in step_4["example_call"]

    def test_first_time_guide_quick_start_summary_has_9_entries(self, test_user):
        """AC3: quick_start_summary should have 9 entries."""
        mcp_response = first_time_user_guide({}, test_user)
        result = _extract_mcp_data(mcp_response)

        guide = result["guide"]
        assert "quick_start_summary" in guide

        summary = guide["quick_start_summary"]
        assert len(summary) == 9, "Should have 9 entries in quick_start_summary"

        # Verify entry 3 mentions discovery
        entry_3 = summary[2]  # 0-indexed
        assert "3." in entry_3
        assert "cidx-meta-global" in entry_3

    def test_first_time_guide_includes_meta_global_fallback_error(self, test_user):
        """AC4: common_errors should include cidx-meta-global fallback guidance."""
        mcp_response = first_time_user_guide({}, test_user)
        result = _extract_mcp_data(mcp_response)

        guide = result["guide"]
        assert "common_errors" in guide

        errors = guide["common_errors"]

        # Find error about cidx-meta-global not found
        meta_error = None
        for error_entry in errors:
            if "cidx-meta-global" in error_entry.get("error", ""):
                meta_error = error_entry
                break

        assert meta_error is not None, "Should have error entry for cidx-meta-global not found"
        assert "error" in meta_error
        assert "cidx-meta-global" in meta_error["error"]
        assert "not found" in meta_error["error"].lower()

        assert "solution" in meta_error
        assert "list_global_repos()" in meta_error["solution"]
