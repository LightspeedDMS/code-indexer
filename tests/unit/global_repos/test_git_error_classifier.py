"""
Unit tests for git fetch error classification (Bug #1341).

GitLab's permanent access/existence error ("The project you were looking for
could not be found or you don't have permission to view it.") also emits a
generic "fatal: Could not read from remote repository." line. Before this
fix, that generic line matched the TRANSIENT_PATTERNS entry
"Could not read from remote" (checked before corruption/unknown), so the
scheduler classified a permanently-broken upstream as "transient" and
retried/re-cloned it forever.

These tests exercise the real classify_fetch_error() function directly
(no mocks) to prove:
  - GitLab's permanent error classifies as "permanent", not "transient".
  - A GitHub-style "Repository not found" error also classifies as
    "permanent".
  - Genuine transient errors (timeout, connection reset, DNS failure)
    still classify as "transient".
  - Corruption and unknown classification are unaffected by the new
    PERMANENT category.
"""

from code_indexer.global_repos.git_error_classifier import classify_fetch_error


GITLAB_PERMANENT_STDERR = (
    "remote: ERROR: The project you were looking for could not be found "
    "or you don't have permission to view it.\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights and the "
    "repository exists.\n"
)

GITHUB_NOT_FOUND_STDERR = (
    "remote: Repository not found.\n"
    "fatal: repository 'https://github.com/org/deleted-repo.git/' not found\n"
)


class TestPermanentClassification:
    def test_gitlab_project_not_found_classifies_as_permanent(self):
        assert classify_fetch_error(GITLAB_PERMANENT_STDERR) == "permanent"

    def test_gitlab_permanent_error_is_not_transient(self):
        # Regression guard for the exact bug: the generic "Could not read
        # from remote repository." line must NOT win over the permanent
        # classification just because TRANSIENT_PATTERNS also matches it.
        assert classify_fetch_error(GITLAB_PERMANENT_STDERR) != "transient"

    def test_github_repository_not_found_classifies_as_permanent(self):
        assert classify_fetch_error(GITHUB_NOT_FOUND_STDERR) == "permanent"


class TestTransientClassificationUnaffected:
    def test_connection_timed_out_classifies_as_transient(self):
        stderr = "ssh: connect to host example.com port 22: Connection timed out\n"
        assert classify_fetch_error(stderr) == "transient"

    def test_connection_refused_classifies_as_transient(self):
        stderr = "ssh: connect to host example.com port 22: Connection refused\n"
        assert classify_fetch_error(stderr) == "transient"

    def test_dns_resolution_failure_classifies_as_transient(self):
        stderr = "fatal: unable to access 'https://example.com/repo.git/': Could not resolve host: example.com\n"
        assert classify_fetch_error(stderr) == "transient"

    def test_http_basic_access_denied_classifies_as_transient_not_permanent(self):
        # Code review follow-up on Bug #1341: GitLab's "HTTP Basic: Access
        # denied" is a TRANSIENT token-rotation/credential blip, NOT a
        # permanent access revocation -- it must never be quarantined /
        # backed off as harshly as a genuinely permanent error. The real
        # git fetch failure also includes git's own
        # "fatal: Authentication failed for '<url>'" line, which already
        # matches the existing TRANSIENT_PATTERNS entry "Authentication
        # failed", so this classifies transient without needing a new
        # "access denied" pattern (which was removed from PERMANENT_PATTERNS
        # for being dead/miscategorized).
        stderr = (
            "remote: HTTP Basic: Access denied\n"
            "fatal: Authentication failed for "
            "'https://gitlab.example.com/group/repo.git/'\n"
        )
        assert classify_fetch_error(stderr) == "transient"
        assert classify_fetch_error(stderr) != "permanent"


class TestCorruptionAndUnknownUnaffected:
    def test_corrupt_object_database_still_classifies_as_corruption(self):
        stderr = "error: object file .git/objects/ab/cdef is empty\nfatal: loose object is corrupt\n"
        assert classify_fetch_error(stderr) == "corruption"

    def test_unrecognized_error_still_classifies_as_unknown(self):
        stderr = "fatal: some completely novel git error never seen before\n"
        assert classify_fetch_error(stderr) == "unknown"
