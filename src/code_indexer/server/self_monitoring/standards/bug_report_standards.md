# Bug Report Standards

**PURPOSE**: Standard format and workflow for creating bug reports when problems are discovered

**MODE**: Hybrid - GitHub/GitLab issues (if remote exists) or files (fallback) - **AUTOMATIC via issue_manager.py**

**Referenced by**: CLAUDE.md troubleshooting methodology, subagents, slash commands

---

## Using issue_manager.py - MANDATORY

**ALL bug reports MUST be created via `issue_manager.py` script.**

**Script Location**: `~/.claude/scripts/utils/issue_manager.py`

### Creating Bug Reports

```bash
# Write bug content to temp file
cat > .tmp/bug_report.md <<'EOF'
## Bug Description
[Description]

## Steps to Reproduce
1. Step 1
2. Step 2

## Expected Behavior
[Expected]

## Actual Behavior
[Actual]

## Environment
- OS: Linux
- Version: 1.0.0

## Error Messages
```
[Error logs]
```
EOF

# Create bug via script (mode detection automatic)
python3 ~/.claude/scripts/utils/issue_manager.py create bug .tmp/bug_report.md \
  --title "Brief bug description"
```

**Returns JSON**:
```json
{
  "number": 789,
  "title": "[BUG] Brief bug description",
  "labels": ["bug", "backlog", "priority-1"],
  "state": "open",
  "url": "https://github.com/owner/repo/issues/789",  // or file://path
  "platform": "github"  // or "gitlab" or "files"
}
```

### Reading/Updating Bugs

```bash
# Read bug
python3 ~/.claude/scripts/utils/issue_manager.py read 789

# Update status to active (started fixing)
python3 ~/.claude/scripts/utils/issue_manager.py update 789 --labels "bug,active,priority-1"

# Mark bug as fixed
python3 ~/.claude/scripts/utils/issue_manager.py update 789 \
  --labels "bug,completed,priority-1" \
  --state closed
```

---

## When to Create Bug Reports

**AUTOMATIC TRIGGER**: When any of these occur:
- User reports "it's broken", "not working", "there's a bug"
- Tests are failing unexpectedly
- Unexpected errors or crashes discovered
- Production issues identified
- Regression found after changes
- Performance degradation detected
- Data corruption or integrity issues

**Proactive Creation**: When troubleshooting reveals a defect that needs tracking beyond immediate fix

---

## Mode Detection - AUTOMATIC (Script Handles This)

**Script automatically detects**:
- Git repo + GitHub remote → GitHub Issue with `[BUG]` prefix
- Git repo + GitLab remote → GitLab Issue with `[BUG]` prefix
- Git repo + No remote → File in `reports/bugs/bug_<num>_<title>.md`
- Not git repo → File in `reports/bugs/bug_<num>_<title>.md`

**NO MANUAL DETECTION NEEDED** - Just call the script.

---

## Bug Issue Format (Issues Mode)

### Title Format
```
[BUG] Brief bug description (max 80 chars)
```

**Examples**:
```
[BUG] Server crashes on empty repository list
[BUG] Authentication token expires prematurely
[BUG] Query returns incorrect results for special characters
[BUG] Memory leak in watch daemon after 24h operation
```

### Required Labels
- `bug` - Type label
- Status label: `backlog`, `active`, or `completed`
- Priority label: `priority-1` (critical), `priority-2` (high), `priority-3` (medium), `priority-4` (low)
- Feature label (optional): `feat:feature-name` if bug relates to specific feature

### Priority Determination
**priority-1 (Critical)**:
- System crashes or data loss
- Security vulnerabilities
- Complete feature breakdown
- Blocking production deployment

**priority-2 (High)**:
- Major functionality broken
- Significant user impact
- Performance severely degraded
- Workaround exists but difficult

**priority-3 (Medium)**:
- Minor functionality issues
- Limited user impact
- Easy workaround available
- Cosmetic issues affecting usability

**priority-4 (Low)**:
- Cosmetic issues only
- Minor inconveniences
- Edge cases rarely encountered
- Documentation errors

### Bug Issue Body Template

