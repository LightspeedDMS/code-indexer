"""
SSH Config Manager Service.

Manages ~/.ssh/config file with CIDX-managed sections that preserve user entries.
Provides atomic write operations and conflict detection.
"""

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
import shutil
from typing import List, Tuple

from .ssh_input_validation import (
    SSHConfigFormatError,
    has_control_characters,
    is_valid_hostname,
)


class CorruptedConfigError(Exception):
    """Raised when SSH config has CIDX start marker but missing end marker."""

    pass


@dataclass
class ParsedConfig:
    """Parsed SSH config file contents."""

    cidx_section: List[str] = field(default_factory=list)
    user_section: List[str] = field(default_factory=list)
    include_directives: List[Tuple[str, int]] = field(default_factory=list)


@dataclass
class HostEntry:
    """SSH Host entry for CIDX-managed section."""

    host: str
    hostname: str
    key_path: str


@dataclass
class ConflictInfo:
    """Information about a host conflict in SSH config."""

    exists: bool = False
    in_user_section: bool = False


class SSHConfigManager:
    """
    Manager for SSH config file with CIDX-managed sections.

    Maintains isolation between CIDX-managed Host blocks and user-defined entries.
    Preserves user formatting byte-for-byte while managing CIDX section atomically.
    """

    CIDX_START_MARKER = "# BEGIN CIDX-MANAGED SSH KEYS - DO NOT EDIT"
    CIDX_END_MARKER = "# END CIDX-MANAGED SSH KEYS"

    def parse_config(self, config_path: Path) -> ParsedConfig:
        """
        Parse SSH config file into CIDX and user sections.

        Args:
            config_path: Path to SSH config file

        Returns:
            ParsedConfig with cidx_section, user_section, and include_directives
        """
        if not config_path.exists():
            return ParsedConfig()

        content = config_path.read_text()
        lines = content.split("\n")

        cidx_section: List[str] = []
        user_section: List[str] = []
        include_directives: List[Tuple[str, int]] = []
        in_cidx_section = False
        cidx_start_found = False
        cidx_end_found = False

        for position, line in enumerate(lines):
            # Check for Include directive
            stripped = line.strip()
            if stripped.lower().startswith("include "):
                include_directives.append((line, position))
                continue

            # Check for CIDX markers
            if stripped == self.CIDX_START_MARKER:
                cidx_start_found = True
                in_cidx_section = True
                continue

            if stripped == self.CIDX_END_MARKER:
                cidx_end_found = True
                in_cidx_section = False
                continue

            # Add to appropriate section
            if in_cidx_section:
                cidx_section.append(line)
            else:
                user_section.append(line)

        # Check for corrupted config (start marker without end marker)
        if cidx_start_found and not cidx_end_found:
            # Create backup before raising error
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_path = (
                config_path.parent / f"{config_path.name}.cidx-backup-{timestamp}"
            )
            shutil.copy2(config_path, backup_path)
            raise CorruptedConfigError(
                f"Missing end marker in CIDX section. Backup created at {backup_path}"
            )

        return ParsedConfig(
            cidx_section=cidx_section,
            user_section=user_section,
            include_directives=include_directives,
        )

    def write_config(
        self,
        config_path: Path,
        parsed_config: ParsedConfig,
        new_cidx_entries: List[HostEntry],
    ) -> None:
        """
        Write SSH config file with CIDX-managed section.

        Args:
            config_path: Path to SSH config file
            parsed_config: Previously parsed config to preserve user section
            new_cidx_entries: List of HostEntry to write in CIDX section
        """
        import os

        content = ""

        # Add Include directives first (must be at top per OpenSSH spec)
        for directive, _ in parsed_config.include_directives:
            content += directive + "\n"

        if parsed_config.include_directives:
            content += "\n"

        # Add CIDX-managed section
        content += self.CIDX_START_MARKER + "\n"
        for entry in new_cidx_entries:
            content += self._format_host_block(entry)
        content += self.CIDX_END_MARKER + "\n"

        # Preserve user section (excluding Include directives already written).
        #
        # Bug fix: ~/.ssh/config grew by one blank line on every sync. The
        # single separator blank line added below (between the CIDX end
        # marker and the user section) lies OUTSIDE the CIDX markers, so
        # parse_config() folds it back into user_section as a leading empty
        # string on the next parse. Without stripping it here, the next
        # write would stack ANOTHER separator on top of that leading blank
        # line, growing the file by one blank line per round-trip forever.
        # Only LEADING blank lines are stripped -- blank lines the user
        # placed further into their own section are preserved untouched.
        user_lines = list(parsed_config.user_section)
        while user_lines and user_lines[0] == "":
            user_lines.pop(0)

        if user_lines:
            content += "\n" + "\n".join(user_lines)
        else:
            content += "\n"

        # Ensure parent directory exists
        config_path.parent.mkdir(parents=True, exist_ok=True)

        # Atomic write: write to temp file, then rename
        temp_path = config_path.parent / f"{config_path.name}.tmp"
        temp_path.write_text(content)
        os.chmod(temp_path, 0o600)
        temp_path.rename(config_path)

    def _format_host_block(self, entry: HostEntry) -> str:
        """
        Format a single Host block for SSH config.

        Format-time backstop: every caller (SSHKeyManager, SSHKeySyncService)
        is expected to have already validated/filtered ``entry`` before it
        ever reaches here -- this is a defensive-invariant assertion (Messi
        Rule #15), not the primary rejection point. A value written into a
        Host block must never contain a character that can end or extend
        its config line (a newline or carriage return); the checks below
        are what stops that.

        Args:
            entry: HostEntry with host, hostname, and key_path

        Returns:
            Formatted Host block string

        Raises:
            SSHConfigFormatError: ``entry.host``/``entry.hostname`` fails the
                strict hostname grammar, or ``entry.key_path`` contains a
                control character. Never let a rejected value reach
                ``~/.ssh/config``.
        """
        if not is_valid_hostname(entry.host):
            raise SSHConfigFormatError(f"Invalid Host value: {entry.host!r}")
        if not is_valid_hostname(entry.hostname):
            raise SSHConfigFormatError(f"Invalid HostName value: {entry.hostname!r}")
        if has_control_characters(entry.key_path):
            raise SSHConfigFormatError(
                f"Invalid IdentityFile value: {entry.key_path!r}"
            )

        block = f"Host {entry.host}\n"
        block += f"  HostName {entry.hostname}\n"
        block += "  User git\n"
        block += f"  IdentityFile {entry.key_path}\n"
        block += "  IdentitiesOnly yes\n"
        block += "\n"
        return block

    def check_host_conflict(self, config_path: Path, hostname: str) -> ConflictInfo:
        """
        Check if a hostname exists in the user section of SSH config.

        Args:
            config_path: Path to SSH config file
            hostname: Hostname to check for conflicts

        Returns:
            ConflictInfo with exists and in_user_section flags
        """
        parsed = self.parse_config(config_path)

        for line in parsed.user_section:
            stripped = line.strip()
            if stripped.lower().startswith("host "):
                # Extract host pattern from "Host hostname" line
                host_part = stripped[5:].strip()
                if hostname in host_part.split():
                    return ConflictInfo(exists=True, in_user_section=True)

        return ConflictInfo(exists=False, in_user_section=False)
