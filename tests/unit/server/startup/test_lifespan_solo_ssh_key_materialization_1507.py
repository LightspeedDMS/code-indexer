"""Issue #1507: Solo/SQLite server mode has no SSH key materialization
self-heal at startup.

`SSHKeySyncService` (Bug #428/#581/#1072) already writes DB-registered SSH
keys out to ``~/.ssh/`` and is backend-agnostic (``fernet=None`` means
"write private key bytes as-is", exactly solo mode's storage convention),
but it is constructed ONLY inside `lifespan.py`'s
``storage_mode == "postgres"`` branch. A solo/SQLite server (which is what
production actually runs, per this project's CLAUDE.md) had no equivalent
step, so a key correctly registered in the SQLite ``ssh_keys`` table could
still be missing from disk (fresh host, wiped ``~/.ssh``, DB relocated to a
new node) with no way to self-heal at startup.

These tests prove the gap -- and later the fix -- against a REAL SQLite
`SSHKeysSqliteBackend` and a REAL temp-filesystem ssh_dir, no mocked I/O,
mirroring the existing `test_ssh_key_sync_service.py` methodology. The
source-level guard uses real `ast` parsing with structural node inspection
(never regex/substring/unparse-text matching, which is fragile to quoting
style) so it cannot be satisfied by a call sitting in the wrong branch or
by an unrelated bare try/except.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Dict, Optional

from code_indexer.server.storage.sqlite_backends import SSHKeysSqliteBackend

_REPO_ROOT = Path(__file__).resolve().parents[4]
_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)

_TARGET_FUNC = "_materialize_solo_ssh_keys"


class _FakeBackendRegistry:
    """Minimal stand-in exposing only the `.ssh_keys` attribute the helper reads."""

    def __init__(self, ssh_keys_backend: SSHKeysSqliteBackend) -> None:
        self.ssh_keys = ssh_keys_backend


def _register_key_without_materializing(
    backend: SSHKeysSqliteBackend, ssh_dir: Path, name: str
) -> None:
    """Simulate a key that is correctly registered in the DB but whose file
    was never written to disk -- the exact production discovery scenario
    (issue #1507's evidence section)."""
    backend.create_key(
        name=name,
        fingerprint="SHA256:fake_fingerprint",
        key_type="ed25519",
        private_path=str(ssh_dir / name),
        public_path=str(ssh_dir / f"{name}.pub"),
        public_key="ssh-ed25519 AAAAC3 fake-comment",
        email=None,
        description="GitLab deploy key",
        is_imported=False,
        private_key=(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n"
            "-----END OPENSSH PRIVATE KEY-----\n"
        ),
    )


class TestSoloSshKeyMaterializationGap1507:
    """Real-backend, real-filesystem proof of the solo-mode materialization gap."""

    def test_helper_materializes_missing_key_from_real_sqlite_backend(
        self, tmp_path: Path
    ) -> None:
        """A key registered in the real SQLite backend but absent from disk
        must be written out to the real ssh_dir once materialization runs."""
        from code_indexer.server.startup.lifespan import _materialize_solo_ssh_keys
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "cidx_server.db"
        ssh_dir = tmp_path / "ssh"

        # Real schema bootstrap: SSHKeysSqliteBackend alone does not create
        # the `ssh_keys` table -- production always goes through
        # DatabaseSchema.initialize_database() first (see server_dir/data
        # bootstrap in service_init.py), so tests must mirror that.
        DatabaseSchema(str(db_path)).initialize_database()

        backend = SSHKeysSqliteBackend(str(db_path))
        try:
            _register_key_without_materializing(backend, ssh_dir, "deploy_key")

            # Precondition: the key genuinely does not exist on disk yet --
            # proves this test exercises the self-heal gap, not a no-op.
            assert not (ssh_dir / "deploy_key").exists()
            assert not (ssh_dir / "deploy_key.pub").exists()

            fake_registry = _FakeBackendRegistry(backend)

            result = _materialize_solo_ssh_keys(fake_registry, ssh_dir=str(ssh_dir))

            assert result is not None
            assert result["written"] == ["deploy_key"]
            assert result["errors"] == []
            assert (ssh_dir / "deploy_key").exists()
            assert (ssh_dir / "deploy_key").read_text() == (
                "-----BEGIN OPENSSH PRIVATE KEY-----\nFAKE\n"
                "-----END OPENSSH PRIVATE KEY-----\n"
            )
            assert (ssh_dir / "deploy_key.pub").exists()
            assert (
                ssh_dir / "deploy_key.pub"
            ).read_text() == "ssh-ed25519 AAAAC3 fake-comment"
        finally:
            backend.close()

    def test_returns_none_when_backend_registry_has_no_ssh_keys_backend(
        self,
    ) -> None:
        """Defensive fail-soft: an unexpected backend_registry shape (missing
        the `ssh_keys` attribute entirely) must not raise -- matches this
        module's fail-soft startup-wiring convention."""
        from code_indexer.server.startup.lifespan import _materialize_solo_ssh_keys

        class _EmptyRegistry:
            pass

        assert _materialize_solo_ssh_keys(_EmptyRegistry()) is None


# ---------------------------------------------------------------------------
# AST-based source guard: proves the helper is genuinely wired into the
# solo/SQLite startup path (not merely defined), and that the call sits
# inside a real try/except (fail-soft), and that it is NOT nested inside
# the pre-existing `storage_mode == "postgres"` cluster branch.
# ---------------------------------------------------------------------------


def _parse_lifespan() -> ast.Module:
    return ast.parse(_LIFESPAN_PATH.read_text(), filename=str(_LIFESPAN_PATH))


def _build_parent_map(tree: ast.AST) -> Dict[ast.AST, ast.AST]:
    parents: Dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parents[child] = node
    return parents


def _find_target_call(tree: ast.AST) -> Optional[ast.Call]:
    """Find the real call-site Call node for `_materialize_solo_ssh_keys`,
    explicitly excluding the FunctionDef itself (which is not a Call)."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == _TARGET_FUNC
        ):
            return node
    return None


def _ancestors(node: ast.AST, parents: Dict[ast.AST, ast.AST]):
    current = node
    while current in parents:
        current = parents[current]
        yield current


def _contains_storage_mode_eq_postgres(expr: ast.expr) -> bool:
    """Structurally detect `storage_mode == "postgres"` (in either operand
    order) inside an expression -- via real AST node types/values, never by
    matching `ast.unparse()` text (which is fragile to quote-style
    rendering, e.g. 'postgres' vs "postgres")."""
    for node in ast.walk(expr):
        if not isinstance(node, ast.Compare):
            continue
        if not any(isinstance(op, ast.Eq) for op in node.ops):
            continue
        operands = [node.left] + list(node.comparators)
        names = {n.id for n in operands if isinstance(n, ast.Name)}
        constants = {n.value for n in operands if isinstance(n, ast.Constant)}
        if "storage_mode" in names and "postgres" in constants:
            return True
    return False


class TestLifespanDefinesAndCallsHelper1507:
    """AST-based guard: the helper must exist AND actually be invoked
    (Messi Rule #12, anti-orphan-code) -- a helper that exists but is never
    called reproduces the exact bug class this issue reports."""

    def test_lifespan_defines_the_helper(self) -> None:
        tree = _parse_lifespan()
        defined = any(
            isinstance(node, ast.FunctionDef) and node.name == _TARGET_FUNC
            for node in ast.walk(tree)
        )
        assert defined, f"lifespan.py must define {_TARGET_FUNC}(...) (Issue #1507)."

    def test_lifespan_calls_the_helper(self) -> None:
        tree = _parse_lifespan()
        call = _find_target_call(tree)
        assert call is not None, (
            f"lifespan.py must call {_TARGET_FUNC}(...) from a real call "
            "site (not just define it) -- Issue #1507."
        )


class TestLifespanCallSiteIsSafe1507:
    """AST-based guard: the call site must be fail-soft and must genuinely
    live in the solo/SQLite path, not the pre-existing cluster branch."""

    def test_call_sits_inside_a_real_try_except(self) -> None:
        """The call must never be allowed to block/crash startup -- it must
        be a direct/indirect statement inside a `Try` node that has at
        least one `except` handler."""
        tree = _parse_lifespan()
        parents = _build_parent_map(tree)
        call = _find_target_call(tree)
        assert call is not None

        enclosing_try = next(
            (a for a in _ancestors(call, parents) if isinstance(a, ast.Try)),
            None,
        )
        assert enclosing_try is not None, (
            f"{_TARGET_FUNC}(...) call is not inside any try/except block"
        )
        assert enclosing_try.handlers, (
            f"the try block enclosing {_TARGET_FUNC}(...) has no except handler"
        )

    def test_call_is_not_nested_inside_the_postgres_cluster_branch(self) -> None:
        """The call must live in the solo/SQLite path, not be an accidental
        addition inside the pre-existing `storage_mode == "postgres"`
        cluster branch (which already has its own SSHKeySyncService call for
        cluster mode -- this issue is specifically about the OTHER branch)."""
        tree = _parse_lifespan()
        parents = _build_parent_map(tree)
        call = _find_target_call(tree)
        assert call is not None

        for ancestor in _ancestors(call, parents):
            if isinstance(ancestor, ast.If) and _contains_storage_mode_eq_postgres(
                ancestor.test
            ):
                raise AssertionError(
                    f"{_TARGET_FUNC}(...) must not be nested inside the "
                    'existing `storage_mode == "postgres"` cluster branch'
                )
