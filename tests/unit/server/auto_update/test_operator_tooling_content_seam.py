"""Operator tooling under ``content_backend: delegated`` (content seam, phase 5).

Two host-side operator paths touch the repository volume's ROOT directories:

* the auto-updater's CoW-daemon symlink steps
  (``DeploymentExecutor._ensure_{activated,golden}_repos_symlink_for_cow_daemon``
  and their helpers) mkdir/symlink/rename ``data/<golden|activated>-repos``
  into the CoW mount;
* ``cidx global activate`` registers an alias to this host's
  ``<golden-repos>/repos/<name>/.code-indexer/index``.

Under a delegated content backend both are refused, loudly and before any
filesystem access. The local default is pinned byte-identical: a config with
``content_backend: local`` behaves exactly as one that predates the field.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli
from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor
from code_indexer.server.startup.content_backend_wiring import content_backend_name

_STEPS = {
    "activated-repos": "_ensure_activated_repos_symlink_for_cow_daemon",
    "golden-repos": "_ensure_golden_repos_symlink_for_cow_daemon",
}


class _Cow:
    def __init__(self, mount_point: str) -> None:
        self.mount_point = mount_point
        self.daemon_storage_path = None


class _Config:
    """A plain server config: attributes absent unless given (no MagicMock
    auto-attributes, so an unset ``content_backend`` is truly unset)."""

    def __init__(self, **fields: Any) -> None:
        for name, value in fields.items():
            setattr(self, name, value)


def _run(step: str, data_dir: Path, config: Any) -> bool:
    executor = DeploymentExecutor(repo_path=Path("/test/repo"), service_name="cidx")
    with patch(
        "code_indexer.server.utils.config_manager.ServerConfigManager"
    ) as manager:
        manager.return_value.load_config.return_value = config
        with patch(
            "code_indexer.server.auto_update.deployment_executor._cidx_data_dir",
            data_dir,
        ):
            return bool(getattr(executor, step)())


def _tree(root: Path) -> Dict[str, str]:
    """Every path under *root* with what it is (symlinks by their target)."""
    out: Dict[str, str] = {}
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            path = Path(dirpath) / name
            rel = str(path.relative_to(root))
            out[rel] = (
                "-> " + os.readlink(path).replace(str(root), "<root>")
                if path.is_symlink()
                else ("dir" if path.is_dir() else "file")
            )
    return out


def _forbid_local_io() -> List[Any]:
    """Patches that fail the test on any mkdir/symlink/rename/rmdir."""

    def boom(*_a: Any, **_k: Any) -> None:
        raise AssertionError("local filesystem touched under delegated")

    return [
        patch("os.symlink", boom),
        patch.object(Path, "mkdir", boom),
        patch.object(Path, "rename", boom),
        patch.object(Path, "rmdir", boom),
        patch.object(Path, "symlink_to", boom),
        patch.object(Path, "exists", boom),
        patch.object(Path, "is_symlink", boom),
    ]


class TestContentBackendName:
    def test_reads_the_selection(self) -> None:
        assert content_backend_name(_Config(content_backend="delegated")) == "delegated"

    @pytest.mark.parametrize("config", [_Config(), _Config(content_backend=None)])
    def test_unset_is_local(self, config: Any) -> None:
        assert content_backend_name(config) == "local"


@pytest.mark.parametrize("resource", sorted(_STEPS))
class TestCowSymlinkSteps:
    def test_local_default_is_byte_identical(
        self, resource: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """``content_backend: local`` == a config without the field: same
        return, same tree, same log records."""
        results = []
        for case, extra in (("unset", {}), ("local", {"content_backend": "local"})):
            root = tmp_path / case
            (root / "data").mkdir(parents=True)
            config = _Config(
                clone_backend="cow-daemon",
                cow_daemon=_Cow(str(tmp_path / case / "cow")),
                **extra,
            )
            caplog.clear()
            with caplog.at_level(logging.DEBUG):
                ok = _run(_STEPS[resource], root, config)
            logs = [
                (r.levelno, r.getMessage().replace(str(root), "<root>"))
                for r in caplog.records
            ]
            results.append((ok, _tree(root), logs))
        assert results[0] == results[1]
        ok, tree, _ = results[0]
        assert ok is True
        assert tree[f"data/{resource}"] == f"-> <root>/cow/{resource}"

    def test_delegated_refuses_before_any_filesystem_access(
        self, resource: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        pod = tmp_path / "no-such-pod-dir"
        config = _Config(
            content_backend="delegated",
            clone_backend="cow-daemon",
            cow_daemon=_Cow(str(tmp_path / "no-such-cow-mount")),
        )
        patches = _forbid_local_io()
        for p in patches:
            p.start()
        try:
            with caplog.at_level(logging.WARNING):
                ok = _run(_STEPS[resource], pod, config)
        finally:
            for p in patches:
                p.stop()
        assert ok is False
        assert not pod.exists()
        assert not (tmp_path / "no-such-cow-mount").exists()
        messages = " ".join(r.getMessage() for r in caplog.records)
        assert "DEPLOY-GENERAL-226" in messages
        assert f"{resource} symlink for the CoW daemon refused" in messages
        assert "content_backend='delegated'" in messages

    def test_delegated_clone_backend_stays_a_silent_no_op(
        self, resource: str, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """The valid delegated configuration (clone_backend delegated) is
        today's no-op: True, nothing touched, no warning."""
        pod = tmp_path / "no-such-pod-dir"
        config = _Config(content_backend="delegated", clone_backend="delegated")
        with caplog.at_level(logging.WARNING):
            ok = _run(_STEPS[resource], pod, config)
        assert ok is True
        assert not pod.exists()
        assert caplog.records == []