```markdown
## Bug Description

[Clear, concise description of the defect]

## Environment

- **Version**: [Software version or commit hash]
- **OS**: [Operating system and version]
- **Mode**: [local/remote/proxy or applicable configuration]
- **Configuration**: [Relevant config settings]

## Steps to Reproduce

1. [First step]
2. [Second step]
3. [Third step]
4. [Action that triggers bug]

## Expected Behavior

[What should happen]

## Actual Behavior

[What actually happens instead]

## Error Messages / Logs

\`\`\`
[Paste complete error messages, stack traces, relevant log excerpts]
\`\`\`

## Impact Assessment

**Severity**: Critical / High / Medium / Low
**Affected Users**: [Who is impacted - all users, specific workflows, edge cases]
**Frequency**: [Always, Sometimes, Rarely]
**Workaround**: [Available workaround if any, or "None"]

## Root Cause Analysis

**Initial Hypothesis**: [What we think is causing it]

**Investigation Findings**: [Updated after investigation]

**Confirmed Root Cause**: [Technical explanation of the defect]

## Fix Implementation

### Changes Required
- [ ] Fix 1: [Description]
- [ ] Fix 2: [Description]
- [ ] Fix 3: [Description]

### Testing Required
- [ ] Unit tests reproducing bug scenario
- [ ] Integration tests validating fix
- [ ] Regression tests preventing recurrence
- [ ] Manual E2E validation

### Implementation Status
- [ ] Core fix implemented
- [ ] Tests added and passing
- [ ] Code review approved
- [ ] Manual testing completed
- [ ] Documentation updated
- [ ] Regression tests added

**Completion**: 0/6 tasks complete (0%)

## Verification Evidence

[After fix: paste test outputs, execution evidence proving bug is fixed]

## Related Issues

- Related to: #N (if related to epic/story/other bug)
- Blocks: #N (if blocking other work)
- Blocked by: #N (if dependent on other work)

---

**Reported By**: [Person/System/Agent]
**Assigned To**: [Person/Agent]
**Found In**: [Version/Commit]
**Fixed In**: [Version/Commit - updated after fix]
```

---

## Bug File Format (Files Mode)

### File Location
```
reports/bugs/bug_[brief-description]_[YYYYMMDD].md
```

**Examples**:
```
reports/bugs/bug_server_crash_empty_repo_20251106.md
reports/bugs/bug_auth_token_expiry_20251106.md
reports/bugs/bug_query_special_chars_20251106.md
```

### File Structure
**Same as issue template** - Use identical format so migration works seamlessly

**Why**: If you add git remote later, can easily migrate bug reports to issues

---

## Bug Creation Workflow

### Issues Mode (GitHub/GitLab)

**When bug discovered**:
1. Detect platform: `git remote -v | grep github` → use `gh`, else use `glab`
2. Create temporary file with bug body
3. Create issue:
   ```bash
   # GitHub
   BUG_NUM=$(gh issue create \
     --title "[BUG] Brief description" \
     --label "bug,backlog,priority-2" \
     --body-file /tmp/bug.md \
     | grep -oE "[0-9]+$")

   # GitLab
   BUG_NUM=$(glab issue create \
     --title "[BUG] Brief description" \
     --label "bug,backlog,priority-2" \
     --description "$(cat /tmp/bug.md)" \
     | grep -oE "[0-9]+")
   ```
4. Display issue URL to user
5. Delete temp file

**Output to user**:
```
Bug report created: #$BUG_NUM
Title: [BUG] Brief description
Priority: priority-2 (high)
URL: [issue URL]

Track progress: gh issue view $BUG_NUM
```

### Files Mode (No Remote)

**When bug discovered**:
1. Create bug file in `reports/bugs/`
2. Use timestamp in filename
3. Fill template with all sections
4. Tell user where file is

**Output to user**:
```
Bug report created: reports/bugs/bug_description_20251106.md
Priority: High

Review/update: cat reports/bugs/bug_description_20251106.md
```

---

## Bug Fix Workflow

### Issues Mode

**Step 1: Load Bug**
```bash
gh issue view $BUG_NUM
gh issue edit $BUG_NUM --remove-label backlog --add-label active
```

**Step 2: Implement Fix** (via /troubleshoot-and-fix or subagents via Task tool)
- tdd-engineer fixes with TDD
- Adds comment: `gh issue comment $BUG_NUM --body "Fix implemented"`
- Updates body checkboxes for completed items

**Step 3: Code Review**
- code-reviewer adds comment with findings

**Step 4: Manual Testing**
- manual-test-executor validates fix
- Adds comment with evidence
- Checks off testing checkbox in body

**Step 5: Complete**
```bash
gh issue edit $BUG_NUM --remove-label active --add-label completed
gh issue comment $BUG_NUM --body "✅ Bug fixed and validated"
gh issue close $BUG_NUM
```

### Files Mode

**Step 1: Load Bug**
- Read file from `reports/bugs/`

**Step 2-4: Fix Implementation**
- Same TDD workflow
- Update file checkboxes as work progresses

**Step 5: Complete**
- Mark all checkboxes [x]
- Optionally move to `reports/bugs/resolved/`

---

## Integration with /troubleshoot-and-fix

**Enhanced workflow** - /troubleshoot-and-fix should create bug report automatically:

**Current**: Troubleshoot → Fix → Report
**Enhanced**: Troubleshoot → **Create Bug Report** → Fix → Update Bug Report → Close

