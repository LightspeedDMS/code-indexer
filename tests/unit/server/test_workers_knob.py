"""C1 worker-safe --workers: resolve the uvicorn worker count (bootstrap knob)."""

from code_indexer.server.main import _resolve_workers


def test_cli_flag_wins():
    assert _resolve_workers(4) == 4


def test_cli_value_clamped_to_at_least_one():
    assert _resolve_workers(0) == 1
    assert _resolve_workers(-2) == 1


def test_reads_workers_from_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text('{"workers": 3}')
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    assert _resolve_workers(None) == 3


def test_defaults_to_one_without_config(tmp_path, monkeypatch):
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    assert _resolve_workers(None) == 1


def test_defaults_to_one_on_malformed_config(tmp_path, monkeypatch):
    (tmp_path / "config.json").write_text("{ not json")
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    assert _resolve_workers(None) == 1
