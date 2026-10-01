"""User input validation for admin operations."""

import re
from typing import Set


class UserValidationError(Exception):
    """Exception raised when user input validation fails."""

    pass


# Valid roles based on server UserRole enum
VALID_ROLES: Set[str] = {"admin", "power_user", "normal_user"}

# Username validation pattern: alphanumeric, underscores, hyphens, dots
USERNAME_PATTERN = re.compile(r"^[a-zA-Z0-9._-]+$")

# A DENY-list of genuine single-path-component hazards on Linux, used by
# validate_username_path_safe(). This validator's job is path safety, not
# username cosmetics (that is validate_username()'s job), so only real
# hazards are matched here -- display-style usernames (embedded spaces,
# accented/non-Latin letters, apostrophes) are single path components and
# are accepted.
# The invariant: a username never contains a path separator for either
# platform ('/' or '\\'), and never contains NUL or another C0 control
# character (0x00-0x1F) or DEL (0x7F), which includes newline/CR. Exact
# '.' / '..' are hazards by VALUE, not by character, and are checked
# separately below.
_USERNAME_UNSAFE_CHARS_PATTERN = re.compile(r"[\x00-\x1f\x7f/\\]")

# Names that collide with a server-owned entry directly under
# activated_repos_dir. The invariant: a username is never one of these
# names, so a per-user directory (activated_repos_dir/<username>/) is
# never the same path as a server-owned entry (e.g. '.trash' is
# ActivatedRepoManager's shared trash root used by deactivation and swept
# by the startup cleanup -- see activated_repo_manager.py and
# deactivation_helpers.py). Verified by inspection to be the ONLY such
# literal child of activated_repos_dir in the codebase. Shared with
# ActivatedRepoManager._is_safe_path_component (defense in depth for
# already-existing accounts) so both the account-creation-time gate and
# the runtime containment check enforce the same reserved set from one
# place.
RESERVED_ACTIVATED_REPOS_DIR_NAMES = frozenset({".trash"})

# Maximum username length accepted by validate_username_path_safe(). Matches
# the broadest max_length already enforced by server Pydantic models
# (LoginRequest/CreateUserRequest use 255); RegistrationRequest's narrower
# 50-char limit is a stricter subset already satisfied by this bound.
_USERNAME_PATH_SAFE_MAX_LENGTH = 255

# Email validation pattern (basic RFC 5322 compliance)
EMAIL_PATTERN = re.compile(
    r"^[a-zA-Z0-9.!#$%&'*+/=?^_`{|}~-]+@[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?(?:\.[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?)*$"
)


def validate_username(username: str) -> str:
    """Validate username format and requirements.

    Args:
        username: Username to validate

    Returns:
        Validated and cleaned username

    Raises:
        UserValidationError: If username is invalid
    """
    if not username:
        raise UserValidationError("Username cannot be empty")

    # Strip whitespace
    username = username.strip()

    if not username:
        raise UserValidationError("Username cannot be empty or contain only whitespace")

    if len(username) < 3:
        raise UserValidationError("Username must be at least 3 characters long")

    if len(username) > 32:
        raise UserValidationError("Username cannot be longer than 32 characters")

    if not USERNAME_PATTERN.match(username):
        raise UserValidationError(
            "Username can only contain letters, numbers, underscores, hyphens, and dots"
        )

    # Username cannot start or end with special characters
    if username.startswith((".", "-", "_")) or username.endswith((".", "-", "_")):
        raise UserValidationError(
            "Username cannot start or end with dots, hyphens, or underscores"
        )

    # Username cannot contain consecutive special characters
    if re.search(r"[._-]{2,}", username):
        raise UserValidationError(
            "Username cannot contain consecutive dots, hyphens, or underscores"
        )

    return username


