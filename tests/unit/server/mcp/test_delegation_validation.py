"""
Unit tests for collaborative and competitive validation in _validate_open_delegation_params.

Story #462: Enable collaborative and competitive delegation modes.

Tests cover:
- Collaborative mode: steps required, max 10, step fields, duplicate step_ids,
  engine validation per step, dependency validation, terminal step requirement
- Competitive mode: prompt/repositories/engines required, engine validation,
  distribution_strategy validation, approach_count range, min_success_threshold,
  approach_timeout_seconds, decomposer/judge engine validation
- Backward compatibility: single mode unchanged
"""

import json


from code_indexer.server.mcp.handlers import _validate_open_delegation_params


class TestCollaborativeValidation:
    """Tests for collaborative mode parameter validation."""

    def test_collaborative_requires_steps(self):
        """Collaborative mode requires non-empty steps list."""
        result = _validate_open_delegation_params(
            {
                "prompt": "ignored in collaborative",
                "repositories": ["repo"],
                "mode": "collaborative",
                # no steps
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "steps" in data["error"].lower()

    def test_collaborative_empty_steps_rejected(self):
        """Collaborative mode rejects empty steps list."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "steps" in data["error"].lower()

    def test_collaborative_max_10_steps(self):
        """Collaborative mode rejects more than 10 steps."""
        steps = [
            {"step_id": f"s{i}", "engine": "claude-code", "prompt": f"p{i}"}
            for i in range(11)
        ]
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": steps,
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "10" in data["error"]

    def test_collaborative_10_steps_accepted(self):
        """Collaborative mode accepts exactly 10 steps."""
        steps = [
            {"step_id": f"s{i}", "engine": "claude-code", "prompt": f"p{i}"}
            for i in range(10)
        ]
        # Only last step is terminal (no one depends on it)
        for i in range(9):
            steps[i + 1]["depends_on"] = [f"s{i}"]  # type: ignore[assignment]

        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": steps,
            }
        )
        assert result is None

    def test_collaborative_step_requires_step_id(self):
        """Each step must have step_id."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [{"engine": "claude-code", "prompt": "missing step_id"}],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "step_id" in data["error"]

    def test_collaborative_step_requires_engine(self):
        """Each step must have engine."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [{"step_id": "s1", "prompt": "missing engine"}],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "engine" in data["error"].lower()

    def test_collaborative_step_requires_prompt(self):
        """Each step must have prompt."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [{"step_id": "s1", "engine": "claude-code"}],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "prompt" in data["error"].lower()

    def test_collaborative_duplicate_step_ids_rejected(self):
        """Duplicate step_ids are rejected."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "dup", "engine": "claude-code", "prompt": "p1"},
                    {"step_id": "dup", "engine": "codex", "prompt": "p2"},
                ],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "duplicate" in data["error"].lower()

    def test_collaborative_invalid_engine_per_step(self):
        """Invalid engine in a step is rejected."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "s1", "engine": "invalid-engine", "prompt": "p"},
                ],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "engine" in data["error"].lower()

    def test_collaborative_dependency_references_nonexistent_step(self):
        """Dependencies referencing non-existent step_ids are rejected."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "s1", "engine": "claude-code", "prompt": "p1"},
                    {
                        "step_id": "s2",
                        "engine": "claude-code",
                        "prompt": "p2",
                        "depends_on": ["nonexistent"],
                    },
                ],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "nonexistent" in data["error"]

    def test_collaborative_self_dependency_rejected(self):
        """A step depending on itself is rejected."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {
                        "step_id": "s1",
                        "engine": "claude-code",
                        "prompt": "p",
                        "depends_on": ["s1"],
                    },
                ],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "self" in data["error"].lower() or "itself" in data["error"].lower()

    def test_collaborative_requires_exactly_one_terminal_step(self):
        """Must have exactly one terminal step (no other step depends on it)."""
        # Two terminal steps: s1 and s2 (neither is depended on by anyone)
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "s1", "engine": "claude-code", "prompt": "p1"},
                    {"step_id": "s2", "engine": "claude-code", "prompt": "p2"},
                ],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "terminal" in data["error"].lower()

    def test_collaborative_valid_linear_dag_accepted(self):
        """Valid linear DAG (A -> B -> C) is accepted."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "a", "engine": "claude-code", "prompt": "pa"},
                    {
                        "step_id": "b",
                        "engine": "codex",
                        "prompt": "pb",
                        "depends_on": ["a"],
                    },
                    {
                        "step_id": "c",
                        "engine": "gemini",
                        "prompt": "pc",
                        "depends_on": ["b"],
                    },
                ],
            }
        )
        assert result is None

    def test_collaborative_valid_diamond_dag_accepted(self):
        """Valid diamond DAG (A -> B, A -> C, B+C -> D) is accepted."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "a", "engine": "claude-code", "prompt": "pa"},
                    {
                        "step_id": "b",
                        "engine": "claude-code",
                        "prompt": "pb",
                        "depends_on": ["a"],
                    },
                    {
                        "step_id": "c",
                        "engine": "codex",
                        "prompt": "pc",
                        "depends_on": ["a"],
                    },
                    {
                        "step_id": "d",
                        "engine": "gemini",
                        "prompt": "pd",
                        "depends_on": ["b", "c"],
                    },
                ],
            }
        )
        assert result is None

    def test_collaborative_cycle_detected(self):
        """DAG with A->B->C->A cycle must be rejected."""
        args = {
            "mode": "collaborative",
            "steps": [
                {
                    "step_id": "a",
                    "engine": "claude-code",
                    "prompt": "do A",
                    "depends_on": ["c"],
                },
                {
                    "step_id": "b",
                    "engine": "claude-code",
                    "prompt": "do B",
                    "depends_on": ["a"],
                },
                {
                    "step_id": "c",
                    "engine": "claude-code",
                    "prompt": "do C",
                    "depends_on": ["b"],
                },
                {
                    "step_id": "d",
                    "engine": "claude-code",
                    "prompt": "do D",
                    "depends_on": ["c"],
                },
            ],
        }
        result = _validate_open_delegation_params(args)
        assert result is not None
        text = json.loads(result["content"][0]["text"])
        assert "cycle" in text["error"].lower()

    def test_collaborative_single_step_accepted(self):
        """Single step (no dependencies) is valid as long as it is the only terminal."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "collaborative",
                "steps": [
                    {"step_id": "only", "engine": "claude-code", "prompt": "do it"},
                ],
            }
        )
        assert result is None


class TestCompetitiveValidation:
    """Tests for competitive mode parameter validation."""

    def test_competitive_requires_prompt(self):
        """Competitive mode requires prompt."""
        result = _validate_open_delegation_params(
            {
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "prompt" in data["error"].lower()

    def test_competitive_requires_repositories(self):
        """Competitive mode requires repositories."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "mode": "competitive",
                "engines": ["claude-code"],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "repositor" in data["error"].lower()

    def test_competitive_requires_engines(self):
        """Competitive mode requires engines list."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "engines" in data["error"].lower()

    def test_competitive_empty_engines_rejected(self):
        """Competitive mode rejects empty engines list."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": [],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "engines" in data["error"].lower()

    def test_competitive_invalid_engine_rejected(self):
        """Invalid engine name in engines list is rejected."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code", "fake-engine"],
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "engine" in data["error"].lower()

    def test_competitive_valid_distribution_strategies(self):
        """Valid distribution strategies are accepted."""
        for strategy in ["round-robin", "decomposer-decides"]:
            result = _validate_open_delegation_params(
                {
                    "prompt": "p",
                    "repositories": ["repo"],
                    "mode": "competitive",
                    "engines": ["claude-code"],
                    "distribution_strategy": strategy,
                }
            )
            assert result is None, f"Strategy '{strategy}' should be accepted"

    def test_competitive_invalid_distribution_strategy(self):
        """Invalid distribution_strategy is rejected."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "distribution_strategy": "random",
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "distribution_strategy" in data["error"]

    def test_competitive_approach_count_range(self):
        """approach_count must be 2-10."""
        # Too low
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "approach_count": 1,
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False

        # Too high
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "approach_count": 11,
            }
        )
        assert result is not None

        # Valid
        for count in [2, 5, 10]:
            result = _validate_open_delegation_params(
                {
                    "prompt": "p",
                    "repositories": ["repo"],
                    "mode": "competitive",
                    "engines": ["claude-code"],
                    "approach_count": count,
                }
            )
            assert result is None, f"approach_count={count} should be accepted"

    def test_competitive_min_success_threshold_range(self):
        """min_success_threshold must be 1 to approach_count (default 3)."""
        # Too low
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "min_success_threshold": 0,
            }
        )
        assert result is not None

        # Too high (default approach_count is 3)
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "min_success_threshold": 4,
            }
        )
        assert result is not None

        # Valid within default approach_count=3
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "min_success_threshold": 2,
            }
        )
        assert result is None

    def test_competitive_approach_timeout_must_be_positive(self):
        """approach_timeout_seconds must be >= 1 if provided."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "approach_timeout_seconds": 0,
            }
        )
        assert result is not None

        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "approach_timeout_seconds": 1,
            }
        )
        assert result is None

    def test_competitive_decomposer_engine_validated(self):
        """Decomposer engine must be a valid engine name."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "decomposer": {"engine": "bad-engine"},
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "decomposer" in data["error"].lower()

    def test_competitive_judge_engine_validated(self):
        """Judge engine must be a valid engine name."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
                "judge": {"engine": "bad-engine"},
            }
        )
        assert result is not None
        data = json.loads(result["content"][0]["text"])
        assert data["success"] is False
        assert "judge" in data["error"].lower()

    def test_competitive_valid_full_params_accepted(self):
        """Full valid competitive params are accepted."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code", "codex"],
                "distribution_strategy": "round-robin",
                "approach_count": 4,
                "min_success_threshold": 2,
                "approach_timeout_seconds": 600,
                "decomposer": {"engine": "claude-code"},
                "judge": {"engine": "gemini"},
            }
        )
        assert result is None

    def test_competitive_no_optional_params_accepted(self):
        """Competitive with only required params is accepted."""
        result = _validate_open_delegation_params(
            {
                "prompt": "p",
                "repositories": ["repo"],
                "mode": "competitive",
                "engines": ["claude-code"],
            }
        )
        assert result is None


class TestSingleModeBackwardCompatibility:
    """Verify single mode validation remains unchanged."""

    def test_single_mode_valid_params_accepted(self):
        """Single mode with valid params returns None (no error)."""
        result = _validate_open_delegation_params(
            {
                "prompt": "Fix bug",
                "repositories": ["main-app"],
                "engine": "claude-code",
                "mode": "single",
            }
        )
        assert result is None

    def test_single_mode_missing_prompt_rejected(self):
        """Single mode missing prompt is still rejected."""
        result = _validate_open_delegation_params(
            {
                "repositories": ["main-app"],
                "mode": "single",
            }
        )
        assert result is not None

    def test_default_mode_is_single(self):
        """When mode not specified, defaults to single and validates accordingly."""
        result = _validate_open_delegation_params(
            {
                "prompt": "Fix bug",
                "repositories": ["main-app"],
            }
        )
        assert result is None
