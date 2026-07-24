"""Unit tests for the delegated SCIP indexer (EVO-65021)."""

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from code_indexer.scip.indexers.base import IndexerStatus
from code_indexer.scip.indexers.delegated import DelegatedIndexer


class _FakeRequestException(Exception):
    """Stand-in for ``requests.RequestException``."""


def _fake_response(*, status_code=200, payload=None, text=""):
    resp = MagicMock()
    resp.status_code = status_code
    resp.text = text
    if payload is None:
        resp.json.side_effect = ValueError("no json")
    else:
        resp.json.return_value = payload
    return resp


def _fake_requests(
    *, post_return=None, post_side_effect=None, get_return=None, get_side_effect=None
):
    module = MagicMock()
    module.RequestException = _FakeRequestException
    if post_side_effect is not None:
        module.post.side_effect = post_side_effect
    else:
        module.post.return_value = post_return
    if get_side_effect is not None:
        module.get.side_effect = get_side_effect
    else:
        module.get.return_value = get_return
    return module


def _config():
    return SimpleNamespace(
        endpoint="http://sandbox-mcp.internal/rpc",
        api_key="secret-token",
    )


def _make_indexer(tmp_path, fake):
    indexer = DelegatedIndexer(config=_config(), repo_root=tmp_path)
    # Inject the fake requests module (lazy-imported inside the indexer).
    indexer._requests = lambda: fake  # type: ignore[method-assign]
    return indexer


def _rpc_result(**envelope):
    return {"jsonrpc": "2.0", "id": 1, "result": envelope}


class TestDelegatedIndexerSuccess:
    def test_root_project_success(self, tmp_path):
        """exit_code 0 + index.scip present at output_dir => SUCCESS."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        (output_dir / "index.scip").write_bytes(b"\x00\x01")

        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(
                    exit_code=0, output="indexing done", duration_ms=12345
                )
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            workspace_id="W-123",
            language="typescript",
        )

        assert result.status == IndexerStatus.SUCCESS
        assert result.output_file == output_dir / "index.scip"
        assert result.exit_code == 0
        assert abs(result.duration_seconds - 12.345) < 1e-6
        assert result.stderr == ""

        # Verify the JSON-RPC request shape (contract §2b).
        _, kwargs = fake.post.call_args
        args = kwargs["json"]["params"]["arguments"]
        assert kwargs["json"]["method"] == "tools/call"
        assert kwargs["json"]["params"]["name"] == "sandbox_scip"
        assert args == {
            "workspace_id": "W-123",
            "project_path": ".",
            "build_system": "npm",
            "language": "typescript",
            "output_dir": ".code-indexer/scip",
        }
        assert kwargs["headers"]["Authorization"] == "Bearer secret-token"

    def test_nested_project_relative_paths(self, tmp_path):
        """A nested project yields workspace-relative project_path/output_dir."""
        output_dir = tmp_path / ".code-indexer" / "scip" / "services" / "foo"
        output_dir.mkdir(parents=True)
        (output_dir / "index.scip").write_bytes(b"\x00")
        project_dir = tmp_path / "services" / "foo"
        project_dir.mkdir(parents=True)

        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(exit_code=0, output="", duration_ms=1000)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            project_dir,
            output_dir,
            "npm",
            workspace_id="W",
            language="typescript",
        )

        assert result.status == IndexerStatus.SUCCESS
        args = fake.post.call_args.kwargs["json"]["params"]["arguments"]
        assert args["project_path"] == "services/foo"
        assert args["output_dir"] == ".code-indexer/scip/services/foo"

    def test_success_from_mcp_content_text_envelope(self, tmp_path):
        """SandboxRunResult delivered as a JSON string in result.content[0].text."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        (output_dir / "index.scip").write_bytes(b"\x00")

        envelope = {"exit_code": 0, "output": "ok", "duration_ms": 500}
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(envelope)}]},
        }
        fake = _fake_requests(post_return=_fake_response(payload=payload))
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.SUCCESS


