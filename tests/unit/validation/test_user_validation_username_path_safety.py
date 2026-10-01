"""
Tests for validate_username_path_safe.

Usernames without character validation would become
traversable activated-repos path components (server/repositories/
activated_repo_manager.py joins raw username into filesystem paths).

This validator is the account-creation-time gate:
reject '.', '..', path separators, NUL, control characters, and
whitespace, while still accepting legitimate OIDC-derived usernames
(including '@' for UPN/email-style identities from IdPs whose
username_claim is configured to an email-shaped claim).

Foundation #1 compliant: no mocks, real function calls with real inputs.
"""

import pytest

from code_indexer.validation.user_validation import (
    validate_username_path_safe,
    UserValidationError,
)


class TestValidateUsernamePathSafeRejectsTraversal:
    """Discriminating RED cases: unsafe path-component
    usernames."""

    def test_rejects_single_dot(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe(".")

    def test_rejects_double_dot(self):
        """The parent-directory username."""
        with pytest.raises(UserValidationError):
            validate_username_path_safe("..")

    def test_rejects_forward_slash(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a/b")

    def test_rejects_leading_traversal_with_slash(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("../a")

    def test_rejects_backslash(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\\b")

    def test_rejects_nul_byte(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\x00b")

    def test_rejects_control_character(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\nb")

    def test_accepts_embedded_whitespace(self):
        assert validate_username_path_safe("a b") == "a b"

    def test_rejects_empty_string(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("")

    def test_rejects_none(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe(None)  # type: ignore[arg-type]

    def test_accepts_leading_dot(self):
        assert validate_username_path_safe(".hidden") == ".hidden"

    def test_accepts_leading_hyphen(self):
        """No subprocess call site in the codebase takes a bare username
        as a CLI argument (verified by inspection) -- restoring
        pre-existing behaviour for this no-real-hazard case."""
        assert validate_username_path_safe("-rf") == "-rf"

    def test_rejects_overlong_username(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a" * 256)

    def test_rejects_reserved_trash_directory_name(self):
        """A username of '.trash' would collide with the server-owned
        activated_repos_dir/.trash directory."""
        with pytest.raises(UserValidationError):
            validate_username_path_safe(".trash")


class TestValidateUsernamePathSafeAcceptsLegitimateUsernames:
    """Must not break real accounts: default admin, existing test corpus
    usernames, and OIDC-derived usernames (default claim 'preferred_username'
    plus UPN/email-shaped custom claims)."""

    @pytest.mark.parametrize(
        "username",
        [
            "admin",
            "alice",
            "bob.smith",
            "test_user",
            "test-user",
            "power_user1",
            "u1",
            "jdoe",
            "jane.doe@example.com",  # UPN-style OIDC preferred_username
            "john+tag@example.com",  # real-world OIDC username with '+'
        ],
    )
    def test_accepts_legitimate_username(self, username):
        assert validate_username_path_safe(username) == username

    def test_three_dots_accepted_not_an_os_special_component(self):
        """'...' is not an OS-special relative path component -- only the
        exact strings '.' and '..' resolve specially on Linux -- so it
        must be accepted."""
        assert validate_username_path_safe("...") == "..."


class TestValidateUsernamePathSafeRejectsHazardAtAnyPosition:
    """The character-class check does not depend on WHERE the hazard
    character sits (leading, trailing, or embedded) or on check order --
    exercised entirely through the public function, not a private regex
    constant."""

    def test_rejects_trailing_newline(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("alice\n")

    def test_rejects_embedded_newline(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("al\nice")

    def test_rejects_leading_newline(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("\nalice")
