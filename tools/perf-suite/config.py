"""
Scenario loading and validation for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC1: CLI Entry Point and Configuration - scenario loading and validation.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any

VALID_PROTOCOLS = {"mcp", "rest"}
VALID_PRIORITIES = {"highest", "high", "medium"}
REQUIRED_FIELDS = ("name", "endpoint", "protocol", "method", "parameters", "repo_alias", "priority")


@dataclass
class Scenario:
    """A single performance test scenario loaded from a JSON file."""

    name: str
    endpoint: str
    protocol: str
    method: str
    parameters: dict[str, Any]
    repo_alias: str
    priority: str
    warmup_count: int = 3
    measurement_count: int = 20


def _validate_scenario(data: dict[str, Any]) -> Scenario:
    """
    Validate a scenario dict and return a Scenario dataclass.

    Raises:
        ValueError: If any required field is missing or has an invalid value.
    """
    for required in REQUIRED_FIELDS:
        if required not in data:
            raise ValueError(
                f"Scenario is missing required field '{required}': {data}"
            )

    protocol = data["protocol"]
    if protocol not in VALID_PROTOCOLS:
        raise ValueError(
            f"Invalid protocol '{protocol}'. Must be one of: {sorted(VALID_PROTOCOLS)}"
        )

    priority = data["priority"]
    if priority not in VALID_PRIORITIES:
        raise ValueError(
            f"Invalid priority '{priority}'. Must be one of: {sorted(VALID_PRIORITIES)}"
        )

    return Scenario(
        name=data["name"],
        endpoint=data["endpoint"],
        protocol=data["protocol"],
        method=data["method"],
        parameters=data["parameters"],
        repo_alias=data["repo_alias"],
        priority=data["priority"],
        warmup_count=data.get("warmup_count", 3),
        measurement_count=data.get("measurement_count", 20),
    )


def load_scenarios_from_file(path: str) -> list[Scenario]:
    """
    Load and validate scenarios from a single JSON file.

    Args:
        path: Absolute or relative path to a JSON file containing a list of scenarios.

    Returns:
        List of validated Scenario dataclasses.

    Raises:
        FileNotFoundError: If the file does not exist.
        ValueError: If the JSON is malformed, not a list, or any scenario is invalid.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Scenario file not found: {path}")

    with open(path, "r", encoding="utf-8") as f:
        raw = f.read()

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError(f"Malformed JSON in scenario file '{path}': {exc}") from exc

    if not isinstance(data, list):
        raise ValueError(
            f"Scenario file '{path}' must contain a JSON array of scenarios, got {type(data).__name__}"
        )

    return [_validate_scenario(item) for item in data]


def load_scenarios_from_dir(directory: str) -> list[Scenario]:
    """
    Load all .json scenario files from a directory.

    Args:
        directory: Path to directory containing .json scenario files.

    Returns:
        Combined list of validated Scenario dataclasses from all files.

    Raises:
        FileNotFoundError: If the directory does not exist.
        ValueError: If any file has malformed JSON or invalid scenarios.
    """
    if not os.path.isdir(directory):
        raise FileNotFoundError(f"Scenario directory not found: {directory}")

    scenarios: list[Scenario] = []
    for filename in sorted(os.listdir(directory)):
        if filename.endswith(".json"):
            filepath = os.path.join(directory, filename)
            scenarios.extend(load_scenarios_from_file(filepath))

    return scenarios
