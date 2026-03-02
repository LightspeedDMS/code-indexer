"""
Pre-flight validation for the CIDX performance test harness.

Story #333: Performance Test Harness with Single-User Baselines
AC8: Test Repository Prerequisites - validate repo aliases before running tests.
"""

from __future__ import annotations

import json

import httpx

from client import PerfClient, build_auth_headers, build_mcp_envelope


def _parse_repo_aliases_from_response(body: dict) -> set[str]:
    """
    Extract repository aliases from a list_global_repos MCP response body.

    The response wraps repos in result.content[].text as JSON.
    Returns an empty set if the response cannot be parsed.
    """
    available: set[str] = set()
    content = body.get("result", {}).get("content", [])
    for item in content:
        if not isinstance(item, dict) or "text" not in item:
            continue
        try:
            repos_data = json.loads(item["text"])
            if isinstance(repos_data, dict):
                repo_list = repos_data.get("repos") or repos_data.get("repositories") or []
                for repo in repo_list:
                    if "alias" in repo:
                        available.add(repo["alias"])
        except (ValueError, json.JSONDecodeError):
            # Item text is not valid JSON - skip it
            pass
    return available


async def validate_repos_exist(
    server_url: str,
    username: str,
    password: str,
    repo_aliases: list[str],
) -> list[str]:
    """
    Pre-flight check: validate that all required repo aliases exist on the server.

    Args:
        server_url: Base URL of the CIDX server.
        username: Login username.
        password: Login password.
        repo_aliases: List of repository aliases to check.

    Returns:
        List of missing repo aliases (empty list if all found or check cannot run).

    Raises:
        RuntimeError: If authentication fails.
    """
    perf_client = PerfClient(server_url=server_url, username=username, password=password)

    async with httpx.AsyncClient(timeout=30.0) as http_client:
        await perf_client.authenticate(http_client)

        # Initial check: confirm the MCP endpoint responds
        result = await perf_client.execute_mcp(
            client=http_client,
            tool_name="list_global_repos",
            arguments={},
        )

        if not result.success:
            raise RuntimeError(
                f"Failed to list repositories for pre-flight check: {result.error_message}"
            )

        # Make a raw request to parse the full response body
        try:
            token = perf_client._token_tracker.token  # type: ignore[union-attr]
            envelope = build_mcp_envelope("list_global_repos", {}, 999)
            headers = build_auth_headers(token)
            response = await http_client.post(perf_client.mcp_url, json=envelope, headers=headers)
            body = response.json()
            available = _parse_repo_aliases_from_response(body)
            return [alias for alias in repo_aliases if alias not in available]

        except (ValueError, json.JSONDecodeError, AttributeError):
            # Cannot parse repo list - skip pre-flight check rather than false-positive
            return []