class TestDelegatedIndexerFailure:
    def test_nonzero_exit_is_failure(self, tmp_path):
        """exit_code != 0 => FAILED, no output_file, stderr from errors."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)

        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(
                    exit_code=2,
                    output="boom",
                    duration_ms=800,
                    errors=["build failed", "missing dep"],
                )
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )

        assert result.status == IndexerStatus.FAILED
        assert result.output_file is None
        assert result.exit_code == 2
        assert "build failed" in result.stderr
        assert "missing dep" in result.stderr

    def test_exit_zero_but_missing_file_is_failure(self, tmp_path):
        """exit_code 0 but no index.scip on disk => FAILED (contract §1/§4)."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        # deliberately do NOT create index.scip

        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(exit_code=0, output="claims ok", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert result.output_file is None
        assert "no index.scip" in result.stderr

    def test_transport_error_is_failure_not_raise(self, tmp_path):
        """A transport error fail-softs to FAILED (never raises, no fallback)."""
        fake = _fake_requests(
            post_side_effect=_FakeRequestException("connection refused")
        )
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert result.output_file is None
        assert "connection refused" in result.stderr

    def test_http_error_status_is_failure(self, tmp_path):
        fake = _fake_requests(
            post_return=_fake_response(status_code=500, text="internal error")
        )
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert "HTTP 500" in result.stderr

    def test_jsonrpc_error_payload_is_failure(self, tmp_path):
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "error": {"code": -32000, "message": "no tool"},
        }
        fake = _fake_requests(post_return=_fake_response(payload=payload))
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert "no SandboxRunResult" in result.stderr

    def test_missing_workspace_id_is_failure_and_no_call(self, tmp_path):
        fake = _fake_requests(
            post_return=_fake_response(payload=_rpc_result(exit_code=0))
        )
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id=None, language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert "workspace_id" in result.stderr
        fake.post.assert_not_called()

    def test_missing_language_is_failure(self, tmp_path):
        fake = _fake_requests(
            post_return=_fake_response(payload=_rpc_result(exit_code=0))
        )
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language=None
        )
        assert result.status == IndexerStatus.FAILED
        assert "language" in result.stderr
        fake.post.assert_not_called()


class TestDelegatedIndexerTransport:
    """EVO-65021 seam #2: JSON-RPC route + health route match the sandbox contract."""

    def test_post_targets_v1_sandbox_mcp_front_door(self, tmp_path):
        """endpoint is the sandbox-mcp BASE url; POST appends /v1/sandbox/mcp."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        (output_dir / "index.scip").write_bytes(b"\x00")

        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(exit_code=0, output="", duration_ms=1)
            )
        )
        indexer = DelegatedIndexer(
            config=SimpleNamespace(endpoint="http://sandbox-mcp:8200", api_key="tok"),
            repo_root=tmp_path,
        )
        indexer._requests = lambda: fake  # type: ignore[method-assign]

        indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        # Default JSON-RPC front door per the authoritative sandbox contract.
        assert fake.post.call_args.args[0] == "http://sandbox-mcp:8200/v1/sandbox/mcp"

    def test_health_targets_unversioned_health_route(self, tmp_path):
        fake = _fake_requests(get_return=_fake_response(status_code=200))
        indexer = DelegatedIndexer(
            config=SimpleNamespace(endpoint="http://sandbox-mcp:8200", api_key="tok"),
            repo_root=tmp_path,
        )
        indexer._requests = lambda: fake  # type: ignore[method-assign]
        assert indexer.is_available() is True
        # Health is un-versioned (no /v1 prefix) per the sandbox contract.
        assert fake.get.call_args.args[0] == "http://sandbox-mcp:8200/health"


class TestDelegatedIndexerHealth:
    def test_is_available_true_on_2xx(self, tmp_path):
        fake = _fake_requests(get_return=_fake_response(status_code=200))
        indexer = _make_indexer(tmp_path, fake)
        assert indexer.is_available() is True
        # health path is GET {endpoint}/health
        assert fake.get.call_args.args[0] == "http://sandbox-mcp.internal/rpc/health"

    def test_is_available_false_on_error(self, tmp_path):
        fake = _fake_requests(get_side_effect=_FakeRequestException("down"))
        indexer = _make_indexer(tmp_path, fake)
        assert indexer.is_available() is False

    def test_is_available_false_on_5xx(self, tmp_path):
        fake = _fake_requests(get_return=_fake_response(status_code=503))
        indexer = _make_indexer(tmp_path, fake)
        assert indexer.is_available() is False

    def test_is_available_false_without_endpoint(self, tmp_path):
        indexer = DelegatedIndexer(
            config=SimpleNamespace(endpoint="", api_key=""), repo_root=tmp_path
        )
        assert indexer.is_available() is False

    def test_get_version_is_none(self, tmp_path):
        indexer = DelegatedIndexer(config=_config(), repo_root=tmp_path)
        assert indexer.get_version() is None
