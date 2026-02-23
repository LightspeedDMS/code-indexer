#!/usr/bin/env python3
"""
Issue Manager - CRUD operations for GitHub/GitLab epics/stories/bugs

Provides reusable interface for creating, reading, updating, and deleting
issues across GitHub and GitLab platforms with automatic label creation.

Usage:
    # Create epic
    issue_manager.py create epic .tmp/epic_content.md --title "Epic Title"

    # Create story linked to epic
    issue_manager.py create story .tmp/story_content.md --title "Story Title" --epic 123

    # Create bug
    issue_manager.py create bug .tmp/bug_content.md --title "Bug Title"

    # Update issue body
    issue_manager.py update 123 --body-file .tmp/updated_content.md

    # Update issue title
    issue_manager.py update 123 --title "New Title"

    # Update issue labels
    issue_manager.py update 123 --labels "bug,priority-1,backlog"

    # Update issue state
    issue_manager.py update 123 --state closed

    # Read issue
    issue_manager.py read 123

    # Delete (close) issue
    issue_manager.py delete 123
"""

import os
import sys
import json
import subprocess
import re
import tempfile
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, asdict


@dataclass
class IssueData:
    """Normalized issue data across platforms"""
    number: int
    title: str
    body: str
    labels: List[str]
    state: str
    url: str
    platform: str


