Update the repository description based on changes since last analysis.

**Last Analyzed:** {last_analyzed}

**Existing Description:**
{existing_description}

**Repository Type Discovery:**
Examine the folder structure to determine the repository type:
- Git repository: Contains a .git directory
- Langfuse trace repository: Contains UUID-named folders with JSON trace files

**For Git Repositories:**
1. Run: git log --since="{last_analyzed}" --oneline
2. If material changes detected (not just cosmetic commits), update the description
3. If no material changes, return the existing description unchanged

**For Langfuse Trace Repositories:**
1. Find files modified after {last_analyzed} using file modification timestamps
2. IMPORTANT: Langfuse traces are immutable once established
3. Focus on NEW trace files only (files with modification time > last_analyzed)
4. Extract new findings from new traces:
   - New user IDs from trace.userId
   - New projects from metadata.project_name
   - New activities from trace.input and metadata.intel_task_type
5. MERGE new findings with existing description (preserve existing user_identity and projects_detected)
6. DO NOT replace existing data - only ADD new discoveries

**Update Strategy:**
- Update description only if material changes detected
- Preserve existing YAML frontmatter structure
- For Langfuse: merge new findings, don't replace
- Update last_analyzed timestamp to current time

**Output Format:**
Return updated YAML frontmatter + markdown body with same structure as original.
Include repo_type field in YAML.
If no material changes: return existing description with updated last_analyzed timestamp only.

**IMPORTANT:**
- Output ONLY the YAML + markdown (no explanations, no code blocks)
- Preserve all existing fields in YAML frontmatter
- For Langfuse: keep existing user_identity and projects_detected, only add new entries
