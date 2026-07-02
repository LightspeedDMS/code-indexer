"""
Unit tests for forge_client PR/MR comment methods.

Story #448: list_pull_request_comments - Read review comments and threads

Tests:
  - GitHubForgeClient.list_pull_request_comments: merges review + general comments
  - GitLabForgeClient.list_merge_request_notes: filters system notes, maps fields
"""

import pytest
from unittest.mock import patch, MagicMock


# ---------------------------------------------------------------------------
# GitHubForgeClient.list_pull_request_comments
# ---------------------------------------------------------------------------


class TestGitHubForgeClientListPRComments:
    """Tests for GitHubForgeClient.list_pull_request_comments (sync, Story #448)."""

    def _make_review_comment(
        self,
        id=101,
        login="reviewer1",
        body="Inline review comment",
        created_at="2026-03-11T10:00:00Z",
        updated_at="2026-03-11T10:00:00Z",
        path="src/auth.py",
        line=42,
        original_line=42,
        in_reply_to_id=None,
    ):
        """Build a minimal GitHub pull request review comment (inline)."""
        data = {
            "id": id,
            "user": {"login": login},
            "body": body,
            "created_at": created_at,
            "updated_at": updated_at,
            "path": path,
            "line": line,
            "original_line": original_line,
        }
        if in_reply_to_id is not None:
            data["in_reply_to_id"] = in_reply_to_id
        return data

    def _make_issue_comment(
        self,
        id=201,
        login="commenter1",
        body="General conversation comment",
        created_at="2026-03-11T11:00:00Z",
        updated_at="2026-03-11T11:00:00Z",
    ):
        """Build a minimal GitHub issue comment (general conversation)."""
        return {
            "id": id,
            "user": {"login": login},
            "body": body,
            "created_at": created_at,
            "updated_at": updated_at,
        }

    def test_github_merges_review_and_general_comments(self):
        """list_pull_request_comments merges both API endpoints into one result."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        review_comment = self._make_review_comment(id=101)
        issue_comment = self._make_issue_comment(id=201)

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = [review_comment]

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = [issue_comment]

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert len(result) == 2
        ids = {c["id"] for c in result}
        assert 101 in ids
        assert 201 in ids

    def test_github_review_comments_have_file_path(self):
        """Review comments have file_path, line_number, and is_review_comment=True."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        review_comment = self._make_review_comment(id=101, path="src/auth.py", line=42)

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = [review_comment]

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = []

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert len(result) == 1
        comment = result[0]
        assert comment["file_path"] == "src/auth.py"
        assert comment["line_number"] == 42
        assert comment["is_review_comment"] is True
        assert comment["resolved"] is None  # not available in GitHub API

    def test_github_general_comments_have_null_file_path(self):
        """General (issue) comments have file_path=None, line_number=None, is_review_comment=False."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        issue_comment = self._make_issue_comment(id=201)

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = []

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = [issue_comment]

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert len(result) == 1
        comment = result[0]
        assert comment["file_path"] is None
        assert comment["line_number"] is None
        assert comment["is_review_comment"] is False
        assert comment["resolved"] is None

    def test_github_sorted_by_created_at(self):
        """Comments are sorted chronologically by created_at after merging."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        # Review comment created later
        review_comment = self._make_review_comment(
            id=101, created_at="2026-03-11T12:00:00Z"
        )
        # Issue comment created earlier
        issue_comment = self._make_issue_comment(
            id=201, created_at="2026-03-11T09:00:00Z"
        )

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = [review_comment]

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = [issue_comment]

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert len(result) == 2
        # issue_comment (09:00) should come before review_comment (12:00)
        assert result[0]["id"] == 201
        assert result[1]["id"] == 101

    def test_github_limit_applied(self):
        """Limit caps the total number of comments returned after merging."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        review_comments = [
            self._make_review_comment(id=100 + i, created_at=f"2026-03-11T10:0{i}:00Z")
            for i in range(3)
        ]
        issue_comments = [
            self._make_issue_comment(id=200 + i, created_at=f"2026-03-11T11:0{i}:00Z")
            for i in range(3)
        ]

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = review_comments

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = issue_comments

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
                limit=4,
            )

        assert len(result) == 4

    def test_github_401_raises(self):
        """HTTP 401 on either GitHub endpoint raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitHubForgeClient,
            ForgeAuthenticationError,
        )

        mock_response_401 = MagicMock()
        mock_response_401.status_code = 401
        mock_response_401.text = "Unauthorized"

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_response_401):
            with pytest.raises(ForgeAuthenticationError):
                client.list_pull_request_comments(
                    token="bad_token",
                    host="github.com",
                    owner="owner",
                    repo="repo",
                    number=42,
                )

    def test_github_review_comment_uses_original_line_when_line_none(self):
        """If 'line' is None (outdated comment), fall back to 'original_line'."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        review_comment = self._make_review_comment(
            id=101, path="src/utils.py", line=None, original_line=55
        )

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = [review_comment]

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = []

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert len(result) == 1
        assert result[0]["line_number"] == 55

    def test_github_unified_fields_present(self):
        """All unified output fields are present in each comment."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        review_comment = self._make_review_comment(id=101)

        mock_review_response = MagicMock()
        mock_review_response.status_code = 200
        mock_review_response.json.return_value = [review_comment]

        mock_issue_response = MagicMock()
        mock_issue_response.status_code = 200
        mock_issue_response.json.return_value = []

        client = GitHubForgeClient()
        with patch(
            "httpx.get", side_effect=[mock_review_response, mock_issue_response]
        ):
            result = client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="owner",
                repo="repo",
                number=42,
            )

        assert len(result) == 1
        comment = result[0]
        required_fields = {
            "id",
            "author",
            "body",
            "created_at",
            "updated_at",
            "file_path",
            "line_number",
            "is_review_comment",
            "resolved",
        }
        assert required_fields.issubset(comment.keys())

    def test_github_uses_correct_api_urls(self):
        """github.com uses api.github.com for both pulls/comments and issues/comments."""
        from code_indexer.server.clients.forge_client import GitHubForgeClient

        mock_ok = MagicMock()
        mock_ok.status_code = 200
        mock_ok.json.return_value = []

        client = GitHubForgeClient()
        with patch("httpx.get", return_value=mock_ok) as mock_get:
            client.list_pull_request_comments(
                token="ghp_testtoken",
                host="github.com",
                owner="myorg",
                repo="myrepo",
                number=7,
            )

        calls = mock_get.call_args_list
        assert len(calls) == 2
        urls = [c[0][0] for c in calls]
        assert any("api.github.com" in u and "pulls/7/comments" in u for u in urls)
        assert any("api.github.com" in u and "issues/7/comments" in u for u in urls)


