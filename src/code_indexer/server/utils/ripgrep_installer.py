"""
Ripgrep installation utility for CIDX server.

Provides shared installation logic used by both ServerInstaller and
DeploymentExecutor to install ripgrep (rg) for fast regex searches.
Includes critical security features like safe tar extraction with
path traversal prevention.
"""

import logging
import os
import platform
import shutil
import stat
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Optional

from code_indexer.server.middleware.correlation import get_correlation_id
from code_indexer.server.logging_utils import format_error_log

logger = logging.getLogger(__name__)


class RipgrepInstaller:
    """Cross-platform ripgrep installer using static MUSL binary."""

    RIPGREP_VERSION = "14.1.1"

    def __init__(self, home_dir: Optional[Path] = None):
        """
        Initialize ripgrep installer.

        Args:
            home_dir: Home directory path (defaults to Path.home())
        """
        self.home_dir = home_dir if home_dir is not None else Path.home()

    def is_installed(self) -> bool:
        """
        Check if ripgrep is installed and accessible.

        Returns:
            True if rg command is available, False otherwise
        """
        try:
            result = subprocess.run(
                ["rg", "--version"],
                capture_output=True,
                text=True,
                timeout=10,
            )
            return result.returncode == 0
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def get_install_path(self) -> Path:
        """
        Get the installation path for ripgrep binary.

        Returns:
            Path to ~/.local/bin/rg
        """
        return self.home_dir / ".local" / "bin" / "rg"

    def _safe_extract_tar(self, tar: tarfile.TarFile, path: str) -> None:
        """
        Safely extract tarball, preventing path traversal attacks.

        This method validates all tar members before extraction to prevent
        malicious tarballs from writing files outside the destination directory.

        Args:
            tar: Open tarfile object
            path: Destination directory for extraction

        Raises:
            ValueError: If path traversal attempt detected in any tar member
        """
        abs_dest = os.path.abspath(path)

        for member in tar.getmembers():
            member_path = os.path.join(path, member.name)
            abs_path = os.path.abspath(member_path)

            # Verify the absolute path starts with the destination directory
            # Use os.sep to ensure proper directory boundary checking
            if not abs_path.startswith(abs_dest + os.sep) and abs_path != abs_dest:
                raise ValueError(
                    f"Path traversal detected in tar: {member.name}. "
                    f"Attempted to extract outside destination directory."
                )

        # All members validated - safe to extract
        tar.extractall(path)

    def _download_file(self, url: str, dest_path: Path) -> None:
        """
        Download file from URL to destination path with timeout.

        Args:
            url: URL to download from
            dest_path: Destination file path

        Raises:
            Exception: If download fails
        """
        with urllib.request.urlopen(url, timeout=60) as response:
            with open(dest_path, "wb") as f:
                f.write(response.read())

    def install(self) -> bool:
        """
        Install ripgrep from GitHub MUSL binary.

        Uses statically-linked MUSL binary which works on all Linux distros
        without requiring specific glibc versions. Installs to ~/.local/bin/rg.

        Returns:
            True if ripgrep available (already installed or newly installed),
            False if installation failed or architecture not supported.
        """
        # Check if already installed (idempotent)
        if self.is_installed():
            logger.info(
                "Ripgrep already installed",
                extra={"correlation_id": get_correlation_id()},
            )
            return True

        # Check architecture
        if platform.machine() != "x86_64":
            logger.warning(
                format_error_log(
                    "REPO-GENERAL-045",
                    f"Ripgrep binary only available for x86_64, found {platform.machine()}",
                    extra={"correlation_id": get_correlation_id()},
                )
            )
            return False

        try:
            # Download ripgrep MUSL binary (works on all Linux distros)
            url = f"https://github.com/BurntSushi/ripgrep/releases/download/{self.RIPGREP_VERSION}/ripgrep-{self.RIPGREP_VERSION}-x86_64-unknown-linux-musl.tar.gz"

            logger.info(
                f"Installing ripgrep {self.RIPGREP_VERSION} from GitHub",
                extra={"correlation_id": get_correlation_id(), "url": url},
            )

            # Create temp directory for download
            temp_dir = tempfile.mkdtemp()
            try:
                tar_path = Path(temp_dir) / "ripgrep.tar.gz"

                # Download tarball
                self._download_file(url, tar_path)

                # Extract tarball with safety checks
                with tarfile.open(tar_path, "r:gz") as tar:
                    self._safe_extract_tar(tar, temp_dir)

                # Find rg binary in extracted files
                extracted_dir = (
                    Path(temp_dir)
                    / f"ripgrep-{self.RIPGREP_VERSION}-x86_64-unknown-linux-musl"
                )
                rg_binary = extracted_dir / "rg"

                # Create ~/.local/bin directory
                local_bin = self.home_dir / ".local" / "bin"
                local_bin.mkdir(parents=True, exist_ok=True)

                # Copy rg binary to ~/.local/bin/
                install_path = self.get_install_path()
                shutil.copy2(rg_binary, install_path)

                # Make executable
                install_path.chmod(install_path.stat().st_mode | stat.S_IEXEC)

            finally:
                # Cleanup temp directory
                shutil.rmtree(temp_dir, ignore_errors=True)

            # Verify installation
            if self.is_installed():
                logger.info(
                    f"Ripgrep {self.RIPGREP_VERSION} installed successfully",
                    extra={
                        "correlation_id": get_correlation_id(),
                        "install_path": str(install_path),
                    },
                )
                return True
            else:
                logger.error(
                    format_error_log(
                        "REPO-GENERAL-046",
                        "Ripgrep installation verification failed",
                        extra={"correlation_id": get_correlation_id()},
                    )
                )
                return False

        except Exception as e:
            logger.error(
                format_error_log(
                    "REPO-GENERAL-047",
                    f"Ripgrep installation failed: {e}",
                    extra={
                        "correlation_id": get_correlation_id(),
                        "error": str(e),
                        "manual_install_url": f"https://github.com/BurntSushi/ripgrep/releases/tag/{self.RIPGREP_VERSION}",
                    },
                )
            )
            return False
