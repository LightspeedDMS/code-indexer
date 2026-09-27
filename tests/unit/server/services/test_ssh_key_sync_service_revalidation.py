"""
SSHKeySyncService row validation for ~/.ssh/config regeneration.

``_sync_ssh_config`` must apply the SAME config-line validation as the
manager to rows read from the shared backend, and skip+log any row that
fails it -- so a legacy row (written before this grammar existed, or by a
node that has not yet upgraded) cannot corrupt every other node's
``~/.ssh/config`` the next time this service runs at startup.

Discriminating RED: without row validation, a hostname or key name
containing a config-line-breaking character (e.g. a newline) would be
written straight into the local ``~/.ssh/config``. These tests
fail on that actual behavior (unsafe content landing in the config file),
not on import errors.

Trailing-LF/CR and
scoped-IPv6 cases are covered at this layer too, mirroring the two classes of
input handled in ``ssh_input_validation.py`` (``re.match`` + ``$``
accepts a trailing newline; ``ipaddress.ip_address()`` accepts a
scoped IPv6 zone id, letting OpenSSH's ``%h``/``%n`` tokens through).

``_write_key_file`` does not apply the config-line
grammar -- writing a key FILE is a different surface from writing a config
line. It applies a containment check instead (the resolved path must stay
a direct child of ``ssh_dir``), so a name that merely contains a
config-line-breaking character (e.g. a newline, with no path separator) is
still written to disk, while a name that would escape ``ssh_dir`` (e.g. an
absolute path) is refused.
"""

from __future__ import annotations

import logging
from pathlib import Path
from unittest.mock import MagicMock

TEST_BACKEND_IDENTITY = "ssh-sync-backend-identity"

HOSTNAME_WITH_NEWLINE = "example.com\nHost other"
KEY_NAME_WITH_NEWLINE = "key\nHost other"


def _make_backend(keys: list) -> MagicMock:
    backend = MagicMock()
    backend.list_keys.return_value = keys
    return backend


def _key_data(
    name: str,
    private_key: str = "PRIVATE_KEY_CONTENT",
    public_key: str = "ssh-ed25519 AAAA comment",
    hosts: list | None = None,
) -> dict:
    return {
        "name": name,
        "private_key": private_key,
        "public_key": public_key,
        "fingerprint": f"SHA256:fake_{name}",
        "key_type": "ed25519",
        "hosts": list(hosts) if hosts else [],
    }


def _make_service(backend, ssh_dir: Path):
    from code_indexer.server.services.ssh_key_sync_service import SSHKeySyncService

    return SSHKeySyncService(
        ssh_keys_backend=backend,
        ssh_dir=str(ssh_dir),
        backend_identity=TEST_BACKEND_IDENTITY,
    )


def test_sync_excludes_hostname_with_newline_from_ssh_config(
    tmp_path: Path, caplog
) -> None:
    """A legitimate key with a hostname that breaks out of a config line
    must never get a Host block written for that hostname -- the key file
    itself is still fine to write (the unsafe value lives only in `hosts`,
    not in `name`)."""
    backend = _make_backend([_key_data("deploy_key", hosts=[HOSTNAME_WITH_NEWLINE])])
    svc = _make_service(backend, tmp_path)

    with caplog.at_level(logging.WARNING):
        result = svc.sync()

    config_path = tmp_path / "config"
    if config_path.exists():
        content = config_path.read_text()
        assert "ProxyCommand" not in content
        assert HOSTNAME_WITH_NEWLINE not in content
    assert result["errors"], "expected the skip to be surfaced in errors"
    # The log must never contain the raw, literal newline -- only an
    # escaped (repr-style) representation such as '...\\nHost other...'.
    for record in caplog.records:
        assert "\n" not in record.getMessage()


def test_sync_excludes_key_name_with_newline_from_ssh_config(
    tmp_path: Path, caplog
) -> None:
    """A key NAME that breaks out of a config line must not appear as a
    Host block, and must not be reported as synced by the config-writing
    path (its key FILE may still legitimately be written -- see
    test_write_key_file_writes_file_for_name_with_config_breaking_character)."""
    backend = _make_backend([_key_data(KEY_NAME_WITH_NEWLINE, hosts=["github.com"])])
    svc = _make_service(backend, tmp_path)

    with caplog.at_level(logging.WARNING):
        svc.sync()

    config_path = tmp_path / "config"
    if config_path.exists():
        assert "ProxyCommand" not in config_path.read_text()
        assert KEY_NAME_WITH_NEWLINE not in config_path.read_text()


