"""
TDD tests for Story #1199 AC0: code_indexer.server.main --workers extension.

Behavioral: calls real main() with patched uvicorn.run and asserts kwargs.
"""

from unittest.mock import patch


class TestAC0MainPy:
    """AC0: code_indexer.server.main accepts --workers and forwards to uvicorn.run."""

    def test_workers_forwarded_to_uvicorn_run(self) -> None:
        """AC0 BEHAVIORAL: uvicorn.run receives workers=3 when --workers 3 is passed."""
        import code_indexer.server.main as main_mod

        with patch.object(main_mod, "uvicorn") as mock_uvicorn:
            with patch("sys.argv", ["main.py", "--workers", "3"]):
                main_mod.main()

        mock_uvicorn.run.assert_called_once()
        _, kwargs = mock_uvicorn.run.call_args
        assert kwargs.get("workers") == 3, (
            f"uvicorn.run must get workers=3; got: {kwargs}"
        )

    def test_no_log_level_added(self) -> None:
        """AC0/CRITICAL-A: must NOT pass log_level to uvicorn.run (in-process setting)."""
        import code_indexer.server.main as main_mod

        with patch.object(main_mod, "uvicorn") as mock_uvicorn:
            with patch("sys.argv", ["main.py", "--workers", "2"]):
                main_mod.main()

        _, kwargs = mock_uvicorn.run.call_args
        assert "log_level" not in kwargs, (
            f"uvicorn.run must NOT receive log_level= (CRITICAL-A); got: {kwargs}"
        )

    def test_port_host_reload_preserved(self) -> None:
        """AC0: --port, --host, --reload behavior unchanged after adding --workers."""
        import code_indexer.server.main as main_mod

        with patch.object(main_mod, "uvicorn") as mock_uvicorn:
            with patch(
                "sys.argv",
                ["main.py", "--port", "9999", "--host", "0.0.0.0", "--workers", "2"],
            ):
                main_mod.main()

        _, kwargs = mock_uvicorn.run.call_args
        assert kwargs.get("port") == 9999
        assert kwargs.get("host") == "0.0.0.0"
        assert "workers" in kwargs

    def test_default_workers_is_1(self) -> None:
        """AC0: Default workers=1 when --workers not specified."""
        import code_indexer.server.main as main_mod

        with patch.object(main_mod, "uvicorn") as mock_uvicorn:
            with patch("sys.argv", ["main.py"]):
                main_mod.main()

        _, kwargs = mock_uvicorn.run.call_args
        assert kwargs.get("workers") == 1, (
            f"Default workers must be 1; got: {kwargs.get('workers')}"
        )