def _server_dir(
    tmp_path: Path, config: Optional[Dict[str, Any]], raw: str = ""
) -> Path:
    server_dir = tmp_path / "server"
    server_dir.mkdir()
    if config is not None:
        (server_dir / "config.json").write_text(json.dumps(config))
    elif raw:
        (server_dir / "config.json").write_text(raw)
    return server_dir


def _global_activate(
    tmp_path: Path, server_dir: Path, on_activator: Callable[..., Any]
) -> Tuple[int, str]:
    """``cidx global activate myrepo``: (exit code, output with Rich's line
    wrapping undone -- compare words, not terminal layout)."""
    env = {
        "CIDX_SERVER_DATA_DIR": str(server_dir),
        "CIDX_GOLDEN_REPOS_DIR": str(tmp_path / "no-such-golden-repos"),
    }
    with patch(
        "code_indexer.global_repos.global_activation.GlobalActivator",
        side_effect=on_activator,
    ):
        result = CliRunner().invoke(cli, ["global", "activate", "myrepo"], env=env)
    return result.exit_code, " ".join(result.output.split())


class TestGlobalActivate:
    def _never(self, *_a: Any, **_k: Any) -> None:
        raise AssertionError("GlobalActivator constructed")

    def test_local_default_is_byte_identical(self, tmp_path: Path) -> None:
        """No server config, and ``content_backend: local``: the same
        output and exit code as before (the repo-not-found path here)."""
        outputs = []
        for case, config in (
            ("unset", None),
            ("local", {"server_dir": "x", "content_backend": "local"}),
        ):
            root = tmp_path / case
            root.mkdir()
            code, output = _global_activate(
                root, _server_dir(root, config), self._never
            )
            outputs.append((code, output.replace(str(root), "<root>")))
        assert outputs[0] == outputs[1]
        assert outputs[0][0] == 1
        assert "Golden repo 'myrepo' not found" in outputs[0][1]

    def test_delegated_refuses_before_touching_the_host(self, tmp_path: Path) -> None:
        server_dir = _server_dir(
            tmp_path, {"server_dir": "x", "content_backend": "delegated"}
        )
        probed: List[str] = []
        real_exists = Path.exists

        def spy(self: Path) -> bool:
            if "golden-repos" in str(self):
                probed.append(str(self))
            return real_exists(self)

        with patch.object(Path, "exists", spy):
            code, output = _global_activate(tmp_path, server_dir, self._never)
        assert code == 1
        assert "not available when the server's content_backend is" in output
        assert probed == []

    def test_unreadable_server_config_is_refused(self, tmp_path: Path) -> None:
        server_dir = _server_dir(tmp_path, None, raw="{not json")
        code, output = _global_activate(tmp_path, server_dir, self._never)
        assert code == 1
        assert "cannot read the server config" in output
