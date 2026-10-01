"""
Key-name grammar enforcement at creation.

Discriminating RED: a blocklist-only ``SSHKeyGenerator._validate_key_name``
(``/``, ``..``, ``;``, leading ``-``) would let a key name containing a
space, a newline, or ``%`` into the ``IdentityFile`` line written into
``~/.ssh/config`` verbatim. These tests
fail against that blocklist and pass once ``_validate_key_name`` delegates to
the strict ``ssh_input_validation.is_valid_key_name`` grammar.
"""

from __future__ import annotations

import pytest

from code_indexer.server.services.ssh_key_generator import (
    InvalidKeyNameError,
    SSHKeyGenerator,
)


@pytest.fixture
def generator(tmp_path):
    return SSHKeyGenerator(ssh_dir=tmp_path / "ssh")


@pytest.mark.parametrize(
    "key_name",
    [
        "deploy key\nHost other",  # newline breaks the config line
        "deploy key",  # bare space -- old blocklist allowed this
        "deploy%key",  # OpenSSH %-token expansion
        "deploy'key",
        'deploy"key',
        "a" * 256,  # over length
    ],
)
def test_validate_key_name_rejects_config_line_breaking_characters(generator, key_name):
    with pytest.raises(InvalidKeyNameError):
        generator._validate_key_name(key_name)


@pytest.mark.parametrize(
    "key_name",
    [
        "deploy-key_1.v2",
        "GitLab",
        "assign_test_key",
        "solo_assign_key",
        # A leading '.' is safe (contained under
        # ssh_dir) -- see
        # tests/unit/server/services/test_ssh_config_grammar_widening_regression.py.
        ".hidden",
    ],
)
def test_validate_key_name_accepts_legitimate_values(generator, key_name):
    generator._validate_key_name(key_name)  # must not raise


def test_validate_key_name_still_rejects_path_traversal(generator):
    with pytest.raises(InvalidKeyNameError):
        generator._validate_key_name("../other")


def test_validate_key_name_still_rejects_leading_dash(generator):
    with pytest.raises(InvalidKeyNameError):
        generator._validate_key_name("-rf")