class GitHubAPI:
    """GitHub API wrapper using gh CLI"""

    def __init__(self, repo: str):
        self.repo = repo
        self.platform = "github"
        self._ensured_labels = set()
        self._project_cache = {}  # Cache project lookups

    def ensure_labels_exist(self, labels: List[str]):
        """Ensure labels exist in repository (create if needed)"""
        for label in labels:
            if label in self._ensured_labels:
                continue

            # Check if label exists
            check_cmd = ["gh", "label", "list", "--repo", self.repo, "--json", "name"]
            try:
                result = subprocess.run(check_cmd, capture_output=True, text=True, check=True)
                existing_labels = [item["name"] for item in json.loads(result.stdout)]

                if label not in existing_labels:
                    # Create label with default color
                    create_cmd = ["gh", "label", "create", label, "--repo", self.repo, "--color", "0366d6"]
                    try:
                        subprocess.run(create_cmd, capture_output=True, text=True, check=True)
                        print(f"  Created label: {label}", file=sys.stderr)
                    except subprocess.CalledProcessError:
                        # Label might have been created by concurrent process, ignore
                        pass

                self._ensured_labels.add(label)
            except subprocess.CalledProcessError as e:
                print(f"  Warning: Could not check/create label {label}: {e}", file=sys.stderr)

    def create_issue(self, title: str, body: str, labels: List[str]) -> IssueData:
        """Create issue and return data"""
        # Ensure all labels exist first
        if labels:
            self.ensure_labels_exist(labels)

        # Write body to temp file (body may be too long for command line)
        with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
            f.write(body)
            temp_file = f.name

        try:
            # Build command
            cmd = [
                "gh", "issue", "create",
                "--repo", self.repo,
                "--title", title,
                "--body-file", temp_file
            ]

            if labels:
                cmd.extend(["--label", ",".join(labels)])

            # Disable prompts and pager
            env = os.environ.copy()
            env['GH_PROMPT_DISABLED'] = '1'
            env['GH_NO_UPDATE_NOTIFIER'] = '1'
            env['GH_PAGER'] = ''

            result = subprocess.run(cmd, capture_output=True, text=True, env=env)

            if result.returncode != 0:
                # Check for rate limiting
                if "rate limit" in result.stderr.lower() or "temporarily blocked" in result.stderr.lower():
                    raise RuntimeError(f"GitHub rate limit exceeded. Wait a few minutes and retry. Error: {result.stderr}")
                raise RuntimeError(f"gh issue create failed: {result.stderr}")

            issue_url = result.stdout.strip()

            if not issue_url:
                raise ValueError(f"gh issue create returned empty output. stderr: {result.stderr}")

            # Extract issue number from URL
            match = re.search(r'/issues/(\d+)$', issue_url)
            if not match:
                raise ValueError(f"Could not parse issue number from URL: {issue_url}")

            issue_num = int(match.group(1))
        finally:
            # Clean up temp file
            os.unlink(temp_file)

        # Fetch full issue data
        return self.get_issue(issue_num)

    def get_issue(self, issue_num: int) -> IssueData:
        """Get issue details"""
        cmd = ["gh", "issue", "view", str(issue_num), "--repo", self.repo, "--json", "number,title,body,labels,state,url"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)

        return IssueData(
            number=data["number"],
            title=data["title"],
            body=data.get("body", ""),
            labels=[label["name"] for label in data.get("labels", [])],
            state=data["state"].lower(),
            url=data["url"],
            platform="github"
        )

    def update_issue(self, issue_num: int, title: Optional[str] = None, body: Optional[str] = None,
                    labels: Optional[List[str]] = None, state: Optional[str] = None) -> IssueData:
        """Update issue fields"""
        # Handle title and body together in one command
        if title or body is not None:
            cmd = ["gh", "issue", "edit", str(issue_num), "--repo", self.repo]

            if title:
                cmd.extend(["--title", title])

            if body is not None:
                # Use temp file for body
                with tempfile.NamedTemporaryFile(mode='w', suffix='.md', delete=False) as f:
                    f.write(body)
                    temp_file = f.name
                try:
                    cmd.extend(["--body-file", temp_file])
                    subprocess.run(cmd, check=True)
                finally:
                    os.unlink(temp_file)
            else:
                # No body update, execute command
                subprocess.run(cmd, check=True)

        # Handle labels separately
        if labels is not None:
            # Ensure labels exist
            if labels:
                self.ensure_labels_exist(labels)

            # Get current labels to remove them
            current_issue = self.get_issue(issue_num)
            current_labels = current_issue.labels

            # Remove current labels first (if any)
            if current_labels:
                remove_cmd = ["gh", "issue", "edit", str(issue_num), "--repo", self.repo]
                for label in current_labels:
                    remove_cmd.extend(["--remove-label", label])
                subprocess.run(remove_cmd, check=True)

            # Add new labels (if any)
            if labels:
                add_cmd = ["gh", "issue", "edit", str(issue_num), "--repo", self.repo]
                add_cmd.extend(["--add-label", ",".join(labels)])
                subprocess.run(add_cmd, check=True)

        # Handle state change separately
        if state:
            if state.lower() == "closed":
                close_cmd = ["gh", "issue", "close", str(issue_num), "--repo", self.repo]
                subprocess.run(close_cmd, check=True)
            elif state.lower() == "open":
                reopen_cmd = ["gh", "issue", "reopen", str(issue_num), "--repo", self.repo]
                subprocess.run(reopen_cmd, check=True)

        # Return updated issue data
        return self.get_issue(issue_num)

    def delete_issue(self, issue_num: int, hard_delete: bool = False) -> bool:
        """Delete issue (close by default, hard delete if specified)"""
        if hard_delete:
            # GitHub doesn't support hard delete via CLI, must use API
            print("Warning: GitHub doesn't support hard delete via CLI. Closing issue instead.", file=sys.stderr)

        cmd = ["gh", "issue", "close", str(issue_num), "--repo", self.repo]
        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    def create_epic_project(self, epic_title: str) -> Optional[str]:
        """Create GitHub Project for epic organization"""
        project_title = f"Epic: {epic_title}"

        # Determine owner (org if repo is org-owned, otherwise user)
        owner = self.repo.split('/')[0] if '/' in self.repo else "@me"

        # Create project
        cmd = ["gh", "project", "create", "--owner", owner, "--title", project_title]
        result = subprocess.run(cmd, capture_output=True, text=True)

        if result.returncode != 0:
            # Check if auth scopes are missing
            if "missing required scopes" in result.stderr:
                print(f"  ⚠️  GitHub Project creation requires additional scopes. Run: gh auth refresh -s project", file=sys.stderr)
                print(f"  Skipping project creation for epic '{epic_title}'", file=sys.stderr)
                return None
            else:
                print(f"  Warning: Could not create GitHub Project: {result.stderr}", file=sys.stderr)
                return None

        # gh project create doesn't output ID, must query for it
        list_cmd = ["gh", "project", "list", "--owner", owner, "--format", "json", "--limit", "50"]
        list_result = subprocess.run(list_cmd, capture_output=True, text=True, check=True)
        projects = json.loads(list_result.stdout)

        # Find project by title (just created, should be in recent list)
        project_number = None
        for proj in projects.get("projects", []):
            if proj.get("title") == project_title:
                project_number = proj.get("number")
                break

        if not project_number:
            print(f"  Warning: Project created but could not find ID", file=sys.stderr)
            return None

        print(f"  Created GitHub Project: {project_title} (number: {project_number}, owner: {owner})", file=sys.stderr)
        return str(project_number)

    def add_issue_to_project(self, project_number: str, issue_num: int, owner: Optional[str] = None):
        """Add issue to GitHub Project"""
        issue_url = f"https://github.com/{self.repo}/issues/{issue_num}"

        # Use provided owner or extract from repo
        if not owner:
            owner = self.repo.split('/')[0] if '/' in self.repo else "@me"

        cmd = ["gh", "project", "item-add", project_number, "--owner", owner, "--url", issue_url]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"  Added issue #{issue_num} to project #{project_number} (owner: {owner})", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"  Warning: Could not add issue to project: {e.stderr}", file=sys.stderr)


