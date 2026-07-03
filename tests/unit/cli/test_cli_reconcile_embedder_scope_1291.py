"""Story #1291 AC10: --reconcile-embedder CLI flag scopes reconcile to
specific temporal embedder(s).

Follows the source-text regression testing convention established in
test_cli_temporal_embedder_loop_1290.py for this exact area of cli.py (a
full CliRunner invocation of `cidx index --index-commits --reconcile` would
require heavy git/config/vector-store mocking for a purely mechanical
wiring check).
"""

from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[3]
_CLI_PATH = _REPO_ROOT / "src" / "code_indexer" / "cli.py"


def _read_cli_source() -> str:
    return _CLI_PATH.read_text()


def test_reconcile_embedder_option_declared():
    """The --reconcile-embedder click option must exist on the index command."""
    source = _read_cli_source()
    assert '"--reconcile-embedder"' in source
    assert '"reconcile_embedder"' in source


def test_reconcile_embedder_param_in_index_signature():
    """index() must accept reconcile_embedder as a parameter."""
    source = _read_cli_source()
    def_pos = source.find("def index(\n    ctx,")
    assert def_pos != -1, "index() function definition not found"
    # Signature window: from def index( through the closing paren area.
    window = source[def_pos : def_pos + 600]
    assert "reconcile_embedder: tuple" in window


def test_reconcile_embedder_threaded_into_primary_index_commits_call():
    """The PRIMARY (first) index_commits() call must pass embedder_scope
    derived from reconcile_embedder -- not just the loop-extra call."""
    source = _read_cli_source()
    primary_call_marker = "indexing_result = temporal_indexer.index_commits("
    call_pos = source.find(primary_call_marker)
    assert call_pos != -1, "Primary index_commits() call not found"

    window = source[call_pos : call_pos + 500]
    assert "embedder_scope=list(reconcile_embedder)" in window, (
        f"embedder_scope not threaded into the primary index_commits() call. "
        f"Window inspected: {window!r}"
    )
