"""
TDD tests for Bug #1231: save_config / save_config_dict must be atomic.

The current (broken) implementation does truncate-then-write:
    with open(self.config_file_path, "w") as f:
        json.dump(...)
If a crash or disk-full error occurs after truncation but before flush,
config.json is left as an empty/corrupt file — the only copy of bootstrap keys
(host, port, storage_mode, etc.) is destroyed.

Fix: write to a NamedTemporaryFile/mkstemp in the SAME directory as
config_file_path, flush, os.fsync(fileno), then os.replace(tmp, config_file_path).
On any exception unlink the tmp and re-raise.  Output is byte-identical to today
on the success path.

Tests:
1. Round-trip: save_config then load_config returns identical content.
2. save_config_dict output ends with '\\n' (byte-identical contract).
3. Atomicity: os.replace raises -> original config.json intact, no temp artifacts.
4. Same atomicity for save_config.
5. Success path: no temp artifacts remain after successful save.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_manager(server_dir: Path) -> ServerConfigManager:
    server_dir.mkdir(parents=True, exist_ok=True)
    return ServerConfigManager(server_dir_path=str(server_dir))


# ---------------------------------------------------------------------------
# Test 1 — round-trip: save_config → load_config returns identical content
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_save_config_load_config_round_trip(self, tmp_path: Path) -> None:
        """save_config then load_config returns identical content."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        original = ServerConfig(
            server_dir=str(server_dir),
            host="0.0.0.0",
            port=9000,
            workers=4,
            log_level="debug",
        )
        mgr.save_config(original)

        loaded = mgr.load_config()
        assert loaded is not None
        assert loaded.host == "0.0.0.0"
        assert loaded.port == 9000
        assert loaded.workers == 4
        assert loaded.log_level == "debug"

    def test_save_config_dict_round_trip(self, tmp_path: Path) -> None:
        """save_config_dict then load_config returns expected values."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        mgr.save_config_dict(
            {
                "server_dir": str(server_dir),
                "host": "10.0.0.1",
                "port": 7777,
                "workers": 8,
            }
        )

        loaded = mgr.load_config()
        assert loaded is not None
        assert loaded.host == "10.0.0.1"
        assert loaded.port == 7777
        assert loaded.workers == 8

    # -----------------------------------------------------------------------
    # Test 2 — byte-identical contract: save_config_dict ends with '\n'
    # -----------------------------------------------------------------------

    def test_save_config_dict_output_ends_with_newline(self, tmp_path: Path) -> None:
        """save_config_dict output always ends with '\\n' (byte-identical contract)."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        mgr.save_config_dict({"server_dir": str(server_dir), "host": "127.0.0.1"})

        raw = (server_dir / "config.json").read_text()
        assert raw.endswith("\n"), (
            "save_config_dict must write a trailing '\\n' (byte-identical with today's "
            "implementation). Got: " + repr(raw[-10:])
        )

    def test_save_config_no_trailing_newline(self, tmp_path: Path) -> None:
        """save_config does NOT write trailing newline (byte-identical contract)."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        cfg = ServerConfig(server_dir=str(server_dir))
        mgr.save_config(cfg)

        raw = (server_dir / "config.json").read_text()
        # json.dump with indent=2 does NOT add trailing newline
        assert raw.endswith("}"), (
            "save_config must NOT add trailing '\\n'. Got: " + repr(raw[-10:])
        )


# ---------------------------------------------------------------------------
# Test 3 — atomicity: save_config_dict preserves original on os.replace failure
# ---------------------------------------------------------------------------


class TestAtomicity:
    """Verify original config.json is left intact when os.replace raises."""

    def test_save_config_dict_preserves_original_when_os_replace_fails(
        self, tmp_path: Path
    ) -> None:
        """os.replace failure must leave original config.json untouched.

        Before the fix: the file is truncated before os.replace is called;
        any failure leaves an empty/corrupt config.json.
        After the fix: write to a temp file first; original is only replaced
        on success via os.replace; failure leaves original intact.
        """
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        # Write known original content
        original_dict = {"server_dir": str(server_dir), "host": "127.0.0.1"}
        (server_dir / "config.json").write_text(
            json.dumps(original_dict, indent=2) + "\n"
        )

        # Patch os.replace to simulate disk-full / rename failure
        with patch("os.replace", side_effect=OSError("simulated disk full")):
            with pytest.raises(OSError, match="simulated disk full"):
                mgr.save_config_dict({"server_dir": str(server_dir), "host": "0.0.0.0"})

        # Original must be intact
        surviving = json.loads((server_dir / "config.json").read_text())
        assert surviving.get("host") == "127.0.0.1", (
            "Bug #1231: original config.json must be preserved when os.replace fails. "
            f"Got: {surviving!r}"
        )

    def test_save_config_dict_no_temp_artifact_on_os_replace_failure(
        self, tmp_path: Path
    ) -> None:
        """After a failed os.replace, no temp files remain in the server directory."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        (server_dir / "config.json").write_text(
            json.dumps({"server_dir": str(server_dir), "host": "127.0.0.1"}, indent=2)
            + "\n"
        )

        with patch("os.replace", side_effect=OSError("simulated disk full")):
            with pytest.raises(OSError):
                mgr.save_config_dict({"server_dir": str(server_dir), "host": "new"})

        # Only config.json may exist; no .tmp / temp files remain
        files = list(server_dir.iterdir())
        non_config = [f for f in files if f.name != "config.json"]
        assert non_config == [], (
            f"Bug #1231: temp file must be cleaned up on failure. "
            f"Found leftover files: {[f.name for f in non_config]}"
        )

    def test_save_config_preserves_original_when_os_replace_fails(
        self, tmp_path: Path
    ) -> None:
        """save_config must also be atomic (same guarantee as save_config_dict)."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        original = ServerConfig(server_dir=str(server_dir), host="original-host")
        mgr.save_config(original)

        modified = ServerConfig(server_dir=str(server_dir), host="modified-host")

        with patch("os.replace", side_effect=OSError("simulated disk full")):
            with pytest.raises(OSError, match="simulated disk full"):
                mgr.save_config(modified)

        # Original must still be in place
        surviving = json.loads((server_dir / "config.json").read_text())
        assert surviving.get("host") == "original-host", (
            f"Bug #1231: save_config must be atomic. Got host={surviving.get('host')!r}"
        )

    def test_save_config_no_temp_artifact_on_os_replace_failure(
        self, tmp_path: Path
    ) -> None:
        """save_config leaves no temp artifact after failed os.replace."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        original = ServerConfig(server_dir=str(server_dir), host="127.0.0.1")
        mgr.save_config(original)

        with patch("os.replace", side_effect=OSError("simulated disk full")):
            with pytest.raises(OSError):
                mgr.save_config(ServerConfig(server_dir=str(server_dir), host="new"))

        files = list(server_dir.iterdir())
        non_config = [f for f in files if f.name != "config.json"]
        assert non_config == [], (
            f"Bug #1231: save_config must clean up temp file on failure. "
            f"Leftover: {[f.name for f in non_config]}"
        )