class GitLabAPI:
    """GitLab API wrapper using glab CLI"""

    def __init__(self, repo: str):
        self.repo = repo
        self.platform = "gitlab"
        self._ensured_labels = set()

    def ensure_labels_exist(self, labels: List[str]):
        """Ensure labels exist in repository (create if needed)"""
        for label in labels:
            if label in self._ensured_labels:
                continue

            # Check if label exists
            check_cmd = ["glab", "label", "list", "--repo", self.repo]
            try:
                result = subprocess.run(check_cmd, capture_output=True, text=True, check=True)
                existing_labels = [line.strip() for line in result.stdout.split('\n') if line.strip()]

                if label not in existing_labels:
                    # Create label
                    create_cmd = ["glab", "label", "create", label, "--repo", self.repo, "--color", "#0366d6"]
                    try:
                        subprocess.run(create_cmd, capture_output=True, text=True, check=True)
                        print(f"  Created label: {label}", file=sys.stderr)
                    except subprocess.CalledProcessError:
                        # Label might have been created by concurrent process, ignore
                        pass

                self._ensured_labels.add(label)
            except subprocess.CalledProcessError as e:
                print(f"  Warning: Could not check/create label {label}: {e}", file=sys.stderr)

    def create_issue(self, title: str, body: str, labels: List[str]) -> IssueData:
        """Create issue and return data"""
        # Ensure all labels exist first
        if labels:
            self.ensure_labels_exist(labels)

        # Build command
        cmd = [
            "glab", "issue", "create",
            "--repo", self.repo,
            "--title", title,
            "--description", body
        ]

        if labels:
            cmd.extend(["--label", ",".join(labels)])

        result = subprocess.run(cmd, capture_output=True, text=True, check=True)

        # Parse issue number from output
        # Can be either "#123" format or URL format "/-/issues/123"
        match = re.search(r'#(\d+)', result.stdout)
        if not match:
            match = re.search(r'/-/issues/(\d+)', result.stdout)
        if not match:
            raise ValueError(f"Could not parse issue number from: {result.stdout}")

        issue_num = int(match.group(1))

        # Fetch full issue data
        return self.get_issue(issue_num)

    def get_issue(self, issue_num: int) -> IssueData:
        """Get issue details"""
        cmd = ["glab", "issue", "view", str(issue_num), "--repo", self.repo, "--output", "json"]
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
        data = json.loads(result.stdout)

        return IssueData(
            number=data.get("iid"),
            title=data.get("title", ""),
            body=data.get("description", ""),
            labels=data.get("labels", []),
            state=data.get("state", "").lower(),
            url=data.get("web_url", ""),
            platform="gitlab"
        )

    def update_issue(self, issue_num: int, title: Optional[str] = None, body: Optional[str] = None,
                    labels: Optional[List[str]] = None, state: Optional[str] = None) -> IssueData:
        """Update issue fields"""
        # Handle title and body together
        if title or body is not None:
            cmd = ["glab", "issue", "update", str(issue_num), "--repo", self.repo]

            if title:
                cmd.extend(["--title", title])

            if body is not None:
                cmd.extend(["--description", body])

            subprocess.run(cmd, check=True)

        # Handle labels separately (need to remove old ones first)
        if labels is not None:
            # Ensure labels exist
            if labels:
                self.ensure_labels_exist(labels)

            # Get current labels
            current_issue = self.get_issue(issue_num)
            current_labels = current_issue.labels

            # Remove current labels first
            if current_labels:
                unlabel_cmd = ["glab", "issue", "update", str(issue_num), "--repo", self.repo]
                for label in current_labels:
                    unlabel_cmd.extend(["--unlabel", label])
                subprocess.run(unlabel_cmd, check=True)

            # Add new labels
            if labels:
                label_cmd = ["glab", "issue", "update", str(issue_num), "--repo", self.repo]
                label_cmd.extend(["--label", ",".join(labels)])
                subprocess.run(label_cmd, check=True)

        # Handle state change
        if state:
            if state.lower() == "closed":
                close_cmd = ["glab", "issue", "close", str(issue_num), "--repo", self.repo]
                subprocess.run(close_cmd, check=True)
            elif state.lower() == "opened" or state.lower() == "open":
                reopen_cmd = ["glab", "issue", "reopen", str(issue_num), "--repo", self.repo]
                subprocess.run(reopen_cmd, check=True)

        # Return updated issue data
        return self.get_issue(issue_num)

    def delete_issue(self, issue_num: int, hard_delete: bool = False) -> bool:
        """Delete issue (close by default, hard delete if specified)"""
        if hard_delete:
            cmd = ["glab", "issue", "delete", str(issue_num), "--repo", self.repo, "--yes"]
        else:
            cmd = ["glab", "issue", "close", str(issue_num), "--repo", self.repo]

        result = subprocess.run(cmd, capture_output=True, text=True)
        return result.returncode == 0

    def create_epic_milestone(self, epic_title: str) -> str:
        """Create GitLab Milestone for epic organization"""
        milestone_title = f"Epic: {epic_title}"

        # Create milestone via API (glab milestone create doesn't support --repo flag)
        # Need to extract project ID from repo path
        project_path = self.repo.replace('/', '%2F')  # URL encode slashes

        cmd = [
            "glab", "api", f"projects/{project_path}/milestones",
            "-X", "POST",
            "-f", f"title={milestone_title}"
        ]

        try:
            result = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(result.stdout)
            milestone_id = data['id']
            print(f"  Created GitLab Milestone: {milestone_title} (ID: {milestone_id})", file=sys.stderr)
            return milestone_title  # Return title, not ID (used in issue assignment)
        except subprocess.CalledProcessError as e:
            print(f"  Warning: Could not create milestone: {e.stderr}", file=sys.stderr)
            return None

    def assign_to_milestone(self, issue_num: int, milestone_title: str):
        """Assign issue to GitLab Milestone"""
        if not milestone_title:
            return

        cmd = ["glab", "issue", "update", str(issue_num), "--repo", self.repo, "--milestone", milestone_title]
        try:
            subprocess.run(cmd, capture_output=True, text=True, check=True)
            print(f"  Assigned issue #{issue_num} to milestone '{milestone_title}'", file=sys.stderr)
        except subprocess.CalledProcessError as e:
            print(f"  Warning: Could not assign to milestone: {e.stderr}", file=sys.stderr)


