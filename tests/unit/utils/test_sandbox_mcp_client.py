"""SandboxMcpClient: the sandbox-mcp JSON-RPC transport shared by SCIP and X-Ray.

The client was extracted from DelegatedIndexer, so the SCIP tests at the end
pin the WHOLE wire exchange the indexer produced before the extraction -- URL,
JSON body including the id sequence, headers, timeout -- and the exact failure
strings (``HTTP 429`` is what the build queue's backpressure retry matches).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from code_indexer.scip.indexers.base import IndexerStatus
from code_indexer.scip.indexers.delegated import DelegatedIndexer
from code_indexer.utils.sandbox_mcp_client import (
    SandboxMcpClient,
    SandboxMcpError,
    extract_run_result,
    extract_tool_payload,
)


class _FakeRequestException(Exception):
    """Stand-in for ``requests.RequestException``."""


def _response(*, status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


def _requests(*, post=None, post_raises=None, get=None, get_raises=None):
    module = MagicMock()
    module.RequestException = _FakeRequestException
    if post_raises is not None:
        module.post.side_effect = post_raises
    else:
        module.post.return_value = post
    if get_raises is not None:
        module.get.side_effect = get_raises
    else:
        module.get.return_value = get
    return module


def _client(fake, *, endpoint="http://sandbox-mcp:8200/", api_key="tok"):
    return SandboxMcpClient(
        SimpleNamespace(endpoint=endpoint, api_key=api_key),
        timeout_seconds=42,
        requests_provider=lambda: fake,
    )


ENVELOPE = {"exit_code": 0, "output": "ok", "duration_ms": 5}


class TestToolCall:
    def test_posts_one_tools_call_with_bearer_auth(self):
        fake = _requests(post=_response(payload={"result": ENVELOPE}))
        client = _client(fake)

        assert client.call_tool("sandbox_xray", {"mode": "scan"}) == ENVELOPE

        fake.post.assert_called_once_with(
            "http://sandbox-mcp:8200/v1/sandbox/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "sandbox_xray", "arguments": {"mode": "scan"}},
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer tok",
            },
            timeout=42,
        )

    def test_request_ids_increase_per_call(self):
        client = _client(_requests())
        assert client.build_tool_call("t", {})["id"] == 1
        assert client.build_tool_call("t", {})["id"] == 2

    def test_no_api_key_sends_no_authorization_header(self):
        assert _client(_requests(), api_key="").headers() == {
            "Content-Type": "application/json"
        }

    @pytest.mark.parametrize(
        "kwargs, message",
        [
            (
                {"post_raises": _FakeRequestException("refused")},
                "t request to 'http://sandbox-mcp:8200/v1/sandbox/mcp' failed: refused",
            ),
            (
                {"post": _response(status_code=429, text="busy")},
                "t failed (HTTP 429): busy",
            ),
            (
                {"post": _response(status_code=200)},
                "t returned non-JSON response: no json",
            ),
            (
                {"post": _response(payload={"error": {"code": -1}})},
                't response contained no SandboxRunResult envelope: {"error": '
                '{"code": -1}}',
            ),
        ],
    )
    def test_failures_raise_with_the_operator_message(self, kwargs, message):
        with pytest.raises(SandboxMcpError) as excinfo:
            _client(_requests(**kwargs)).call_tool("t", {})
        assert str(excinfo.value) == message

    def test_nonzero_exit_is_returned_not_raised(self):
        envelope = {"exit_code": 3, "output": "boom"}
        fake = _requests(post=_response(payload={"result": envelope}))
        assert _client(fake).call_tool("t", {}) == envelope


class TestEnvelopeShapes:
    def test_inline_result(self):
        assert extract_run_result({"result": ENVELOPE}) == ENVELOPE

    def test_structured_content(self):
        payload = {"result": {"structuredContent": ENVELOPE}}
        assert extract_run_result(payload) == ENVELOPE

    def test_text_content(self):
        payload = {"result": {"content": [{"text": json.dumps(ENVELOPE)}]}}
        assert extract_run_result(payload) == ENVELOPE

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            {"error": {"code": 1}, "result": ENVELOPE},
            {"result": {"content": [{"text": "not json"}]}},
            {"result": {"content": []}},
        ],
    )
    def test_no_envelope(self, payload):
        assert extract_run_result(payload) is None


FETCHED = {"result_id": "r1", "chunk": 0, "final": True, "data": "e30="}


class TestToolPayload:
    """call_tool_payload: tools whose reply is not a run-result envelope
    (sandbox_result_fetch pages a parked result; it has no exit_code)."""

    def test_returns_an_envelope_less_result_with_the_same_wire_shape(self):
        fake = _requests(post=_response(payload={"result": FETCHED}))
        client = _client(fake)

        assert client.call_tool_payload("sandbox_result_fetch", {"chunk": 0}) == FETCHED

        fake.post.assert_called_once_with(
            "http://sandbox-mcp:8200/v1/sandbox/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "sandbox_result_fetch", "arguments": {"chunk": 0}},
            },
            headers={
                "Content-Type": "application/json",
                "Authorization": "Bearer tok",
            },
            timeout=42,
        )

    def test_call_tool_still_refuses_it(self):
        fake = _requests(post=_response(payload={"result": FETCHED}))
        with pytest.raises(SandboxMcpError, match="no SandboxRunResult envelope"):
            _client(fake).call_tool("sandbox_result_fetch", {"chunk": 0})

    @pytest.mark.parametrize(
        "payload",
        [
            {"result": FETCHED},
            {"result": {"structuredContent": FETCHED, "content": []}},
            {"result": {"content": [{"type": "text", "text": json.dumps(FETCHED)}]}},
        ],
    )
    def test_accepted_shapes(self, payload):
        assert extract_tool_payload(payload) == FETCHED

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            [],
            {"error": {"code": -32000, "message": "unknown result_id"}},
            {"result": "text"},
            {"result": {"isError": True, "content": [{"text": "{}"}]}},
            {"result": {"content": []}},
            {"result": {"content": [{"type": "text", "text": "not json"}]}},
            {"result": {"content": [{"type": "text", "text": "[1]"}]}},
        ],
    )
    def test_rejected_shapes(self, payload):
        assert extract_tool_payload(payload) is None

    def test_a_tool_error_raises_with_the_reply(self):
        payload = {"jsonrpc": "2.0", "error": {"message": "unknown result_id"}}
        fake = _requests(post=_response(payload=payload))
        with pytest.raises(SandboxMcpError) as excinfo:
            _client(fake).call_tool_payload("sandbox_result_fetch", {})
        assert "sandbox_result_fetch returned no result object" in str(excinfo.value)
        assert "unknown result_id" in str(excinfo.value)

    @pytest.mark.parametrize(
        "fake, expected",
        [
            (
                _requests(post_raises=_FakeRequestException("refused")),
                "sandbox_result_fetch request to",
            ),
            (
                _requests(post=_response(status_code=503, text="busy")),
                "sandbox_result_fetch failed (HTTP 503): busy",
            ),
            (
                _requests(post=_response(payload=None)),
                "sandbox_result_fetch returned non-JSON response",
            ),
        ],
    )
    def test_transport_failures_raise(self, fake, expected):
        with pytest.raises(SandboxMcpError) as excinfo:
            _client(fake).call_tool_payload("sandbox_result_fetch", {})
        assert expected in str(excinfo.value)


class TestHealth:
    def test_2xx_is_healthy_and_unversioned(self):
        fake = _requests(get=_response(status_code=204))
        assert _client(fake).is_healthy() is True
        assert fake.get.call_args.args[0] == "http://sandbox-mcp:8200/health"

    def test_5xx_and_transport_errors_are_unhealthy(self):
        assert _client(_requests(get=_response(status_code=503))).is_healthy() is False
        fake = _requests(get_raises=_FakeRequestException("down"))
        assert _client(fake).is_healthy() is False

    def test_no_endpoint_never_probes(self):
        fake = _requests()
        assert _client(fake, endpoint="").is_healthy() is False
        fake.get.assert_not_called()


class TestDelegatedIndexerWireShapeUnchanged:
    """The bytes DelegatedIndexer puts on the wire, pinned exactly."""

    def _indexer(self, tmp_path, fake):
        indexer = DelegatedIndexer(
            config=SimpleNamespace(endpoint="http://sandbox-mcp:8200", api_key="k"),
            repo_root=tmp_path,
        )
        indexer._requests = lambda: fake  # type: ignore[method-assign]
        indexer._index_settle_seconds = 0.01
        indexer._index_poll_seconds = 0.005
        return indexer

    def test_two_golden_builds_exact_requests(self, tmp_path):
        out = tmp_path / ".code-indexer" / "scip"
        out.mkdir(parents=True)
        response = _response(payload={"result": ENVELOPE})

        def _post(*_a, **_k):
            (out / "index.scip").write_bytes(b"\x00")
            return response

        fake = _requests()
        fake.post.side_effect = _post
        indexer = self._indexer(tmp_path, fake)

        for allow in (False, True):
            result = indexer.generate(
                tmp_path,
                out,
                "gradle",
                golden_alias="svc",
                language="java",
                allow_degenerate=allow,
            )
            assert result.status == IndexerStatus.SUCCESS

        expected_args = {
            "project_path": ".",
            "build_system": "gradle",
            "language": "java",
            "output_dir": ".code-indexer/scip",
            "golden_alias": "svc",
        }
        calls = [(c.args, c.kwargs) for c in fake.post.call_args_list]
        assert calls == [
            (
                ("http://sandbox-mcp:8200/v1/sandbox/mcp",),
                {
                    "json": {
                        "jsonrpc": "2.0",
                        "id": request_id,
                        "method": "tools/call",
                        "params": {"name": "sandbox_scip", "arguments": args},
                    },
                    "headers": {
                        "Content-Type": "application/json",
                        "Authorization": "Bearer k",
                    },
                    "timeout": 1800,
                },
            )
            for request_id, args in (
                (1, expected_args),
                (2, {**expected_args, "allow_degenerate": True}),
            )
        ]

    def test_http_429_keeps_the_backpressure_token(self, tmp_path):
        fake = _requests(post=_response(status_code=429, text="slow down"))
        result = self._indexer(tmp_path, fake).generate(
            tmp_path,
            tmp_path / "out",
            "npm",
            workspace_id="W",
            language="typescript",
        )
        assert result.status == IndexerStatus.FAILED
        assert result.stderr == "sandbox_scip failed (HTTP 429): slow down"
        assert result.exit_code == -1
