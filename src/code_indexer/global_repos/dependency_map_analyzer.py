"""
Dependency Map Analyzer for Story #192 (Epic #191).

Implements multi-pass Claude CLI pipeline to analyze source code across
all golden repositories and produce domain-clustered dependency documents.

Architecture:
- Pass 1 (Synthesis): Reads cidx-meta descriptions to identify domain clusters
- Pass 2 (Per-domain): Analyzes source code for each domain
- Pass 3 (Index): Generates catalog and repo-to-domain matrix

Output:
- Domain-clustered markdown files with YAML frontmatter
- Written to cidx-meta/dependency-map/ directory
"""

import json
import logging
import os
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)


class DependencyMapAnalyzer:
    """
    Analyzes dependencies across golden repositories using Claude CLI.

    Uses multi-pass pipeline:
    1. Domain synthesis from repo descriptions
    2. Per-domain source code analysis
    3. Index generation with catalog and matrix
    """

    def __init__(
        self,
        golden_repos_root: Path,
        cidx_meta_path: Path,
        pass_timeout: int,
        mcp_registration_service=None,
    ):
        """
        Initialize dependency map analyzer.

        Args:
            golden_repos_root: Root directory containing all golden repo clones
            cidx_meta_path: Path to cidx-meta directory for output
            pass_timeout: Timeout in seconds for each pass (Pass 2 uses full, others use half)
            mcp_registration_service: MCPSelfRegistrationService for auto-registering CIDX as MCP server
        """
        self.golden_repos_root = Path(golden_repos_root)
        self.cidx_meta_path = Path(cidx_meta_path)
        self.pass_timeout = pass_timeout
        self._mcp_registration_service = mcp_registration_service

    def generate_claude_md(self, repo_list: List[Dict[str, Any]]) -> None:
        """
        Generate CLAUDE.md orientation file in golden-repos root (AC2).

        Creates orientation context for Claude CLI listing all repositories
        and the dependency analysis task.

        Args:
            repo_list: List of repository metadata dicts with 'alias' and 'description_summary'
        """
        content = "# CIDX Dependency Map Analysis\n\n"
        content += "## Available Repositories\n\n"

        for repo in repo_list:
            alias = repo.get("alias", "unknown")
            summary = repo.get("description_summary", "No description")
            content += f"- **{alias}**: {summary}\n"

        content += "\n## Task\n\n"
        content += (
            "Analyze cross-repository dependencies at the domain and subdomain level.\n"
        )
        content += "Focus on identifying:\n"
        content += "- Domain clusters that span multiple repositories\n"
        content += "- Code-level dependencies (imports, shared libraries, type reuse)\n"
        content += "- Data contract dependencies (shared database tables/views/schemas, file formats)\n"
        content += "- Service integration dependencies (REST/HTTP/MCP/gRPC API calls)\n"
        content += "- External tool invocation dependencies (CLI tools, subprocess calls)\n"
        content += "- Configuration coupling (shared env vars, config keys, feature flags)\n"
        content += "- Message/event contract dependencies (queues, webhooks, pub/sub)\n"
        content += "- Deployment dependencies (runtime availability requirements)\n"
        content += "- Semantic coupling (behavioral contracts without code imports)\n"

        claude_md_path = self.golden_repos_root / "CLAUDE.md"
        claude_md_path.write_text(content)
        logger.info(f"Generated CLAUDE.md orientation file at {claude_md_path}")

    def run_pass_1_synthesis(
        self,
        staging_dir: Path,
        repo_descriptions: Dict[str, str],
        max_turns: int,
    ) -> List[Dict[str, Any]]:
        """
        Run Pass 1: Domain synthesis from repository descriptions (AC1).

        Analyzes cidx-meta repository descriptions to identify domain clusters.

        Args:
            staging_dir: Staging directory for output files
            repo_descriptions: Dict mapping repo alias to description content
            max_turns: Maximum Claude CLI turns for this pass

        Returns:
            List of domain dicts with 'name', 'description', 'participating_repos'
        """
        # Build synthesis prompt
        prompt = "# Domain Synthesis Task\n\n"
        prompt += "You are running in the golden-repos root directory with filesystem access to all repositories.\n\n"
        prompt += "Analyze the following repository descriptions and identify domain clusters.\n\n"

        prompt += "## Repository Descriptions\n\n"
        for alias, content in repo_descriptions.items():
            prompt += f"### {alias}\n\n"
            prompt += f"{content}\n\n"

        prompt += "## Instructions\n\n"
        prompt += "Identify domain clusters and list participating repos per domain.\n\n"
        prompt += "If descriptions are thin or lack context:\n"
        prompt += "- Examine directory structures (folder names, file naming patterns)\n"
        prompt += "- Sample entry points (main.py, app.py, index.ts, README files)\n"
        prompt += "- Check configuration files (package.json, setup.py, Dockerfile)\n"
        prompt += "- Inspect interesting modules to infer purpose and integration patterns\n\n"
        prompt += "Cluster repositories by integration-level relationships, not just functional similarity.\n"
        prompt += "Consider: shared data sources, service-to-service calls, tool chains, deployment coupling.\n\n"
        prompt += "Output ONLY valid JSON array (no markdown, no explanations):\n"
        prompt += "[\n"
        prompt += '  {"name": "domain-name", "description": "1-sentence domain scope", "participating_repos": ["alias1", "alias2"]}\n'
        prompt += "]\n"

        # Invoke Claude CLI
        timeout = self.pass_timeout // 2  # Pass 1 uses half timeout (lighter workload)
        result = self._invoke_claude_cli(prompt, timeout, max_turns)

        # Parse JSON response
        logger.debug(f"Pass 1 raw output length: {len(result)} chars")
        try:
            domain_list = self._extract_json(result)
        except (json.JSONDecodeError, ValueError) as e:
            raise RuntimeError(
                f"Pass 1 (Synthesis) returned unparseable output: {e}"
            ) from e

        # Write domains.json to staging
        domains_file = staging_dir / "_domains.json"
        domains_file.write_text(json.dumps(domain_list, indent=2))
        logger.info(
            f"Pass 1 complete: identified {len(domain_list)} domains, wrote {domains_file}"
        )

        return domain_list

    def run_pass_2_per_domain(
        self,
        staging_dir: Path,
        domain: Dict[str, Any],
        domain_list: List[Dict[str, Any]],
        max_turns: int,
        previous_domain_dir: Optional[Path] = None,
    ) -> None:
        """
        Run Pass 2: Per-domain source code analysis (AC1).

        Analyzes source code interface surfaces for repositories in the domain.

        Args:
            staging_dir: Staging directory for output files
            domain: Domain dict with 'name', 'description', 'participating_repos'
            domain_list: Full list of all domains (for cross-domain awareness)
            max_turns: Maximum Claude CLI turns for this pass
            previous_domain_dir: Previous dependency-map dir for incremental improvement
        """
        domain_name = domain["name"]
        participating_repos = domain.get("participating_repos", [])

        # Build per-domain prompt
        prompt = f"# Domain Analysis: {domain_name}\n\n"
        prompt += f"**Domain Description**: {domain.get('description', 'N/A')}\n\n"

        prompt += "## Full Domain Structure (for cross-domain awareness)\n\n"
        for d in domain_list:
            prompt += f"- **{d['name']}**: {d.get('description', 'N/A')}\n"
            prompt += f"  - Repos: {', '.join(d.get('participating_repos', []))}\n"

        prompt += f"\n## Focus Analysis on Domain: {domain_name}\n\n"
        prompt += f"Analyze dependencies for: {', '.join(participating_repos)}\n\n"

        prompt += "## Dependency Types to Identify\n\n"
        prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
        prompt += "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        prompt += "  Example: 'web-app imports shared-types package for User interface'\n\n"
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += "  Example: 'lambda-processor reads customer_summary_view exposed by core-db'\n\n"
        prompt += "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        prompt += "  Example: 'frontend calls backend /api/auth endpoint for login'\n\n"
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "  Example: 'deployment-scripts invoke cidx CLI for indexing'\n\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "  Example: 'worker-service and api-service both read REDIS_URL from env'\n\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "  Example: 'order-service publishes order.created event consumed by notification-service'\n\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += "  Example: 'web-app requires auth-service to be running and reachable'\n\n"
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n"
        prompt += "  Example: 'analytics-pipeline expects user-service to always include email field in user records'\n\n"

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"
        prompt += "**CORRECT**: 'auth-service JWT subsystem provides token validation consumed by web-app middleware layer'\n\n"
        prompt += "**INCORRECT (too granular)**: 'auth-service/src/jwt/validator.py:validate_token() called by web-app/src/middleware/auth.py'\n\n"
        prompt += "**INCORRECT (too abstract)**: 'auth-service is used by web-app'\n\n"

        # Feed existing analysis for incremental improvement
        if previous_domain_dir and (previous_domain_dir / f"{domain_name}.md").exists():
            existing_content = (previous_domain_dir / f"{domain_name}.md").read_text()
            prompt += "## Previous Analysis (refine and improve)\n\n"
            prompt += existing_content + "\n\n"

        prompt += "## Output Format\n\n"
        prompt += "Provide: overview, repo roles, subdomain dependencies, cross-domain connections.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        # Invoke Claude CLI
        result = self._invoke_claude_cli(prompt, self.pass_timeout, max_turns)

        # Build YAML frontmatter
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = "---\n"
        frontmatter += f"domain: {domain_name}\n"
        frontmatter += f"last_analyzed: {now}\n"
        frontmatter += "participating_repos:\n"
        for repo in participating_repos:
            frontmatter += f"  - {repo}\n"
        frontmatter += "---\n\n"

        # Write domain file
        domain_file = staging_dir / f"{domain_name}.md"
        domain_file.write_text(frontmatter + result)
        logger.info(f"Pass 2 complete for domain '{domain_name}': wrote {domain_file}")

    def run_pass_3_index(
        self,
        staging_dir: Path,
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        max_turns: int,
    ) -> None:
        """
        Run Pass 3: Index generation with catalog and matrix (AC1).

        Generates domain catalog table and repo-to-domain matrix.

        Args:
            staging_dir: Staging directory for output files
            domain_list: List of domain dicts
            repo_list: List of repository metadata dicts
            max_turns: Maximum Claude CLI turns for this pass
        """
        # Build index generation prompt
        prompt = "# Index Generation Task\n\n"
        prompt += "Read all domain files in the staging directory and generate:\n"
        prompt += "1. Domain Catalog table listing all identified domains\n"
        prompt += "2. Repo-to-Domain Matrix mapping repos to domains\n\n"

        prompt += "## Domain List\n\n"
        for domain in domain_list:
            prompt += f"- {domain['name']}: {domain.get('description', 'N/A')}\n"

        prompt += "\n## Repository List\n\n"
        for repo in repo_list:
            prompt += f"- {repo.get('alias', 'unknown')}: {repo.get('description_summary', 'N/A')}\n"

        prompt += "\n## Output Format\n\n"
        prompt += "Generate markdown with domain catalog and matrix tables.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        # Invoke Claude CLI
        timeout = self.pass_timeout // 2  # Pass 3 uses half timeout (lighter workload)
        result = self._invoke_claude_cli(prompt, timeout, max_turns)

        # Build YAML frontmatter
        now = datetime.now(timezone.utc).isoformat()
        frontmatter = "---\n"
        frontmatter += "schema_version: 1.0\n"
        frontmatter += f"last_analyzed: {now}\n"
        frontmatter += f"repos_analyzed_count: {len(repo_list)}\n"
        frontmatter += f"domains_count: {len(domain_list)}\n"
        frontmatter += "repos_analyzed:\n"
        for repo in repo_list:
            alias = repo.get("alias", repo.get("name", "unknown"))
            frontmatter += f"  - {alias}\n"
        frontmatter += "---\n\n"

        # Write index file
        index_file = staging_dir / "_index.md"
        index_file.write_text(frontmatter + result)
        logger.info(f"Pass 3 complete: wrote {index_file}")

    @staticmethod
    def _strip_code_fences(text: str) -> str:
        """Strip markdown code fences from Claude CLI output."""
        text = text.strip()
        if text.startswith("```"):
            # Remove first line (```json or ```)
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3].rstrip()
        return text

    @staticmethod
    def _extract_json(text: str) -> Any:
        """
        Extract JSON from Claude CLI output, handling preambles and code fences.

        Claude CLI with --print sometimes returns natural language text before JSON:
        "Based on my analysis... [JSON]" or "```json [JSON] ```"

        This method:
        1. Strips markdown code fences (```json...```)
        2. Finds first JSON bracket ([ or {)
        3. Extracts from that position to matching closing bracket
        4. Validates and parses the JSON

        Args:
            text: Raw Claude CLI output

        Returns:
            Parsed JSON object (dict or list)

        Raises:
            ValueError: If no valid JSON found in the text
        """
        logger.debug(f"Extracting JSON from output (length={len(text)})")

        # Step 1: Strip markdown code fences
        text = DependencyMapAnalyzer._strip_code_fences(text)

        # Step 2: Find first JSON bracket
        start_idx = -1
        start_bracket = None
        for i, char in enumerate(text):
            if char in "[{":
                start_idx = i
                start_bracket = char
                break

        if start_idx == -1:
            raise ValueError(
                f"No JSON found in output (first 200 chars): {text[:200]}"
            )

        # Step 3: Find matching closing bracket using bracket counting
        # Track string state to ignore brackets inside JSON string values
        bracket_count = 0
        end_idx = -1
        in_string = False
        escape_next = False

        for i in range(start_idx, len(text)):
            char = text[i]

            # Handle escape sequences
            if escape_next:
                escape_next = False
                continue
            if char == '\\' and in_string:
                escape_next = True
                continue

            # Track string boundaries
            if char == '"':
                in_string = not in_string
                continue

            # Only count brackets outside of strings
            if in_string:
                continue

            if char in "[{":
                bracket_count += 1
            elif char in "]}":
                bracket_count -= 1
                if bracket_count == 0:
                    end_idx = i
                    break

        if end_idx == -1:
            raise ValueError(
                f"No matching closing bracket found for JSON starting at position {start_idx} "
                f"(context: {text[start_idx:start_idx+200]})"
            )

        # Step 4: Extract and validate JSON
        json_text = text[start_idx : end_idx + 1]
        try:
            parsed = json.loads(json_text)
            logger.debug(f"Successfully extracted JSON: {type(parsed).__name__}")
            return parsed
        except json.JSONDecodeError as e:
            raise ValueError(
                f"Extracted text is not valid JSON: {e} (first 200 chars): {json_text[:200]}"
            ) from e

    def _invoke_claude_cli(self, prompt: str, timeout: int, max_turns: int) -> str:
        """
        Invoke Claude CLI with direct subprocess (AC1).

        Verifies ANTHROPIC_API_KEY environment variable is available, then invokes
        Claude CLI as a subprocess from golden-repos root.

        Args:
            prompt: Prompt to send to Claude
            timeout: Timeout in seconds
            max_turns: Maximum number of agentic turns

        Returns:
            Claude CLI stdout output

        Raises:
            subprocess.CalledProcessError: If Claude CLI fails
            subprocess.TimeoutExpired: If timeout is exceeded
        """
        # Verify API key is available
        if not os.environ.get("ANTHROPIC_API_KEY"):
            raise RuntimeError(
                "Claude API key not available -- configure Claude integration first"
            )

        # Auto-register CIDX as MCP server (Story #203)
        if self._mcp_registration_service:
            self._mcp_registration_service.ensure_registered()

        # Build command
        cmd = ["claude", "--print", "--max-turns", str(max_turns), "-p", prompt]

        # Run subprocess
        result = subprocess.run(
            cmd,
            cwd=str(self.golden_repos_root),
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ},  # Inherit environment including ANTHROPIC_API_KEY
            stdin=subprocess.DEVNULL,  # Prevent Claude CLI from hanging on stdin
        )

        if result.returncode != 0:
            logger.error(f"Claude CLI failed: {result.stderr}")
            raise subprocess.CalledProcessError(
                result.returncode, cmd, result.stdout, result.stderr
            )

        return self._strip_code_fences(result.stdout)

    # ========================================================================
    # Story #193: Delta Refresh Prompt Methods
    # ========================================================================

    def build_delta_merge_prompt(
        self,
        domain_name: str,
        existing_content: str,
        changed_repos: List[str],
        new_repos: List[str],
        removed_repos: List[str],
        domain_list: List[str],
    ) -> str:
        """
        Build delta merge prompt with self-correction mandate (Story #193).

        Args:
            domain_name: Name of the domain to update
            existing_content: Current domain file content
            changed_repos: List of changed repo aliases
            new_repos: List of new repo aliases
            removed_repos: List of removed repo aliases
            domain_list: List of all domain names for cross-domain awareness

        Returns:
            Prompt for Claude CLI delta merge
        """
        prompt = f"# Delta Update for Domain: {domain_name}\n\n"

        prompt += "## Task\n\n"
        prompt += "Update the existing domain analysis by incorporating changes from modified repositories.\n\n"

        prompt += "## Existing Domain Analysis\n\n"
        prompt += existing_content + "\n\n"

        prompt += "## Changed Repositories\n\n"
        if changed_repos:
            prompt += "Re-verify ALL dependencies for these repos (commit changes detected):\n"
            for alias in changed_repos:
                prompt += f"- {alias}\n"
            prompt += "\n"
        else:
            prompt += "None\n\n"

        prompt += "## New Repositories\n\n"
        if new_repos:
            prompt += "Incorporate these newly registered repos:\n"
            for alias in new_repos:
                prompt += f"- {alias}\n"
            prompt += "\n"
        else:
            prompt += "None\n\n"

        prompt += "## Removed Repositories\n\n"
        if removed_repos:
            prompt += "Remove ALL references to these repos (no longer registered):\n"
            for alias in removed_repos:
                prompt += f"- {alias}\n"
            prompt += "\n"
        else:
            prompt += "None\n\n"

        prompt += "## Domain Context\n\n"
        prompt += "All domains in this analysis:\n"
        for domain in domain_list:
            prompt += f"- {domain}\n"
        prompt += "\n"

        prompt += "## Dependency Types to Identify\n\n"
        prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
        prompt += "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n\n"

        prompt += "## CRITICAL SELF-CORRECTION RULES\n\n"
        prompt += "1. For every CHANGED repo: re-verify ALL dependencies listed for that repo against current source code\n"
        prompt += "2. REMOVE dependencies that are no longer present in source code (do NOT preserve stale deps)\n"
        prompt += (
            "3. CORRECT dependencies where the nature of the relationship changed\n"
        )
        prompt += "4. ADD new dependencies discovered in changed/new repos\n"
        prompt += "5. For UNCHANGED repos: preserve existing analysis as-is\n\n"

        prompt += "If you cannot confirm a previously documented dependency from current source code, REMOVE it.\n\n"

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"
        prompt += "**CORRECT**: 'auth-service JWT subsystem provides token validation consumed by web-app middleware layer'\n\n"
        prompt += "**INCORRECT (too granular)**: 'auth-service/src/jwt/validator.py:validate_token() called by web-app/src/middleware/auth.py'\n\n"
        prompt += "**INCORRECT (too abstract)**: 'auth-service is used by web-app'\n\n"

        prompt += "## Output Format\n\n"
        prompt += "Provide: overview, repo roles, subdomain dependencies, cross-domain connections.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        return prompt

    def build_domain_discovery_prompt(
        self,
        new_repos: List[Dict[str, Any]],
        existing_domains: List[str],
    ) -> str:
        """
        Build domain discovery prompt for new repos (Story #193).

        Args:
            new_repos: List of new repo dicts with alias and description_summary
            existing_domains: List of existing domain names

        Returns:
            Prompt for discovering which domains new repos belong to
        """
        prompt = "# Domain Discovery for New Repositories\n\n"

        prompt += "## New Repositories\n\n"
        for repo in new_repos:
            alias = repo.get("alias", "unknown")
            summary = repo.get("description_summary", "No description")
            prompt += f"- **{alias}**: {summary}\n"
        prompt += "\n"

        prompt += "## Existing Domains\n\n"
        for domain in existing_domains:
            prompt += f"- {domain}\n"
        prompt += "\n"

        prompt += "## Task\n\n"
        prompt += "For each new repository, determine which existing domain(s) it belongs to, "
        prompt += "or identify if it represents a new domain.\n\n"

        prompt += "Output JSON array:\n"
        prompt += "[\n"
        prompt += '  {"repo": "alias", "domains": ["domain1", "domain2"]}\n'
        prompt += "]\n"

        return prompt

    def build_new_domain_prompt(
        self,
        domain_name: str,
        participating_repos: List[str],
    ) -> str:
        """
        Build prompt for creating a new domain file (Story #193).

        Args:
            domain_name: Name of the new domain
            participating_repos: List of repo aliases in this domain

        Returns:
            Prompt for generating new domain analysis
        """
        prompt = f"# Create New Domain: {domain_name}\n\n"

        prompt += "## Participating Repositories\n\n"
        for alias in participating_repos:
            prompt += f"- {alias}\n"
        prompt += "\n"

        prompt += "## Task\n\n"
        prompt += f"Analyze source code to create a new domain analysis for '{domain_name}'.\n\n"

        prompt += "## Dependency Types to Identify\n\n"
        prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
        prompt += "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n\n"

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "Provide: overview, repo roles, subdomain dependencies, cross-domain connections.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        return prompt

    def invoke_delta_merge(self, prompt: str, timeout: int, max_turns: int) -> str:
        """
        Invoke Claude CLI for delta merge analysis (Story #193).

        Public method for delta merge invocations that maintains encapsulation
        by wrapping the private _invoke_claude_cli method.

        Args:
            prompt: Delta merge prompt to send to Claude
            timeout: Timeout in seconds
            max_turns: Maximum number of agentic turns

        Returns:
            Claude CLI stdout output

        Raises:
            subprocess.CalledProcessError: If Claude CLI fails
            subprocess.TimeoutExpired: If timeout is exceeded
        """
        return self._invoke_claude_cli(prompt, timeout, max_turns)