class FileBasedAPI:
    """File-based backend for epics/stories/bugs (fallback when no git remote)"""

    def __init__(self, base_dir: str):
        self.base_dir = Path(base_dir).resolve()
        self.platform = "files"
        self.plans_dir = self.base_dir / "plans" / "backlog"
        self.bugs_dir = self.base_dir / "reports" / "bugs"
        self.metadata_file = self.base_dir / ".tmp" / "issue_metadata.json"

        # Ensure directories exist
        self.plans_dir.mkdir(parents=True, exist_ok=True)
        self.bugs_dir.mkdir(parents=True, exist_ok=True)
        self.metadata_file.parent.mkdir(parents=True, exist_ok=True)

        # Load or initialize metadata
        self._load_metadata()

    def _load_metadata(self):
        """Load issue metadata (issue numbers, epic mapping, etc.)"""
        if self.metadata_file.exists():
            with open(self.metadata_file, 'r') as f:
                self.metadata = json.load(f)
        else:
            self.metadata = {
                "next_issue_num": 1,
                "issues": {},  # issue_num -> file_path mapping
                "epic_stories": {}  # epic_num -> [story_nums]
            }
            self._save_metadata()

    def _save_metadata(self):
        """Save issue metadata"""
        with open(self.metadata_file, 'w') as f:
            json.dump(self.metadata, f, indent=2)

    def _get_next_issue_num(self) -> int:
        """Get next available issue number"""
        num = self.metadata["next_issue_num"]
        self.metadata["next_issue_num"] = num + 1
        self._save_metadata()
        return num

    def _parse_metadata_from_content(self, content: str, issue_type: str) -> Dict:
        """Extract metadata from markdown content"""
        # Extract title
        title_match = re.search(r'^#\s+(?:Epic|Story|Bug Report):\s*(.+)$', content, re.MULTILINE)
        title = title_match.group(1).strip() if title_match else "Untitled"

        # Extract labels from content
        labels = [issue_type.lower()]

        # Status
        if re.search(r'Status:\s*completed|Status:\s*done|Status:\s*closed', content, re.IGNORECASE):
            labels.append("completed")
            state = "closed"
        elif re.search(r'Status:\s*active|Status:\s*in[- ]progress', content, re.IGNORECASE):
            labels.append("active")
            state = "open"
        else:
            labels.append("backlog")
            state = "open"

        # Priority
        priority = extract_priority(content)
        labels.append(priority)

        return {"title": title, "labels": labels, "state": state}

    def _get_file_path(self, issue_num: int, issue_type: str, epic_num: Optional[int] = None,
                      feature: Optional[str] = None, title: str = "") -> Path:
        """Generate file path for issue"""
        if issue_type == "bug":
            # bugs go to reports/bugs/
            safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')[:50]
            filename = f"bug_{issue_num}_{safe_title}.md"
            return self.bugs_dir / filename
        elif issue_type == "epic":
            # epics go to plans/backlog/Epic_<name>/
            safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')
            epic_dir = self.plans_dir / f"Epic_{safe_title}"
            epic_dir.mkdir(parents=True, exist_ok=True)
            return epic_dir / f"Epic_{safe_title}.md"
        elif issue_type == "story":
            # stories go to plans/backlog/Epic_<name>/<feature>/Story_<name>.md
            if not epic_num:
                raise ValueError("Story requires epic_num")

            # Find epic directory
            epic_meta = self.metadata["issues"].get(str(epic_num))
            if not epic_meta:
                raise ValueError(f"Epic #{epic_num} not found")

            epic_path = epic_meta.get("path")
            if not epic_path:
                raise ValueError(f"Epic #{epic_num} file path not found")

            epic_dir = Path(epic_path).parent

            # Create feature directory
            safe_feature = re.sub(r'[^\w\s-]', '', feature or "General").strip().replace(' ', '_')
            feature_dir = epic_dir / f"Feat_{safe_feature}"
            feature_dir.mkdir(parents=True, exist_ok=True)

            # Story filename
            safe_title = re.sub(r'[^\w\s-]', '', title).strip().replace(' ', '_')[:50]
            return feature_dir / f"{issue_num}_Story_{safe_title}.md"
        else:
            raise ValueError(f"Unknown issue type: {issue_type}")

    def create_issue(self, title: str, body: str, labels: List[str]) -> IssueData:
        """Create file-based issue"""
        # Determine issue type from labels
        if "epic" in labels:
            issue_type = "epic"
        elif "story" in labels:
            issue_type = "story"
        elif "bug" in labels:
            issue_type = "bug"
        else:
            raise ValueError("Could not determine issue type from labels")

        # Get issue number
        issue_num = self._get_next_issue_num()

        # Determine state from labels
        if "completed" in labels:
            state = "closed"
        else:
            state = "open"

        # Get file path (epic_num and feature will be set in cmd_create if story)
        # For now, we'll store this temporarily and update in create_story
        self.metadata["issues"][str(issue_num)] = {
            "type": issue_type,
            "title": title,
            "labels": labels,
            "state": state,
            "path": None  # Will be set when file is written
        }
        self._save_metadata()

        # Return issue data (path will be set by caller)
        # Note: title already has prefix from cmd_create, don't add another
        return IssueData(
            number=issue_num,
            title=title,
            body=body,
            labels=labels,
            state=state,
            url=f"file://{self.base_dir}/[pending]",
            platform="files"
        )

    def _write_issue_file(self, issue_num: int, file_path: Path, content: str):
        """Write issue content to file and update metadata"""
        file_path.write_text(content)
        self.metadata["issues"][str(issue_num)]["path"] = str(file_path)
        self._save_metadata()

    def get_issue(self, issue_num: int) -> IssueData:
        """Read file-based issue"""
        issue_meta = self.metadata["issues"].get(str(issue_num))
        if not issue_meta:
            raise ValueError(f"Issue #{issue_num} not found")

        file_path = issue_meta.get("path")
        if not file_path or not Path(file_path).exists():
            raise ValueError(f"Issue file not found for #{issue_num}")

        content = Path(file_path).read_text()

        return IssueData(
            number=issue_num,
            title=issue_meta["title"],
            body=content,
            labels=issue_meta["labels"],
            state=issue_meta["state"],
            url=f"file://{file_path}",
            platform="files"
        )

    def update_issue(self, issue_num: int, title: Optional[str] = None, body: Optional[str] = None,
                    labels: Optional[List[str]] = None, state: Optional[str] = None) -> IssueData:
        """Update file-based issue"""
        issue_meta = self.metadata["issues"].get(str(issue_num))
        if not issue_meta:
            raise ValueError(f"Issue #{issue_num} not found")

        # Update metadata
        if title:
            issue_meta["title"] = title
        if labels is not None:
            issue_meta["labels"] = labels
        if state:
            issue_meta["state"] = state

        # Update file content
        file_path = Path(issue_meta["path"])
        if body is not None:
            file_path.write_text(body)

        self._save_metadata()

        return self.get_issue(issue_num)

    def delete_issue(self, issue_num: int, hard_delete: bool = False) -> bool:
        """Delete file-based issue"""
        issue_meta = self.metadata["issues"].get(str(issue_num))
        if not issue_meta:
            return False

        if hard_delete:
            # Delete file
            file_path = Path(issue_meta["path"])
            if file_path.exists():
                file_path.unlink()
            # Remove from metadata
            del self.metadata["issues"][str(issue_num)]
        else:
            # Soft delete - just mark as closed
            issue_meta["state"] = "closed"
            if "completed" not in issue_meta["labels"]:
                issue_meta["labels"] = [l for l in issue_meta["labels"] if l not in ["backlog", "active"]]
                issue_meta["labels"].append("completed")

        self._save_metadata()
        return True


