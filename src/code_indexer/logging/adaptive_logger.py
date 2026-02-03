"""Adaptive logging for image extraction - Story #64 AC5-AC6.

Provides context-aware logging that adapts output format based on execution environment:
- CLI mode: Human-readable warnings to stderr (verbose mode only)
- Server mode: Structured JSON logging via Python logging module
"""

import json
import logging
import os
import sys


class AdaptiveLogger:
    """Logger that adapts output based on execution context."""

    def __init__(self):
        """Initialize adaptive logger and detect context."""
        self._context = self._detect_context()
        self._py_logger = logging.getLogger("cidx.image_extractor")

    def _detect_context(self) -> str:
        """Detect if running in CLI or server mode.

        Returns:
            "cli" or "server"

        Detection logic:
        1. Check if running under uvicorn (sys.argv contains 'uvicorn')
        2. Check for FASTAPI_APP environment variable
        3. Default to "cli"
        """
        # Check sys.argv for uvicorn
        if "uvicorn" in " ".join(sys.argv):
            return "server"

        # Check environment variable
        if os.environ.get("FASTAPI_APP") == "true":
            return "server"

        # Default to CLI
        return "cli"

    def warn_image_skipped(
        self, file_path: str, image_ref: str, reason: str, verbose: bool = False
    ) -> None:
        """Log image skip warning in appropriate format.

        Args:
            file_path: Path to the file containing the image reference
            image_ref: The image reference that was skipped
            reason: Human-readable reason for skipping
            verbose: In CLI mode, only log if verbose=True. Ignored in server mode.

        Formats:
            CLI (verbose=True):
                [WARN] Skipping image in docs/article.md:
                  Image: images/missing.png
                  Reason: File not found

            Server (always logs):
                {"level": "warning", "event": "image_skipped", "file_path": "...",
                 "image_ref": "...", "reason": "..."}
        """
        if self._context == "cli":
            self._log_to_console(file_path, image_ref, reason, verbose)
        else:
            self._log_to_central(file_path, image_ref, reason)

    def _log_to_console(
        self, file_path: str, image_ref: str, reason: str, verbose: bool
    ) -> None:
        """Log to console in human-readable format (CLI mode).

        Args:
            file_path: Path to the file containing the image reference
            image_ref: The image reference that was skipped
            reason: Human-readable reason for skipping
            verbose: Only log if True
        """
        if not verbose:
            return

        # Multi-line human-readable format
        message = (
            f"[WARN] Skipping image in {file_path}:\n"
            f"  Image: {image_ref}\n"
            f"  Reason: {reason}"
        )
        print(message, file=sys.stderr)

    def _log_to_central(self, file_path: str, image_ref: str, reason: str) -> None:
        """Log to central logging system in JSON format (server mode).

        Args:
            file_path: Path to the file containing the image reference
            image_ref: The image reference that was skipped
            reason: Human-readable reason for skipping
        """
        log_data = {
            "level": "warning",
            "event": "image_skipped",
            "file_path": file_path,
            "image_ref": image_ref,
            "reason": reason,
        }

        # Use Python logging module with JSON format
        self._py_logger.warning(json.dumps(log_data))
