"""Regression guard: golden-repo registry-orphan reconcile must run at
server startup (Bug #1317, requirement 2).

Full lifespan execution requires a constructed FastAPI app + all
collaborators, so -- following the established pattern in
test_lifespan_golden_backend_wiring_bug.py -- this is a source-text guard:
it asserts lifespan.py actually calls reconcile_golden_repo_registry() in a
fail-soft block, rather than the reconcile module existing but never being
invoked (Messi Rule #12, anti-orphan-code).
"""

import re
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_CALL_RE = re.compile(r"reconcile_golden_repo_registry\s*\(")


def _source() -> str:
    return _LIFESPAN_PATH.read_text()


class TestLifespanGoldenRepoReconcileWiringBug1317:
    """Source-text guard: lifespan.py must invoke the Bug #1317 reconcile."""

    def test_lifespan_imports_reconcile_golden_repo_registry(self):
        """lifespan.py must import the reconcile entrypoint."""
        source = _source()
        assert "from code_indexer.server.services.golden_repo_reconciler import" in (
            source
        ), (
            "lifespan.py must import reconcile_golden_repo_registry from "
            "golden_repo_reconciler -- reconcile module exists but is never "
            "wired at startup (Bug #1317)."
        )
        assert "reconcile_golden_repo_registry" in source

    def test_lifespan_calls_reconcile_with_golden_repo_manager(self):
        """The reconcile call must be passed the live golden_repo_manager."""
        source = _source()
        match = _CALL_RE.search(source)
        assert match is not None, (
            "reconcile_golden_repo_registry(...) call not found in lifespan.py"
        )
        closing_paren = source.find(")", match.end())
        assert closing_paren != -1, "closing paren not found after reconcile call"

        call_args = source[match.end() : closing_paren]
        assert "golden_repo_manager" in call_args

    def test_reconcile_call_is_fail_soft(self):
        """The reconcile call must never be allowed to block/crash startup --
        it must be wrapped in a try/except that logs on failure."""
        source = _source()
        match = _CALL_RE.search(source)
        assert match is not None

        # Search backwards for the nearest preceding 'try:' and forwards for
        # the nearest following 'except' clause -- proves the call sits
        # inside a try/except block, not bare at module/function scope.
        preceding = source[: match.start()]
        try_match = list(re.finditer(r"\btry\s*:", preceding))
        assert try_match, "no preceding 'try:' found before the reconcile call"

        following = source[match.end() :]
        except_match = re.search(r"\bexcept\b", following)
        assert except_match is not None, "no 'except' found after the reconcile call"