def detect_platform() -> Tuple[str, str, object]:
    """Detect git platform and return (platform, repo_path, api_client)

    Returns file-based backend as fallback if no git remote found.
    """
    try:
        result = subprocess.run(["git", "remote", "-v"], capture_output=True, text=True, check=True)
        remotes = result.stdout

        # Check for GitHub (owner/repo format)
        github_match = re.search(r'github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?(?:\s|$)', remotes)
        if github_match:
            repo = github_match.group(1)
            return ("github", repo, GitHubAPI(repo))

        # Check for GitLab (supports nested paths like group/subgroup/project)
        gitlab_match = re.search(r'gitlab\.com[:/]([^\s]+?)(?:\.git)?(?:\s|$)', remotes)
        if gitlab_match:
            repo = gitlab_match.group(1)
            return ("gitlab", repo, GitLabAPI(repo))

        # Git repo but no GitHub/GitLab remote - use file-based
        print("No GitHub/GitLab remote found - using file-based backend", file=sys.stderr)
        return ("files", os.getcwd(), FileBasedAPI(os.getcwd()))

    except subprocess.CalledProcessError:
        # Not a git repo - use file-based
        print("Not a git repository - using file-based backend", file=sys.stderr)
        return ("files", os.getcwd(), FileBasedAPI(os.getcwd()))