**Workflow**:
```
1. User reports issue
2. /troubleshoot-and-fix analyzes
3. Create bug report (issue or file based on mode)
4. Reference bug #N in fix implementation
5. Update bug report as fix progresses
6. Close bug when validated
```

---

## Bug Report Best Practices

### Description Quality
- **Be specific**: "Server crashes" → "Server crashes with SEGFAULT when processing empty repo list"
- **Include context**: Environment, configuration, timing
- **Attach evidence**: Error messages, logs, stack traces

### Reproduction Steps
- **Minimal**: Fewest steps to reproduce
- **Precise**: Exact commands, inputs, conditions
- **Reproducible**: Should work for anyone following steps

### Expected vs Actual
- **Clear contrast**: Make difference obvious
- **Specific**: Not "doesn't work", but "returns 500 instead of 200"

### Error Messages
- **Complete**: Full stack trace, not excerpts
- **Formatted**: Use code blocks for readability
- **Context**: Include relevant log lines before/after

### Impact Assessment
- **Honest**: Don't exaggerate or minimize
- **User-focused**: Explain user impact, not just technical details
- **Quantified**: "Affects 20% of users" better than "some users"

---

## CLI Quick Reference

### GitHub
```bash
# Create bug
gh issue create --title "[BUG] Title" --label "bug,backlog,priority-2"

# Update bug
gh issue edit 123 --remove-label backlog --add-label active

# Add findings
gh issue comment 123 --body "Root cause: [explanation]"

# Close bug
gh issue close 123 --comment "Fixed and validated"

# View bug
gh issue view 123

# List bugs
gh issue list --label bug
gh issue list --label bug --state closed  # Fixed bugs
```

### GitLab
```bash
# Create bug
glab issue create --title "[BUG] Title" --label "bug,backlog,priority-2"

# Update bug
glab issue update 123 --label "bug,active,priority-2"

# Add findings
glab issue note 123 --message "Root cause: [explanation]"

# Close bug
glab issue close 123

# View bug
glab issue view 123

# List bugs
glab issue list --label bug
glab issue list --label bug --state closed  # Fixed bugs
```

---

## File-Based Bug Reports

### Location
```
reports/bugs/bug_[description]_[YYYYMMDD].md
```

### Organization
```
reports/
├── bugs/
│   ├── bug_server_crash_20251106.md      # Active bugs
│   ├── bug_auth_failure_20251105.md
│   └── resolved/                          # Optional: resolved bugs
│       └── bug_query_timeout_20251104.md
```

### Management
- Create in `reports/bugs/`
- Update as investigation progresses
- Optionally move to `resolved/` when fixed
- Or migrate to issues when remote added

---

## CRITICAL: Auto-Create on Problem Discovery

**MANDATORY**: When troubleshooting reveals a reproducible defect, AUTOMATICALLY create bug report before or during fix implementation.

**Why**:
- Tracks problem formally
- Prevents loss of reproduction steps
- Documents root cause for future
- Links fix commits to bug report
- Provides history for similar issues

**Workflow Integration**:
```
Problem discovered
  ↓
Auto-create bug report (issue or file)
  ↓
Implement fix (reference bug #N)
  ↓
Update bug report with fix details
  ↓
Close bug when validated
```

**User never needs to manually create bug reports** - subagents do it automatically when problems found.

---

## Examples

### Example 1: GitHub Repo Bug Discovery
```
User: "The server is crashing when I pass an empty repo list"

Claude:
1. Detects: git repo with GitHub remote
2. Creates: Issue #789 "[BUG] Server crashes on empty repository list"
3. Labels: bug, backlog, priority-1
4. Body: Filled with user's description, reproduction steps
5. Investigates and updates issue with root cause
6. Implements fix via /troubleshoot-and-fix
7. References #789 in fix commits
8. Closes #789 after validation
```

### Example 2: Local Project Bug Discovery
```
User: "The query is returning wrong results for special characters"

Claude:
1. Detects: not a git repo OR no remote
2. Creates: reports/bugs/bug_query_special_chars_20251106.md
3. Fills template with description, steps, error logs
4. Investigates and updates file with root cause
5. Implements fix
6. Updates file with fix details and test evidence
7. Marks all checkboxes [x] when validated
```

---

## Reference Templates

### Quick Bug Report (Minimal)
Use for simple, obvious bugs:
```markdown
[BUG] Title

**Problem**: [One sentence]
**Reproduce**: [Command that fails]
**Error**: [Error message]
**Expected**: [What should happen]
**Priority**: [1-4]
```

### Complete Bug Report (Comprehensive)
Use for complex, critical bugs - full template as shown above.

---

**This standard ensures consistent bug tracking regardless of environment, with automatic creation during problem discovery and hybrid support for issues or files.**
