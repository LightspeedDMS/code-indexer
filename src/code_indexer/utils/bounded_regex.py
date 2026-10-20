"""Caller-supplied regular expressions, matched under a bounded budget.

Every caller-supplied pattern that is matched in Python goes through
``BoundedPattern``: one use (one search request) has one total matching
budget, each call gets what remains of it (optionally capped per call),
and a call that runs out raises ``PatternBudgetExhausted``. One WARNING is
logged per use, naming its purpose and never the pattern. Only time spent
matching counts.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, List, Optional, TypeVar

import regex

logger = logging.getLogger(__name__)

CALLER_PATTERN_BUDGET_SECONDS = 1.0

T = TypeVar("T")


class PatternBudgetExhausted(TimeoutError):
    """A caller-supplied pattern ran out of matching time."""


class BoundedPattern:
    """One use of a caller-supplied pattern (see the module docstring)."""

    def __init__(
        self,
        pattern: str,
        flags: int = 0,
        *,
        purpose: str,
        budget_seconds: float = CALLER_PATTERN_BUDGET_SECONDS,
        call_seconds: Optional[float] = None,
    ) -> None:
        """Raises ``re.error`` for an invalid pattern, as ``re.compile``
        does, so callers keep their invalid-pattern handling."""
        try:
            self._compiled = regex.compile(pattern, int(flags))
        except regex.error as exc:
            raise re.error(str(exc)) from exc
        self._purpose = purpose
        self._remaining = budget_seconds
        self._call_seconds = call_seconds
        self._warned = False

    @property
    def exhausted(self) -> bool:
        """True once the total budget is spent."""
        return self._remaining <= 0

    def search(self, text: str) -> Optional[Any]:
        """The first match in *text*, or None.

        Raises:
            PatternBudgetExhausted: this call ran out of time.
        """
        return self._timed(lambda timeout: self._compiled.search(text, timeout=timeout))

    def finditer(self, text: str) -> List[Any]:
        """Every match in *text*, in order (a list: a drop-in for
        ``re.Pattern.finditer`` that iterates the result).

        Raises:
            PatternBudgetExhausted: this call ran out of time.
        """
        return self._timed(
            lambda timeout: list(self._compiled.finditer(text, timeout=timeout))
        )

    def _timed(self, match: Callable[[float], T]) -> T:
        if self.exhausted:
            raise self._out_of_time()
        timeout = self._remaining
        if self._call_seconds is not None:
            timeout = min(timeout, self._call_seconds)
        started = time.monotonic()
        try:
            return match(timeout)
        except TimeoutError as exc:
            raise self._out_of_time() from exc
        finally:
            self._remaining -= time.monotonic() - started

    def _out_of_time(self) -> PatternBudgetExhausted:
        if not self._warned:
            self._warned = True
            logger.warning(
                "A caller-supplied %s pattern exceeded its matching time budget; "
                "its remaining matches are skipped",
                self._purpose,
            )
        return PatternBudgetExhausted(
            f"{self._purpose} pattern exceeded its matching time budget"
        )
