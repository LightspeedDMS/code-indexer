"""
The hostname/key-name grammar rejects only what can break a ``~/.ssh/config``
line; legitimate values that cannot break one are accepted.

Proven here at the grammar layer (``ssh_input_validation.py``) and
propagated (via the shared functions) to every entry point:

  1. Key names such as ``user@host``, ``work+github``, ``clé``, and
     ``.hidden`` are accepted.
  2. Hosts with ``_`` or a trailing ``.`` (``my_host.internal``,
     ``github.com.``) are accepted.
  3. (Covered in test_ssh_key_manager_legacy_visibility_regression.py):
     legacy stored keys/hosts that still fail the grammar are never
     silently dropped -- logged once per key, at the right severity.

Discriminating RED: every "widened acceptance" case below is a value a
strict ASCII allow-list would reject. Every "still rejected" case is a value
that can break a config line or trigger OpenSSH expansion, so accepting the
widened set never admits one of those.
"""

from __future__ import annotations

import shutil
import subprocess

import pytest

from code_indexer.server.services.ssh_config_manager import (
    HostEntry,
    ParsedConfig,
    SSHConfigManager,
)
from code_indexer.server.services.ssh_input_validation import (
    InvalidHostnameError,
    is_valid_hostname,
    is_valid_key_name,
    validate_hostname,
)
from code_indexer.server.services.ssh_key_generator import (
    InvalidKeyNameError,
    SSHKeyGenerator,
)
from code_indexer.server.services.ssh_key_manager import SSHKeyManager

# ---------------------------------------------------------------------------
# Widened key-name acceptance (Regression #1)
# ---------------------------------------------------------------------------

WIDENED_KEY_NAMES = [
    "user@host",
    "work+github",
    "clé",
    ".hidden",
    "hash#1",  # '#' does not start a comment mid-token in an
    # absolute IdentityFile path -- confirmed via `ssh -G`.
    "deploy\\key",  # a single backslash is kept verbatim in an unquoted
    # IdentityFile value -- confirmed via `ssh -G`.
]

STILL_REJECTED_KEY_NAMES = [
    "key\nIdentitiesOnly yes",  # newline
    "deploy key",  # bare space
    "deploy%key",  # OpenSSH %-token
    "deploy;key",
    "deploy'key",
    'deploy"key',
    "deploy${HOME}",  # OpenSSH environment-variable expansion
    "deploy${UNDEFINED_VAR_XYZ}",
    "../other",
    "..",
    "a..b",  # any '..' substring, not only an exact ".." name
    "-flag-like",
    "a/b",
    "",
    "a" * 256,
]


@pytest.mark.parametrize("key_name", WIDENED_KEY_NAMES)
def test_is_valid_key_name_accepts_widened_values(key_name: str) -> None:
    assert is_valid_key_name(key_name) is True


@pytest.mark.parametrize("key_name", STILL_REJECTED_KEY_NAMES)
def test_is_valid_key_name_still_rejects_config_line_breaking_characters(
    key_name: str,
) -> None:
    assert is_valid_key_name(key_name) is False


def test_key_name_leading_dot_containment_still_holds(tmp_path) -> None:
    """A leading '.' is only safe because the resulting path is still a
    direct, contained child of ssh_dir -- prove that explicitly, not just
    that the grammar accepts the string."""
    key_name = ".hidden"
    resolved = (tmp_path / key_name).resolve()
    assert resolved.parent == tmp_path.resolve()
    assert is_valid_key_name(key_name) is True


# ---------------------------------------------------------------------------
# Widened hostname acceptance (Regression #2)
# ---------------------------------------------------------------------------

WIDENED_HOSTNAMES = [
    "my_host.internal",
    "github.com.",
    "_leading.example.com",  # '_' allowed anywhere in a label
    "trailing_.example.com",
    "-example.com",  # never reaches a raw ssh command line in this
    # codebase (only ~/.ssh/config values + PG storage); confirmed safe as
    # a HostName VALUE via `ssh -G` (resolves cleanly, no directive change).
]

STILL_REJECTED_HOSTNAMES = [
    "example.com\nHost other",
    "example.com\r\nHost other",
    "example.com IdentitiesOnly=yes",
    "example.com%h",
    "example.com;other",
    "example.com,example.org",
    'example.com"',
    "example.com'",
    "*.example.com",
    "example.com!",
    "example.com?",
    "",
    "." * 260,
    "example.com..",  # more than one trailing dot must still be rejected
    "example.com${HOME}",  # '$'/'{' never legitimate in a hostname
]


