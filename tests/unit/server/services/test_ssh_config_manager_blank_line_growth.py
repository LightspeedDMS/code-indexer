"""
Regression tests for SSHConfigManager blank-line growth bug.

Bug: ~/.ssh/config grows by one blank line on every SSH key sync operation.
Found during manual regression testing of Story #1519/#1521: 4 consecutive
SSH key operations grew the file from 521->522->523->524 bytes, and by the
time of discovery 202 of 215 total lines in the file were blank.

Root cause: ``SSHConfigManager.write_config`` unconditionally appends a
blank-line separator ("\\n") immediately after the CIDX end marker, then
joins ``parsed_config.user_section`` on top of it. On the NEXT parse of that
same file, that separator blank line is captured as a *leading* empty string
inside ``user_section`` (it lies textually between the end marker and the
first real user line, outside the CIDX markers). The following write then
adds ANOTHER unconditional separator on top of the leading blank line that
is already baked into ``user_section`` -- so every parse/write round-trip
bakes in one more blank line than the last, forever.

These tests exercise ``SSHConfigManager`` directly (parse_config ->
write_config), never through ``SSHKeySyncService``, whose ``_sync_ssh_config``
idempotency guard (only calls ``write_config`` when the desired CIDX host
mappings differ from what's on disk) would mask the underlying defect for
the unchanged-entries case. All file I/O happens under ``tmp_path`` -- the
real ``~/.ssh`` directory is never touched.
"""

from __future__ import annotations

from pathlib import Path

from code_indexer.server.services.ssh_config_manager import (
    HostEntry,
    SSHConfigManager,
)


def _count_blank_lines(content: str) -> int:
    """Count actual blank PHYSICAL lines, excluding the phantom trailing
    empty element ``str.split("\\n")`` produces when content ends in a
    newline (that is a line terminator, not a blank line)."""
    return sum(1 for line in content.splitlines() if line == "")


def _separator_blank_line_count(content: str, end_marker: str) -> int:
    """Count the blank lines immediately following the CIDX end marker,
    i.e. the separator between the CIDX-managed section and the preserved
    user section. This must always be exactly 1, regardless of how many
    entries the CIDX section carries or how many sync operations already
    ran -- unlike a raw total blank-line count, it is NOT expected to grow
    just because more Host entries (each with their own legitimate
    trailing spacer blank line) were added to the CIDX section itself.
    """
    lines = content.splitlines()
    end_idx = lines.index(end_marker)
    count = 0
    for line in lines[end_idx + 1 :]:
        if line == "":
            count += 1
        else:
            break
    return count


class TestSSHConfigManagerBlankLineGrowth:
    """Repeated write_config() round-trips must not grow the file."""

    def test_repeated_write_config_with_unchanged_entries_is_idempotent(
        self, tmp_path: Path
    ) -> None:
        """N consecutive parse->write cycles with the SAME entries and no
        actual content change must produce byte-identical output every
        time -- no blank-line accumulation, no file-size growth.
        """
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        config_path = ssh_dir / "config"
        config_path.write_text(
            "Host github.com\n"
            "  HostName github.com\n"
            "  User myuser\n"
            "  IdentityFile ~/.ssh/id_ed25519\n"
        )

        manager = SSHConfigManager()
        entries = [
            HostEntry(
                host="gitlab.example.com",
                hostname="gitlab.example.com",
                key_path=str(ssh_dir / "cidx_key"),
            )
        ]

        sizes = []
        blank_counts = []
        for _ in range(6):
            parsed = manager.parse_config(config_path)
            manager.write_config(config_path, parsed, entries)
            content = config_path.read_text()
            sizes.append(len(content))
            blank_counts.append(_count_blank_lines(content))

        assert sizes == [sizes[0]] * len(sizes), (
            "SSHConfigManager.write_config must be idempotent across "
            f"repeated round-trips with unchanged entries; sizes grew: {sizes}"
        )
        assert blank_counts == [blank_counts[0]] * len(blank_counts), (
            "Blank-line count must not grow across repeated round-trips "
            f"with unchanged entries; counts grew: {blank_counts}"
        )

    def test_repeated_write_config_with_incrementally_added_entries_keeps_separator_at_one_blank_line(
        self, tmp_path: Path
    ) -> None:
        """Mirrors the actual reported bug: 4 consecutive SSH key
        operations, each adding one more managed host, must NOT
        accumulate one extra separator blank line per operation.

        Growing the CIDX host list legitimately adds more total blank
        lines (each Host block carries its own trailing spacer), so this
        asserts the SPECIFIC separator between the CIDX end marker and
        the user section -- which must stay pinned at exactly 1 no matter
        how many sync operations already ran.
        """
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        config_path = ssh_dir / "config"
        config_path.write_text(
            "Host my-personal-server\n  HostName 10.0.0.5\n  User myuser\n"
        )

        manager = SSHConfigManager()

        separator_counts = []
        for i in range(4):
            entries = [
                HostEntry(
                    host=f"cidx-host-{j}.example.com",
                    hostname=f"cidx-host-{j}.example.com",
                    key_path=str(ssh_dir / f"cidx_key_{j}"),
                )
                for j in range(i + 1)
            ]
            parsed = manager.parse_config(config_path)
            manager.write_config(config_path, parsed, entries)
            separator_counts.append(
                _separator_blank_line_count(
                    config_path.read_text(), SSHConfigManager.CIDX_END_MARKER
                )
            )

        assert separator_counts == [1, 1, 1, 1], (
            "The blank-line separator between the CIDX end marker and the "
            "user section must stay at exactly 1 across every sync "
            f"operation, not grow with each one; counts: {separator_counts}"
        )

    def test_user_authored_host_block_preserved_byte_for_byte_across_writes(
        self, tmp_path: Path
    ) -> None:
        """The user's own Host block content must survive repeated
        round-trips byte-for-byte, even while the blank-line-growth fix
        normalizes the separator between the CIDX section and it.
        """
        ssh_dir = tmp_path / ".ssh"
        ssh_dir.mkdir()
        config_path = ssh_dir / "config"
        user_block = (
            "Host my-personal-server\n  HostName 10.0.0.5\n  User myuser\n  Port 2222\n"
        )
        config_path.write_text(user_block)

        manager = SSHConfigManager()
        entries = [
            HostEntry(
                host="github.com",
                hostname="github.com",
                key_path=str(ssh_dir / "cidx_github_key"),
            )
        ]

        for _ in range(5):
            parsed = manager.parse_config(config_path)
            manager.write_config(config_path, parsed, entries)

        final_content = config_path.read_text()
        user_start = final_content.index("Host my-personal-server")
        # The extracted region must reproduce the ORIGINAL user block
        # byte-for-byte, not merely contain equivalent-looking lines: a
        # stray trailing/leading blank line, altered indentation, or a
        # different line-ending would all pass a substring/splitlines
        # check but fail this exact-prefix comparison.
        assert final_content[user_start:].startswith(user_block), (
            "User-authored Host block must be preserved byte-for-byte; "
            f"got: {final_content[user_start : user_start + len(user_block) + 20]!r}"
        )
