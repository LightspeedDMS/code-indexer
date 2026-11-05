"""SDK version and the version-range check used at feature load time.

Only a small, explicit subset of PEP 440 is supported, because the SDK has no
dependencies (``packaging`` is not available to it):

* versions are dotted non-negative integers: ``1``, ``0.1``, ``2.10.3``
  (no pre-release, post-release, local or epoch segments);
* a range is one or more comma-separated clauses, all of which must hold;
* operators: ``>=``, ``>``, ``<=``, ``<``, ``==``, ``!=`` and ``~=``
  (compatible release: ``~=1.4`` means ``>=1.4,<2``; ``~=1.4.2`` means
  ``>=1.4.2,<1.5``).

Missing trailing components compare as zero, so ``1.0 == 1.0.0``.
Anything outside the subset raises :class:`InvalidVersionError` instead of
being guessed at.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Tuple

from cidx_sdk._version import __version__
from cidx_sdk.errors import InvalidVersionError

SDK_VERSION: str = __version__

_VERSION_RE = re.compile(r"^[0-9]+(?:\.[0-9]+)*$")
_CLAUSE_RE = re.compile(r"^(~=|==|!=|<=|>=|<|>)\s*([0-9]+(?:\.[0-9]+)*)$")

VersionTuple = Tuple[int, ...]


def parse_version(text: str) -> VersionTuple:
    """Parse ``"1.2.3"`` into ``(1, 2, 3)``.

    Raises:
        InvalidVersionError: the text is not a dotted-integer version.
    """
    if not isinstance(text, str):
        raise InvalidVersionError(f"version must be a string, got {type(text)!r}")
    stripped = text.strip()
    if not _VERSION_RE.match(stripped):
        raise InvalidVersionError(
            f"unsupported version {text!r}: expected dotted integers such as '1.2.3'"
        )
    return tuple(int(part) for part in stripped.split("."))


def _padded(a: VersionTuple, b: VersionTuple) -> Tuple[VersionTuple, VersionTuple]:
    width = max(len(a), len(b))
    return a + (0,) * (width - len(a)), b + (0,) * (width - len(b))


def _compare(a: VersionTuple, b: VersionTuple) -> int:
    left, right = _padded(a, b)
    return (left > right) - (left < right)


@dataclass(frozen=True)
class VersionClause:
    """One ``<operator><version>`` term of a range."""

    operator: str
    version: VersionTuple

    def contains(self, candidate: VersionTuple) -> bool:
        cmp = _compare(candidate, self.version)
        if self.operator == ">=":
            return cmp >= 0
        if self.operator == ">":
            return cmp > 0
        if self.operator == "<=":
            return cmp <= 0
        if self.operator == "<":
            return cmp < 0
        if self.operator == "==":
            return cmp == 0
        if self.operator == "!=":
            return cmp != 0
        # "~=": at least this version, and the same release series, i.e. the
        # same components up to (not including) the last one.
        prefix = self.version[:-1]
        candidate_prefix = (candidate + (0,) * len(prefix))[: len(prefix)]
        return cmp >= 0 and candidate_prefix == prefix

    def __str__(self) -> str:
        return self.operator + ".".join(str(part) for part in self.version)


@dataclass(frozen=True)
class VersionRange:
    """A conjunction of :class:`VersionClause` terms, e.g. ``>=0.1,<0.2``."""

    clauses: Tuple[VersionClause, ...]

    @classmethod
    def parse(cls, spec: str) -> "VersionRange":
        """Parse a range string.

        Raises:
            InvalidVersionError: the spec is empty or uses unsupported syntax.
        """
        if not isinstance(spec, str) or not spec.strip():
            raise InvalidVersionError(
                f"version range must be a non-empty string, got {spec!r}"
            )
        clauses = []
        for raw in spec.split(","):
            term = raw.strip()
            match = _CLAUSE_RE.match(term)
            if match is None:
                raise InvalidVersionError(
                    f"unsupported version range clause {term!r} in {spec!r}"
                )
            operator, version_text = match.group(1), match.group(2)
            version = parse_version(version_text)
            if operator == "~=" and len(version) < 2:
                raise InvalidVersionError(
                    f"'~=' needs at least two version components, got {term!r}"
                )
            clauses.append(VersionClause(operator, version))
        return cls(tuple(clauses))

    def contains(self, version: str) -> bool:
        """Whether *version* satisfies every clause."""
        candidate = parse_version(version)
        return all(clause.contains(candidate) for clause in self.clauses)

    def __str__(self) -> str:
        return ",".join(str(clause) for clause in self.clauses)


def version_satisfies(version: str, spec: str) -> bool:
    """Whether *version* lies inside the range *spec*.

    Raises:
        InvalidVersionError: either argument is outside the supported subset.
    """
    return VersionRange.parse(spec).contains(version)


def sdk_satisfies(requires_sdk: str, sdk_version: str = SDK_VERSION) -> bool:
    """Whether the running SDK (or *sdk_version*) satisfies *requires_sdk*."""
    return version_satisfies(sdk_version, requires_sdk)