# ---------------------------------------------------------------------------
# Test 5 — success path: no temp artifacts remain after successful save
# ---------------------------------------------------------------------------


class TestSuccessPathCleanup:
    """On success, temp file is renamed to config.json — no temp artifact remains."""

    def test_save_config_dict_no_temp_artifact_on_success(self, tmp_path: Path) -> None:
        """After successful save_config_dict, no temp files remain."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        mgr.save_config_dict({"server_dir": str(server_dir), "host": "127.0.0.1"})

        files = list(server_dir.iterdir())
        assert len(files) == 1, (
            f"Only config.json should exist after success. Found: {[f.name for f in files]}"
        )
        assert files[0].name == "config.json"

    def test_save_config_no_temp_artifact_on_success(self, tmp_path: Path) -> None:
        """After successful save_config, no temp files remain."""
        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        mgr.save_config(ServerConfig(server_dir=str(server_dir)))

        files = list(server_dir.iterdir())
        assert len(files) == 1, (
            f"Only config.json should exist after success. Found: {[f.name for f in files]}"
        )
        assert files[0].name == "config.json"

    def test_temp_file_created_in_same_dir_as_config(self, tmp_path: Path) -> None:
        """Temp file is created in same directory as config.json (same filesystem,
        enabling atomic os.replace without cross-device moves)."""
        import tempfile as _tempfile

        server_dir = tmp_path / "server"
        mgr = _make_manager(server_dir)

        created_tmp_paths: list = []
        original_mkstemp = _tempfile.mkstemp

        def capturing_mkstemp(**kwargs):  # type: ignore[no-untyped-def]
            fd, path = original_mkstemp(**kwargs)
            created_tmp_paths.append(Path(path))
            return fd, path

        with patch("tempfile.mkstemp", side_effect=capturing_mkstemp):
            mgr.save_config_dict({"server_dir": str(server_dir)})

        assert created_tmp_paths, "mkstemp must have been called for atomic write"
        for p in created_tmp_paths:
            assert p.parent == server_dir, (
                f"Temp file {p} must be in same dir as config.json ({server_dir}) "
                "for atomic os.replace — cross-device rename is not atomic"
            )