# ---------------------------------------------------------------------------
# GitLabForgeClient.list_merge_request_notes
# ---------------------------------------------------------------------------


class TestGitLabForgeClientListMRNotes:
    """Tests for GitLabForgeClient.list_merge_request_notes (sync, Story #448)."""

    def _make_note(
        self,
        id=301,
        username="reviewer1",
        body="This needs error handling",
        created_at="2026-03-11T10:00:00Z",
        updated_at="2026-03-11T10:00:00Z",
        system=False,
        position=None,
        resolvable=False,
        resolved=False,
    ):
        """Build a minimal GitLab merge request note."""
        data = {
            "id": id,
            "author": {"username": username},
            "body": body,
            "created_at": created_at,
            "updated_at": updated_at,
            "system": system,
            "resolvable": resolvable,
            "resolved": resolved,
        }
        if position is not None:
            data["position"] = position
        return data

    def _make_inline_position(self, new_path="src/auth.py", new_line=42):
        """Build a GitLab note position dict for an inline comment."""
        return {
            "new_path": new_path,
            "new_line": new_line,
        }

    def test_gitlab_filters_system_notes(self):
        """System notes (system=True) are excluded from results."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        user_note = self._make_note(id=301, system=False, body="Real comment")
        system_note = self._make_note(id=302, system=True, body="assigned to @alice")

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [user_note, system_note]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert len(result) == 1
        assert result[0]["id"] == 301
        assert result[0]["body"] == "Real comment"

    def test_gitlab_inline_notes_have_file_path(self):
        """Inline notes with position have file_path, line_number, is_review_comment=True."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        inline_note = self._make_note(
            id=301,
            position=self._make_inline_position(new_path="src/auth.py", new_line=42),
            resolvable=True,
            resolved=False,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [inline_note]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert len(result) == 1
        comment = result[0]
        assert comment["file_path"] == "src/auth.py"
        assert comment["line_number"] == 42
        assert comment["is_review_comment"] is True

    def test_gitlab_general_notes_null_file_path(self):
        """Notes without position have file_path=None, line_number=None, is_review_comment=False."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        general_note = self._make_note(id=301)  # No position

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [general_note]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert len(result) == 1
        comment = result[0]
        assert comment["file_path"] is None
        assert comment["line_number"] is None
        assert comment["is_review_comment"] is False

    def test_gitlab_resolved_field(self):
        """resolved and resolvable fields are mapped to the resolved output field."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        resolved_note = self._make_note(
            id=301,
            resolvable=True,
            resolved=True,
            position=self._make_inline_position(),
        )
        unresolved_note = self._make_note(
            id=302,
            resolvable=True,
            resolved=False,
            position=self._make_inline_position(),
        )
        non_resolvable_note = self._make_note(
            id=303,
            resolvable=False,
            resolved=False,
        )

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            resolved_note,
            unresolved_note,
            non_resolvable_note,
        ]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert len(result) == 3
        # resolvable=True, resolved=True -> resolved=True
        assert result[0]["resolved"] is True
        # resolvable=True, resolved=False -> resolved=False
        assert result[1]["resolved"] is False
        # resolvable=False -> resolved=None (not applicable)
        assert result[2]["resolved"] is None

    def test_gitlab_limit_applied(self):
        """Limit caps total notes returned."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        notes = [self._make_note(id=300 + i) for i in range(10)]

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = notes

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
                limit=3,
            )

        assert len(result) == 3

    def test_gitlab_401_raises(self):
        """HTTP 401 from GitLab raises ForgeAuthenticationError."""
        from code_indexer.server.clients.forge_client import (
            GitLabForgeClient,
            ForgeAuthenticationError,
        )

        mock_response = MagicMock()
        mock_response.status_code = 401
        mock_response.text = "Unauthorized"

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            with pytest.raises(ForgeAuthenticationError):
                client.list_merge_request_notes(
                    token="bad_token",
                    host="gitlab.com",
                    owner="owner",
                    repo="repo",
                    number=5,
                )

    def test_gitlab_uses_url_encoded_project_path(self):
        """GitLab notes API uses URL-encoded project path."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response) as mock_get:
            client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="group/subgroup",
                repo="repo",
                number=5,
            )

        call_url = mock_get.call_args[0][0]
        assert "%2F" in call_url
        assert "notes" in call_url

    def test_gitlab_unified_fields_present(self):
        """All unified output fields are present in each note."""
        from code_indexer.server.clients.forge_client import GitLabForgeClient

        note = self._make_note(id=301)

        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [note]

        client = GitLabForgeClient()
        with patch("httpx.get", return_value=mock_response):
            result = client.list_merge_request_notes(
                token="glpat-testtoken",
                host="gitlab.com",
                owner="owner",
                repo="repo",
                number=5,
            )

        assert len(result) == 1
        comment = result[0]
        required_fields = {
            "id",
            "author",
            "body",
            "created_at",
            "updated_at",
            "file_path",
            "line_number",
            "is_review_comment",
            "resolved",
        }
        assert required_fields.issubset(comment.keys())
