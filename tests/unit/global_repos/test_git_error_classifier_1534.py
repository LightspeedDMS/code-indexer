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


# Codex review Finding 2: the bare substring "does not appear to be a git
# repository" is broad enough to also match a DIFFERENT remote name hitting a
# superficially similar error, potentially co-occurring with genuinely
# transient wording (e.g. an NFS mount blip). Only the exact, git-specific
# phrasing for the "origin" remote (Bug #1534's actual scenario) should be
# treated as permanent.
OTHER_REMOTE_NOT_A_GIT_REPO_STDERR = (
    "fatal: 'upstream' does not appear to be a git repository\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights\n"
    "and the repository exists.\n"
)


def test_non_origin_remote_not_a_git_repo_is_not_misclassified_permanent():
    """
    A stderr message referencing a DIFFERENT quoted remote name (not
    'origin') must not be swept into "permanent" by an over-broad substring
    match. Since it also carries the generic "Could not read from remote"
    transient wording, it must classify as "transient".
    """
    category = classify_fetch_error(OTHER_REMOTE_NOT_A_GIT_REPO_STDERR)
    assert category == "transient"


GENERIC_TRANSIENT_MOUNT_STDERR = (
    "fatal: unable to access the repository\n"
    "Could not read from remote repository.\n"
    "Connection timed out\n"
    "Network is unreachable\n"
)


def test_generic_transient_mount_message_without_origin_phrase_stays_transient():
    """
    Ordinary transient wording (network/mount blip) that never mentions
    "does not appear to be a git repository" at all must still classify as
    "transient" -- locking in that the PERMANENT_PATTERNS entry never
    over-broadens to swallow genuinely recoverable failures.
    """
    category = classify_fetch_error(GENERIC_TRANSIENT_MOUNT_STDERR)
    assert category == "transient"
