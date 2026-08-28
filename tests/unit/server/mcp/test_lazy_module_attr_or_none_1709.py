"""Bug #1709: generalized safe lazy-attribute probe for code_indexer.server.app.

Bug #1693 introduced `_lazy_singleton_app_or_none()` in xray.py, scoped only
to the "app" module attribute, to stop a bare
`getattr(_utils.app_module, "app", None)` probe from permanently
constructing the process-wide app singleton as a side effect of merely
reading it (PEP 562 `__getattr__`, Bug #1638/#1678).

Bug #1709 found that several MORE module-level attribute names on
`code_indexer.server.app` -- "activated_repo_manager", "golden_repo_manager",
"background_job_manager" -- are ALSO members of that module's
`_LAZY_INIT_ATTRS` and trigger the identical construction side effect when
bare-`getattr`-probed with a `None` default across xray.py, scip.py,
repos.py, and files.py. This module adds `_utils._lazy_module_attr_or_none`,
a generalization of the same technique (real `ModuleType` -> raw
`__dict__.get()` read, bypassing `__getattr__`; Mock stand-in -> plain
`getattr()` so unittest.mock attribute interception keeps working) that
works for ANY lazy attribute name, not just "app".
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

SRC_ROOT = str(Path(__file__).parent.parent.parent.parent.parent / "src")
SUBPROCESS_TIMEOUT_SECONDS = 60

# Missing-value sentinel for the dict-restore test below -- must be an
# object identity, never a string, or a genuinely-stored string value could
# collide with it under `==` comparison.
_ABSENT = object()


def _run_and_assert_ok(code: str, env: dict) -> str:
    """Run `code` in a fresh subprocess, assert clean exit, return stdout."""
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        env=env,
    )
    assert result.returncode == 0, (
        f"Subprocess failed:\nstdout: {result.stdout}\nstderr: {result.stderr}"
    )
    return result.stdout


class TestLazyModuleAttrOrNoneRealModuleNeverConstructs:
    """Reading any lazy attr name via the helper, on the REAL app module,
    must never trigger `__getattr__` construction (proxy: no on-disk DB
    file is created -- the same proxy test_diagnostics_router_lazy_singleton
    established for this exact class of bug)."""

    @pytest.mark.parametrize(
        "attr_name",
        ["golden_repo_manager", "activated_repo_manager"],
    )
    def test_probe_lazy_attr_does_not_construct_singleton(
        self, attr_name: str, tmp_path
    ) -> None:
        fake_server_dir = tmp_path / f"cidx-server-fake-{attr_name}-probe"
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(fake_server_dir)}
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from code_indexer.server.mcp.handlers import _utils; "
            f"result = _utils._lazy_module_attr_or_none({attr_name!r}); "
            "print('result_is_none:', result is None)"
        )
        stdout = _run_and_assert_ok(code, env)
        assert "result_is_none: True" in stdout, (
            f"Bug #1709: _lazy_module_attr_or_none({attr_name!r}) must return "
            f"None WITHOUT constructing the singleton. Got: {stdout!r}"
        )

        db_path = fake_server_dir / "data" / "cidx_server.db"
        assert not db_path.exists(), (
            f"Bug #1709: probing {attr_name!r} via _lazy_module_attr_or_none() "
            f"must not construct the process-wide app singleton as a side "
            f"effect (found db at {db_path})"
        )


class TestLazyModuleAttrOrNoneMockStandInPreservesInterception:
    """A Mock stand-in for app_module (the established test-double pattern,
    see test_xray_cell_limiter.py) is not a real ModuleType -- the helper
    must fall through to plain getattr() so Mock's normal attribute
    interception keeps working."""

    def test_mock_app_module_attribute_is_returned_via_getattr(self) -> None:
        from code_indexer.server.mcp.handlers import _utils

        sentinel = object()
        mock_module = MagicMock()
        mock_module.some_lazy_attr = sentinel

        with patch.object(_utils, "app_module", mock_module):
            result = _utils._lazy_module_attr_or_none("some_lazy_attr")

        assert result is sentinel

    def test_mock_app_module_missing_attribute_returns_none(self) -> None:
        from code_indexer.server.mcp.handlers import _utils

        mock_module = MagicMock(spec=[])  # no attributes defined

        with patch.object(_utils, "app_module", mock_module):
            result = _utils._lazy_module_attr_or_none("nonexistent_attr")

        assert result is None


class TestLazyModuleAttrOrNoneReturnsGenuinelyConstructedValue:
    """Once the real singleton HAS genuinely been constructed elsewhere
    (i.e. the attribute is present in the real module's __dict__), the
    helper must return the real value via the dict lookup -- not just
    always return None."""

    def test_returns_value_once_present_in_module_dict(self) -> None:
        from code_indexer.server.mcp.handlers import _utils
        from code_indexer.server import app as real_app_module

        sentinel = object()
        original = real_app_module.__dict__.get("golden_repo_manager", _ABSENT)
        real_app_module.__dict__["golden_repo_manager"] = sentinel
        try:
            result = _utils._lazy_module_attr_or_none("golden_repo_manager")
        finally:
            if original is _ABSENT:
                real_app_module.__dict__.pop("golden_repo_manager", None)
            else:
                real_app_module.__dict__["golden_repo_manager"] = original

        assert result is sentinel


class TestLazyModuleAttrOrNoneRecoversAfterMockPatchDelattrTeardown:
    """Bug #1709 regression (found during full-suite verification, not the
    original issue text): the first "raw __dict__ read" implementation
    missed app.py's own `__getattr__` recovery fallback for the exact
    "mock.patch delattr-then-hasattr teardown" scenario ITS OWN docstring
    documents -- confirmed live via
    tests/unit/server/mcp/test_access_control_side_channels.py:274's
    `patch.object(app_module, "app", mock_app)` (no `create=True`): when
    "app" is NOT yet a literal `__dict__` key at patch time, mock.patch
    captures the original via a `getattr()` fallback (`is_local=False`),
    and its teardown calls `delattr(target, "app")` rather than restoring
    it -- leaving "app" permanently absent from `__dict__` for the rest of
    the process, even though `_initialized=True` and `_lazy_values["app"]`
    still hold the real singleton (recoverable via ordinary
    `getattr(app_module, "app", ...)`/`hasattr()`, which route through
    `__getattr__`). A raw `__dict__.get("app")` read misses this recovery
    path entirely and permanently returns `None` -- reproduced live as 3
    failing tests in test_write_mode_watch_suppression.py when the full
    tests/unit/server/mcp/ suite runs in file order after
    test_access_control_side_channels.py.

    This test exercises the REAL `unittest.mock.patch.object` mechanism
    (not a hand-simulated end state) so it stays valid even if mock's
    internal delattr-vs-restore logic ever changes.
    """

    def test_recovers_via_lazy_values_after_delattr_teardown(self, tmp_path) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from unittest.mock import Mock, patch; "
            "from code_indexer.server import app as m; "
            "assert 'app' not in vars(m)\n"
            # patch.object's OWN get_original() is the first-ever access --
            # it falls through to getattr(target, 'app', DEFAULT), which
            # constructs via __getattr__ and returns is_local=False, so
            # teardown below deletes 'app' from __dict__ rather than
            # restoring it (the exact scenario this test reproduces).
            "with patch.object(m, 'app', Mock()):\n"
            "    pass\n"
            "real_app = m._lazy_values.get('app')\n"
            "from code_indexer.server.mcp.handlers import _utils; "
            "result = _utils._lazy_module_attr_or_none('app'); "
            "print('app_in_dict_after_teardown:', 'app' in vars(m)); "
            "print('real_app_is_none:', real_app is None); "
            "print('recovered_same_object:', result is real_app); "
            "print('result_is_none:', result is None)"
        )
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(tmp_path)}
        stdout = _run_and_assert_ok(code, env)
        assert "app_in_dict_after_teardown: False" in stdout, (
            "Test precondition failed: expected mock.patch's teardown to "
            f"delete 'app' from __dict__ (is_local=False path). Got: {stdout!r}"
        )
        assert "result_is_none: False" in stdout, (
            "Bug #1709 regression: _lazy_module_attr_or_none('app') must "
            "recover the real singleton via _lazy_values after a "
            "mock.patch delattr-then-hasattr teardown sequence, not "
            f"return None. Got: {stdout!r}"
        )
        assert "recovered_same_object: True" in stdout, (
            "Bug #1709 regression: recovered value must be the SAME "
            f"already-constructed singleton, not a new one. Got: {stdout!r}"
        )


class TestXrayLazySingletonAppOrNoneDelegatesAndRecovers:
    """Code-review remediation (rejected commit 45e7fa4e, Blocker 1):
    xray.py's OLD Bug #1693 `_lazy_singleton_app_or_none()` did a raw
    `app_module.__dict__.get("app")` read with no `_lazy_values` fallback
    -- the EXACT insufficiency this issue's fix addresses for the new
    generalized helper. It must delegate to
    `_utils._lazy_module_attr_or_none("app")` so it inherits the same
    `_lazy_values` recovery after a real mock.patch delattr-teardown
    sequence (see TestLazyModuleAttrOrNoneRecoversAfterMockPatchDelattrTeardown
    above for the full scenario rationale)."""

    def test_xray_lazy_singleton_app_or_none_recovers_via_lazy_values(
        self, tmp_path
    ) -> None:
        code = (
            "import sys; "
            f"sys.path.insert(0, {SRC_ROOT!r}); "
            "from unittest.mock import Mock, patch; "
            "from code_indexer.server import app as m; "
            "assert 'app' not in vars(m)\n"
            "with patch.object(m, 'app', Mock()):\n"
            "    pass\n"
            "real_app = m._lazy_values.get('app')\n"
            "from code_indexer.server.mcp.handlers import xray; "
            "result = xray._lazy_singleton_app_or_none(); "
            "print('app_in_dict_after_teardown:', 'app' in vars(m)); "
            "print('real_app_is_none:', real_app is None); "
            "print('recovered_same_object:', result is real_app); "
            "print('result_is_none:', result is None)"
        )
        env = {**os.environ, "CIDX_SERVER_DATA_DIR": str(tmp_path)}
        stdout = _run_and_assert_ok(code, env)
        assert "app_in_dict_after_teardown: False" in stdout, (
            "Test precondition failed: expected mock.patch's teardown to "
            f"delete 'app' from __dict__ (is_local=False path). Got: {stdout!r}"
        )
        assert "result_is_none: False" in stdout, (
            "Code review Blocker 1: xray._lazy_singleton_app_or_none() "
            "must recover the real singleton via _lazy_values after a "
            "mock.patch delattr-then-hasattr teardown sequence, not "
            f"return None. Got: {stdout!r}"
        )
        assert "recovered_same_object: True" in stdout, (
            "Code review Blocker 1: recovered value must be the SAME "
            f"already-constructed singleton, not a new one. Got: {stdout!r}"
        )
