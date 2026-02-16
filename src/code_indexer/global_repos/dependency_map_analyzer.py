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
import re
import shlex
import subprocess
import tempfile
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
        analysis_model: str = "opus",
    ):
        """
        Initialize dependency map analyzer.

        Args:
            golden_repos_root: Root directory containing all golden repo clones
            cidx_meta_path: Path to cidx-meta directory for output
            pass_timeout: Timeout in seconds for each pass (Pass 2 uses full, others use half)
            mcp_registration_service: MCPSelfRegistrationService for auto-registering CIDX as MCP server
            analysis_model: Claude model to use ("opus" or "sonnet", default: "opus")
        """
        self.golden_repos_root = Path(golden_repos_root)
        self.cidx_meta_path = Path(cidx_meta_path)
        self.pass_timeout = pass_timeout
        self._mcp_registration_service = mcp_registration_service
        self.analysis_model = analysis_model

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
        content += (
            "- External tool invocation dependencies (CLI tools, subprocess calls)\n"
        )
        content += (
            "- Configuration coupling (shared env vars, config keys, feature flags)\n"
        )
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
            file_count = repo.get("file_count", "?")
            total_mb = round(repo.get("total_bytes", 0) / (1024 * 1024), 1)
            prompt += f"- **{alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
        prompt += "\n"

        prompt += "## Instructions\n\n"
        prompt += (
            "Identify domain clusters and list participating repos per domain.\n\n"
        )

        prompt += "### Source-Code-First Exploration (MANDATORY)\n\n"
        prompt += "ALWAYS examine source code, not just descriptions. Documentation may be incomplete or misleading. Source code is the ground truth.\n\n"
        prompt += "For each repository:\n"
        prompt += "1. Assess documentation depth relative to codebase size (file count, directory depth)\n"
        prompt += "2. If a repo description is short/generic but has many source files, the description is unreliable - explore source\n"
        prompt += "3. Look at imports, entry points, and config/manifest files (e.g., package.json, requirements.txt, setup.py, pyproject.toml, go.mod, Cargo.toml, pom.xml, build.gradle, *.csproj, CMakeLists.txt, Makefile, Dockerfile)\n"
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
        prompt += (
            "DO NOT cluster based on general knowledge without source code evidence.\n"
        )
        prompt += "But DO cluster when you find source-code-verified integration of ANY type listed above.\n\n"
        repo_count = len(repo_list)
        if repo_count <= 20:
            domain_guidance = "3-7"
        elif repo_count <= 50:
            domain_guidance = "5-15"
        elif repo_count <= 100:
            domain_guidance = "10-30"
        else:
            domain_guidance = "15-50"
        prompt += f"AIM for {domain_guidance} domains for a codebase of {repo_count} repositories. If you find very few domains,\n"
        prompt += "consider whether you may be applying too strict a threshold for integration evidence.\n\n"

        prompt += "### COMPLETENESS MANDATE\n\n"
        prompt += f"There are exactly {len(repo_list)} repositories. Your output MUST assign ALL {len(repo_list)} of them.\n"
        prompt += "Verify INTERNALLY that total repos across all domains equals " + str(len(repo_list)) + ". Do NOT output the verification.\n"
        prompt += "If you cannot find integration evidence for a repo, assign it to its own standalone domain.\n"
        prompt += "MISSING REPOS = FAILED ANALYSIS. Every valid alias must appear exactly once.\n"
        prompt += "All verification must be done INTERNALLY. Your output must contain ONLY JSON.\n\n"

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
        prompt += "\nAny domain containing repos not in this list will be rejected by validation.\n"
        prompt += f"\nCOMPLETENESS CHECK: Your output must contain exactly {len(repo_list)} repos total across all domains.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "Your ENTIRE response must be ONLY a valid JSON array. No markdown, no explanations, no verification text, no preamble.\n"
        prompt += "Do NOT output completeness checks, summaries, or commentary. ONLY the JSON array.\n"
        prompt += "For each domain, include a `repo_paths` object mapping each alias to its FULL filesystem path.\n"
        prompt += "If you cannot provide the real path for a repo, DO NOT include that repo.\n\n"
        prompt += "[\n"
        prompt += '  {"name": "domain-name", "description": "1-sentence domain scope", '
        prompt += '"participating_repos": ["alias1", "alias2"], '
        prompt += '"repo_paths": {"alias1": "/full/path/to/alias1", "alias2": "/full/path/to/alias2"}, '
        prompt += '"evidence": "Brief justification referencing actual files/patterns observed"}\n'
        prompt += "]\n"

        # Invoke Claude CLI (Pass 1 explores all repos to identify domains and outputs JSON)
        timeout = self.pass_timeout  # Pass 1 uses full timeout (heaviest phase: explores all repos)
        result = self._invoke_claude_cli(prompt, timeout, max_turns, allowed_tools=None)

        # Parse JSON response
        logger.debug(f"Pass 1 raw output length: {len(result)} chars")
        try:
            domain_list = self._extract_json(result)
        except (json.JSONDecodeError, ValueError) as e:
            logger.warning(
                f"Pass 1 agentic attempt returned no parseable JSON ({e}), "
                f"output preview: {result[:200]!r} -- "
                "retrying in single-shot mode (max_turns=0)"
            )
            result = self._invoke_claude_cli(prompt, timeout, 0, allowed_tools=None)
            logger.debug(f"Pass 1 single-shot retry output length: {len(result)} chars")
            try:
                domain_list = self._extract_json(result)
            except (json.JSONDecodeError, ValueError) as e2:
                raise RuntimeError(
                    f"Pass 1 (Synthesis) returned unparseable output on both attempts: {e2}"
                ) from e2

        # Validate Pass 1 output: filter hallucinated repos, catch unassigned repos
        valid_aliases = {r.get("alias") for r in repo_list}

        # Filter out hallucinated repo aliases and repos with wrong/missing paths
        for domain in domain_list:
            original_repos = domain.get("participating_repos", [])
            repo_paths = domain.get("repo_paths", {})
            filtered_repos = []
            for r in original_repos:
                if r not in valid_aliases:
                    logger.warning(
                        f"Pass 1 hallucinated repo '{r}' in domain "
                        f"'{domain.get('name')}' - not in valid alias list"
                    )
                    continue
                # Validate path if provided - check alias appears as delimited segment
                # (lenient: allows different directory structures like .versioned/)
                # Uses regex to avoid false positives (e.g., "db" matching "adobe")
                claimed_path = repo_paths.get(r)
                if claimed_path:
                    # Check alias appears as a delimited segment in path (not arbitrary substring)
                    # This handles paths like /repos/.versioned/flask-large/v_123/
                    pattern = r'(?:^|[/\\_.-])' + re.escape(r) + r'(?:$|[/\\_.-])'
                    if not re.search(pattern, claimed_path):
                        logger.warning(
                            f"Pass 1 repo '{r}' has suspicious path '{claimed_path}' "
                            f"(alias not found in path) in domain "
                            f"'{domain.get('name')}' - removed"
                        )
                        continue
                filtered_repos.append(r)
            removed = set(original_repos) - set(filtered_repos)
            if removed:
                logger.warning(
                    f"Pass 1 filtered repo(s) {removed} from domain "
                    f"'{domain.get('name')}'"
                )
            domain["participating_repos"] = filtered_repos
            # Clean up repo_paths from output (not needed downstream)
            domain.pop("repo_paths", None)

        # Remove domains that became empty after filtering
        domain_list = [d for d in domain_list if d.get("participating_repos")]

        # Auto-create standalone domains for unassigned repos
        assigned_repos = set()
        for domain in domain_list:
            assigned_repos.update(domain.get("participating_repos", []))

        # Check for duplicate repo assignments
        all_assigned = []
        for domain in domain_list:
            all_assigned.extend(domain.get("participating_repos", []))
        seen = set()
        for r in all_assigned:
            if r in seen:
                logger.warning(f"Pass 1 assigned repo '{r}' to multiple domains")
            seen.add(r)

        unassigned = valid_aliases - assigned_repos
        for alias in sorted(unassigned):
            # Find description from repo_list
            desc = "No description"
            for r in repo_list:
                if r.get("alias") == alias:
                    desc = r.get("description_summary", "No description")
                    break

            # Strip markdown heading markers from description
            desc = desc.lstrip('#').strip()

            # If description equals alias name or is empty, use better default
            if not desc or desc.lower() == alias.lower():
                desc = f"{alias} (standalone repository)"

            domain_list.append(
                {
                    "name": alias,
                    "description": desc,
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

    def _build_output_first_prompt(
        self,
        domain: Dict[str, Any],
        domain_list: List[Dict[str, Any]],
        repo_list: List[Dict[str, Any]],
        previous_domain_dir: Optional[Path] = None,
    ) -> str:
        """Build output-first prompt for large domains (>3 repos)."""
        domain_name = domain["name"]
        participating_repos = domain.get("participating_repos", [])

        # Sort participating repos by size (largest first) using repo_list metadata
        repo_size_map = {r.get("alias"): r.get("total_bytes", 0) for r in repo_list}
        participating_repos_sorted = sorted(
            participating_repos, key=lambda a: repo_size_map.get(a, 0), reverse=True
        )

        prompt = f"# Domain Analysis: {domain_name}\n\n"
        prompt += "## CRITICAL INSTRUCTION: WRITE YOUR ANALYSIS FIRST\n\n"
        prompt += "You MUST write your complete domain analysis output BEFORE doing any MCP searches.\n"
        prompt += "Your primary source material is the Pass 1 evidence and repository descriptions below.\n"
        prompt += "MCP searches are OPTIONAL and limited to AT MOST 5 calls for verification only.\n\n"

        prompt += f"**Domain Description**: {domain.get('description', 'N/A')}\n\n"

        # Pass 1 evidence prominently
        evidence = domain.get("evidence", "")
        if evidence:
            prompt += f"## Pass 1 Evidence (PRIMARY SOURCE)\n\n{evidence}\n\n"

        # Domain structure
        prompt += "## Full Domain Structure\n\n"
        for d in domain_list:
            prompt += f"- **{d['name']}**: {d.get('description', 'N/A')}\n"
            prompt += f"  - Repos: {', '.join(d.get('participating_repos', []))}\n"

        prompt += "\n## Participating Repositories\n\n"
        path_map = {r.get("alias"): r.get("clone_path") for r in repo_list}
        repo_file_count_map = {r.get("alias"): r.get("file_count", "?") for r in repo_list}
        for repo_alias in participating_repos_sorted:
            clone_path = path_map.get(repo_alias, "path not found")
            file_count = repo_file_count_map.get(repo_alias, "?")
            total_mb = round(repo_size_map.get(repo_alias, 0) / (1024 * 1024), 1)
            prompt += f"- **{repo_alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
        prompt += "\n"

        # Inside-out analysis strategy (only if there are repos)
        if participating_repos_sorted:
            prompt += "## INSIDE-OUT ANALYSIS STRATEGY\n\n"
            prompt += f"Start your analysis from **{participating_repos_sorted[0]}** (largest repository, "
            prompt += f"{repo_size_map.get(participating_repos_sorted[0], 0) // 1024} KB). "
            prompt += "Map its integration points first, then fan out to discover how the smaller repositories connect to it.\n"
            prompt += "This ensures the dominant codebase anchors the dependency graph.\n\n"

        # Feed previous analysis if good quality - with explicit improvement mandate
        if previous_domain_dir and (previous_domain_dir / f"{domain_name}.md").exists():
            existing_content = (previous_domain_dir / f"{domain_name}.md").read_text()
            if self._has_markdown_headings(existing_content) and len(existing_content.strip()) > 1000:
                prompt += "## Previous Analysis (EXTEND, IMPROVE, and CORRECT)\n\n"
                prompt += "A previous analysis exists for this domain. You MUST:\n"
                prompt += "1. **Preserve** accurate findings from the previous analysis\n"
                prompt += "2. **Correct** any errors, inaccuracies, or outdated information\n"
                prompt += "3. **Extend** with new dependencies or details not previously documented\n"
                prompt += "4. **Improve** clarity, evidence quality, and structural organization\n\n"
                prompt += "Do NOT start from scratch - build upon the previous work.\n\n"
                prompt += existing_content + "\n\n"

        # Output template
        prompt += "## OUTPUT TEMPLATE (fill in each section)\n\n"
        prompt += "Your output MUST follow this exact structure:\n\n"
        prompt += "```\n"
        prompt += f"# Domain Analysis: {domain_name}\n\n"
        prompt += "## Overview\n"
        prompt += "[1-2 paragraphs describing domain scope, purpose, and how repos relate]\n\n"
        prompt += "## Repository Roles\n"
        prompt += "[For each repo: name, primary language, role within domain]\n\n"
        prompt += "## Intra-Domain Dependencies\n"
        prompt += "[Dependencies BETWEEN repos in this domain, with evidence]\n\n"
        prompt += "## Cross-Domain Connections\n"
        prompt += "[Dependencies to/from repos in OTHER domains]\n"
        prompt += "```\n\n"

        # Dependency types (condensed)
        prompt += "## Dependency Types to Document\n\n"
        prompt += "- Code-level (imports, shared libraries)\n"
        prompt += "- Data contracts (shared schemas, file formats)\n"
        prompt += "- Service integration (REST/HTTP/MCP/gRPC API calls)\n"
        prompt += "- External tool invocation (CLI tools, subprocess calls)\n"
        prompt += "- Configuration coupling (shared env vars, config keys)\n"
        prompt += "- Deployment dependencies (runtime requirements)\n"
        prompt += "- Semantic coupling (behavioral contracts)\n\n"

        # Evidence requirements (condensed)
        prompt += "## Evidence Requirements\n\n"
        prompt += "Every dependency MUST include: source reference (module/subsystem), evidence type, reasoning.\n"
        prompt += "If you cannot find concrete evidence, DO NOT include the dependency.\n\n"

        # OPTIONAL verification searches at the end
        prompt += "## OPTIONAL: MCP Verification Searches (max 5 calls)\n\n"
        prompt += "After writing your analysis, you MAY use the `search_code` MCP tool for verification.\n"
        prompt += "Limit: AT MOST 5 search_code calls total. Do NOT explore extensively.\n"
        prompt += "These searches are for CONFIRMING what you wrote, not for discovery.\n\n"

        # Prohibited content
        prompt += "## PROHIBITED Content\n\n"
        prompt += "- YAML frontmatter blocks (system adds automatically)\n"
        prompt += "- Speculative sections (Recommendations, Future Considerations)\n"
        prompt += "- Meta-commentary about your process or thinking\n"
        prompt += "- Content not supported by evidence\n\n"

        prompt += "## Output Format\n\n"
        prompt += f"Your output MUST begin with: # Domain Analysis: {domain_name}\n"
        prompt += "Follow the template structure above exactly.\n"
        prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        return prompt

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
        is_large_domain = len(participating_repos) > 3

        # Build per-domain prompt
        if is_large_domain:
            # Use output-first prompt for large domains (>3 repos)
            prompt = self._build_output_first_prompt(
                domain, domain_list, repo_list, previous_domain_dir
            )
        else:
            # Use standard prompt for small domains (<=3 repos)
            # Sort participating repos by size (largest first)
            repo_size_map = {r.get("alias"): r.get("total_bytes", 0) for r in repo_list}
            participating_repos_sorted = sorted(
                participating_repos, key=lambda a: repo_size_map.get(a, 0), reverse=True
            )

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
            prompt += f"Analyze dependencies for: {', '.join(participating_repos_sorted)}\n\n"

            # Inside-out analysis strategy (only if there are repos)
            if participating_repos_sorted:
                prompt += "## INSIDE-OUT ANALYSIS STRATEGY\n\n"
                prompt += f"Start your analysis from **{participating_repos_sorted[0]}** (largest repository, "
                prompt += f"{repo_size_map.get(participating_repos_sorted[0], 0) // 1024} KB). "
                prompt += "Map its integration points first, then fan out to discover how the smaller repositories connect to it.\n"
                prompt += "This ensures the dominant codebase anchors the dependency graph.\n\n"

            prompt += "## Repository Filesystem Locations\n\n"
            prompt += "IMPORTANT: Each repository is a directory on disk. You MUST explore source code using these paths.\n"
            prompt += "Start by listing each repo's directory structure, then read key files (entry points, configs, manifests).\n\n"
            # Build path mapping for participating repos
            path_map = {r.get("alias"): r.get("clone_path") for r in repo_list}
            repo_file_count_map = {r.get("alias"): r.get("file_count", "?") for r in repo_list}
            for repo_alias in participating_repos_sorted:
                clone_path = path_map.get(repo_alias, "path not found")
                file_count = repo_file_count_map.get(repo_alias, "?")
                total_mb = round(repo_size_map.get(repo_alias, 0) / (1024 * 1024), 1)
                prompt += f"- **{repo_alias}**: `{clone_path}` ({file_count} files, {total_mb} MB)\n"
            prompt += "\n"
    
            prompt += "## CIDX Semantic Search (MCP Tools) - MANDATORY\n\n"
            prompt += "You MUST use the `cidx-local` MCP server's `search_code` tool during this analysis.\n"
            prompt += (
                "It provides semantic search across ALL indexed golden repositories.\n\n"
            )
            prompt += "### Required Searches\n\n"
            prompt += "For EACH participating repository, run at least one search:\n"
            for repo_alias in participating_repos_sorted:
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
            prompt += (
                "DO NOT rely solely on README files or documentation. Actively explore:\n"
            )
            prompt += "- Import statements and package dependencies (requirements.txt, package.json, setup.py, go.mod)\n"
            prompt += "- Entry points (main.py, app.py, index.ts, cmd/ directories)\n"
            prompt += "- Configuration files for references to other repos/services\n"
            prompt += "- API endpoint definitions and client code\n"
            prompt += "- Test files (often reveal integration dependencies)\n"
            prompt += "- Build and deployment scripts\n\n"
            prompt += (
                "Assess each repo's documentation depth relative to its codebase size.\n"
            )
            prompt += "A repo with 100+ source files and a 5-line README has unreliable documentation - explore its source code thoroughly.\n\n"
    
            prompt += "## Dependency Types to Identify\n\n"
            prompt += "**CRITICAL**: ABSENCE of code imports does NOT mean absence of dependency.\n\n"
            prompt += (
                "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
            )
            prompt += (
                "  Example: 'web-app imports shared-types package for User interface'\n\n"
            )
            prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
            prompt += "  Example: 'lambda-processor reads customer_summary_view exposed by core-db'\n\n"
            prompt += (
                "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
            )
            prompt += "  Example: 'frontend calls backend /api/auth endpoint for login'\n\n"
            prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
            prompt += "  Example: 'deployment-scripts invoke cidx CLI for indexing'\n\n"
            prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
            prompt += "  Example: 'worker-service and api-service both read REDIS_URL from env'\n\n"
            prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
            prompt += "  Example: 'order-service publishes order.created event consumed by notification-service'\n\n"
            prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
            prompt += (
                "  Example: 'web-app requires auth-service to be running and reachable'\n\n"
            )
            prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n"
            prompt += "  Example: 'analytics-pipeline expects user-service to always include email field in user records'\n\n"
    
            prompt += "## MANDATORY: Fact-Check Pass 1 Domain Assignments\n\n"
            prompt += "Before analyzing dependencies, verify that each repository listed in this domain actually belongs here.\n"
            prompt += "For each participating repo:\n"
            prompt += "1. Examine its source code, imports, and integration points\n"
            prompt += "2. Confirm it has actual code-level or integration relationships with other repos in this domain\n"
            prompt += "3. If a repo does NOT belong in this domain based on source code evidence, state this explicitly\n\n"
    
            prompt += "## MANDATORY: Technology Stack Verification\n\n"
            prompt += "When describing a repository's technology stack or primary language:\n"
            prompt += "1. Search for dependency manifests (requirements.txt, package.json, Cargo.toml, go.mod, *.csproj, pom.xml, pyproject.toml)\n"
            prompt += "2. Check actual source file extensions in the repository (.py, .ts, .js, .rs, .go, .cs, .java, .pas)\n"
            prompt += "3. Do NOT assume technology based on tool names, library names, or general knowledge\n"
            prompt += "4. If a repo uses a library written in language X as a binding/wrapper in language Y, the repo's primary language is Y, not X\n"
            prompt += "5. State only what the dependency manifest and source files confirm\n\n"
    
            prompt += "## MANDATORY: Evidence-Based Claims\n\n"
            prompt += "Every dependency you document MUST include:\n"
            prompt += '1. **Source reference**: The specific module, package, or subsystem where the dependency manifests (e.g., "code-indexer\'s server/mcp/handlers.py module")\n'
            prompt += "2. **Evidence type**: What you observed (import statement, API endpoint definition, configuration key, subprocess invocation, etc.)\n"
            prompt += "3. **Reasoning**: Why this constitutes a dependency and what would break if the depended-on component changed\n\n"
            prompt += "DO NOT document dependencies based on:\n"
            prompt += '- Assumptions about what "should" exist\n'
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
            # FIX 2 (Iteration 11): Only feed previous analysis if it was good quality (has headings and sufficient length)
            # Bad previous content (meta-commentary) can confuse Claude into producing more meta-commentary
            if previous_domain_dir and (previous_domain_dir / f"{domain_name}.md").exists():
                existing_content = (previous_domain_dir / f"{domain_name}.md").read_text()
                if self._has_markdown_headings(existing_content) and len(existing_content.strip()) > 1000:
                    prompt += "## Previous Analysis (refine and improve)\n\n"
                    prompt += existing_content + "\n\n"
                else:
                    logger.info(
                        f"Skipping low-quality previous analysis for domain '{domain_name}' "
                        f"({len(existing_content)} chars, headings={self._has_markdown_headings(existing_content)})"
                    )

            # Iteration 14: Add conciseness constraints BEFORE prohibitions
            prompt += "## Content Guidelines\n\n"
            prompt += "Write CONCISE analysis focused on inter-repository navigation. Your audience is an MCP user deciding which repos to explore.\n"
            prompt += "- Document precise dependency connections (who calls who, shared data, integration points)\n"
            prompt += "- Include specific evidence (file names, function names, config keys) but NOT full code snippets\n"
            prompt += "- Keep each section to 3-8 sentences. Shorter is better if precise.\n"
            prompt += "- Do NOT reproduce source code, JSON schemas, or directory listings\n\n"

            # Iteration 15: Add output budget
            prompt += "## Output Budget\n\n"
            prompt += "Your analysis MUST be between 3,000 and 10,000 characters.\n"
            prompt += "If you find yourself writing more, you are including too much detail.\n"
            prompt += "Focus on WHAT connects repos, not HOW the internals work.\n\n"

            # Iteration 15: Add output template (same as output-first prompt)
            prompt += "## OUTPUT TEMPLATE (fill in each section)\n\n"
            prompt += "Your output MUST follow this exact structure:\n\n"
            prompt += f"# Domain Analysis: {domain_name}\n\n"
            prompt += "## Overview\n"
            prompt += "[1-2 paragraphs: domain scope, purpose, how repos relate]\n\n"
            prompt += "## Repository Roles\n"
            prompt += "[Table: repo | language | role within domain]\n\n"
            prompt += "## Intra-Domain Dependencies\n"
            prompt += "[Numbered list of dependencies BETWEEN repos, with evidence]\n\n"
            prompt += "## Cross-Domain Connections\n"
            prompt += "[Dependencies to/from repos in OTHER domains]\n\n"

            # Fix: Iteration 9 - Add guardrails against YAML output and speculative content
            # Iteration 15: Add more prohibited items
            prompt += "## PROHIBITED Content\n\n"
            prompt += "Do NOT include any of the following in your output:\n"
            prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
            prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', 'Future Considerations', or 'Suggested Improvements'\n"
            prompt += "- Advisory content about what SHOULD be done or COULD be integrated\n"
            prompt += "- 'MCP Searches Performed' or search audit trail sections\n"
            prompt += "- Code snippets or source code blocks\n"
            prompt += "- JSON schema definitions or field-by-field breakdowns\n"
            prompt += "- Directory listings or file tree dumps\n"
            prompt += "- Any content not directly supported by source code evidence\n\n"
            prompt += "Document ONLY verified, factual dependencies and relationships found in source code.\n\n"
    
            prompt += "## Output Format\n\n"
            prompt += "CRITICAL: Your output MUST begin with a markdown heading (# Domain Analysis: domain-name).\n"
            prompt += "Do NOT start with summary text, meta-commentary, or a description of what you found.\n"
            prompt += "The VERY FIRST LINE of your output must be a markdown heading.\n\n"
            prompt += "Provide: overview, repo roles, subdomain dependencies, cross-domain connections.\n"
            prompt += "Do NOT include any meta-commentary about your process, thinking, or search strategy.\n"
            prompt += "Do NOT generate YAML frontmatter (--- blocks). The system handles frontmatter automatically.\n"
            prompt += "Start your output directly with the analysis content (headings, sections, findings).\n\n"
            prompt += "Output ONLY the content (no markdown code blocks, no preamble).\n"

        # Fix 1 (Iteration 12): PostToolUse hook to prevent turn exhaustion
        # _invoke_claude_cli() builds turn-aware bash script with escalating urgency messages
        # Iteration 14: Purpose-driven hook emphasizing conciseness and navigation assistance
        # Iteration 15: Add character budget to hook reminder
        hook_reminder = (
            "Remember: you are documenting precise, factual, short semantic dependencies "
            "to assist inter-repository navigation. TARGET: 3,000-10,000 chars. "
            "Be concise — no code snippets, no schema dumps, no full file listings. "
            "Your output MUST begin with # Domain Analysis heading."
        )

        # Iteration 13: Use earlier hook thresholds for large domains
        if is_large_domain:
            hook_thresh = (max(3, int(max_turns * 0.15)), max(8, int(max_turns * 0.35)))
        else:
            hook_thresh = None

        # Invoke Claude CLI (Pass 2 needs MCP search_code tool for source code analysis)
        result = self._invoke_claude_cli(
            prompt, self.pass_timeout, max_turns,
            allowed_tools="mcp__cidx-local__search_code",
            post_tool_hook=hook_reminder,
            hook_thresholds=hook_thresh,
        )

        # Strip meta-commentary from output
        result = self._strip_meta_commentary(result)

        # Fix 2 (Iteration 12): Detect max-turns exhaustion and retry with search budget guidance
        # Iteration 13: Large domains use write-only retry, small domains use budget search retry
        if re.search(r"Error:\s*Reached max turns\s*\(\d+\)", result.strip()):
            logger.warning(
                f"Pass 2 hit max-turns exhaustion for domain '{domain_name}', "
                f"retrying with {'write-only mode' if is_large_domain else 'search budget guidance'}"
            )
            if is_large_domain:
                # Large domain: retry with strict write-only mode (no MCP tools)
                budget_prompt = (
                    "CRITICAL: You MUST write your complete analysis NOW. "
                    "Do NOT use any search tools. Write based on your existing knowledge "
                    "and the Pass 1 evidence provided.\n\n"
                ) + prompt
                result = self._invoke_claude_cli(
                    budget_prompt, self.pass_timeout, 8,
                    allowed_tools="",  # NO MCP tools
                    post_tool_hook=hook_reminder,
                )
            else:
                # Small domain: existing retry logic (budget search)
                budget_prompt = (
                    "CRITICAL INSTRUCTION: You have a STRICT search budget. "
                    "Use AT MOST 3 search_code calls total. After your searches, "
                    "you MUST write your complete analysis output immediately.\n\n"
                ) + prompt
                result = self._invoke_claude_cli(
                    budget_prompt, self.pass_timeout, 15,
                    allowed_tools="mcp__cidx-local__search_code",
                    post_tool_hook=hook_reminder,
                )
            result = self._strip_meta_commentary(result)

        # Check for insufficient output and retry with reduced turns (Fix 1: raised threshold to 1000)
        # FIX 2 (Iteration 10): Also check for missing headings (pure meta-commentary)
        has_headings = self._has_markdown_headings(result)
        if len(result.strip()) < 1000 or not has_headings:
            reason = "no headings" if not has_headings else f"{len(result)} chars"
            logger.warning(
                f"Pass 2 returned insufficient output for domain '{domain_name}' "
                f"({reason}), retrying with reduced turns"
            )
            # Iteration 14: Retry with write-only mode (no MCP tools) and write-focused prompt
            # Primary attempt produced no headings, so force writing with existing knowledge
            retry_prompt = (
                "IMPORTANT: Your previous attempt did not produce properly formatted output. "
                "Write your dependency analysis NOW with NO searching. Use the Pass 1 evidence and "
                "any knowledge you have. Focus on precise inter-repo connections for navigation.\n\n"
            ) + prompt
            result = self._invoke_claude_cli(
                retry_prompt, self.pass_timeout, 10,
                allowed_tools="",  # No MCP tools - write only
                post_tool_hook=hook_reminder
            )
            result = self._strip_meta_commentary(result)

            # If retry also fails (insufficient), skip writing garbage file
            # FIX 3 (Iteration 11): When both attempts fail, skip this domain - don't write garbage
            # Writing 485 bytes of "please approve my write" is worse than no file at all
            has_headings = self._has_markdown_headings(result)
            if len(result.strip()) < 1000 or not has_headings:
                reason = "no headings" if not has_headings else f"{len(result)} chars"
                logger.error(
                    f"Pass 2 retry also returned insufficient output for domain '{domain_name}' "
                    f"({reason}) - SKIPPING domain file (will not write garbage content)"
                )
                return

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

        prompt += (
            "## AUTHORITATIVE Domain Assignments (from Pass 1 - use these EXACTLY)\n\n"
        )
        for domain in domain_list:
            prompt += f"- **{domain['name']}**: {domain.get('description', 'N/A')}\n"
            participating = domain.get("participating_repos", [])
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

        # Invoke Claude CLI (Pass 3 does not need MCP tools - just reads domain files and generates index)
        timeout = self.pass_timeout // 2  # Pass 3 uses half timeout (lighter workload)
        result = self._invoke_claude_cli(prompt, timeout, max_turns, allowed_tools=None)

        # Strip meta-commentary from output (Fix 3: same as Pass 2)
        result = self._strip_meta_commentary(result)

        # Build cross-domain graph (Iteration 16)
        graph_section = self._build_cross_domain_graph(staging_dir, domain_list)

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

        # Write index file (with graph section appended if edges exist)
        index_file = staging_dir / "_index.md"
        index_file.write_text(frontmatter + result + graph_section)
        logger.info(f"Pass 3 complete: wrote {index_file}")

    @staticmethod
    def _extract_cross_domain_section(content: str) -> str:
        """
        Extract Cross-Domain section from domain markdown file.

        Finds the ## Cross-Domain heading (with or without "Connections")
        and returns all text until the next ## heading or EOF.

        Args:
            content: Domain markdown file content

        Returns:
            Cross-Domain section text, or empty string if not found
        """
        if not content:
            return ""

        lines = content.split("\n")

        # Find ## Cross-Domain heading (case-insensitive, flexible wording)
        heading_pattern = re.compile(r'^##\s+Cross[- ]Domain\b', re.IGNORECASE)

        start_idx = None
        for i, line in enumerate(lines):
            if heading_pattern.match(line.strip()):
                start_idx = i + 1  # Start collecting from next line
                break

        if start_idx is None:
            return ""

        # Collect lines until next ## heading or EOF
        result_lines = []
        for i in range(start_idx, len(lines)):
            line = lines[i]
            # Stop at next level-2 heading
            if line.strip().startswith("## "):
                break
            result_lines.append(line)

        return "\n".join(result_lines)

    @staticmethod
    def _build_cross_domain_graph(staging_dir: Path, domain_list: List[Dict]) -> str:
        """
        Build cross-domain dependency graph from domain files.

        Parses each domain's Cross-Domain section, detects repo name mentions,
        and builds a directed graph showing which domains connect to which.

        Args:
            staging_dir: Directory containing domain .md files
            domain_list: List of domain dicts with 'name' and 'participating_repos'

        Returns:
            Markdown section with cross-domain graph table and summary,
            or empty string if no cross-domain edges found
        """
        # Build reverse mapping: repo alias → domain name
        repo_to_domain = {}
        for domain in domain_list:
            domain_name = domain["name"]
            for repo in domain.get("participating_repos", []):
                repo_to_domain[repo] = domain_name

        # Track edges: (source_domain, target_domain) → set of via_repos
        edges = {}

        # Process each domain file
        for domain in domain_list:
            domain_name = domain["name"]
            domain_file = staging_dir / f"{domain_name}.md"

            # Skip if file doesn't exist
            if not domain_file.exists():
                continue

            # Read and extract Cross-Domain section
            content = domain_file.read_text()
            cross_domain_text = DependencyMapAnalyzer._extract_cross_domain_section(content)

            if not cross_domain_text:
                continue

            # Check for mentions of OTHER domains' repos (word-boundary match)
            for target_domain_dict in domain_list:
                target_domain = target_domain_dict["name"]

                # Don't create self-edges
                if target_domain == domain_name:
                    continue

                # Check each repo in target domain
                for repo_alias in target_domain_dict.get("participating_repos", []):
                    # Word-boundary regex to avoid false positives (e.g., "db" in "adobe")
                    pattern = r'(?<![a-zA-Z0-9_-])' + re.escape(repo_alias) + r'(?![a-zA-Z0-9_-])'

                    if re.search(pattern, cross_domain_text):
                        # Found edge: source_domain → target_domain via repo_alias
                        edge_key = (domain_name, target_domain)
                        if edge_key not in edges:
                            edges[edge_key] = set()
                        edges[edge_key].add(repo_alias)

        # Return empty string if no edges
        if not edges:
            return ""

        # Sort edges alphabetically by (source, target)
        sorted_edges = sorted(edges.items(), key=lambda x: (x[0][0], x[0][1]))

        # Build markdown table
        output = "\n\n## Cross-Domain Dependency Graph\n\n"
        output += "Directed connections between domains (source mentions target's repos in its Cross-Domain Connections section).\n\n"
        output += "| Source Domain | Target Domain | Via Repos |\n"
        output += "|---|---|---|\n"

        for (source, target), via_repos in sorted_edges:
            via_repos_str = ", ".join(sorted(via_repos))
            output += f"| {source} | {target} | {via_repos_str} |\n"

        # Calculate summary
        edge_count = len(sorted_edges)
        total_domains = len(domain_list)

        # Determine standalone domains (domains with no outgoing OR incoming edges)
        domains_with_edges = set()
        for (source, target), _ in sorted_edges:
            domains_with_edges.add(source)
            domains_with_edges.add(target)

        all_domain_names = {d["name"] for d in domain_list}
        standalone_domains = sorted(all_domain_names - domains_with_edges)

        output += f"\n**Summary**: {edge_count} cross-domain edges across {total_domains} domains."
        if standalone_domains:
            standalone_str = ", ".join(standalone_domains)
            output += f" {len(standalone_domains)} standalone domains: {standalone_str}."

        return output

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
    def _has_markdown_headings(text: str) -> bool:
        """
        Check if text contains markdown headings (levels 1-3).

        Used to detect if Claude output contains actual structured content
        vs pure meta-commentary.

        Args:
            text: Text to check

        Returns:
            True if text contains at least one markdown heading (# , ## , ### )
        """
        if not text:
            return False

        for line in text.split("\n"):
            stripped = line.strip()
            if stripped.startswith("# ") or stripped.startswith("## ") or stripped.startswith("### "):
                return True

        return False

    @staticmethod
    def _strip_meta_commentary(text: str) -> str:
        """
        Strip meta-commentary from Claude CLI output.

        Claude sometimes returns meta-commentary before the actual analysis content:
        - "Based on my comprehensive analysis..."
        - "Perfect. Now I have sufficient evidence..."
        - "Let me compile the findings:"

        Also strips YAML frontmatter blocks that Claude may generate.

        This method strips such lines from the beginning until actual content is found.

        Args:
            text: Raw Claude CLI output

        Returns:
            Text with meta-commentary removed
        """
        if not text:
            return text

        # Strip YAML frontmatter blocks (loop to handle multiple consecutive blocks)
        # Fix: Iteration 9 - Claude sometimes outputs two consecutive YAML frontmatter blocks
        max_iterations = 10  # Safety limit to prevent infinite loops
        iteration = 0
        while iteration < max_iterations:
            iteration += 1
            # Strip leading whitespace/newlines between YAML blocks
            text = text.lstrip()
            if not text:
                break

            lines = text.split("\n")
            stripped_first = lines[0].strip() if lines else ""

            # Track if we found and stripped a YAML block in this iteration
            stripped_yaml = False

            if stripped_first == "---":
                # Find closing ---
                for i in range(1, len(lines)):
                    if lines[i].strip() == "---":
                        # Found closing delimiter - strip entire frontmatter
                        text = "\n".join(lines[i + 1:])
                        stripped_yaml = True
                        break
            else:
                # Fix 2: Also detect YAML-like content without opening ---
                # Claude sometimes omits the opening delimiter
                yaml_keys = ("domain:", "last_analyzed:", "participating_repos:", "schema_version:")
                first_content = stripped_first.lower()
                if any(first_content.startswith(k) for k in yaml_keys):
                    # Find closing ---
                    for i in range(1, len(lines)):
                        if lines[i].strip() == "---":
                            text = "\n".join(lines[i + 1:])
                            stripped_yaml = True
                            break

            # If no YAML block was found/stripped, we're done
            if not stripped_yaml:
                break

        # FIX 1 (Iteration 10): Strip everything before first markdown heading
        # This handles meta-commentary like "I have enough data..." or "Good - the code-indexer..."
        # that appears before the actual analysis content starts
        lines = text.split("\n")
        first_heading_idx = None
        for i, line in enumerate(lines):
            stripped = line.strip()
            # Check for markdown heading (level 1, 2, or 3: #, ##, ###)
            if stripped.startswith("# ") or stripped.startswith("## ") or stripped.startswith("### "):
                first_heading_idx = i
                break

        # If we found a heading, strip everything before it
        if first_heading_idx is not None:
            text = "\n".join(lines[first_heading_idx:])
        else:
            # No heading found - return as-is (quality gate will catch this)
            # Don't run line-by-line cleanup since there's no structured content
            return text

        # Re-split for the line-by-line meta-pattern cleanup (secondary cleanup)
        # This only runs if we found a heading above
        lines = text.split("\n")

        # Meta-commentary patterns (case-insensitive starts)
        meta_patterns = [
            "based on",
            "perfect.",
            "now i have",
            "let me",
            "i now have",
            "here is",
            "here's",
            "i have gathered",  # Fix 4
            "i have all",  # Fix 3: Pass 3 meta-commentary
            "now i can",  # Fix 4
            "i'll",  # Fix 4
            "i will",  # Fix 4
        ]

        # Find first line of actual content
        content_start_idx = 0
        for i, line in enumerate(lines):
            stripped = line.strip()

            # Skip empty lines at start
            if not stripped:
                continue

            # Skip spurious YAML-like separators
            if stripped == "---":
                content_start_idx = i + 1
                continue

            # Check if line is meta-commentary
            lower = stripped.lower()
            is_meta = any(lower.startswith(pattern) for pattern in meta_patterns)

            if is_meta:
                content_start_idx = i + 1
                continue

            # Found actual content - stop stripping
            # Content lines start with: #, ##, -, |, **, or regular text
            # NOTE: Numbered lists (digits) require special handling (see below)
            if (stripped.startswith("#") or
                stripped.startswith("**") or
                stripped.startswith("-") or
                stripped.startswith("|")):
                break

            # Special handling for numbered list items (Fix 4)
            # If line starts with digit + period + space, and doesn't contain "#",
            # it's likely Claude's internal notes before the real analysis
            if stripped and stripped[0].isdigit():
                # Check if it's a numbered list item (e.g., "1. Some text")
                if len(stripped) > 2 and stripped[1] == "." and stripped[2] == " ":
                    # Check if this numbered item contains a heading marker
                    if "#" not in stripped:
                        # It's a pre-findings note - skip and continue looking for real content
                        content_start_idx = i + 1
                        continue
                # If it's a numbered list that IS part of the content, break
                break

            # If we hit a line that doesn't match meta patterns and isn't clearly content,
            # assume it's the start of actual content
            break

        # Return from content start onwards
        result_lines = lines[content_start_idx:]

        # Strip leading empty lines from the result
        while result_lines and not result_lines[0].strip():
            result_lines.pop(0)

        # FIX 3 (Iteration 12): Strip trailing meta-commentary patterns
        # Claude sometimes adds conversational endings like "Please let me know if you need changes."
        # or standalone `---` separators before conversational text
        trailing_patterns = [
            "would you like",
            "should i ",
            "shall i ",
            "do you want",
            "i can also",
            "if you'd like",
            "is there anything",
            "do you need",
            "please ",
            "let me know",
            "if you need",
            "happy to",
            "feel free",
        ]

        # Strip from the end backwards
        while result_lines:
            last_line = result_lines[-1].strip().lower()

            # Skip trailing empty lines
            if not last_line:
                result_lines.pop()
                continue

            # Strip standalone --- separator at the end (often precedes conversational text)
            if last_line == "---":
                result_lines.pop()
                continue

            # Check for trailing meta-commentary patterns (use 'in' not 'startswith')
            if any(p in last_line for p in trailing_patterns):
                result_lines.pop()
                continue

            # No more trailing meta-commentary found
            break

        # If we stripped everything, return the original text (edge case: only meta-commentary)
        result = "\n".join(result_lines)
        if not result.strip():
            return text

        return result

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
        for i, char in enumerate(text):
            if char in "[{":
                start_idx = i
                break

        if start_idx == -1:
            raise ValueError(f"No JSON found in output (first 200 chars): {text[:200]}")

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
            if char == "\\" and in_string:
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

    def _invoke_claude_cli(
        self,
        prompt: str,
        timeout: int,
        max_turns: int,
        allowed_tools: Optional[str] = None,
        post_tool_hook: Optional[str] = None,
        hook_thresholds: Optional[tuple] = None,
    ) -> str:
        """
        Invoke Claude CLI with direct subprocess (AC1).

        Verifies ANTHROPIC_API_KEY environment variable is available, then invokes
        Claude CLI as a subprocess from golden-repos root.

        Args:
            prompt: Prompt to send to Claude
            timeout: Timeout in seconds
            max_turns: Maximum number of agentic turns.
                      - 0 = single-shot print mode (no --max-turns flag, no tool use, no agentic loop)
                      - >0 = agentic mode with --max-turns N (enables tool use for up to N turns)
            allowed_tools: Tool access control for Claude CLI (only controls MCP server tools):
                          - None: No --allowedTools flag is added (all built-in tools available;
                                  MCP tools available only if registered). Built-in tools (Read, Bash,
                                  Glob, etc.) are ALWAYS available regardless of --allowedTools flag.
                          - "" (empty string): --allowedTools "" (NO MCP tools, but built-in tools still available)
                          - "tool_name": --allowedTools "tool_name" (specific MCP tool only)
            post_tool_hook: Optional PostToolUse hook reminder text. If provided with max_turns > 0,
                           adds --settings flag with PostToolUse hook that echoes the reminder after
                           each tool call. Use for Pass 2 to reinforce output format requirements.

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
            "--model",
            self.analysis_model,
        ]

        # Guard against negative max_turns
        if max_turns < 0:
            logger.warning(f"max_turns={max_turns} is negative, treating as 0 (single-shot mode)")
            max_turns = 0

        # max_turns=0 means single-shot print mode (no tool use, no agentic loop)
        # max_turns>0 means agentic mode with tool use for up to N turns
        counter_file = None
        if max_turns > 0:
            cmd.extend(["--max-turns", str(max_turns)])
            # Fix 1 (Iteration 12): Turn-aware PostToolUse hook with counter file.
            # Replaces static echo with bash script that tracks tool call count and
            # escalates urgency messages as turns run out (prevents Claude from
            # exhausting all 50 turns on search without ever writing output).
            if post_tool_hook is not None:
                # Create temporary counter file
                counter_file = tempfile.NamedTemporaryFile(
                    mode='w', prefix='depmap_hook_', suffix='.cnt', delete=False
                )
                counter_file.write("0")
                counter_file.close()

                # Calculate thresholds
                if hook_thresholds is not None:
                    early_threshold, late_threshold = hook_thresholds
                else:
                    early_threshold = max(5, int(max_turns * 0.3))
                    late_threshold = max(10, int(max_turns * 0.6))

                # Build bash one-liner that reads/increments counter and escalates messages
                # Iteration 14: Purpose-driven threshold messages emphasizing conciseness
                bash_script = (
                    f"F={shlex.quote(counter_file.name)}; "
                    f"C=$(cat \"$F\"); C=$((C+1)); echo \"$C\" > \"$F\"; "
                    f"if [ \"$C\" -gt {late_threshold} ]; then "
                    f"echo {shlex.quote('CRITICAL: STOP searching. Write your concise dependency analysis NOW. Document precise inter-repo connections only — no code snippets, no implementation details. Start with # Domain Analysis heading.')}; "
                    f"elif [ \"$C\" -gt {early_threshold} ]; then "
                    f"echo {shlex.quote('WARNING: Running low on turns. Start writing your concise dependency analysis. Focus on precise inter-repo connections for navigation — no code snippets, no verbose details. Output starts with # Domain Analysis heading.')}; "
                    f"else "
                    f"echo {shlex.quote(post_tool_hook)}; "
                    f"fi"
                )

                hook_settings = json.dumps({
                    "hooks": {
                        "PostToolUse": [{
                            "matcher": "",
                            "command": f"bash -c {shlex.quote(bash_script)}"
                        }]
                    }
                })
                cmd.extend(["--settings", hook_settings])

        # Add --allowedTools only if specified
        if allowed_tools is not None:
            cmd.extend(["--allowedTools", allowed_tools])

        cmd.extend(["-p", prompt])

        # Run subprocess (wrapped in try/finally to ensure counter file cleanup)
        try:
            result = subprocess.run(
                cmd,
                cwd=str(self.golden_repos_root),
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ},  # Inherit environment including ANTHROPIC_API_KEY
                stdin=subprocess.DEVNULL,  # Prevent Claude CLI from hanging on stdin
            )
        finally:
            # Clean up counter file if it was created
            if counter_file is not None:
                try:
                    os.unlink(counter_file.name)
                except OSError:
                    pass

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
            logger.warning(f"Claude CLI returned very short stdout: {result.stdout!r}")
        else:
            logger.debug(f"Claude CLI stdout (first 500 chars): {result.stdout[:500]}")

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
        prompt += (
            "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        )
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += (
            "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        )
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
        prompt += '"I assume this exists" is NOT evidence. "I found import X in module Y" IS evidence.\n\n'

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"
        prompt += "**CORRECT**: 'auth-service JWT subsystem provides token validation consumed by web-app middleware layer'\n\n"
        prompt += "**INCORRECT (too granular)**: 'auth-service/src/jwt/validator.py:validate_token() called by web-app/src/middleware/auth.py'\n\n"
        prompt += "**INCORRECT (too abstract)**: 'auth-service is used by web-app'\n\n"

        prompt += "## PROHIBITED Content\n\n"
        prompt += "Do NOT include any of the following in your output:\n"
        prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
        prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', 'Future Considerations', or 'Suggested Improvements'\n"
        prompt += "- Advisory content about what SHOULD be done or COULD be integrated\n"
        prompt += "- Any content not directly supported by source code evidence\n\n"
        prompt += "Document ONLY verified, factual dependencies and relationships found in source code.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "CRITICAL: Your output MUST begin with a markdown heading (# Domain Analysis: domain-name).\n"
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
        prompt += (
            "- **Code-level**: Direct imports, shared libraries, type/interface reuse\n"
        )
        prompt += "- **Data contracts**: Shared database tables/views/schemas, shared file formats\n"
        prompt += (
            "- **Service integration**: REST/HTTP/MCP/gRPC API calls between repos\n"
        )
        prompt += "- **External tool invocation**: CLI tools, subprocess calls, shell commands invoking another repo\n"
        prompt += "- **Configuration coupling**: Shared env vars, config keys, feature flags, connection strings\n"
        prompt += "- **Message/event contracts**: Queue messages, webhooks, pub/sub events, callback URLs\n"
        prompt += "- **Deployment dependencies**: Runtime availability requirements (repo A must be running for repo B)\n"
        prompt += "- **Semantic coupling**: Behavioral contracts where changing logic in repo A breaks expectations in repo B\n\n"

        prompt += "## Granularity Guidelines\n\n"
        prompt += "Document at MODULE/SUBSYSTEM level, not files or functions.\n\n"

        prompt += "## PROHIBITED Content\n\n"
        prompt += "Do NOT include any of the following in your output:\n"
        prompt += "- YAML frontmatter blocks (the system adds these automatically)\n"
        prompt += "- Speculative sections like 'Recommendations', 'Potential Integration Opportunities', 'Future Considerations', or 'Suggested Improvements'\n"
        prompt += "- Advisory content about what SHOULD be done or COULD be integrated\n"
        prompt += "- Any content not directly supported by source code evidence\n\n"
        prompt += "Document ONLY verified, factual dependencies and relationships found in source code.\n\n"

        prompt += "## Output Format\n\n"
        prompt += "CRITICAL: Your output MUST begin with a markdown heading (# Domain Analysis: domain-name).\n"
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
