Analyze this repository and generate a comprehensive semantic description.

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders (e.g., 550e8400-e29b-41d4-a716-446655440000) with JSON trace files matching pattern NNN_turn_HASH.json

**For Git Repositories:**
Examine README, source files, and package files to extract:
- summary: 2-3 sentence description of what this repository does
- technologies: List of all technologies and tools detected
- features: Key features
- use_cases: Primary use cases
- purpose: One of: api, service, library, cli-tool, web-application, data-structure, utility, framework, general-purpose

**For Langfuse Trace Repositories:**
Extract intelligence from trace files (JSON files in UUID folders):
- user_identity: Extract from trace.userId field
- projects_detected: Extract from metadata.project_name field
- activity_summary: Summarize from trace.input and metadata.intel_task_type fields
- features: Key features based on trace patterns
- use_cases: Primary use cases inferred from traces

**Output Format:**
Generate YAML frontmatter + markdown body with these exact fields:
---
name: repository-name
repo_type: git|langfuse
technologies:
  - Technology 1
  - Technology 2
purpose: inferred-purpose
last_analyzed: (current timestamp)
user_identity: (Langfuse only - extracted user IDs)
projects_detected: (Langfuse only - list of project names)
---

# Repository Name

(Summary description)

## Key Features
- Feature 1
- Feature 2

## Technologies
- Tech 1
- Tech 2

## Primary Use Cases
- Use case 1
- Use case 2

## Activity Summary (Langfuse only)
(Summary of user activities based on traces)

**IMPORTANT:**
- Set repo_type field in YAML frontmatter to "git" or "langfuse"
- For Langfuse repos, include user_identity, projects_detected, and activity_summary sections
- Output ONLY the YAML + markdown (no explanations, no code blocks)