@pytest.mark.parametrize("hostname", WIDENED_HOSTNAMES)
def test_is_valid_hostname_accepts_widened_values(hostname: str) -> None:
    assert is_valid_hostname(hostname) is True


@pytest.mark.parametrize("hostname", STILL_REJECTED_HOSTNAMES)
def test_is_valid_hostname_still_rejects_config_line_breaking_characters(
    hostname: str,
) -> None:
    assert is_valid_hostname(hostname) is False


def test_validate_hostname_accepts_trailing_dot_without_raising() -> None:
    validate_hostname("github.com.")  # must not raise


def test_validate_hostname_accepts_underscore_without_raising() -> None:
    validate_hostname("my_host.internal")  # must not raise


def test_validate_hostname_still_raises_on_double_trailing_dot() -> None:
    with pytest.raises(InvalidHostnameError):
        validate_hostname("github.com..")


# ---------------------------------------------------------------------------
# Propagation: SSHKeyGenerator (key-name entry point)
# ---------------------------------------------------------------------------


@pytest.fixture
def generator(tmp_path):
    return SSHKeyGenerator(ssh_dir=tmp_path / "ssh")


@pytest.mark.parametrize("key_name", WIDENED_KEY_NAMES)
def test_generator_validate_key_name_accepts_widened_values(generator, key_name):
    generator._validate_key_name(key_name)  # must not raise


@pytest.mark.parametrize("key_name", STILL_REJECTED_KEY_NAMES)
def test_generator_validate_key_name_still_rejects_config_line_breaking_characters(
    generator, key_name
):
    with pytest.raises(InvalidKeyNameError):
        generator._validate_key_name(key_name)


# ---------------------------------------------------------------------------
# Propagation: SSHKeyManager.assign_key_to_host (hostname entry point)
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path):
    """Real SSHKeyManager, JSON metadata storage, entirely confined to
    tmp_path -- mirrors test_ssh_key_manager_hostname_validation.py's
    fixture (no SQLite backend, which requires migrations this test does not
    run)."""
    return SSHKeyManager(
        ssh_dir=tmp_path / "ssh",
        metadata_dir=tmp_path / "metadata",
        config_path=tmp_path / "ssh" / "config",
    )


@pytest.mark.parametrize("hostname", WIDENED_HOSTNAMES)
def test_manager_assign_key_to_host_accepts_widened_hostname(manager, hostname):
    manager.create_key("widened-hostname-key")
    metadata = manager.assign_key_to_host("widened-hostname-key", hostname)
    assert hostname in metadata.hosts

    content = manager.config_path.read_text()
    assert f"HostName {hostname}" in content


@pytest.mark.parametrize("hostname", STILL_REJECTED_HOSTNAMES)
def test_manager_assign_key_to_host_still_rejects_config_line_breaking_hostname(
    manager, hostname
):
    manager.create_key("rejected-hostname-key")
    with pytest.raises(InvalidHostnameError):
        manager.assign_key_to_host("rejected-hostname-key", hostname)


@pytest.mark.parametrize("key_name", WIDENED_KEY_NAMES)
def test_manager_create_key_accepts_widened_key_name(manager, key_name):
    metadata = manager.create_key(key_name)
    assert metadata.name == key_name


# ---------------------------------------------------------------------------
# ~/.ssh/config write + parse-back safety proof: a widened name/host must
# still produce a config that OpenSSH parses as ONE Host block with NO extra
# directives -- widening the grammar must not reintroduce a config-line hazard.
# ---------------------------------------------------------------------------


def _write_single_entry_config(tmp_path, host: str, hostname: str, key_path: str):
    config_manager = SSHConfigManager()
    config_path = tmp_path / "config"
    config_manager.write_config(
        config_path,
        ParsedConfig(),
        [HostEntry(host=host, hostname=hostname, key_path=key_path)],
    )
    return config_path


@pytest.mark.parametrize("hostname", WIDENED_HOSTNAMES)
def test_widened_hostname_round_trips_safely_through_config_manager(tmp_path, hostname):
    key_path = str(tmp_path / "widened_host_key")
    config_path = _write_single_entry_config(tmp_path, hostname, hostname, key_path)
    content = config_path.read_text()

    # Exactly one Host block, no extra directive lines.
    assert content.count("Host ") == 1
    assert content.count("HostName ") == 1
    assert "ProxyCommand" not in content
    assert "LocalCommand" not in content
    assert f"HostName {hostname}\n" in content


