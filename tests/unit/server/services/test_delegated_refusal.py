"""refuse_when_delegated: a no-op unless the content backend is delegated."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from code_indexer.server.services.delegated_refusal import (
    UNSUPPORTED_SUFFIX,
    DelegatedRepositoryUnsupportedError,
    delegated_refusal,
    refuse_when_delegated,
)
from code_indexer.server.startup import content_backend_wiring


def _park(monkeypatch: pytest.MonkeyPatch, backend: object) -> None:
    monkeypatch.setattr(
        content_backend_wiring, "server_content_backend", lambda: backend
    )


@pytest.mark.parametrize(
    "backend", [None, SimpleNamespace(is_local=True)], ids=["no-server", "local"]
)
def test_local_or_no_server_runs_the_feature(
    monkeypatch: pytest.MonkeyPatch, backend: object
) -> None:
    _park(monkeypatch, backend)
    refuse_when_delegated("git blame", "no blame in git-mcp")
    assert delegated_refusal("git blame", "no blame in git-mcp") is None


def test_delegated_refuses_naming_feature_and_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _park(monkeypatch, SimpleNamespace(is_local=False))
    with pytest.raises(DelegatedRepositoryUnsupportedError) as caught:
        refuse_when_delegated("git blame", "no blame in git-mcp")
    assert caught.value.feature == "git blame"
    assert caught.value.reason == "no blame in git-mcp"
    message = str(caught.value)
    assert message.startswith("git blame " + UNSUPPORTED_SUFFIX)
    assert "no blame in git-mcp" in message
    assert delegated_refusal("git blame", "no blame in git-mcp") == message


def test_the_error_is_a_runtime_error() -> None:
    """Callers that already map RuntimeError to a 4xx/5xx keep doing so."""
    assert issubclass(DelegatedRepositoryUnsupportedError, RuntimeError)
