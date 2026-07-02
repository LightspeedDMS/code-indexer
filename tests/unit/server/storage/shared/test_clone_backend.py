"""
Unit tests for CloneBackend Protocol, LocalCloneBackend, OntapCloneBackend,
and CloneBackendFactory (Story #510, AC1-AC3, AC5, AC6).

Mocking policy:
- subprocess.run mocked because we test argument formation, not real cp.
- OntapFlexCloneClient mocked because no real ONTAP in unit tests.
- Filesystem operations use tmp_path to avoid real I/O where possible.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# CloneBackend Protocol (AC1)
# ---------------------------------------------------------------------------


class TestCloneBackendProtocol:
    """Verify CloneBackend Protocol definition exists and has required methods."""

    def test_protocol_importable(self):
        """CloneBackend can be imported from clone_backend module."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackend

        assert CloneBackend is not None

    def test_protocol_has_create_clone(self):
        """CloneBackend Protocol has create_clone method."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackend

        assert hasattr(CloneBackend, "create_clone")

    def test_protocol_has_delete_clone(self):
        """CloneBackend Protocol has delete_clone method."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackend

        assert hasattr(CloneBackend, "delete_clone")

    def test_protocol_has_list_clones(self):
        """CloneBackend Protocol has list_clones method."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackend

        assert hasattr(CloneBackend, "list_clones")

    def test_protocol_has_clone_exists(self):
        """CloneBackend Protocol has clone_exists method."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackend

        assert hasattr(CloneBackend, "clone_exists")

    def test_protocol_uses_typing_protocol(self):
        """CloneBackend is defined with typing.Protocol (not abc.ABC)."""
        import typing
        from code_indexer.server.storage.shared.clone_backend import CloneBackend

        # Must be a Protocol subclass (structural subtyping, not ABC).
        # issubclass(X, typing.Protocol) is rejected by mypy because typing.Protocol
        # is a special form, not a concrete class.  Inspecting __mro__ achieves the
        # same runtime assertion without the type error.
        assert typing.Protocol in CloneBackend.__mro__


# ---------------------------------------------------------------------------
# LocalCloneBackend (AC2)
# ---------------------------------------------------------------------------