@pytest.mark.skipif(shutil.which("ssh") is None, reason="ssh binary not available")
@pytest.mark.parametrize("hostname", WIDENED_HOSTNAMES)
def test_widened_hostname_parses_back_cleanly_via_ssh_dash_g(tmp_path, hostname):
    """Parse the generated config back with the real OpenSSH client
    (``ssh -G -F <config> <host>``) and prove no ProxyCommand/LocalCommand
    directive was introduced -- the actual mechanism a newline in a config
    line can trigger.

    Looks up a fixed, safe Host PATTERN ("lookup-target") rather than
    ``hostname`` itself: a value starting with '-' (e.g. "-example.com") is
    safe as a config-file VALUE (that is what this test proves), but ssh's
    OWN command-line argument parser misinterprets a bare leading-hyphen
    argument as a flag -- an artifact of invoking the ssh binary directly,
    never a hazard in the config file. Production never passes a hostname
    as a raw ssh command-line argument (only into config-file values and
    PG storage), so this keeps the test's own invocation from tripping over
    a concern the code under test does not have.
    """
    key_path = str(tmp_path / "widened_host_key")
    config_path = _write_single_entry_config(
        tmp_path, "lookup-target", hostname, key_path
    )

    result = subprocess.run(
        ["ssh", "-G", "-F", str(config_path), "lookup-target"],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    resolved_lines = {
        line.split(None, 1)[0]: line.split(None, 1)[1] if " " in line else ""
        for line in result.stdout.splitlines()
        if line.strip()
    }
    assert "proxycommand" not in resolved_lines
    assert "localcommand" not in resolved_lines
    assert resolved_lines.get("hostname") == hostname


@pytest.mark.parametrize("key_name", WIDENED_KEY_NAMES)
def test_widened_key_name_round_trips_safely_through_config_manager(tmp_path, key_name):
    key_path = str(tmp_path / key_name)
    config_path = _write_single_entry_config(
        tmp_path, "example.com", "example.com", key_path
    )
    content = config_path.read_text()

    assert content.count("IdentityFile ") == 1
    assert "ProxyCommand" not in content
    assert f"IdentityFile {key_path}\n" in content


# ---------------------------------------------------------------------------
# Backslash in a key name: the name is a bare filename, ssh-keygen receives
# its path as a separate argv element (no shell), and OpenSSH keeps a single
# backslash verbatim in an unquoted IdentityFile value.
# ---------------------------------------------------------------------------

BACKSLASH_KEY_NAME = "deploy\\key"


def _ssh_dash_g(config_path, target: str) -> dict:
    result = subprocess.run(
        ["ssh", "-G", "-F", str(config_path), target],
        capture_output=True,
        text=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    resolved: dict = {}
    for line in result.stdout.splitlines():
        parts = line.split(None, 1)
        if len(parts) == 2:
            resolved.setdefault(parts[0], []).append(parts[1])
    return resolved


@pytest.mark.skipif(
    shutil.which("ssh") is None or shutil.which("ssh-keygen") is None,
    reason="ssh/ssh-keygen binaries not available",
)
def test_backslash_key_name_generates_lists_maps_and_deletes(tmp_path) -> None:
    """A key named ``deploy\\key`` is generated, listed as managed, mapped
    in ~/.ssh/config with an IdentityFile that OpenSSH resolves to the real
    key file, and deleted cleanly."""
    ssh_dir = tmp_path / "ssh"
    config_path = ssh_dir / "config"
    manager = SSHKeyManager(
        ssh_dir=ssh_dir,
        metadata_dir=tmp_path / "metadata",
        config_path=config_path,
    )
    private_path = ssh_dir / BACKSLASH_KEY_NAME

    manager.create_key(BACKSLASH_KEY_NAME)
    assert private_path.is_file()
    assert (ssh_dir / f"{BACKSLASH_KEY_NAME}.pub").is_file()

    managed_names = [key.name for key in manager.list_keys().managed]
    assert BACKSLASH_KEY_NAME in managed_names

    manager.assign_key_to_host(BACKSLASH_KEY_NAME, "example.com")
    assert f"IdentityFile {private_path}\n" in config_path.read_text()
    resolved = _ssh_dash_g(config_path, "example.com")
    assert resolved["identityfile"] == [str(private_path)]

    assert manager.delete_key(BACKSLASH_KEY_NAME) is True
    assert not private_path.exists()
    assert not (ssh_dir / f"{BACKSLASH_KEY_NAME}.pub").exists()
    managed_names = [key.name for key in manager.list_keys().managed]
    assert BACKSLASH_KEY_NAME not in managed_names
    assert "IdentityFile" not in config_path.read_text()