def test_write_key_file_writes_file_for_name_with_config_breaking_character(
    tmp_path: Path,
) -> None:
    """Writing a key FILE is not a config-line surface. A name containing a
    newline (no path separator) still resolves to a direct child of
    ssh_dir, so it is written. The config-line grammar (tested above) is
    what keeps it out of ~/.ssh/config, not this method."""
    backend = _make_backend([])
    svc = _make_service(backend, tmp_path)

    result = svc._write_key_file(KEY_NAME_WITH_NEWLINE, "PRIVATE", "PUBLIC")

    assert result is True
    written_names = {entry.name for entry in tmp_path.iterdir()}
    assert KEY_NAME_WITH_NEWLINE in written_names
    assert f"{KEY_NAME_WITH_NEWLINE}.pub" in written_names


def test_write_key_file_refuses_name_that_escapes_ssh_dir(tmp_path: Path) -> None:
    """The containment check this method now applies in place of the
    config-line grammar: a name that resolves outside ssh_dir (here, an
    absolute path -- ``Path.__truediv__`` discards the left operand when
    the right operand is itself absolute) must never be written."""
    backend = _make_backend([])
    svc = _make_service(backend, tmp_path)
    escaping_name = str(tmp_path.parent / "escaped_key")

    result = svc._write_key_file(escaping_name, "PRIVATE", "PUBLIC")

    assert result is False
    assert not Path(escaping_name).exists()
    assert not Path(f"{escaping_name}.pub").exists()


def test_write_key_file_refuses_cleanly_on_nul_byte(tmp_path: Path) -> None:
    """A NUL byte would otherwise raise inside Path.resolve() -- this
    method must refuse cleanly (False + WARNING) instead."""
    backend = _make_backend([])
    svc = _make_service(backend, tmp_path)

    result = svc._write_key_file("key\x00name", "PRIVATE", "PUBLIC")

    assert result is False
    assert list(tmp_path.iterdir()) == []


def test_write_key_file_refuses_cleanly_on_dot_dot_path_component(
    tmp_path: Path,
) -> None:
    """A name containing a '..' path component (e.g. "a/../b") may resolve
    back inside ssh_dir, so plain containment alone would not catch it --
    this method must refuse it explicitly regardless."""
    backend = _make_backend([])
    svc = _make_service(backend, tmp_path)

    result = svc._write_key_file("a/../b", "PRIVATE", "PUBLIC")

    assert result is False
    assert not (tmp_path / "b").exists()


def test_config_exclusion_does_not_block_legitimate_rows_in_the_same_sync(
    tmp_path: Path,
) -> None:
    """The regression this guards against: one row that fails the
    config-line grammar must not abort the ENTIRE ~/.ssh/config write and
    silently drop every OTHER legitimate key's Host block too."""
    backend = _make_backend(
        [
            _key_data("good_key", hosts=["github.com"]),
            _key_data("deploy_key", hosts=[HOSTNAME_WITH_NEWLINE]),
        ]
    )
    svc = _make_service(backend, tmp_path)

    svc.sync()

    config_path = tmp_path / "config"
    assert config_path.exists(), (
        "a row that fails the config-line grammar must not prevent "
        "legitimate rows from being synced"
    )
    content = config_path.read_text()
    assert "Host github.com" in content
    assert "ProxyCommand" not in content


def test_sync_still_writes_legitimate_keys_and_hosts(tmp_path: Path) -> None:
    backend = _make_backend(
        [_key_data("deploy-key_1.v2", hosts=["gitlab.example.com", "192.0.2.10"])]
    )
    svc = _make_service(backend, tmp_path)

    result = svc.sync()

    assert result["written"] == ["deploy-key_1.v2"]
    assert (tmp_path / "deploy-key_1.v2").exists()
    assert (tmp_path / "deploy-key_1.v2.pub").exists()
    content = (tmp_path / "config").read_text()
    assert "Host gitlab.example.com" in content
    assert "Host 192.0.2.10" in content
    assert "ProxyCommand" not in content


# ---------------------------------------------------------------------------
# Trailing-newline and scoped-IPv6 cases at the sync-service layer too.
# ---------------------------------------------------------------------------


def test_sync_excludes_hostname_with_trailing_newline(tmp_path: Path) -> None:
    backend = _make_backend(
        [
            _key_data("good_key", hosts=["github.com"]),
            _key_data("deploy_key", hosts=["example.com\n"]),
        ]
    )
    svc = _make_service(backend, tmp_path)

    svc.sync()

    content = (tmp_path / "config").read_text()
    # Only the legitimate key's Host block may appear -- the hostname that
    # breaks out of its config line must not produce a second (mangled)
    # Host block at all.
    assert content.count("Host ") == 1
    assert "example.com" not in content