class TestLocalCloneBackendCreateClone:
    """Tests for LocalCloneBackend.create_clone() - wraps cp --reflink=auto."""

    def test_create_clone_calls_cp_reflink_auto(self, tmp_path: Path):
        """create_clone runs cp --reflink=auto -a source dest."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        source = tmp_path / "source"
        source.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            backend.create_clone(str(source), "cidx", "clone-001")

        mock_run.assert_called_once()
        cmd = mock_run.call_args[0][0]
        assert cmd[0] == "cp"
        assert "--reflink=auto" in cmd
        assert "-a" in cmd
        assert str(source) in cmd

    def test_create_clone_destination_is_namespace_name_under_versioned(
        self, tmp_path: Path
    ):
        """Destination path is {versioned_base}/.versioned/{namespace}/{name}."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        source = tmp_path / "source"
        source.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = backend.create_clone(str(source), "cidx", "clone-001")

        expected_dest = str(tmp_path / ".versioned" / "cidx" / "clone-001")
        assert result == expected_dest
        cmd = mock_run.call_args[0][0]
        assert expected_dest in cmd

    def test_create_clone_creates_parent_directory(self, tmp_path: Path):
        """create_clone creates the parent .versioned/{namespace}/ directory."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        source = tmp_path / "source"
        source.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            backend.create_clone(str(source), "myns", "my-clone")

        parent = tmp_path / ".versioned" / "myns"
        assert parent.exists()

    def test_create_clone_returns_absolute_path(self, tmp_path: Path):
        """create_clone returns an absolute filesystem path string."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        source = tmp_path / "source"
        source.mkdir()

        with patch("subprocess.run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0)
            result = backend.create_clone(str(source), "ns", "clone-x")

        assert Path(result).is_absolute()

    def test_create_clone_propagates_subprocess_error(self, tmp_path: Path):
        """create_clone propagates CalledProcessError from cp."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        source = tmp_path / "source"
        source.mkdir()

        with patch(
            "subprocess.run",
            side_effect=subprocess.CalledProcessError(1, "cp"),
        ):
            with pytest.raises(subprocess.CalledProcessError):
                backend.create_clone(str(source), "ns", "clone-fail")


class TestLocalCloneBackendDeleteClone:
    """Tests for LocalCloneBackend.delete_clone()."""

    def test_delete_clone_removes_directory(self, tmp_path: Path):
        """delete_clone calls shutil.rmtree on the clone path."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        clone_dir = tmp_path / ".versioned" / "ns" / "clone-001"
        clone_dir.mkdir(parents=True)
        (clone_dir / "file.txt").write_text("data")

        result = backend.delete_clone(str(clone_dir))

        assert result is True
        assert not clone_dir.exists()

    def test_delete_clone_returns_true_when_not_exists(self, tmp_path: Path):
        """delete_clone returns True even when path does not exist (idempotent)."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))

        result = backend.delete_clone("/nonexistent/path/clone-x")

        assert result is True


class TestLocalCloneBackendListClones:
    """Tests for LocalCloneBackend.list_clones()."""

    def test_list_clones_returns_empty_when_namespace_absent(self, tmp_path: Path):
        """list_clones returns [] when namespace directory does not exist."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        result = backend.list_clones("cidx")

        assert result == []

    def test_list_clones_returns_subdirectories(self, tmp_path: Path):
        """list_clones returns each subdirectory of .versioned/{namespace}/."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        ns_dir = tmp_path / ".versioned" / "cidx"
        (ns_dir / "clone-a").mkdir(parents=True)
        (ns_dir / "clone-b").mkdir(parents=True)

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        result = backend.list_clones("cidx")

        names = {r["name"] for r in result}
        assert "clone-a" in names
        assert "clone-b" in names

    def test_list_clones_result_contains_required_keys(self, tmp_path: Path):
        """Each item in list_clones result has namespace, name, clone_path keys."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        ns_dir = tmp_path / ".versioned" / "ns"
        (ns_dir / "myc").mkdir(parents=True)

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        result = backend.list_clones("ns")

        assert len(result) == 1
        item = result[0]
        assert "namespace" in item
        assert "name" in item
        assert "clone_path" in item


class TestLocalCloneBackendCloneExists:
    """Tests for LocalCloneBackend.clone_exists()."""

    def test_clone_exists_true_when_directory_present(self, tmp_path: Path):
        """clone_exists returns True when the clone directory exists."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        clone_dir = tmp_path / ".versioned" / "ns" / "clone-a"
        clone_dir.mkdir(parents=True)

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        assert backend.clone_exists("ns", "clone-a") is True

    def test_clone_exists_false_when_directory_absent(self, tmp_path: Path):
        """clone_exists returns False when the clone directory does not exist."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        assert backend.clone_exists("ns", "nonexistent") is False


# ---------------------------------------------------------------------------
# OntapCloneBackend (AC3)
# ---------------------------------------------------------------------------


class TestOntapCloneBackendCreateClone:
    """Tests for OntapCloneBackend.create_clone() - delegates to OntapFlexCloneClient."""

    def _make_mock_client(self):
        from code_indexer.server.storage.shared.ontap_flexclone_client import (
            OntapFlexCloneClient,
        )

        client = MagicMock(spec=OntapFlexCloneClient)
        client.create_clone.return_value = {
            "uuid": "test-uuid",
            "name": "cidx_clone_myrepo_1700000000",
            "job_uuid": "job-abc",
        }
        client.delete_clone.return_value = True
        client.list_clones.return_value = []
        return client

    def test_create_clone_calls_flexclone_client(self):
        """create_clone delegates to OntapFlexCloneClient.create_clone."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        backend.create_clone("/ignored/source", "cidx", "cidx_clone_myrepo_1700000000")

        client.create_clone.assert_called_once()

    def test_create_clone_passes_clone_name(self):
        """create_clone passes the clone name to OntapFlexCloneClient.create_clone."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        backend.create_clone("/src", "cidx", "my-clone")

        call_args = client.create_clone.call_args
        assert "my-clone" in str(call_args)

    def test_create_clone_returns_mount_path(self):
        """create_clone returns mount_point/clone_name."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        result = backend.create_clone("/src", "cidx", "my-clone")

        assert result == "/mnt/fsx/my-clone"

    def test_delete_clone_delegates_to_flexclone_client(self):
        """delete_clone delegates to OntapFlexCloneClient.delete_clone."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        result = backend.delete_clone("/mnt/fsx/cidx_clone_myrepo_1700000000")

        assert result is True
        client.delete_clone.assert_called_once_with("cidx_clone_myrepo_1700000000")

    def test_list_clones_delegates_to_flexclone_client(self):
        """list_clones delegates to OntapFlexCloneClient.list_clones."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        client.list_clones.return_value = [
            {"name": "cidx_clone_repo_1", "uuid": "u1"},
            {"name": "cidx_clone_repo_2", "uuid": "u2"},
        ]
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        result = backend.list_clones("cidx")

        client.list_clones.assert_called_once()
        assert len(result) == 2

    def test_clone_exists_returns_true_when_found(self):
        """clone_exists returns True when get_volume_info finds the volume."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        client.get_volume_info = MagicMock(
            return_value={"name": "my-clone", "uuid": "u1"}
        )
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        assert backend.clone_exists("cidx", "my-clone") is True

    def test_clone_exists_returns_false_when_not_found(self):
        """clone_exists returns False when get_volume_info returns None."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend

        client = self._make_mock_client()
        client.get_volume_info = MagicMock(return_value=None)
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        assert backend.clone_exists("cidx", "nonexistent") is False


# ---------------------------------------------------------------------------
# CloneBackendFactory (AC6)
# ---------------------------------------------------------------------------


class TestCloneBackendFactory:
    """Tests for CloneBackendFactory."""

    def test_local_backend_returned_for_local_config(self):
        """Factory returns LocalCloneBackend when clone_backend='local'."""
        from code_indexer.server.storage.shared.clone_backend import (
            CloneBackendFactory,
            LocalCloneBackend,
        )

        backend = CloneBackendFactory.create(
            clone_backend_type="local",
            versioned_base="/tmp/versioned",
        )

        assert isinstance(backend, LocalCloneBackend)

    def test_ontap_backend_returned_for_ontap_config(self):
        """Factory returns OntapCloneBackend when clone_backend='ontap'."""
        from code_indexer.server.storage.shared.clone_backend import (
            CloneBackendFactory,
            OntapCloneBackend,
        )
        from code_indexer.server.utils.config_manager import OntapConfig

        ontap_config = OntapConfig(
            endpoint="10.0.0.1",
            svm_name="svm1",
            parent_volume="vol1",
            mount_point="/mnt/fsx",
            admin_user="admin",
            admin_password="pass",
        )

        backend = CloneBackendFactory.create(
            clone_backend_type="ontap",
            ontap_config=ontap_config,
        )

        assert isinstance(backend, OntapCloneBackend)

    def test_cow_daemon_backend_returned_for_cow_daemon_config(self):
        """Factory returns CowDaemonBackend when clone_backend='cow-daemon'."""
        from code_indexer.server.storage.shared.clone_backend import (
            CloneBackendFactory,
            CowDaemonBackend,
        )
        from code_indexer.server.utils.config_manager import CowDaemonConfig

        cow_config = CowDaemonConfig(
            daemon_url="http://storage:8081",
            api_key="secret-key",
            mount_point="/mnt/nfs/cidx",
        )

        backend = CloneBackendFactory.create(
            clone_backend_type="cow-daemon",
            cow_daemon_config=cow_config,
        )

        assert isinstance(backend, CowDaemonBackend)

    def test_invalid_backend_type_raises_value_error(self):
        """Factory raises ValueError for unknown backend type."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackendFactory

        with pytest.raises(ValueError, match="Unsupported clone_backend"):
            CloneBackendFactory.create(clone_backend_type="unknown-backend")

    def test_value_error_lists_valid_options(self):
        """ValueError message includes valid options."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackendFactory

        with pytest.raises(ValueError) as exc_info:
            CloneBackendFactory.create(clone_backend_type="bad")

        msg = str(exc_info.value)
        assert "local" in msg
        assert "ontap" in msg
        assert "cow-daemon" in msg


# ---------------------------------------------------------------------------
# Protocol conformance checks (duck-type)
# ---------------------------------------------------------------------------


class TestProtocolConformance:
    """All backends satisfy CloneBackend Protocol (duck-type check)."""

    def test_local_backend_conforms_to_protocol(self, tmp_path):
        """LocalCloneBackend has all CloneBackend Protocol methods."""
        from code_indexer.server.storage.shared.clone_backend import (
            LocalCloneBackend,
        )

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        # Runtime check: each required method exists and is callable
        assert callable(getattr(backend, "create_clone", None))
        assert callable(getattr(backend, "delete_clone", None))
        assert callable(getattr(backend, "list_clones", None))
        assert callable(getattr(backend, "clone_exists", None))

    def test_ontap_backend_conforms_to_protocol(self):
        """OntapCloneBackend has all CloneBackend Protocol methods."""
        from code_indexer.server.storage.shared.clone_backend import OntapCloneBackend
        from code_indexer.server.storage.shared.ontap_flexclone_client import (
            OntapFlexCloneClient,
        )

        client = MagicMock(spec=OntapFlexCloneClient)
        backend = OntapCloneBackend(flexclone_client=client, mount_point="/mnt/fsx")

        assert callable(getattr(backend, "create_clone", None))
        assert callable(getattr(backend, "delete_clone", None))
        assert callable(getattr(backend, "list_clones", None))
        assert callable(getattr(backend, "clone_exists", None))

    def test_cow_daemon_backend_conforms_to_protocol(self):
        """CowDaemonBackend has all CloneBackend Protocol methods."""
        from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend
        from code_indexer.server.utils.config_manager import CowDaemonConfig

        config = CowDaemonConfig(
            daemon_url="http://localhost:8081",
            api_key="key",
            mount_point="/mnt/nfs/cidx",
        )
        backend = CowDaemonBackend(config=config)

        assert callable(getattr(backend, "create_clone", None))
        assert callable(getattr(backend, "delete_clone", None))
        assert callable(getattr(backend, "list_clones", None))
        assert callable(getattr(backend, "clone_exists", None))


# ---------------------------------------------------------------------------
# Shared helpers for CowDaemonBackend tests
# ---------------------------------------------------------------------------


def _make_response(status_code: int, json_data=None):
    """Build a MagicMock HTTP response with the given status code and JSON body."""
    mock = MagicMock()
    mock.status_code = status_code
    mock.json.return_value = json_data if json_data is not None else {}
    mock.raise_for_status = MagicMock()
    return mock


def _make_cow_config(timeout_seconds: int = 30):
    from code_indexer.server.utils.config_manager import CowDaemonConfig

    return CowDaemonConfig(
        daemon_url="http://daemon:8081",
        api_key="test-api-key",
        mount_point="/mnt/nfs/cidx",
        poll_interval_seconds=1,
        timeout_seconds=timeout_seconds,
    )


def _make_cow_backend(timeout_seconds: int = 30):
    from code_indexer.server.storage.shared.clone_backend import CowDaemonBackend

    return CowDaemonBackend(config=_make_cow_config(timeout_seconds=timeout_seconds))


def _mock_requests_module(post_resp=None, get_resp=None, delete_resp=None):
    """Return a MagicMock requests module wired with given response objects."""
    mock_req = MagicMock()
    if post_resp is not None:
        mock_req.post.return_value = post_resp
    if get_resp is not None:
        if isinstance(get_resp, list):
            mock_req.get.side_effect = get_resp
        else:
            mock_req.get.return_value = get_resp
    if delete_resp is not None:
        mock_req.delete.return_value = delete_resp
    return mock_req


# ---------------------------------------------------------------------------
# CowDaemonBackend HTTP behaviour (AC4)
# ---------------------------------------------------------------------------


class TestCowDaemonBackendCreateClone:
    """Tests for CowDaemonBackend.create_clone() HTTP + poll loop."""

    def test_create_clone_posts_to_clones_endpoint(self):
        """create_clone POSTs to /api/v1/clones with source, namespace, name."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-123"})
        poll_resp = _make_response(
            200, {"status": "completed", "clone_path": "ns/name"}
        )
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=poll_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.create_clone("/src/repo", "ns", "name")

        mock_req.post.assert_called_once()
        call_kwargs = mock_req.post.call_args
        assert "/api/v1/clones" in call_kwargs[0][0]
        body = call_kwargs[1]["json"]
        assert body["source_path"] == "/src/repo"
        assert body["namespace"] == "ns"
        assert body["name"] == "name"

    def test_create_clone_sends_bearer_auth(self):
        """create_clone includes Authorization Bearer header on POST."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-abc"})
        poll_resp = _make_response(200, {"status": "completed", "clone_path": "ns/c"})
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=poll_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.create_clone("/src", "ns", "c")

        headers = mock_req.post.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-api-key"

    def test_create_clone_polls_until_completed(self):
        """create_clone polls job endpoint: pending -> running -> completed."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "job-xyz"})
        poll_resps = [
            _make_response(200, {"status": "pending"}),
            _make_response(200, {"status": "running"}),
            _make_response(200, {"status": "completed", "clone_path": "ns/done"}),
        ]
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=poll_resps)

        with patch.dict(sys.modules, {"requests": mock_req}):
            with patch("time.sleep"):
                result = backend.create_clone("/src", "ns", "done")

        assert mock_req.get.call_count == 3
        assert result == "/mnt/nfs/cidx/ns/done"

    def test_create_clone_returns_mount_point_plus_clone_path(self):
        """create_clone returns mount_point/clone_path from completed job."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "j"})
        done_resp = _make_response(
            200, {"status": "completed", "clone_path": "myns/my-clone"}
        )
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=done_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.create_clone("/src", "myns", "my-clone")

        assert result == "/mnt/nfs/cidx/myns/my-clone"

    def test_create_clone_raises_runtime_error_on_failed_job(self):
        """create_clone raises RuntimeError when job status is 'failed'."""
        backend = _make_cow_backend()
        post_resp = _make_response(202, {"job_id": "fail-job"})
        fail_resp = _make_response(200, {"status": "failed", "error": "disk full"})
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=fail_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            with pytest.raises(RuntimeError, match="disk full"):
                backend.create_clone("/src", "ns", "clone")

    def test_create_clone_raises_timeout_error_when_job_stalls(self):
        """create_clone raises TimeoutError when job does not complete in time."""
        backend = _make_cow_backend(timeout_seconds=0)
        post_resp = _make_response(202, {"job_id": "slow-job"})
        running_resp = _make_response(200, {"status": "running"})
        mock_req = _mock_requests_module(post_resp=post_resp, get_resp=running_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            with pytest.raises(TimeoutError, match="slow-job"):
                backend.create_clone("/src", "ns", "clone")


class TestCowDaemonBackendDeleteClone:
    """Tests for CowDaemonBackend.delete_clone()."""

    def test_delete_clone_sends_delete_request(self):
        """delete_clone sends DELETE to /api/v1/clones/{namespace}/{name}."""
        backend = _make_cow_backend()
        del_resp = _make_response(204)
        mock_req = _mock_requests_module(delete_resp=del_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.delete_clone("/mnt/nfs/cidx/myns/my-clone")

        mock_req.delete.assert_called_once()
        url = mock_req.delete.call_args[0][0]
        assert "myns/my-clone" in url
        assert result is True

    def test_delete_clone_404_returns_true(self):
        """delete_clone returns True when daemon returns 404 (already gone)."""
        backend = _make_cow_backend()
        del_resp = _make_response(404)
        mock_req = _mock_requests_module(delete_resp=del_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.delete_clone("/mnt/nfs/cidx/ns/clone")

        assert result is True
        del_resp.raise_for_status.assert_not_called()

    def test_delete_clone_path_outside_mount_raises_value_error(self):
        """delete_clone raises ValueError when path is not under mount_point."""
        backend = _make_cow_backend()

        with pytest.raises(ValueError, match="not under mount_point"):
            backend.delete_clone("/tmp/other/ns/clone")

    def test_delete_clone_path_missing_namespace_name_raises_value_error(self):
        """delete_clone raises ValueError when path has no namespace/name segment."""
        backend = _make_cow_backend()

        with pytest.raises(ValueError, match="at least namespace/name"):
            backend.delete_clone("/mnt/nfs/cidx/only-one-segment")

    def test_delete_clone_sends_bearer_auth(self):
        """delete_clone includes Authorization Bearer header."""
        backend = _make_cow_backend()
        del_resp = _make_response(204)
        mock_req = _mock_requests_module(delete_resp=del_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.delete_clone("/mnt/nfs/cidx/ns/c")

        headers = mock_req.delete.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-api-key"


class TestCowDaemonBackendListClones:
    """Tests for CowDaemonBackend.list_clones()."""

    def test_list_clones_get_request_with_namespace_param(self):
        """list_clones GETs /api/v1/clones?namespace=X."""
        backend = _make_cow_backend()
        data = [{"namespace": "ns", "name": "c1"}]
        get_resp = _make_response(200, data)
        get_resp.json.return_value = data
        mock_req = _mock_requests_module(get_resp=get_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.list_clones("ns")

        mock_req.get.assert_called_once()
        call_args = mock_req.get.call_args
        assert "/api/v1/clones" in call_args[0][0]
        assert call_args[1]["params"]["namespace"] == "ns"
        assert result == data

    def test_list_clones_sends_bearer_auth(self):
        """list_clones includes Authorization Bearer header."""
        backend = _make_cow_backend()
        get_resp = _make_response(200, [])
        get_resp.json.return_value = []
        mock_req = _mock_requests_module(get_resp=get_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.list_clones("ns")

        headers = mock_req.get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-api-key"


class TestCowDaemonBackendCloneExists:
    """Tests for CowDaemonBackend.clone_exists()."""

    def test_clone_exists_returns_true_on_200(self):
        """clone_exists returns True when daemon returns 200."""
        backend = _make_cow_backend()
        get_resp = _make_response(200)
        mock_req = _mock_requests_module(get_resp=get_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.clone_exists("ns", "my-clone")

        assert result is True

    def test_clone_exists_returns_false_on_404(self):
        """clone_exists returns False when daemon returns 404."""
        backend = _make_cow_backend()
        get_resp = _make_response(404)
        mock_req = _mock_requests_module(get_resp=get_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            result = backend.clone_exists("ns", "gone")

        assert result is False

    def test_clone_exists_sends_bearer_auth(self):
        """clone_exists includes Authorization Bearer header."""
        backend = _make_cow_backend()
        get_resp = _make_response(200)
        mock_req = _mock_requests_module(get_resp=get_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.clone_exists("ns", "c")

        headers = mock_req.get.call_args[1]["headers"]
        assert headers["Authorization"] == "Bearer test-api-key"

    def test_clone_exists_url_contains_namespace_and_name(self):
        """clone_exists GETs /api/v1/clones/{namespace}/{name}."""
        backend = _make_cow_backend()
        get_resp = _make_response(200)
        mock_req = _mock_requests_module(get_resp=get_resp)

        with patch.dict(sys.modules, {"requests": mock_req}):
            backend.clone_exists("myns", "mycl")

        url = mock_req.get.call_args[0][0]
        assert "myns/mycl" in url


# ---------------------------------------------------------------------------
# Path traversal validation
# ---------------------------------------------------------------------------


class TestPathTraversalValidation:
    """LocalCloneBackend rejects namespace/name with traversal characters."""

    def test_namespace_with_double_dot_raises(self, tmp_path: Path):
        """namespace='../evil' raises ValueError."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        with pytest.raises(ValueError, match="invalid characters"):
            backend.create_clone("/src", "../evil", "name")

    def test_name_with_slash_raises(self, tmp_path: Path):
        """name='a/b' raises ValueError."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        with pytest.raises(ValueError, match="invalid characters"):
            backend.clone_exists("ns", "a/b")

    def test_namespace_with_null_byte_raises(self, tmp_path: Path):
        """namespace containing null byte raises ValueError."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        with pytest.raises(ValueError, match="invalid characters"):
            backend.list_clones("ns\x00evil")


