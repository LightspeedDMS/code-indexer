"""
Discriminating test for the P2 review finding on #1894/#1895: both
config-validation paths (`CommandModeDetector._validate_local_config()` and
`RefreshScheduler._is_local_config_valid()`) catch
`(json.JSONDecodeError, FileNotFoundError, PermissionError)` /
`(json.JSONDecodeError, OSError)` but NOT `UnicodeDecodeError`. A
partially-corrupt, non-UTF-8 config.json raises `UnicodeDecodeError` from
`open(...).read()` inside `json.load()`, which is a `ValueError` subclass,
NOT a subclass of any of the caught exception types -- so it propagates
uncaught instead of being treated as "invalid config".

For CommandModeDetector, that crashes `detect_mode()` (and with it every
CLI command, including the `cidx init --force` repair self-heal) instead of
gracefully falling back to "uninitialized".

This test fails on the BEHAVIOUR (an uncaught UnicodeDecodeError escapes
detect_mode()) before the fix, not on a missing symbol.
"""

from pathlib import Path

from code_indexer.mode_detection.command_mode_detector import CommandModeDetector


def test_detect_mode_treats_non_utf8_config_as_invalid_not_a_crash(
    tmp_path: Path,
) -> None:
    """A non-UTF-8 config.json must make detect_mode() gracefully report
    "uninitialized" (config is invalid), not raise an uncaught
    UnicodeDecodeError that crashes every CLI command."""
    project_root = tmp_path / "proj"
    config_dir = project_root / ".code-indexer"
    config_dir.mkdir(parents=True)
    config_path = config_dir / "config.json"
    # 0xFF/0xFE is not valid UTF-8 in this position -- triggers
    # UnicodeDecodeError when read in default text mode.
    config_path.write_bytes(b'{"codebase_dir": "\xff\xfe broken"}')

    detector = CommandModeDetector(project_root)

    mode = detector.detect_mode()

    assert mode == "uninitialized", (
        f"expected detect_mode() to gracefully treat a non-UTF-8 config as "
        f"invalid ('uninitialized'), but got {mode!r} (or it raised instead "
        "of returning, meaning UnicodeDecodeError escaped uncaught)"
    )
