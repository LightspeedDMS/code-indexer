"""pytest fixtures for the cidx-curl.sh wrapper test suite (Story #929 Item #2a).

Placing fixtures here (conftest.py) allows pytest to discover them
automatically for all test files in tests/unit/, including the aggregator
module test_cidx_curl_wrapper.py that imports test classes from sibling files.

Note: _CFG.curl_timeout is not used here — it belongs in the _run/_run_no_curl
helper functions that actually invoke subprocesses.
"""

import os
import tempfile
from pathlib import Path

import pytest

from tests.unit.test_cidx_curl_wrapper_helpers import _CFG


@pytest.fixture
def isolated_config():
    """Yield (config_path, env) where CIDX_SERVER_DATA_DIR points to a temp dir.

    The temp dir is created fresh for each test and torn down afterwards,
    ensuring no config state leaks between tests.
    """
    with tempfile.TemporaryDirectory() as td:
        td_path = Path(td)
        config_path = td_path / "config.json"
        env = os.environ.copy()
        env["CIDX_SERVER_DATA_DIR"] = str(td_path)
        yield config_path, env


@pytest.fixture
def fake_curl_env(tmp_path, isolated_config):
    """Yield (env_with_fake, config_path) with a fake curl intercept on PATH.

    The fake curl prints each argument on a separate line as 'ARG=<arg>',
    then exits 0. Tests that need to inspect curl invocation args use this
    fixture to intercept the exec'd curl without making real network calls.
    Uses _CFG.fake_curl_shebang for the script header so the shebang is
    configurable from the central WrapperTestConfig.
    PATH is built with os.pathsep and empty segments are omitted to avoid
    implicitly adding the current working directory to the lookup path.
    """
    config_path, env = isolated_config
    fake_curl_dir = tmp_path / "fake-bin"
    fake_curl_dir.mkdir()
    fake_curl = fake_curl_dir / "curl"
    fake_curl.write_text(
        f'{_CFG.fake_curl_shebang}\nfor arg in "$@"; do echo "ARG=$arg"; done\nexit 0\n'
    )
    fake_curl.chmod(0o755)
    env_with_fake = env.copy()
    env_with_fake["PATH"] = os.pathsep.join(
        p for p in (str(fake_curl_dir), env.get("PATH", "")) if p
    )
    yield env_with_fake, config_path
