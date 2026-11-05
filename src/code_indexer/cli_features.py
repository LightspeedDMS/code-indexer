"""Feature add-on CLI commands (``cli_commands()``), found only when needed.

The ``cidx`` group (``cli.ModeAwareGroup``) asks this module ONLY when a
command name is not a built-in one, and when listing commands for help, so
no built-in command pays anything for features: nothing here is imported on
the normal path (the CLI startup import budget).

Which features: every INSTALLED one (entry points in ``code_indexer.features``),
independent of the server's ``enabled_features``. A feature's CLI commands work
on the user's own machine and need no server, so a CLI-only user keeps
``cidx xray`` as long as the cidx-xray distribution is installed, and
uninstalling it removes the command. (The SERVER side of a feature -- tools,
routes, pages, migrations -- still requires the name in ``enabled_features``.)
Each feature is loaded and validated on its own by the server's loader
(``load_cli_commands``: the manifest and ``cli_commands()`` only, the same
refusals), so one refused add-on hides nothing else; a feature's
``on_startup`` never runs -- a command gets no ``FeatureContext``. Loading imports
``code_indexer.server``, whose import installs the kernel capabilities a
command reaches (runtime config, telemetry, the content seam).
"""

from __future__ import annotations

import sys
from typing import Dict, List, Optional

import click

#: ``cidx_sdk.FEATURES_ENTRY_POINT_GROUP``, spelled here so the no-feature
#: path imports neither the SDK nor the server (the CLI startup budget); a
#: test pins the two equal.
FEATURES_GROUP = "code_indexer.features"

_commands: Optional[Dict[str, click.Command]] = None
#: Installed features refused for the CLI: feature name -> why.
_refusals: Dict[str, str] = {}


def feature_commands() -> Dict[str, click.Command]:
    """Subcommand name -> click command, for every installed feature that
    loads. Loaded once per process; refusals are kept in :data:`_refusals`.

    Raises:
        click.ClickException: a contribution is not a click command, or two
            features register the same command name.
    """
    global _commands
    if _commands is not None:
        return _commands
    if not _any_feature_installed():
        _commands = {}
        return _commands
    from cidx_sdk import KNOWN_CAPABILITIES

    from code_indexer.server.features.loader import (
        discover_installed_features,
        load_cli_commands,
    )

    try:
        candidates = discover_installed_features()
    except Exception as exc:  # two distributions claim one name
        raise click.ClickException(f"feature add-ons: {exc}") from exc
    by_feature, refusals = load_cli_commands(
        candidates,
        # The server provides every capability; a command never gets one.
        provided_capabilities=KNOWN_CAPABILITIES,
    )
    found: Dict[str, click.Command] = {}
    for feature, contributions in by_feature.items():
        for contribution in contributions:
            command = contribution.command
            if not isinstance(command, click.Command):
                raise click.ClickException(
                    f"feature {feature!r} command {contribution.name!r} "
                    f"is a {type(command).__name__}, not a click command"
                )
            if contribution.name in found:
                raise click.ClickException(
                    f"feature command {contribution.name!r} is registered twice"
                )
            found[contribution.name] = command
    _refusals.clear()
    _refusals.update(refusals)
    _commands = found
    return _commands


def _any_feature_installed() -> bool:
    """Whether any feature entry point exists, read from distribution
    metadata (and a source-tree run's SDK registrations, which exist only if
    the SDK resolver is already loaded) without importing the SDK or the
    server."""
    from importlib.metadata import entry_points

    if list(entry_points(group=FEATURES_GROUP)):
        return True
    resolver = sys.modules.get("cidx_sdk.seams.resolver")
    extras = getattr(resolver, "extra_entry_points", None)
    return bool(extras is not None and extras().get(FEATURES_GROUP))


def _refusal_message() -> str:
    return "feature add-ons refused: " + "; ".join(
        f"{name}: {why}" for name, why in sorted(_refusals.items())
    )


def feature_command(name: str) -> Optional[click.Command]:
    """The installed feature's command *name*, or None. A name no feature
    provides while an installed add-on was refused fails loud (the command
    may be that add-on's)."""
    command = feature_commands().get(name)
    if command is None and _refusals:
        raise click.ClickException(_refusal_message())
    return command


def listed_feature_commands(builtin: List[str]) -> List[str]:
    """Feature command names for help output, minus any shadowed by a
    built-in. A refused feature is reported on stderr, not raised: help
    must stay usable."""
    try:
        names = sorted(feature_commands())
    except click.ClickException as exc:
        print(f"Warning: {exc.message}", file=sys.stderr)
        return []
    if _refusals:
        print(f"Warning: {_refusal_message()}", file=sys.stderr)
    return [name for name in names if name not in builtin]


def reset_feature_commands() -> None:
    """Forget the loaded commands (tests)."""
    global _commands
    _commands = None
    _refusals.clear()
