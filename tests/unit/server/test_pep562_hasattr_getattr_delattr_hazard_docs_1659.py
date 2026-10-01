"""Issue #1659: PEP-562 lazy-init `__getattr__` docstrings must document the
hasattr()/getattr()/delattr() hazard at the point of the magic.

Bug #1638 (`server/app.py`) and Bug #1650 (`server/services/git_operations_service.py`)
both introduced a module-level `__getattr__` (PEP 562) that lazily constructs a
heavy singleton service on first genuine attribute access. That mechanism has
three non-obvious consequences for any `hasattr()`/`getattr()`/`delattr()` call
against these modules:

  * `hasattr(module, name)` for any name in `_LAZY_INIT_ATTRS` ALWAYS returns
    True, even before real construction has happened -- it cannot distinguish
    "genuinely set" from "will be synthesized on demand".
  * A bare `getattr(module, name, default)` -- even one written defensively,
    expecting the attribute might legitimately be absent -- silently triggers
    full lazy construction (real DB reads, background thread spawns) as a
    side effect.
  * `delattr()` bypasses `__getattr__` entirely (PEP 562 defines no
    `__delattr__` hook) and can raise `AttributeError` even though `hasattr()`
    just reported the attribute exists (concrete breakage: issue #1658).

Issue #1659 audited every call site against both modules (production code and
tests) and found zero call sites with real live impact -- every existing
`getattr(module, name, default)` use is default-bearing (safe regardless of
PEP-562 internal state) and the two `delattr()` uses in tests are structurally
guarded such that the #1658 failure shape can never be reached (see commit
fc1a26b8's message for the full site-by-site audit trail). Per Messi Rule #17
(anti-magic), the non-obvious control flow this creates must still be made
visible at the point of the magic -- these tests are the regression guard for
that documentation, checked directly against each module's live `__getattr__`
docstring so the guard fails if a future edit strips (or waters down) the
hazard note. Each required phrase below ties the hazard to its specific
cause (not a loose co-occurrence of unrelated keywords), so the guard cannot
be satisfied by an unrelated docstring that merely happens to mention
"hasattr" and "delattr" somewhere.
"""

import code_indexer.server.app as app_module
import code_indexer.server.services.git_operations_service as gos_module

# Exact (lowercased) phrases the __getattr__ docstring of a module using this
# lazy-init pattern must contain. Each phrase pins the hazard to its specific
# cause, not just a bag of unrelated keywords, so this cannot be satisfied by
# an unrelated docstring that happens to mention "hasattr" and "delattr".
_REQUIRED_HAZARD_PHRASES = (
    # hasattr() always returns True for a lazy name -- tied to WHY (synthesized
    # on demand rather than raising).
    "hasattr(module, name) for any name in _lazy_init_attrs always returns true",
    # getattr() with a default silently triggers construction -- tied to the
    # side-effect consequence, not just the word "getattr".
    "getattr(module, name, default) -- even one written defensively, "
    "expecting the attribute might legitimately be absent -- triggers "
    "full lazy construction as a side effect on first access",
    # delattr() bypasses __getattr__ and can raise despite hasattr() saying True.
    "delattr(module, name) bypasses this __getattr__ entirely",
    "raise attributeerror even though hasattr() just reported the attribute exists",
    # The correct alternative: inspect the internal sentinels directly.
    "inspect _initialized/_lazy_values directly instead of calling hasattr()/getattr()",
)


def _assert_hazard_documented(doc: str, module_label: str) -> None:
    lowered = " ".join((doc or "").lower().split())
    for phrase in _REQUIRED_HAZARD_PHRASES:
        normalized_phrase = " ".join(phrase.split())
        assert normalized_phrase in lowered, (
            f"{module_label}'s module __getattr__() docstring is missing "
            f"the required Issue #1659 hazard phrase:\n  {phrase!r}\n"
            f"Full docstring was:\n{doc}"
        )


class TestAppModuleGetattrDocumentsHazard:
    """`code_indexer.server.app.__getattr__` must document the hazard."""

    def test_docstring_documents_full_hasattr_getattr_delattr_hazard(
        self,
    ) -> None:
        _assert_hazard_documented(app_module.__getattr__.__doc__ or "", "server/app.py")


class TestGitOperationsServiceGetattrDocumentsHazard:
    """`git_operations_service.__getattr__` must document the same hazard."""

    def test_docstring_documents_full_hasattr_getattr_delattr_hazard(
        self,
    ) -> None:
        _assert_hazard_documented(
            gos_module.__getattr__.__doc__ or "",
            "server/services/git_operations_service.py",
        )
