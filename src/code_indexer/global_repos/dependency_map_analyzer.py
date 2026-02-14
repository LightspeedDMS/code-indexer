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
            clone_path = repo.get("clone_path", "unknown")
            content += f"- **{alias}**: {summary}\n"
            content += f"  - Path: `{clone_path}`\n"

        content += "\n## Tools Available\n\n"
        content += "You MUST use the `cidx-local` MCP server's `search_code` tool for semantic code search.\n"
        content += "Search for repo names, class names, and API endpoints across all repositories to discover integration patterns.\n\n"

        content += "## Task\n\n"
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
        repo_list: List[Dict[str, Any]],
        max_turns: int,
    ) -> List[Dict[str, Any]]:
        """
        Run Pass 1: Domain synthesis from repository descriptions (AC1).

        Analyzes cidx-meta repository descriptions to identify domain clusters.

        Args:
            staging_dir: Staging directory for output files
            repo_descriptions: Dict mapping repo alias to description content
            repo_list: List of repository metadata dicts with alias and clone_path
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

        prompt += "## Repository Filesystem Locations\n\n"
        prompt += "Each repository is available on disk. Use these paths to explore source code:\n\n"
        # Build alias-to-path mapping from repo_list
        for repo in repo_list:
            alias = repo.get("alias", "unknown")
            clone_path = repo.get("clone_path", "unknown")
            prompt += f"- **{alias}**: `{clone_path}`\n"
        prompt += "\n"

        prompt += "## Instructions\n\n"
        prompt += "Identify domain clusters and list participating repos per domain.\n\n"

        prompt += "### Source-Code-First Exploration (MANDATORY)\n\n"
        prompt += "ALWAYS examine source code, not just descriptions. Documentation may be incomplete or misleading. Source code is the ground truth.\n\n"
        prompt += "For each repository:\n"
        prompt += "1. Assess documentation depth relative to codebase size (file count, directory depth)\n"
        prompt += "2. If a repo description is short/generic but has many source files, the description is unreliable - explore source\n"
        prompt += "3. Look at imports, entry points (main.py, app.py, index.ts), config files (package.json, setup.py, requirements.txt, Dockerfile)\n"
        prompt += "4. Check build files, test patterns, directory structures to understand actual repo purpose\n"
        prompt += "5. Examine interesting modules to infer purpose and integration patterns\n\n"

        prompt += "### Evidence-Based Domain Clustering\n\n"
        prompt += "Cluster repositories by integration-level relationships, not just functional similarity.\n"
        prompt += "Consider: shared data sources, service-to-service calls, tool chains, deployment coupling.\n\n"
        prompt += "For each domain clustering decision, briefly justify WHY repos belong together based on what you observed in source code (not just description similarity).\n\n"

        prompt += "### Domain Clustering Standards\n\n"
        prompt += "Cluster repositories that have integration relationships. Evidence can include:\n"
        prompt += "- Direct: shared imports, API calls, configuration references, deployment scripts\n"
        prompt += "- Transitive: A depends on B depends on C (all three belong in same domain)\n"
        prompt += "- Semantic: A reads data that B produces, A calls services that B exposes\n"
        prompt += "- Ecosystem: A and B are tools in the same workflow (e.g., one generates data, another visualizes it)\n\n"
        prompt += "DO NOT cluster based solely on naming similarity without verifying in source code.\n"
        prompt += "DO NOT cluster based on general knowledge without source code evidence.\n"
        prompt += "But DO cluster when you find source-code-verified integration of ANY type listed above.\n\n"
        prompt += "AIM for 3-7 domains for a typical multi-repo codebase. If you find fewer than 3 domains,\n"
        prompt += "consider whether you may be applying too strict a threshold for integration evidence.\n\n"

        prompt += "### Unassigned Repository Handling\n\n"
        prompt += "If a repository does not fit any domain (no integration evidence found), assign it to a\n"
        prompt += "single-repo domain named after the repository itself (e.g., 'code-indexer' domain with\n"
        prompt += "just the code-indexer repo). This ensures every repository appears in at least one domain.\n"
        prompt += "Do NOT leave repositories unassigned.\n\n"

        prompt += "### CRITICAL: Valid Repository Aliases\n\n"
        prompt += "ONLY the following repository aliases are valid. Do NOT invent or modify alias names.\n"
        prompt += "Every alias in your output MUST come from this exact list:\n\n"
        for repo in repo_list:
            alias = repo.get("alias", "unknown")
            prompt += f"- `{alias}`\n"
        prompt += "\nAny domain containing repos not in this list will be rejected by validation.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "Output ONLY valid JSON array (no markdown, no explanations):\n"
        prompt += "[\n"
        prompt += '  {"name": "domain-name", "description": "1-sentence domain scope", "participating_repos": ["alias1", "alias2"], "evidence": "Brief justification referencing actual files/patterns observed"}\n'
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

        # Validate Pass 1 output: filter hallucinated repos, catch unassigned repos
        valid_aliases = {r.get("alias") for r in repo_list}

        # Filter out hallucinated repo aliases from domain assignments
        for domain in domain_list:
            original_repos = domain.get("participating_repos", [])
            filtered_repos = [r for r in original_repos if r in valid_aliases]
            removed = set(original_repos) - set(filtered_repos)
            if removed:
                logger.warning(
                    f"Pass 1 hallucinated repo(s) {removed} in domain "
                    f"'{domain.get('name')}' - removed"
                )
            domain["participating_repos"] = filtered_repos

        # Remove domains that became empty after filtering
        domain_list = [
            d for d in domain_list if d.get("participating_repos")
        ]

        # Auto-create standalone domains for unassigned repos
        assigned_repos = set()
        for domain in domain_list:
            assigned_repos.update(domain.get("participating_repos", []))

        unassigned = valid_aliases - assigned_repos
        for alias in sorted(unassigned):
            # Find description from repo_list
            desc = "No description"
            for r in repo_list:
                if r.get("alias") == alias:
                    desc = r.get("description_summary", "No description")
                    break
            domain_list.append(
                {
                    "name": alias,
                    "description": f"Standalone: {desc}",
                    "participating_repos": [alias],
                    "evidence": f"Auto-assigned: {alias} was not placed in any domain by Pass 1",
                }
            )
            logger.warning(
                f"Pass 1 did not assign repo '{alias}' - auto-creating standalone domain"
            )

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
        repo_list: List[Dict[str, Any]],
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
            repo_list: List of repository metadata dicts with alias and clone_path
            max_turns: Maximum Claude CLI turns for this pass
            previous_domain_dir: Previous dependency-map dir for incremental improvement
        """
        domain_name = domain["name"]
        participating_repos = domain.get("participating_repos", [])

        # Build per-domain prompt
        prompt = f"# Domain Analysis: {domain_name}\n\n"
        prompt += f"**Domain Description**: {domain.get('description', 'N/A')}\n\n"

        # Include Pass 1 evidence for verification
        evidence = domain.get("evidence", "")
        if evidence:
            prompt += f"**Pass 1 Evidence (verify or refute)**: {evidence}\n\n"

        prompt += "## Full Domain Structure (for cross-domain awareness)\n\n"
        for d in domain_list:
            prompt += f"- **{d['name']}**: {d.get('description', 'N/A')}\n"
            prompt += f"  - Repos: {', '.join(d.get('participating_repos', []))}\n"

        prompt += f"\n## Focus Analysis on Domain: {domain_name}\n\n"
        prompt += f"Analyze dependencies for: {', '.join(participating_repos)}\n\n"

        prompt += "## Repository Filesystem Locations\n\n"
        prompt += "IMPORTANT: Each repository is a directory on disk. You MUST explore source code using these paths.\n"
        prompt += "Start by listing each repo's directory structure, then read key files (entry points, configs, manifests).\n\n"
        # Build path mapping for participating repos
        path_map = {r.get("alias"): r.get("clone_path") for r in repo_list}
        for repo_alias in participating_repos:
            clone_path = path_map.get(repo_alias, "path not found")
            prompt += f"- **{repo_alias}**: `{clone_path}`\n"
        prompt += "\n"

        prompt += "## CIDX Semantic Search (MCP Tools) - MANDATORY\n\n"
        prompt += "You MUST use the `cidx-local` MCP server's `search_code` tool during this analysis.\n"
        prompt += "It provides semantic search across ALL indexed golden repositories.\n\n"
        prompt += "### Required Searches\n\n"
        prompt += "For EACH participating repository, run at least one search:\n"
        for repo_alias in participating_repos:
            prompt += f"- Search for `{repo_alias}` references across all repos\n"
        prompt += "\n"
        prompt += "### How to Use\n\n"
        prompt += "Call the `search_code` tool with:\n"
        prompt += "- `query_text`: The search term (repo name, class name, API endpoint, etc.)\n"
        prompt += "- `limit`: Number of results (start with 10)\n\n"
        prompt += "This reveals cross-repo references that filesystem exploration alone cannot find.\n"
        prompt += "Do NOT skip MCP searches - they are essential for discovering service integration and semantic coupling.\n\n"

        prompt += "### Minimum Search Requirements\n\n"
        prompt += "You MUST call the `search_code` MCP tool AT LEAST 3 times during this analysis.\n"
        prompt += "Failure to use MCP search invalidates the analysis. Recommended searches:\n"
        prompt += "1. Each participating repo name (to find cross-repo references)\n"
        prompt += "2. Key class/function/module names discovered during source code exploration\n"
        prompt += "3. Shared identifiers, API endpoints, or configuration keys\n\n"
        prompt += "### All Repository Aliases (for cross-domain reference searches)\n\n"
        prompt += "These are ALL repos in the ecosystem. Search for these names to find cross-domain connections:\n\n"
        all_aliases = sorted(r.get("alias", "unknown") for r in repo_list)
        for alias in all_aliases:
            if alias not in participating_repos:
                prompt += f"- `{alias}` (other domain)\n"
        prompt += "\n"

        prompt += "## Source Code Exploration Mandate\n\n"
        prompt += "DO NOT rely solely on README files or documentation. Actively explore:\n"
        prompt += "- Import statements and package dependencies (requirements.txt, package.json, setup.py, go.mod)\n"
        prompt += "- Entry points (main.py, app.py, index.ts, cmd/ directories)\n"
        prompt += "- Configuration files for references to other repos/services\n"
        prompt += "- API endpoint definitions and client code\n"
        prompt += "- Test files (often reveal integration dependencies)\n"
        prompt += "- Build and deployment scripts\n\n"
        prompt += "Assess each repo's documentation depth relative to its codebase size.\n"
        prompt += "A repo with 100+ source files and a 5-line README has unreliable documentation - explore its source code thoroughly.\n\n"

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

        prompt += "## MANDATORY: Fact-Check Pass 1 Domain Assignments\n\n"
        prompt += "Before analyzing dependencies, verify that each repository listed in this domain actually belongs here.\n"
        prompt += "For each participating repo:\n"
        prompt += "1. Examine its source code, imports, and integration points\n"
        prompt += "2. Confirm it has actual code-level or integration relationships with other repos in this domain\n"
        prompt += "3. If a repo does NOT belong in this domain based on source code evidence, state this explicitly\n\n"

        prompt += "## MANDATORY: Evidence-Based Claims\n\n"
        prompt += "Every dependency you document MUST include:\n"
        prompt += "1. **Source reference**: The specific module, package, or subsystem where the dependency manifests (e.g., \"code-indexer's server/mcp/handlers.py module\")\n"
        prompt += "2. **Evidence type**: What you observed (import statement, API endpoint definition, configuration key, subprocess invocation, etc.)\n"
        prompt += "3. **Reasoning**: Why this constitutes a dependency and what would break if the depended-on component changed\n\n"
        prompt += "DO NOT document dependencies based on:\n"
        prompt += "- Assumptions about what \"should\" exist\n"
        prompt += "- Naming similarity between repos\n"
        prompt += "- General knowledge about how similar systems typically work\n"
        prompt += "- Documentation claims you cannot verify in source code\n\n"
        prompt += "If you cannot find concrete evidence of a dependency in actual source files, DO NOT include it.\n\n"

        prompt += "### External Dependency Verification\n\n"
        prompt += "For external/third-party dependencies, you MUST read the actual manifest file:\n"
        prompt += "- Python: requirements.txt, setup.py, pyproject.toml\n"
        prompt += "- JavaScript/TypeScript: package.json\n"
        prompt += "- .NET/C#: *.csproj, *.sln, packages.config\n"
        prompt += "- Go: go.mod\n"
        prompt += "- Java: pom.xml, build.gradle\n"
        prompt += "- Rust: Cargo.toml\n\n"
        prompt += "DO NOT list external dependencies from memory or general knowledge of similar systems.\n"
        prompt += "If you cannot find the dependency manifest file, state 'dependency manifest not found' rather than guessing.\n\n"

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

        prompt += "## AUTHORITATIVE Domain Assignments (from Pass 1 - use these EXACTLY)\n\n"
        for domain in domain_list:
            prompt += f"- **{domain['name']}**: {domain.get('description', 'N/A')}\n"
            participating = domain.get('participating_repos', [])
            if participating:
                prompt += f"  - Participating repos: {', '.join(participating)}\n"
            else:
                prompt += "  - Participating repos: (none)\n"

        prompt += "\n## Fact-Check Requirement\n\n"
        prompt += "The Repository-to-Domain Mapping Matrix MUST match the authoritative domain assignments above exactly.\n"
        prompt += "Do NOT reassign repositories to different domains.\n"
        prompt += "Do NOT omit any repository from the mapping.\n"
        prompt += "Every repo in the Repository List below must appear in the mapping matrix.\n\n"

        prompt += "## Repository List\n\n"
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
        cmd = [
            "claude",
            "--print",
            "--max-turns",
            str(max_turns),
            "--allowedTools",
            "mcp__cidx-local__search_code",
            "-p",
            prompt,
        ]

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

        # Diagnostic logging for debugging empty output issues
        raw_stdout_len = len(result.stdout) if result.stdout else 0
        raw_stderr_len = len(result.stderr) if result.stderr else 0
        logger.info(
            f"Claude CLI completed: returncode={result.returncode}, "
            f"stdout={raw_stdout_len} chars, stderr={raw_stderr_len} chars"
        )
        if raw_stdout_len == 0:
            logger.warning(
                f"Claude CLI returned EMPTY stdout. "
                f"stderr (first 1000 chars): {(result.stderr or '')[:1000]}"
            )
        elif raw_stdout_len < 100:
            logger.warning(
                f"Claude CLI returned very short stdout: {result.stdout!r}"
            )
        else:
            logger.debug(
                f"Claude CLI stdout (first 500 chars): {result.stdout[:500]}"
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

        prompt += "## Evidence-Based Claims Requirement\n\n"
        prompt += "Every dependency you document MUST include a source reference (module/subsystem name) and evidence type.\n"
        prompt += "Do NOT preserve or add dependencies you cannot verify from current source code.\n"
        prompt += "\"I assume this exists\" is NOT evidence. \"I found import X in module Y\" IS evidence.\n\n"

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
