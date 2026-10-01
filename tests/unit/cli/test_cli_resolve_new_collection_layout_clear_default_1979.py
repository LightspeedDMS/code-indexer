"""Bug #1979 round 6: the maintainer's own acceptance criteria for this
issue (`gh issue view 1979 --comments`) are explicit and unconditional:
"After a successful clear=true run, every configured collection (all
providers, plus multimodal when the repo has such content) is populated and
uses the CHUNKS_DB layout, whether or not the repo changed since its last
index." This is a deliberate, issue-scoped exception to the general
CLI/daemon SHARDED_JSON-default rule (Story #1488), specific to `--clear`.

`_resolve_new_collection_layout` (cli.py) is the single seam every semantic
`cidx index` call site (foreground and daemon-mode) routes through to decide
`use_chunks_db_for_new_collections`. This test proves its new `clear`-aware
default:

- No explicit `--new-collection-layout` flag (`choice=None`) AND `clear=True`
  -> `True` (CHUNKS_DB), instead of `None` (which previously fell through to
  the ambient CLI/daemon default, SHARDED_JSON).
- No flag AND `clear=False` -> `None`, UNCHANGED (a plain incremental
  `cidx index` must not be affected by this fix at all).
- An EXPLICIT choice (either value) always wins over the clear-implied
  default -- an operator who explicitly asks for legacy `sharded_json`
  together with `--clear` must still get it, matching the maintainer's own
  reference to `test_cli_clear_recovers_damaged_layout_1979.py`'s existing
  explicit-flag mechanism, which this fix reuses rather than replaces.
"""

import pytest

from code_indexer.cli import _resolve_new_collection_layout


class TestResolveNewCollectionLayoutClearDefault:
    @pytest.mark.parametrize(
        "choice, clear, expected",
        [
            # Regression: non-clear behavior is byte-identical to before.
            (None, False, None),
            ("chunks_db", False, True),
            ("sharded_json", False, False),
            # NEW: clear=True with no explicit flag defaults to CHUNKS_DB.
            (None, True, True),
            # Explicit choice always wins, even under clear=True.
            ("chunks_db", True, True),
            ("sharded_json", True, False),
        ],
    )
    def test_maps_choice_and_clear_to_optional_bool(
        self, choice, clear, expected
    ) -> None:
        assert _resolve_new_collection_layout(choice, clear=clear) is expected

    def test_positional_call_without_clear_kwarg_still_works(self) -> None:
        """Regression: every pre-existing call site (and
        test_cli_new_collection_layout_1488.py's own coverage) calls this
        helper with just `choice` -- the new `clear` keyword-only parameter
        must default to False so untouched call sites are unaffected."""
        assert _resolve_new_collection_layout(None) is None
        assert _resolve_new_collection_layout("chunks_db") is True
        assert _resolve_new_collection_layout("sharded_json") is False
