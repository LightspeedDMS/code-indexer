"""The siem_delivery Web Config section and the committed-config read."""

from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server.services.config_service import ConfigService


def _service(server_dir: Path) -> ConfigService:
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = server_dir / "cidx_server.db"
    DatabaseSchema(str(db_path)).initialize_database()
    svc = ConfigService(server_dir_path=str(server_dir))
    svc.load_config()
    svc.initialize_runtime_db(str(db_path))
    return svc


@pytest.fixture()
def server_dir(tmp_path: Path) -> Path:
    path = tmp_path / "server"
    path.mkdir()
    return path


def test_settings_expose_the_section_with_defaults(server_dir: Path) -> None:
    settings = _service(server_dir).get_all_settings()["siem_delivery"]
    assert settings["enabled"] is False
    assert settings["api_version"] == "v1"
    assert settings["max_batch_events"] == 1000
    assert settings["harness_endpoint"] == ""


def test_updates_are_typed(server_dir: Path) -> None:
    svc = _service(server_dir)
    svc.update_settings_atomic(
        [
            ("siem_delivery", "enabled", "true"),
            ("siem_delivery", "max_batch_events", "25"),
            ("siem_delivery", "project_id", " example-project "),
        ]
    )
    section = svc.get_config().siem_delivery_config
    assert section is not None
    assert section.enabled is True and section.max_batch_events == 25
    assert section.project_id == "example-project"


def test_removed_key_path_setting_is_rejected(server_dir: Path) -> None:
    """There is exactly one way to configure the key (the Web UI credential)."""
    svc = _service(server_dir)
    with pytest.raises(ValueError):
        svc.update_settings_atomic(
            [("siem_delivery", "service_account_key_path", "/keys/sa.json")]
        )
    assert "service_account_key_path" not in svc.get_all_settings()["siem_delivery"]


def test_credential_form_accepts_exactly_one_of_paste_or_upload() -> None:
    from code_indexer.server.services.siem_delivery.config_view import (
        credential_text_from_inputs,
    )

    assert credential_text_from_inputs('  {"a": 1}\n', None) == '{"a": 1}'
    assert credential_text_from_inputs("", b'{"b": 2}') == '{"b": 2}'
    assert credential_text_from_inputs("   ", b'{"b": 2}') == '{"b": 2}'
    for pasted, uploaded in (
        ("", None),
        ("  ", b""),
        ('{"a": 1}', b'{"b": 2}'),
        ("", b"\xff\xfe"),
        ("", b"x" * (64 * 1024 + 1)),
    ):
        with pytest.raises(ValueError):
            credential_text_from_inputs(pasted, uploaded)


def test_unknown_key_is_rejected(server_dir: Path) -> None:
    with pytest.raises(ValueError):
        _service(server_dir).update_settings_atomic([("siem_delivery", "nope", "1")])


def test_read_committed_section_sees_another_workers_save(server_dir: Path) -> None:
    worker1 = _service(server_dir)
    worker2 = _service(server_dir)
    v_before, section_before = worker2.read_committed_section("siem_delivery_config")
    assert section_before["enabled"] is False
    worker1.update_settings_atomic([("siem_delivery", "enabled", "true")])
    # worker2's in-memory config is stale (SQLite solo has no reload thread)...
    assert worker2.get_config().siem_delivery_config.enabled is False  # type: ignore[union-attr]
    # ...but the committed read sees the new row and a higher version.
    v_after, section_after = worker2.read_committed_section("siem_delivery_config")
    assert section_after["enabled"] is True
    assert v_after > v_before


@pytest.mark.parametrize("stored", ["[]", "not json", '"text"'])
def test_unreadable_committed_row_raises_instead_of_reading_defaults(
    server_dir: Path, stored: str
) -> None:
    """A row that is not a JSON object must never read back as an empty
    section (the dataclass defaults would silently mean 'disabled')."""
    import sqlite3

    svc = _service(server_dir)
    conn = sqlite3.connect(str(server_dir / "cidx_server.db"))
    try:
        conn.execute("UPDATE server_config SET config_json = ?", (stored,))
        conn.commit()
    finally:
        conn.close()
    with pytest.raises(RuntimeError):
        svc.read_committed_section("siem_delivery_config")


def test_read_committed_section_without_a_runtime_db_raises(tmp_path: Path) -> None:
    svc = ConfigService(server_dir_path=str(tmp_path))
    svc.load_config()
    with pytest.raises(RuntimeError):
        svc.read_committed_section("siem_delivery_config")
