"""
Tests for validate_username_path_safe() accepting real-world display-style
usernames while still rejecting genuine filesystem path hazards.

Invariant: usernames never contain path separators or relative path
components; every other name -- including "john smith", accented Latin
names, non-Latin scripts, and apostrophes -- is accepted.

Foundation #1 compliant: no mocks, real function calls with real inputs.
"""

import pytest

from code_indexer.validation.user_validation import (
    validate_username_path_safe,
    UserValidationError,
)


class TestValidateUsernamePathSafeAcceptsDisplayStyleNames:
    """These names are single path components and must be accepted."""

    @pytest.mark.parametrize(
        "username",
        [
            "john smith",  # embedded space
            "josé",  # accented Latin letter
            "o'brien",  # apostrophe
            "李",  # non-Latin script (CJK)
            "Anaïs Núñez",  # multiple accents + embedded space
            "d'Artagnan",  # leading-lowercase apostrophe name
            "...",  # not OS-special (only exact '.' / '..' are)
            ".hidden",  # leading dot is not a path hazard by itself
        ],
    )
    def test_accepts_display_style_username(self, username):
        assert validate_username_path_safe(username) == username


class TestValidateUsernamePathSafeStillRejectsGenuineHazards:
    """Path separators, relative path components and control characters
    are rejected."""

    def test_rejects_single_dot(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe(".")

    def test_rejects_double_dot(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("..")

    def test_rejects_forward_slash(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a/b")

    def test_rejects_leading_parent_component_with_slash(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("../a")

    def test_rejects_backslash(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\\b")

    def test_rejects_domain_backslash_user(self):
        """The exact OIDC-shaped hazard: a Windows-domain-style claim."""
        with pytest.raises(UserValidationError):
            validate_username_path_safe("DOMAIN\\user")

    def test_rejects_nul_byte(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\x00b")

    def test_rejects_embedded_newline(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\nb")

    def test_rejects_embedded_carriage_return(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a\rb")

    def test_rejects_trailing_newline(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("alice\n")

    def test_rejects_empty_string(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("")

    def test_rejects_none(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe(None)  # type: ignore[arg-type]

    def test_accepts_leading_hyphen(self):
        """No subprocess call site in the codebase takes a bare username
        as a CLI argument (verified by inspection) -- usernames only ever
        appear embedded inside absolute paths, so a leading hyphen
        carries no real hazard here."""
        assert validate_username_path_safe("-rf") == "-rf"

    def test_rejects_overlong_username(self):
        with pytest.raises(UserValidationError):
            validate_username_path_safe("a" * 256)

    def test_accepts_leading_or_trailing_whitespace_unchanged(self):
        """This validator does not strip or reject whitespace itself --
        callers that want trimming (the Pydantic model validators) strip()
        before calling it, matching the behaviour of every
        account-creation path before this validator existed."""
        assert validate_username_path_safe(" alice") == " alice"
        assert validate_username_path_safe("alice ") == "alice "

    def test_rejects_reserved_trash_directory_name(self):
        """A username of '.trash' would collide with the server-owned
        activated_repos_dir/.trash directory used by deactivation and
        the startup cleanup sweep, making that account's per-user
        directory the shared trash root."""
        with pytest.raises(UserValidationError):
            validate_username_path_safe(".trash")


class TestValidateUsernamePathSafeStillAcceptsAsciiUsernames:
    """Existing accounts/test corpus using the narrower ASCII charset must
    keep working unchanged."""

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
            "jane.doe@example.com",
            "john+tag@example.com",
        ],
    )
    def test_accepts_legitimate_username(self, username):
        assert validate_username_path_safe(username) == username
