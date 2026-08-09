"""
Unit test for Bug #1534: classify a fetch failure caused by a missing/invalid
origin remote as "permanent", not "transient".

"fatal: 'origin' does not appear to be a git repository" is emitted by git
when the origin remote does not exist (or points at something that is not a
valid git repository). This is a structurally permanent condition -- no
amount of retrying will fix it -- yet no PERMANENT_PATTERNS entry matched it
prior to this fix, so it fell through to "unknown" or (worse) could be
conflated with transient network errors that retry forever.
"""

from code_indexer.global_repos.git_error_classifier import classify_fetch_error


ORIGIN_NOT_A_GIT_REPO_STDERR = (
    "fatal: 'origin' does not appear to be a git repository\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)


def test_does_not_appear_to_be_a_git_repository_is_permanent():
    """
    A missing/invalid origin remote must classify as "permanent", not
    "transient" -- this failure can never self-resolve via retry.
    """
    category = classify_fetch_error(ORIGIN_NOT_A_GIT_REPO_STDERR)
    assert category == "permanent"