def extract_priority(content: str) -> str:
    """Extract priority from content"""
    content_lower = content.lower()

    if re.search(r'critical|priority.*1|blocking|severe', content_lower):
        return "priority-1"
    elif re.search(r'high|priority.*2|important', content_lower):
        return "priority-2"
    elif re.search(r'low|priority.*4|minor', content_lower):
        return "priority-4"
    else:
        return "priority-3"


def infer_status_from_content(content: str) -> str:
    """Infer status label from content"""
    content_lower = content.lower()

    if re.search(r'status:\s*completed|status:\s*done|status:\s*closed', content_lower):
        return "completed"
    elif re.search(r'status:\s*active|status:\s*in[- ]progress', content_lower):
        return "active"
    elif re.search(r'status:\s*backlog|status:\s*todo', content_lower):
        return "backlog"
    else:
        return "backlog"  # Default


def load_or_create_metadata(base_dir: str = ".") -> Dict:
    """Load or create issue metadata file"""
    metadata_file = Path(base_dir) / ".tmp" / "issue_metadata.json"
    metadata_file.parent.mkdir(parents=True, exist_ok=True)

    if metadata_file.exists():
        with open(metadata_file, 'r') as f:
            return json.load(f)
    else:
        return {
            "next_issue_num": 1,
            "issues": {},
            "epic_containers": {}  # epic_num -> {project_id/milestone}
        }

def save_metadata(metadata: Dict, base_dir: str = "."):
    """Save issue metadata file"""
    metadata_file = Path(base_dir) / ".tmp" / "issue_metadata.json"
    with open(metadata_file, 'w') as f:
        json.dump(metadata, f, indent=2)


