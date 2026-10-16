"""CI identifier validation and forge URL path-segment encoding.

CI run, job and pipeline identifiers are positive integers. They are
validated here before they reach any forge request, so a CI call can only
address the repository the caller was authorised for. The MCP/REST CI
handlers validate before resolving any credential; the forge clients
validate again and percent-encode every interpolated path segment.
"""

from typing import Any
from urllib.parse import quote, unquote

# Forge CI identifiers are signed 64-bit integers.
_MAX_CI_ID = 2**63 - 1
_MAX_CI_ID_DIGITS = len(str(_MAX_CI_ID))
_ASCII_DIGITS = frozenset("0123456789")


class InvalidCIIdentifierError(ValueError):
    """A CI identifier or forge path segment failed validation."""

    def __init__(self, name: str) -> None:
        super().__init__(f"Invalid parameter: {name} must be a positive integer")
        self.name = name


def parse_ci_id(value: Any, name: str) -> int:
    """Return ``value`` as a positive integer CI identifier.

    Accepts an ``int`` (not ``bool``) or a string of ASCII digits only. Signs,
    whitespace, separators, percent-encoding, non-ASCII digits, zero and
    values above the signed 64-bit range are refused.

    Raises:
        InvalidCIIdentifierError: ``value`` is not a positive integer.
    """
    if isinstance(value, bool):
        raise InvalidCIIdentifierError(name)
    if isinstance(value, int):
        number = value
    elif (
        isinstance(value, str)
        and 0 < len(value) <= _MAX_CI_ID_DIGITS
        and set(value) <= _ASCII_DIGITS
    ):
        number = int(value)
    else:
        raise InvalidCIIdentifierError(name)
    if not 0 < number <= _MAX_CI_ID:
        raise InvalidCIIdentifierError(name)
    return number


def ci_id_segment(value: Any, name: str) -> str:
    """Validate a CI identifier and return it as a URL path segment."""
    return str(parse_ci_id(value, name))


def _refuse_dot_segment(raw: str, encoded: str, name: str) -> str:
    if raw in ("", ".", ".."):
        raise ValueError(f"Invalid {name}: empty or dot path segment")
    return encoded


def encode_path_segment(value: str, name: str) -> str:
    """Percent-encode ``value`` as exactly one URL path segment."""
    return _refuse_dot_segment(value, quote(value, safe=""), name)


def encode_github_repository(repository: str) -> str:
    """Encode an ``owner/repo`` repository as its URL path segments."""
    return "/".join(
        encode_path_segment(part, "repository") for part in repository.split("/")
    )


def encode_gitlab_project_id(project_id: str) -> str:
    """Encode a GitLab project id or path as ONE URL path segment.

    ``namespace/project`` becomes ``namespace%2Fproject``. An id that is
    already percent-encoded (``namespace%2Fproject``) is kept as it is, so
    it is never encoded twice. Every segment of the decoded path must be
    non-empty and not ``.`` or ``..``.

    Raises:
        ValueError: the decoded path has an empty or dot segment.
    """
    value = str(project_id)
    for segment in unquote(value).split("/"):
        _refuse_dot_segment(segment, segment, "project_id")
    return quote(value, safe="%")