# ---------------------------------------------------------------------------
# LocalCloneBackend delete error path
# ---------------------------------------------------------------------------


class TestLocalCloneBackendDeleteErrors:
    """LocalCloneBackend.delete_clone returns False on OSError."""

    def test_delete_clone_returns_false_on_os_error(self, tmp_path: Path):
        """delete_clone returns False when shutil.rmtree raises OSError."""
        from code_indexer.server.storage.shared.clone_backend import LocalCloneBackend

        clone_dir = tmp_path / ".versioned" / "ns" / "clone"
        clone_dir.mkdir(parents=True)

        backend = LocalCloneBackend(versioned_base=str(tmp_path))
        with patch("shutil.rmtree", side_effect=OSError("permission denied")):
            result = backend.delete_clone(str(clone_dir))

        assert result is False


# ---------------------------------------------------------------------------
# CloneBackendFactory missing config guards
# ---------------------------------------------------------------------------


class TestCloneBackendFactoryMissingConfig:
    """Factory raises ValueError when required config is None."""

    def test_ontap_backend_without_config_raises_value_error(self):
        """Factory raises ValueError for 'ontap' when ontap_config is None."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackendFactory

        with pytest.raises(ValueError, match="ontap_config is required"):
            CloneBackendFactory.create(clone_backend_type="ontap", ontap_config=None)

    def test_cow_daemon_backend_without_config_raises_value_error(self):
        """Factory raises ValueError for 'cow-daemon' when cow_daemon_config is None."""
        from code_indexer.server.storage.shared.clone_backend import CloneBackendFactory

        with pytest.raises(ValueError, match="cow_daemon_config is required"):
            CloneBackendFactory.create(
                clone_backend_type="cow-daemon", cow_daemon_config=None
            )
