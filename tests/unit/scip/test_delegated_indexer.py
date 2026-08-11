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
    # Unit tests must not wait on the real FlexVol index-visibility settle budget
    # (_index_written): on the exit_code==0 miss path it would poll the full
    # _INDEX_SETTLE_SECONDS. A cache-bust test that needs a specific budget overrides
    # these; everyone else stays fast.
    indexer._index_settle_seconds = 0.05
    indexer._index_poll_seconds = 0.01
    return indexer


def _rpc_result(**envelope):
    return {"jsonrpc": "2.0", "id": 1, "result": envelope}


def _post_writing_index(output_dir, payload, *, status_code=200, data=b"\x00\x01"):
    """A fake ``requests.post`` that writes a FRESH ``index.scip`` (as the sandbox
    would this run) then returns the response.

    EVO-65021: the indexer deletes any stale ``index.scip`` before dispatch, so
    success is judged only on a file written THIS call. Success tests must have
    the POST produce the file, not pre-create it before the call.
    """
    resp = _fake_response(status_code=status_code, payload=payload)

    def _post(*_a, **_k):
        (output_dir / "index.scip").write_bytes(data)
        return resp

    return _post


class TestDelegatedIndexerSuccess:
    def test_root_project_success(self, tmp_path):
        """exit_code 0 + index.scip present at output_dir => SUCCESS."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)

        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir,
                _rpc_result(exit_code=0, output="indexing done", duration_ms=12345),
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
        project_dir = tmp_path / "services" / "foo"
        project_dir.mkdir(parents=True)

        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir,
                _rpc_result(exit_code=0, output="", duration_ms=1000),
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

        envelope = {"exit_code": 0, "output": "ok", "duration_ms": 500}
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "result": {"content": [{"type": "text", "text": json.dumps(envelope)}]},
        }
        fake = _fake_requests(post_side_effect=_post_writing_index(output_dir, payload))
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.SUCCESS


class TestDelegatedIndexerGoldenAlias:
    """EVO-65365 Option C: address the build by golden_alias, not workspace_id."""

    def test_golden_alias_builds_arguments_without_workspace_id(self, tmp_path):
        """golden_alias => arguments carry golden_alias and NO workspace_id."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)

        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir,
                _rpc_result(exit_code=0, output="indexing done", duration_ms=999),
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            golden_alias="svc-foo",
            language="typescript",
        )

        assert result.status == IndexerStatus.SUCCESS
        assert result.output_file == output_dir / "index.scip"

        args = fake.post.call_args.kwargs["json"]["params"]["arguments"]
        # The bare alias is sent verbatim (cidx never builds a path).
        assert args == {
            "golden_alias": "svc-foo",
            "project_path": ".",
            "build_system": "npm",
            "language": "typescript",
            "output_dir": ".code-indexer/scip",
        }
        assert "workspace_id" not in args