def validate_username_path_safe(username: str) -> str:
    """Validate a username is safe to use as a single filesystem path
    component.

    This is the server-side gate applied on EVERY account-creation path
    (RegistrationRequest, CreateUserRequest, UserManager.create_user,
    UserManager.create_oidc_user). The invariant: an accepted username is
    exactly one path component -- never '.', '..', and never containing
    '/' or '\\' -- in the per-user activated-repos directory join
    (``os.path.join(activated_repos_dir, username, user_alias)`` in
    ActivatedRepoManager). Deliberately independent of ActivatedRepoManager's
    own realpath-containment checks (defense in depth -- this function only
    governs whether a username is ever ACCEPTED in the first place, not
    whether an already-stored username is safe to join).

    This is a DENY-list of genuine path hazards, not a character
    allow-list: it accepts embedded spaces, accented/non-Latin letters,
    apostrophes, leading hyphens, leading/trailing whitespace, and any
    other character or shape that is not one of the specific hazards
    below. Does
    NOT enforce validate_username()'s CLI-oriented strictness (minimum
    length, no leading/trailing '.'/'-'/'_', no consecutive specials) --
    that is a separate, stricter UX policy unrelated to path safety. Does
    NOT strip whitespace either -- callers that want trimming (the
    Pydantic model validators) strip() before calling this.

    Args:
        username: Username to validate

    Returns:
        The unchanged username (no normalization)

    Raises:
        UserValidationError: If username is unsafe as a path component
    """
    if not username:
        raise UserValidationError("Username cannot be empty")

    if len(username) > _USERNAME_PATH_SAFE_MAX_LENGTH:
        raise UserValidationError(
            f"Username cannot be longer than {_USERNAME_PATH_SAFE_MAX_LENGTH} characters"
        )

    # Exact '.' / '..' are the OS-special path components that resolve to
    # the current/parent directory.
    if username in (".", ".."):
        raise UserValidationError("Username cannot be '.' or '..'")

    # A username is never the name of a server-owned entry directly under
    # activated_repos_dir (e.g. '.trash').
    if username in RESERVED_ACTIVATED_REPOS_DIR_NAMES:
        raise UserValidationError(
            f"Username {username!r} is reserved for internal server use"
        )

    # search(), not fullmatch() against an allow-list: this flags the
    # presence of a hazard character ANYWHERE in the string (leading,
    # trailing, or embedded), so safety does not depend on where the
    # hazard character sits or on check order. The pattern matches only
    # '/', '\\', the C0 control range (0x00-0x1F), and DEL (0x7F) -- the
    # message below names exactly that set, nothing broader.
    if _USERNAME_UNSAFE_CHARS_PATTERN.search(username):
        raise UserValidationError(
            "Username cannot contain '/', '\\', or a C0 control character "
            "(0x00-0x1F, including NUL/newline/CR) or DEL (0x7F)"
        )

    return username


def validate_email(email: str) -> str:
    """Validate email format and requirements.

    Args:
        email: Email address to validate

    Returns:
        Validated and cleaned email address

    Raises:
        UserValidationError: If email is invalid
    """
    if not email:
        raise UserValidationError("Email cannot be empty")

    # Strip whitespace and convert to lowercase
    email = email.strip().lower()

    if not email:
        raise UserValidationError("Email cannot be empty or contain only whitespace")

    if len(email) > 254:  # RFC 5321 limit
        raise UserValidationError("Email address cannot be longer than 254 characters")

    # Additional checks for common issues (before regex)
    if email.startswith(".") or email.endswith("."):
        raise UserValidationError("Email address cannot start or end with a dot")

    if ".." in email:
        raise UserValidationError("Email address cannot contain consecutive dots")

    if not EMAIL_PATTERN.match(email):
        raise UserValidationError("Invalid email address format")

    # Check local part (before @) length
    local_part = email.split("@")[0]
    if len(local_part) > 64:  # RFC 5321 limit
        raise UserValidationError(
            "Email local part cannot be longer than 64 characters"
        )

    return email


def validate_password(password: str) -> str:
    """Validate password strength and requirements.

    Args:
        password: Password to validate

    Returns:
        Validated password (unchanged)

    Raises:
        UserValidationError: If password is invalid
    """
    if not password:
        raise UserValidationError("Password cannot be empty")

    if len(password) < 8:
        raise UserValidationError("Password must be at least 8 characters long")

    if len(password) > 128:
        raise UserValidationError("Password cannot be longer than 128 characters")

    # Check for common weak patterns first
    if password.lower() in ["password", "12345678", "qwerty123", "abc123456"]:
        raise UserValidationError("Password is too common and easily guessable")

    # Check for at least one uppercase letter
    if not re.search(r"[A-Z]", password):
        raise UserValidationError("Password must contain at least one uppercase letter")

    # Check for at least one lowercase letter
    if not re.search(r"[a-z]", password):
        raise UserValidationError("Password must contain at least one lowercase letter")

    # Check for at least one digit
    if not re.search(r"\d", password):
        raise UserValidationError("Password must contain at least one digit")

    # Check for at least one special character
    if not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
        raise UserValidationError(
            'Password must contain at least one special character (!@#$%^&*(),.?":{}|<>)'
        )

    return password


def validate_role(role: str) -> str:
    """Validate user role against available options.

    Args:
        role: Role to validate

    Returns:
        Validated role

    Raises:
        UserValidationError: If role is invalid
    """
    if not role:
        raise UserValidationError("Role cannot be empty")

    # Strip whitespace and convert to lowercase for comparison
    role = role.strip().lower()

    if not role:
        raise UserValidationError("Role cannot be empty or contain only whitespace")

    if role not in VALID_ROLES:
        valid_roles_str = ", ".join(sorted(VALID_ROLES))
        raise UserValidationError(
            f"Invalid role '{role}'. Valid roles are: {valid_roles_str}"
        )

    return role
