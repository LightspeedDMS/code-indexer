from typing import Any, List

import httpx
import pytest

hub_http = pytest.importorskip("huggingface_hub.utils._http")

_BUILT_UNDER_PATCH: List[Any] = []


def _refuse(request: httpx.Request) -> httpx.Response:
    return httpx.Response(599)


# ^ order this test before test_a_later_test_never_gets_that_session; pytest runs a file top to bottom
def test_a_test_builds_the_hub_session_under_an_httpx_client_patch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_client = httpx.Client
    hub_http.close_session()
    monkeypatch.setattr(
        httpx,
        "Client",
        lambda *a, **k: real_client(*a, transport=httpx.MockTransport(_refuse), **k),
    )
    session = hub_http.get_session()
    _BUILT_UNDER_PATCH.append(session)
    assert session.get("https://example.com/x").status_code == 599


def test_a_later_test_never_gets_that_session() -> None:
    assert _BUILT_UNDER_PATCH, "the first test of this file must run first"
    assert hub_http.get_session() is not _BUILT_UNDER_PATCH[0]