class TestDelegatedIndexerAllowDegenerate:
    """EVO-65365: golden repos opt into the sandbox's locals-only fallback."""

    def test_allow_degenerate_true_adds_argument(self, tmp_path):
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir, _rpc_result(exit_code=0, output="ok", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            golden_alias="svc-foo",
            language="typescript",
            allow_degenerate=True,
        )
        args = fake.post.call_args.kwargs["json"]["params"]["arguments"]
        assert args["allow_degenerate"] is True

    def test_allow_degenerate_false_omits_argument(self, tmp_path):
        """Default (workspace_id path) → no allow_degenerate key in the request."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir, _rpc_result(exit_code=0, output="ok", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            workspace_id="W-1",
            language="typescript",
        )
        args = fake.post.call_args.kwargs["json"]["params"]["arguments"]
        assert "allow_degenerate" not in args

    def test_degenerate_response_flag_is_surfaced_on_result(self, tmp_path):
        """A ``degenerate: true`` envelope → IndexerResult.degenerate is True."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir,
                _rpc_result(exit_code=0, output="ok", duration_ms=1, degenerate=True),
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            golden_alias="svc-foo",
            language="typescript",
            allow_degenerate=True,
        )
        assert result.status == IndexerStatus.SUCCESS
        assert result.degenerate is True

    def test_no_degenerate_flag_defaults_false(self, tmp_path):
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir, _rpc_result(exit_code=0, output="ok", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.SUCCESS
        assert result.degenerate is False


class TestDelegatedIndexerGoldenRepoContract:
    """EVO-65365: the exact golden-repo request cidx sends to sandbox_scip."""

    def test_golden_repo_request_full_contract(self, tmp_path):
        """The exact arguments dict cidx sends for a golden repo (EVO-65365)."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        fake = _fake_requests(
            post_side_effect=_post_writing_index(
                output_dir, _rpc_result(exit_code=0, output="ok", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            golden_alias="svc-foo",
            language="typescript",
            allow_degenerate=True,
        )
        args = fake.post.call_args.kwargs["json"]["params"]["arguments"]
        assert args == {
            "golden_alias": "svc-foo",
            "project_path": ".",
            "build_system": "npm",
            "language": "typescript",
            "output_dir": ".code-indexer/scip",
            "allow_degenerate": True,
        }


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

    def test_stale_index_from_prior_run_is_not_this_run_success(self, tmp_path):
        """EVO-65021: a leftover index.scip must NOT make a no-op build SUCCESS.

        A prior run left index.scip on the reused bind-mounted output dir. This
        run's sandbox exits 0 but writes NOTHING (a no-op / short-circuit). The
        stale file is deleted before dispatch, so success cannot be judged
        against it → FAILED, and the 'no index.scip' guard becomes reachable.
        """
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        # Stale artifact from a previous successful build on the reused path.
        (output_dir / "index.scip").write_bytes(b"\x00stale")

        # Sandbox claims success but does NOT (re)write index.scip this run.
        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(exit_code=0, output="no-op", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )

        assert result.status == IndexerStatus.FAILED
        assert result.output_file is None
        assert "no index.scip" in result.stderr
        # The stale file was removed before dispatch (not resurrected).
        assert not (output_dir / "index.scip").exists()

    def test_stale_index_unremovable_fails_loud(self, tmp_path, monkeypatch):
        """If a stale index.scip cannot be removed, fail loud (never dispatch)."""
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        (output_dir / "index.scip").write_bytes(b"\x00stale")

        fake = _fake_requests(
            post_return=_fake_response(
                payload=_rpc_result(exit_code=0, output="", duration_ms=1)
            )
        )
        indexer = _make_indexer(tmp_path, fake)

        import pathlib

        real_unlink = pathlib.Path.unlink

        def _boom(self, *a, **k):
            if self.name == "index.scip":
                raise OSError("read-only filesystem")
            return real_unlink(self, *a, **k)

        monkeypatch.setattr(pathlib.Path, "unlink", _boom)

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert "could not remove stale index.scip" in result.stderr
        fake.post.assert_not_called()

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

    def test_missing_both_targets_is_failure_and_no_call(self, tmp_path):
        """Neither workspace_id nor golden_alias => XOR violation, no dispatch."""
        fake = _fake_requests(
            post_return=_fake_response(payload=_rpc_result(exit_code=0))
        )
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            workspace_id=None,
            golden_alias=None,
            language="typescript",
        )
        assert result.status == IndexerStatus.FAILED
        assert "EXACTLY ONE" in result.stderr
        assert "workspace_id" in result.stderr
        assert "golden_alias" in result.stderr
        fake.post.assert_not_called()

    def test_both_targets_is_failure_and_no_call(self, tmp_path):
        """Both workspace_id AND golden_alias => XOR violation, no dispatch."""
        fake = _fake_requests(
            post_return=_fake_response(payload=_rpc_result(exit_code=0))
        )
        indexer = _make_indexer(tmp_path, fake)
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            workspace_id="W",
            golden_alias="svc-foo",
            language="typescript",
        )
        assert result.status == IndexerStatus.FAILED
        assert "EXACTLY ONE" in result.stderr
        fake.post.assert_not_called()

    def test_no_endpoint_failure_names_scip_sandbox_endpoint(self, tmp_path):
        """EVO-65365: the endpoint resolves from scip_sandbox (fallback
        workspace_mcp), so the no-endpoint fail-soft message must reference
        ``scip_sandbox`` — pointing an operator only at ``workspace_mcp.endpoint``
        misdirects them to the correct-for-ITS-backend clone-delegation key."""
        fake = _fake_requests(
            post_return=_fake_response(payload=_rpc_result(exit_code=0))
        )
        indexer = DelegatedIndexer(
            config=SimpleNamespace(endpoint="", api_key=""), repo_root=tmp_path
        )
        indexer._requests = lambda: fake  # type: ignore[method-assign]
        output_dir = tmp_path / ".code-indexer" / "scip"

        result = indexer.generate(
            tmp_path, output_dir, "npm", workspace_id="W", language="typescript"
        )
        assert result.status == IndexerStatus.FAILED
        assert "scip_sandbox" in result.stderr
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
        # This test only asserts the POST URL; the post_return does not rewrite the
        # index, so avoid waiting the real FlexVol settle budget on the miss path.
        indexer._index_settle_seconds = 0.05
        indexer._index_poll_seconds = 0.01

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


class TestDelegatedIndexerFlexVolIndexVisibility:
    """EVO-65021: a valid ``index.scip`` written by the sandbox on the shared FlexVol
    can be briefly hidden from cidx by a stale NFS negative-dentry cache (cidx unlinked
    the stale file through its own client before dispatch). ``_index_written`` must
    invalidate that cache (READDIR) so the fresh index is seen, instead of dropping a
    perfectly good index as "no index.scip was written"."""

    def test_stale_negative_dentry_cache_recovered_by_readdir(
        self, tmp_path, monkeypatch
    ):
        import os as _os
        from pathlib import Path as _P

        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)
        scip_file = output_dir / "index.scip"

        payload = _rpc_result(exit_code=0, output="ok", duration_ms=1)
        # The sandbox writes a REAL index.scip on the shared FlexVol this call.
        fake = _fake_requests(post_side_effect=_post_writing_index(output_dir, payload))
        indexer = _make_indexer(tmp_path, fake)

        # Simulate the stale NFS negative-dentry cache: ``exists()`` lies (False) for the
        # freshly-written index until a READDIR (``os.listdir``) of the parent refreshes
        # the client's cached listing.
        refreshed = {"v": False}
        real_listdir = _os.listdir

        def fake_listdir(p):
            if _P(p) == output_dir:
                refreshed["v"] = True
            return real_listdir(p)

        real_exists = _P.exists

        def fake_exists(self):
            if self == scip_file and not refreshed["v"]:
                return False
            return real_exists(self)

        monkeypatch.setattr(_os, "listdir", fake_listdir)
        monkeypatch.setattr(_P, "exists", fake_exists)

        result = indexer.generate(
            tmp_path,
            output_dir,
            "poetry",
            golden_alias="ls-token-validator-python",
            language="python",
            allow_degenerate=True,
        )

        assert result.status is IndexerStatus.SUCCESS
        assert result.output_file == scip_file
        # The readdir cache-bust was actually exercised (the file was cache-hidden).
        assert refreshed["v"] is True

    def test_missing_index_still_fails_within_bounded_budget(self, tmp_path):
        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)

        payload = _rpc_result(
            exit_code=0, output="ran but wrote nothing", duration_ms=1
        )
        # POST does NOT write index.scip — a genuine no-index build.
        fake = _fake_requests(post_return=_fake_response(payload=payload))
        indexer = _make_indexer(tmp_path, fake)
        # Keep the settle budget tiny so the test cannot hang.
        indexer._index_settle_seconds = 0.05
        indexer._index_poll_seconds = 0.01

        result = indexer.generate(
            tmp_path,
            output_dir,
            "poetry",
            golden_alias="x",
            language="python",
            allow_degenerate=True,
        )

        assert result.status is IndexerStatus.FAILED
        assert "no index.scip was written" in result.stderr

    def test_visible_index_returns_without_readdir(self, tmp_path, monkeypatch):
        import os as _os

        output_dir = tmp_path / ".code-indexer" / "scip"
        output_dir.mkdir(parents=True)

        payload = _rpc_result(exit_code=0, output="ok", duration_ms=1)
        fake = _fake_requests(post_side_effect=_post_writing_index(output_dir, payload))
        indexer = _make_indexer(tmp_path, fake)

        # Fast path: a normally-visible index must NOT touch the readdir cache-bust.
        called = {"listdir": False}
        real_listdir = _os.listdir

        def fake_listdir(p):
            called["listdir"] = True
            return real_listdir(p)

        monkeypatch.setattr(_os, "listdir", fake_listdir)

        result = indexer.generate(
            tmp_path,
            output_dir,
            "npm",
            workspace_id="W",
            language="typescript",
        )

        assert result.status is IndexerStatus.SUCCESS
        assert called["listdir"] is False