def cmd_create(api, issue_type: str, content_file: str, title: str, epic_num: Optional[int] = None,
               feature: Optional[str] = None, priority: Optional[str] = None, status: Optional[str] = None) -> int:
    """Create issue command"""
    # Read content from file
    with open(content_file, 'r') as f:
        content = f.read()

    # Auto-detect priority if not provided
    if not priority:
        priority = extract_priority(content)

    # Auto-detect status if not provided
    if not status:
        status = infer_status_from_content(content)

    # Build labels based on issue type
    labels = [issue_type.lower(), status, priority]

    # Add feature label for stories
    if issue_type.lower() == "story" and feature:
        feat_label = f"feat:{feature.lower().replace(' ', '-').replace('_', '-')}"
        labels.append(feat_label)

    # Prepend epic reference for stories
    if issue_type.lower() == "story" and epic_num:
        content = f"**Part of**: #{epic_num}\n\n{content}"

    # Add prefix to title (if not already present)
    prefix_map = {"epic": "[EPIC]", "story": "[STORY]", "bug": "[BUG]"}
    prefix = prefix_map.get(issue_type.lower(), '')
    if not title.startswith(prefix):
        prefixed_title = f"{prefix} {title}".strip()
    else:
        prefixed_title = title

    # Create issue
    issue_data = api.create_issue(prefixed_title, content, labels)

    # For file-based mode, write the actual file
    if api.platform == "files":
        file_path = api._get_file_path(issue_data.number, issue_type, epic_num, feature, title)
        api._write_issue_file(issue_data.number, file_path, content)
        # Update URL in return data
        issue_data.url = f"file://{file_path}"

    # AUTOMATIC CONTAINER MANAGEMENT (transparent to user)
    metadata = load_or_create_metadata()

    if issue_type.lower() == "epic":
        # Automatically create project (GitHub) or milestone (GitLab) for epic
        container_id = None

        if api.platform == "github":
            container_id = api.create_epic_project(title)
            # Add epic itself to project
            if container_id:
                api.add_issue_to_project(container_id, issue_data.number)
        elif api.platform == "gitlab":
            container_id = api.create_epic_milestone(title)

        # Store container info in metadata
        if container_id:
            owner_info = {}
            if api.platform == "github":
                # Store owner (org or user) for GitHub projects
                owner_info["owner"] = api.repo.split('/')[0] if '/' in api.repo else "@me"

            metadata["epic_containers"][str(issue_data.number)] = {
                "platform": api.platform,
                "container_id": container_id,
                "epic_title": title,
                **owner_info
            }
            save_metadata(metadata)

    elif issue_type.lower() == "story" and epic_num:
        # Automatically add story to epic's container
        epic_container = metadata.get("epic_containers", {}).get(str(epic_num))

        if epic_container and api.platform == epic_container["platform"]:
            if api.platform == "github":
                owner = epic_container.get("owner")
                api.add_issue_to_project(epic_container["container_id"], issue_data.number, owner=owner)
            elif api.platform == "gitlab":
                api.assign_to_milestone(issue_data.number, epic_container["container_id"])

    # Output as JSON for easy parsing
    print(json.dumps(asdict(issue_data), indent=2))

    return 0


def cmd_read(api, issue_num: int) -> int:
    """Read issue command"""
    issue_data = api.get_issue(issue_num)
    print(json.dumps(asdict(issue_data), indent=2))
    return 0


def cmd_update(api, issue_num: int, title: Optional[str] = None, body_file: Optional[str] = None,
               labels: Optional[str] = None, state: Optional[str] = None) -> int:
    """Update issue command"""
    # Read body from file if provided
    body = None
    if body_file:
        with open(body_file, 'r') as f:
            body = f.read()

    # Parse labels
    label_list = None
    if labels is not None:
        label_list = [l.strip() for l in labels.split(',')] if labels else []

    # Update issue
    issue_data = api.update_issue(issue_num, title=title, body=body, labels=label_list, state=state)

    # Output as JSON
    print(json.dumps(asdict(issue_data), indent=2))

    return 0


def cmd_delete(api, issue_num: int, hard_delete: bool = False) -> int:
    """Delete issue command"""
    success = api.delete_issue(issue_num, hard_delete=hard_delete)

    if success:
        action = "deleted" if hard_delete else "closed"
        print(json.dumps({"success": True, "message": f"Issue #{issue_num} {action}"}))
        return 0
    else:
        print(json.dumps({"success": False, "message": f"Failed to delete issue #{issue_num}"}), file=sys.stderr)
        return 1


