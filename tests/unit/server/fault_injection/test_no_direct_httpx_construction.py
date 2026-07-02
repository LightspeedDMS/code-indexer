"""
Story #746 Scenario 18 — factory-enforced outbound HTTP client construction
in external PROVIDER hot paths.

All outbound HTTP clients in the provider hot paths MUST be constructed
through HttpClientFactory (async) or via a SyncClientFactory-compatible
factory (sync) so that FaultInjectingTransport / FaultInjectingSyncTransport
can be transparently installed.

Enforcement contract:

  Async provider modules (server layer):
    - Zero direct httpx.AsyncClient(...) constructions anywhere in the module.
    - HttpClientFactory.create_client() must be the only construction point.

  Sync provider modules (server and CLI layers):
    - self._http_client_factory.create_sync_client() MUST be called at least
      once in the module.
    - Zero direct httpx.Client(...) constructions are allowed in any of the
      5 provider modules.  Factories are always non-None (NullFaultFactory is
      used when fault injection is not needed), so no else-branch fallbacks
      remain.

  HttpClientFactory module itself:
    - create_client() must be defined as a method of HttpClientFactory and
      must contain at least one httpx.AsyncClient(...) construction.
    - create_sync_client() must be defined as a method of HttpClientFactory
      and must contain at least one httpx.Client(...) construction.

Admin-side HTTP integrations are explicitly OUT OF SCOPE.

Out-of-scope admin-side files:
  - auth/oidc/oidc_provider.py        (OIDC authentication)
  - clients/forge_client.py           (Forge CI integration)
  - clients/github_actions_client.py  (GitHub Actions integration)
  - clients/gitlab_ci_client.py       (GitLab CI integration)
  - services/diagnostics_service.py   (internal diagnostics)
  - clients/claude_server_client.py   (Claude server integration)
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import List, Set

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_REPO_ROOT = Path(__file__).parent.parent.parent.parent.parent
_SERVER_ROOT = _REPO_ROOT / "src" / "code_indexer" / "server"
_SERVICES_ROOT = _REPO_ROOT / "src" / "code_indexer" / "services"

SERVER_ASYNC_PROVIDER_MODULES: List[str] = [
    "services/api_key_management.py",
]

SERVER_SYNC_PROVIDER_MODULES: List[str] = [
    "clients/reranker_clients.py",
]

CLI_SYNC_PROVIDER_MODULES: List[str] = [
    "voyage_ai.py",
    "cohere_embedding.py",
]


# ---------------------------------------------------------------------------
# AST helpers
# ---------------------------------------------------------------------------


def _parse(file_path: Path) -> ast.Module:
    """Parse a Python source file.  Raises AssertionError on SyntaxError."""
    source = file_path.read_text(encoding="utf-8")
    try:
        return ast.parse(source, filename=str(file_path))
    except SyntaxError as exc:
        raise AssertionError(
            f"AST parse failed for {file_path}: {exc}\nFix the syntax error first."
        ) from exc


def _find_httpx_constructions_module_wide(
    tree: ast.Module, attr_name: str
) -> List[int]:
    """Return line numbers of httpx.<attr_name>(...) calls anywhere in *tree*."""
    hits: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if (
            isinstance(func, ast.Attribute)
            and func.attr == attr_name
            and isinstance(func.value, ast.Name)
            and func.value.id == "httpx"
        ):
            hits.append(node.lineno)
    return hits


def _find_httpx_constructions_in_class_method(
    tree: ast.Module, class_name: str, method_name: str, attr_name: str
) -> List[int]:
    """
    Return line numbers of httpx.<attr_name>(...) calls inside
    <class_name>.<method_name> only.

    Scoped to the specific class body and specific method body so that
    constructions in other classes or helper functions do not produce
    false positives.

    Returns an empty list when the class or method does not exist.
    """
    for class_node in ast.walk(tree):
        if not isinstance(class_node, ast.ClassDef) or class_node.name != class_name:
            continue
        for item in class_node.body:
            if not isinstance(item, ast.FunctionDef) or item.name != method_name:
                continue
            # Found the target method — scan its body only
            hits: List[int] = []
            for node in ast.walk(item):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                if (
                    isinstance(func, ast.Attribute)
                    and func.attr == attr_name
                    and isinstance(func.value, ast.Name)
                    and func.value.id == "httpx"
                ):
                    hits.append(node.lineno)
            return hits
    return []


def _get_class_method_names(tree: ast.Module, class_name: str) -> Set[str]:
    """Return method names defined directly on <class_name>, or empty set."""
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return {
                item.name
                for item in node.body
                if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef))
            }
    return set()


def _find_factory_calls_on_self_attribute(tree: ast.Module) -> List[int]:
    """
    Return line numbers of self._http_client_factory.create_sync_client(...)
    calls anywhere in *tree*.

    Only matches the exact receiver pattern ``self._http_client_factory`` so
    that unrelated method calls with the same name produce no false positives.
    """
    hits: List[int] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if not isinstance(func, ast.Attribute) or func.attr != "create_sync_client":
            continue
        receiver = func.value
        if (
            isinstance(receiver, ast.Attribute)
            and receiver.attr == "_http_client_factory"
            and isinstance(receiver.value, ast.Name)
            and receiver.value.id == "self"
        ):
            hits.append(node.lineno)
    return hits


# ---------------------------------------------------------------------------
# Shared assertion helpers (eliminate duplication across server / CLI variants)
# ---------------------------------------------------------------------------


def _assert_factory_wired(root: Path, relative_path: str) -> None:
    """Assert self._http_client_factory.create_sync_client() is called in module."""
    file_path = root / relative_path
    assert file_path.exists(), (
        f"Provider hot-path module does not exist: {file_path}\n"
        "Update the module list if the file was moved."
    )
    tree = _parse(file_path)
    factory_calls = _find_factory_calls_on_self_attribute(tree)
    assert len(factory_calls) >= 1, (
        f"self._http_client_factory.create_sync_client() not called in "
        f"{relative_path}.\n"
        "Inject a factory and call create_sync_client() so that "
        "FaultInjectingSyncTransport can be installed for resilience testing."
    )


def _assert_zero_direct_client_constructions(root: Path, relative_path: str) -> None:
    """Assert zero direct httpx.Client() constructions in module.

    Story #746 CRITICAL fix: factories are always non-None (NullFaultFactory
    when fault injection is not needed), so no else-branch fallbacks remain.
    """
    file_path = root / relative_path
    assert file_path.exists(), (
        f"Provider hot-path module does not exist: {file_path}\n"
        "Update the module list if the file was moved."
    )
    tree = _parse(file_path)
    direct_calls = _find_httpx_constructions_module_wide(tree, "Client")
    assert direct_calls == [], (
        f"In {relative_path}: {len(direct_calls)} direct httpx.Client(...) "
        "constructions found — expected zero.\n"
        "All sync client construction must go through the factory "
        "(self._http_client_factory.create_sync_client()).\n"
        "Use NullFaultFactory() when fault injection is not needed.\n"
        f"Direct construction lines: {direct_calls}"
    )


# ---------------------------------------------------------------------------
# Tests — async provider hot paths (server layer)
# ---------------------------------------------------------------------------


class TestNoDirectAsyncClientInServerProviderHotPaths:
    """
    Scenario 18 (async): Zero direct httpx.AsyncClient constructions allowed
    in server-layer external provider hot-path modules.
    """

    @pytest.mark.parametrize("relative_path", SERVER_ASYNC_PROVIDER_MODULES)
    def test_server_provider_hot_path_has_no_direct_async_client(
        self, relative_path: str
    ) -> None:
        """Zero direct httpx.AsyncClient(...) constructions in module."""
        file_path = _SERVER_ROOT / relative_path
        assert file_path.exists(), (
            f"Provider hot-path module does not exist: {file_path}\n"
            "Update SERVER_ASYNC_PROVIDER_MODULES if the file was moved."
        )
        tree = _parse(file_path)
        violations = _find_httpx_constructions_module_wide(tree, "AsyncClient")
        assert violations == [], (
            f"Direct httpx.AsyncClient(...) found in {relative_path} "
            f"at lines {violations}.\n"
            "Use HttpClientFactory.create_client() instead."
        )


# ---------------------------------------------------------------------------
# Tests — sync provider hot paths (server layer)
# ---------------------------------------------------------------------------


class TestSyncFactoryWiringInServerProviderHotPaths:
    """
    Scenario 18 (sync, server layer): factory must be called; direct
    construction count must not exceed factory call count.
    """

    @pytest.mark.parametrize("relative_path", SERVER_SYNC_PROVIDER_MODULES)
    def test_server_provider_calls_create_sync_client(self, relative_path: str) -> None:
        """self._http_client_factory.create_sync_client() must be called."""
        _assert_factory_wired(_SERVER_ROOT, relative_path)

    @pytest.mark.parametrize("relative_path", SERVER_SYNC_PROVIDER_MODULES)
    def test_server_provider_zero_direct_client_constructions(
        self, relative_path: str
    ) -> None:
        """Zero direct httpx.Client constructions allowed — factory always used."""
        _assert_zero_direct_client_constructions(_SERVER_ROOT, relative_path)


# ---------------------------------------------------------------------------
# Tests — sync provider hot paths (CLI services layer)
# ---------------------------------------------------------------------------


class TestSyncFactoryWiringInCliProviderHotPaths:
    """
    Scenario 18 (sync, CLI layer): factory must be called; direct
    construction count must not exceed factory call count.
    """

    @pytest.mark.parametrize("relative_path", CLI_SYNC_PROVIDER_MODULES)
    def test_cli_provider_calls_create_sync_client(self, relative_path: str) -> None:
        """self._http_client_factory.create_sync_client() must be called."""
        _assert_factory_wired(_SERVICES_ROOT, relative_path)

    @pytest.mark.parametrize("relative_path", CLI_SYNC_PROVIDER_MODULES)
    def test_cli_provider_zero_direct_client_constructions(
        self, relative_path: str
    ) -> None:
        """Zero direct httpx.Client constructions allowed — factory always used."""
        _assert_zero_direct_client_constructions(_SERVICES_ROOT, relative_path)


# ---------------------------------------------------------------------------
# Sanity checks — HttpClientFactory class retains both methods and constructions
# ---------------------------------------------------------------------------


class TestFactoryClassRetainsConstructionAndMethods:
    """
    HttpClientFactory must define create_client() and create_sync_client() as
    class methods, and those methods must construct the respective httpx client
    types directly.  All checks are scoped to the HttpClientFactory class body
    and specific method bodies.
    """

    def _factory_tree(self) -> ast.Module:
        p = _SERVER_ROOT / "fault_injection" / "http_client_factory.py"
        assert p.exists(), f"HttpClientFactory module not found at {p}"
        return _parse(p)

    def test_factory_class_defines_create_client(self) -> None:
        """HttpClientFactory class must define create_client()."""
        tree = self._factory_tree()
        methods = _get_class_method_names(tree, "HttpClientFactory")
        assert "create_client" in methods, (
            "HttpClientFactory does not define create_client(). "
            "This method is the single permitted async construction point."
        )

    def test_factory_class_defines_create_sync_client(self) -> None:
        """HttpClientFactory class must define create_sync_client()."""
        tree = self._factory_tree()
        methods = _get_class_method_names(tree, "HttpClientFactory")
        assert "create_sync_client" in methods, (
            "HttpClientFactory does not define create_sync_client(). "
            "This method is required for sync provider hot paths (Story #746 C1)."
        )

    def test_create_client_constructs_async_client(self) -> None:
        """
        HttpClientFactory.create_client() must contain at least one direct
        httpx.AsyncClient(...) construction inside the method body.
        """
        tree = self._factory_tree()
        hits = _find_httpx_constructions_in_class_method(
            tree, "HttpClientFactory", "create_client", "AsyncClient"
        )
        assert len(hits) >= 1, (
            "HttpClientFactory.create_client() must construct httpx.AsyncClient "
            "directly inside the method — not found."
        )

    def test_create_sync_client_constructs_sync_client(self) -> None:
        """
        HttpClientFactory.create_sync_client() must contain at least one direct
        httpx.Client(...) construction inside the method body.
        """
        tree = self._factory_tree()
        hits = _find_httpx_constructions_in_class_method(
            tree, "HttpClientFactory", "create_sync_client", "Client"
        )
        assert len(hits) >= 1, (
            "HttpClientFactory.create_sync_client() must construct httpx.Client "
            "directly inside the method — not found."
        )
