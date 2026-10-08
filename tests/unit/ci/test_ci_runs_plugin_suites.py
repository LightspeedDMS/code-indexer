"""Every plugin that ships a test suite must actually be run by CI.

The plugins under ``plugins/`` are separate distributions -- the delegated seams
cidx reaches the outside world through. Each ships its own tests, and until this
guard existed **no CI job collected any of them**: 52 tests across the git
backend and the orchestrator invoker that reported nothing, passing or failing.

This is not the usual "someone forgot to add it to the list". It could not be
noticed from a test count, because pytest's ``testpaths`` is ``["tests"]``, so a
bare ``pytest`` never descends into ``plugins/`` at all. The suites did not
fail, did not skip, and did not appear -- the same silent shape
``test_smoke_covers_fleet_invariants`` was written to close, one directory over.

So it is fixed the same way: **derive the requirement instead of maintaining
it.** A plugin directory containing ``test_*.py`` is, by that fact, a suite CI
must run. Nobody has to remember to add the next one here, and a plugin cannot
land uncollected the way these two did.

Scope: only whether CI *names* the suite. Whether it passes is the suite's own
business, and the pipeline's.
"""

from __future__ import annotations

from pathlib import Path
from typing import Set

import pytest

from tests.unit.ci.test_smoke_covers_fleet_invariants import (
    _IMAGE_JOB,
    _SMOKE_JOB,
    _pytest_test_tokens,
    _tokens_from_script,
)

_REPO = Path(__file__).resolve().parents[3]
_PLUGINS = _REPO / "plugins"
_PREFIX = ("plugins/",)


def _plugins_with_tests() -> Set[str]:
    """Plugin directories that ship at least one ``test_*.py``.

    The directory, not the file: each plugin is one distribution and CI runs it
    with that distribution's own ``PYTHONPATH``, so the directory is the unit
    that can be named in a pytest invocation.
    """
    if not _PLUGINS.is_dir():  # pragma: no cover -- repo always has plugins/
        return set()
    found = set()
    for child in sorted(_PLUGINS.iterdir()):
        if not child.is_dir() or child.name.startswith("."):
            continue
        if any(child.rglob("test_*.py")):
            found.add(str(child.relative_to(_REPO)))
    return found


def _covered() -> Set[str]:
    return _pytest_test_tokens(_SMOKE_JOB, prefixes=_PREFIX) | _pytest_test_tokens(
        _IMAGE_JOB, prefixes=_PREFIX
    )


def _is_covered(plugin: str, tokens: Set[str]) -> bool:
    """Whether some pytest argument runs this plugin's suite.

    A token equal to the directory covers it; so does a token naming a file
    inside it, which is how a plugin with one suite file may reasonably be
    listed. Trailing slashes are normalised so ``plugins/x`` and ``plugins/x/``
    are the same answer.
    """
    plugin = plugin.rstrip("/")
    for token in tokens:
        stripped = token.rstrip("/")
        if stripped == plugin or stripped.startswith(plugin + "/"):
            return True
    return False


def test_every_plugin_suite_is_named_in_a_ci_job() -> None:
    """The guard itself. Fails with the exact line to add."""
    tokens = _covered()
    uncovered = sorted(p for p in _plugins_with_tests() if not _is_covered(p, tokens))
    assert not uncovered, (
        "these plugin suites ship tests that no CI job collects, so they report "
        "nothing -- not a pass, not a failure, not a skip:\n"
        + "\n".join(f"  {p}" for p in uncovered)
        + "\n\nAdd to the `smoke` job's script, one line per plugin:\n"
        + "\n".join(f"    - PYTHONPATH={p} python -m pytest -q {p}" for p in uncovered)
    )


def test_the_repository_actually_has_plugin_suites_to_guard() -> None:
    """Stops the guard passing vacuously.

    If ``plugins/`` is renamed or emptied, the test above goes green by having
    nothing to check. That is the failure mode of every derived requirement, so
    it is asserted rather than assumed.
    """
    found = _plugins_with_tests()
    assert found, (
        "no plugin directory under plugins/ contains a test_*.py, so the guard "
        "above cannot fail -- if the layout changed, update this module"
    )


def test_a_plugin_named_only_in_a_yaml_comment_does_not_count() -> None:
    """The failure mode that bit the sibling guard: matching the raw file text.

    An earlier version of ``test_smoke_covers_fleet_invariants`` did
    ``rel in ci_file_text`` and passed on a *comment* in .gitlab-ci.yml naming
    the path. The comment block above the real invocations names both plugins,
    so a text-matching guard here would pass on the comment alone.

    Parsing the YAML is what makes that impossible, and that is what is asserted
    -- through the same code path the guard uses, not a re-implementation.
    """
    import yaml

    doc = yaml.safe_load(
        "smoke:\n"
        "  script:\n"
        "    # runs plugins/cidx_ghost under pytest\n"
        "    - echo plugins/cidx_ghost\n"
    )
    assert _tokens_from_script(doc["smoke"]["script"], prefixes=_PREFIX) == set(), (
        "a plugin named only in a YAML comment, or in a shell line that never "
        "invokes pytest, was counted as covered"
    )


def test_a_commented_out_invocation_does_not_count() -> None:
    """A script entry that is itself a shell comment runs nothing.

    This is how an invocation gets disabled in a hurry -- and it still contains
    the word pytest and the plugin path, so it is exactly the string a lenient
    parser accepts.
    """
    disabled = ["# - PYTHONPATH=plugins/cidx_ghost python -m pytest plugins/cidx_ghost"]
    assert _tokens_from_script(disabled, prefixes=_PREFIX) == set(), (
        "a commented-out pytest invocation was counted as coverage"
    )


def test_a_deselected_plugin_path_does_not_count_as_running_it() -> None:
    entries = ["python -m pytest plugins/cidx_real --deselect plugins/cidx_ghost"]
    tokens = _tokens_from_script(entries, prefixes=_PREFIX)
    assert "plugins/cidx_real" in tokens
    assert "plugins/cidx_ghost" not in tokens, (
        "--deselect names a path that must NOT run; counting it as coverage "
        "would be exactly backwards"
    )


def test_the_tests_tree_is_still_excluded_from_the_plugin_view() -> None:
    """The prefix parameter must narrow, not merely add.

    If it leaked, every tests/ path in the smoke list would read as a plugin and
    the guard would pass on unrelated coverage.
    """
    entries = ["python -m pytest tests/unit/ci/test_x.py plugins/cidx_real"]
    assert _tokens_from_script(entries, prefixes=_PREFIX) == {"plugins/cidx_real"}


@pytest.mark.parametrize(
    "token,plugin,expected",
    [
        ("plugins/cidx_x", "plugins/cidx_x", True),
        ("plugins/cidx_x/", "plugins/cidx_x", True),
        ("plugins/cidx_x/test_cidx_x.py", "plugins/cidx_x", True),
        ("plugins/cidx_x_extra", "plugins/cidx_x", False),
        ("plugins/cidx_y", "plugins/cidx_x", False),
    ],
)
def test_coverage_matching_is_by_path_boundary_not_prefix(
    token: str, plugin: str, expected: bool
) -> None:
    """``plugins/cidx_x_extra`` must not cover ``plugins/cidx_x``.

    A plain ``startswith`` says it does, which would silently excuse a plugin
    whose neighbour happens to share a name prefix.
    """
    assert _is_covered(plugin, {token}) is expected