def cmd_list(api, epic_num: Optional[int] = None, issue_type: Optional[str] = None) -> int:
    """List issues filtered by epic and/or type"""
    metadata = load_or_create_metadata()

    if epic_num:
        # Get stories for specific epic
        epic_container = metadata.get("epic_containers", {}).get(str(epic_num))

        if not epic_container:
            print(f"No container found for epic #{epic_num}. Stories may still exist with feat: labels.", file=sys.stderr)
            print(json.dumps([]))
            return 0

        if api.platform == "github":
            # List GitHub Project items
            project_id = epic_container["container_id"]
            cmd = ["gh", "project", "item-list", project_id, "--owner", "@me", "--format", "json"]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"Error listing project items: {result.stderr}", file=sys.stderr)
                return 1

            # Parse and filter
            items = json.loads(result.stdout)
            issues = []
            for item in items.get("items", []):
                if item.get("content", {}).get("type") == "Issue":
                    issue_num = item["content"].get("number")
                    if issue_num:
                        # Read full issue data
                        issue_data = api.get_issue(issue_num)
                        if not issue_type or issue_type in issue_data.labels:
                            issues.append(asdict(issue_data))

            print(json.dumps(issues, indent=2))

        elif api.platform == "gitlab":
            # List GitLab Milestone issues
            milestone = epic_container["container_id"]
            cmd = ["glab", "issue", "list", "--milestone", milestone, "--repo", api.repo]
            result = subprocess.run(cmd, capture_output=True, text=True)

            if result.returncode != 0:
                print(f"Error listing milestone issues: {result.stderr}", file=sys.stderr)
                return 1

            # Parse issue numbers from output
            issue_nums = re.findall(r'#(\d+)', result.stdout)
            issues = []
            for num in issue_nums:
                issue_data = api.get_issue(int(num))
                if not issue_type or issue_type in issue_data.labels:
                    issues.append(asdict(issue_data))

            print(json.dumps(issues, indent=2))

        elif api.platform == "files":
            # List files in epic directory
            epic_meta = metadata["issues"].get(str(epic_num))
            if not epic_meta:
                print(json.dumps([]))
                return 0

            epic_path = Path(epic_meta["path"]).parent
            issues = []

            # Find all story files
            for story_file in epic_path.rglob("*_Story_*.md"):
                # Find issue number from metadata
                for num, meta in metadata["issues"].items():
                    if meta.get("path") == str(story_file):
                        issue_data = api.get_issue(int(num))
                        if not issue_type or issue_type in issue_data.labels:
                            issues.append(asdict(issue_data))
                        break

            print(json.dumps(issues, indent=2))

    else:
        print(json.dumps({"error": "No filtering implemented for all issues yet. Use --epic flag."}), file=sys.stderr)
        return 1

    return 0


def main():
    """Main entry point"""
    import argparse

    parser = argparse.ArgumentParser(description='Manage GitHub/GitLab issues (epics/stories/bugs)')
    subparsers = parser.add_subparsers(dest='command', help='Command to execute')

    # Create command
    create_parser = subparsers.add_parser('create', help='Create new issue')
    create_parser.add_argument('type', choices=['epic', 'story', 'bug'], help='Issue type')
    create_parser.add_argument('content_file', help='File containing issue content (markdown)')
    create_parser.add_argument('--title', required=True, help='Issue title')
    create_parser.add_argument('--epic', type=int, help='Parent epic issue number (for stories)')
    create_parser.add_argument('--feature', help='Feature name (for stories)')
    create_parser.add_argument('--priority', choices=['priority-1', 'priority-2', 'priority-3', 'priority-4'],
                             help='Priority level (auto-detected if not provided)')
    create_parser.add_argument('--status', choices=['backlog', 'active', 'completed'],
                             help='Status (auto-detected if not provided)')

    # Read command
    read_parser = subparsers.add_parser('read', help='Read issue details')
    read_parser.add_argument('issue_num', type=int, help='Issue number')

    # Update command
    update_parser = subparsers.add_parser('update', help='Update issue')
    update_parser.add_argument('issue_num', type=int, help='Issue number')
    update_parser.add_argument('--title', help='New title')
    update_parser.add_argument('--body-file', help='File containing new body content')
    update_parser.add_argument('--labels', help='Comma-separated labels (replaces all)')
    update_parser.add_argument('--state', choices=['open', 'closed'], help='Issue state')

    # Delete command
    delete_parser = subparsers.add_parser('delete', help='Delete (close) issue')
    delete_parser.add_argument('issue_num', type=int, help='Issue number')
    delete_parser.add_argument('--hard', action='store_true', help='Hard delete (GitLab only, GitHub will close)')

    # List command
    list_parser = subparsers.add_parser('list', help='List issues by epic')
    list_parser.add_argument('--epic', type=int, required=True, help='Epic issue number')
    list_parser.add_argument('--type', choices=['story', 'bug'], help='Filter by issue type')

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 1

    # Detect platform
    try:
        platform, repo, api = detect_platform()
        print(f"Using {platform}: {repo}", file=sys.stderr)
    except ValueError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    # Execute command
    try:
        if args.command == 'create':
            return cmd_create(api, args.type, args.content_file, args.title,
                            epic_num=args.epic, feature=args.feature,
                            priority=args.priority, status=args.status)
        elif args.command == 'read':
            return cmd_read(api, args.issue_num)
        elif args.command == 'update':
            return cmd_update(api, args.issue_num, title=args.title,
                            body_file=args.body_file, labels=args.labels, state=args.state)
        elif args.command == 'delete':
            return cmd_delete(api, args.issue_num, hard_delete=args.hard)
        elif args.command == 'list':
            return cmd_list(api, epic_num=args.epic, issue_type=args.type)
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    sys.exit(main())