def test_sync_excludes_hostname_with_trailing_carriage_return(tmp_path: Path) -> None:
    backend = _make_backend([_key_data("deploy_key", hosts=["example.com\r"])])
    svc = _make_service(backend, tmp_path)

    svc.sync()

    config_path = tmp_path / "config"
    if config_path.exists():
        assert "\r" not in config_path.read_text()


def test_sync_excludes_key_name_with_trailing_newline_from_ssh_config(
    tmp_path: Path,
) -> None:
    backend = _make_backend([_key_data("deploy_key\n", hosts=["github.com"])])
    svc = _make_service(backend, tmp_path)

    svc.sync()

    config_path = tmp_path / "config"
    if config_path.exists():
        assert "deploy_key\n" not in config_path.read_text()


def test_sync_excludes_scoped_ipv6_hostname(tmp_path: Path) -> None:
    """fe80::1%h must never be treated as a "valid IPv6 literal" -- %h is an
    OpenSSH token-expansion sequence, not a legitimate zone id here."""
    backend = _make_backend(
        [
            _key_data("good_key", hosts=["github.com"]),
            _key_data("deploy_key", hosts=["fe80::1%h"]),
        ]
    )
    svc = _make_service(backend, tmp_path)

    svc.sync()

    content = (tmp_path / "config").read_text()
    assert "Host github.com" in content
    assert "%h" not in content


# ---------------------------------------------------------------------------
# sync() loop: _write_key_file is the single gate for a row's key files. A
# row it refuses is skipped on its own -- sync() still returns normally,
# writes every other row, updates the manifest and generates the config --
# and a refused name is never recorded in `written` or in the manifest.
# ---------------------------------------------------------------------------

REFUSED_ROW_FINGERPRINT = "SHA256:refused-row-fingerprint"
GOOD_KEY_NAME = "good-key"


def _assert_refused_row_skipped_alone(
    tmp_path: Path, refused_name: str, result: dict
) -> None:
    import json

    assert refused_name not in result["written"]
    assert result["written"] == [GOOD_KEY_NAME]
    assert (tmp_path / GOOD_KEY_NAME).exists()
    assert (tmp_path / f"{GOOD_KEY_NAME}.pub").exists()

    # The refusal is reported by the row's fingerprint, never its raw name.
    assert any(REFUSED_ROW_FINGERPRINT in error for error in result["errors"])

    manifest_path = tmp_path / ".cidx-ssh-keys.json"
    assert manifest_path.exists(), "the manifest update must still run"
    manifest = json.loads(manifest_path.read_text())
    managed = manifest["backends"][TEST_BACKEND_IDENTITY]
    assert managed == [GOOD_KEY_NAME]

    config_path = tmp_path / "config"
    assert config_path.exists(), "the config write must still run"
    content = config_path.read_text()
    assert "Host gitlab.example.com" in content
    assert "Host github.com" not in content


def _backend_with_refused_row(refused_name: str) -> MagicMock:
    refused_row = _key_data(refused_name, hosts=["github.com"])
    refused_row["fingerprint"] = REFUSED_ROW_FINGERPRINT
    return _make_backend(
        [refused_row, _key_data(GOOD_KEY_NAME, hosts=["gitlab.example.com"])]
    )


def test_sync_skips_nul_byte_named_row_alone(tmp_path: Path) -> None:
    """A row whose name contains a NUL byte is refused by _write_key_file
    and skipped on its own: sync() returns normally, the name is absent
    from `written` and from the manifest, and every other row is still
    written and mapped in the config."""
    refused_name = "bad\x00name"
    svc = _make_service(_backend_with_refused_row(refused_name), tmp_path)

    result = svc.sync()

    _assert_refused_row_skipped_alone(tmp_path, refused_name, result)


def test_sync_skips_dot_dot_named_row_alone(tmp_path: Path) -> None:
    """A row whose name has a '..' path component is refused by
    _write_key_file, so it is never written and never recorded: `written`
    and the manifest list only names whose files this service wrote."""
    refused_name = "sub/../k2"
    svc = _make_service(_backend_with_refused_row(refused_name), tmp_path)

    result = svc.sync()

    assert not (tmp_path / "k2").exists()
    _assert_refused_row_skipped_alone(tmp_path, refused_name, result)
