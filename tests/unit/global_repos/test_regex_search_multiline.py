"""Unit tests for RegexSearchService multiline and PCRE2 support (Story #621).

Tests the _detect_pcre2_support() method and multiline/pcre2 parameters
added to RegexSearchService.search() and _search_ripgrep().
"""

import inspect
import json
import pytest
from unittest.mock import MagicMock, patch, AsyncMock

from code_indexer.global_repos.regex_search import RegexSearchService
from code_indexer.server.services.subprocess_executor import ExecutionStatus


# ============================================================================
# Module-level helpers
# ============================================================================


def _mk_match(
    path_text: str, lines_text: str, line_number: int, submatch_text: str
) -> dict:
    """Build a ripgrep JSON match entry for use in parse tests."""
    return {
        "type": "match",
        "data": {
            "path": {"text": path_text},
            "lines": {"text": lines_text},
            "line_number": line_number,
            "absolute_offset": 0,
            "submatches": [
                {
                    "match": {"text": submatch_text},
                    "start": 0,
                    "end": len(submatch_text),
                }
            ],
        },
    }


def _make_capture_ripgrep():
    """Return (captured dict, async mock coroutine) for asserting _search_ripgrep flags."""
    captured = {}

    async def mock_ripgrep(
        pattern,
        search_path,
        include_patterns,
        exclude_patterns,
        case_sensitive,
        context_lines,
        max_results,
        timeout_seconds,
        multiline=False,
        pcre2=False,
    ):
        captured["multiline"] = multiline
        captured["pcre2"] = pcre2
        return [], 0

    return captured, mock_ripgrep


# ============================================================================
# Fixtures
# ============================================================================


@pytest.fixture
def test_repo(tmp_path):
    """Create a test repository structure."""
    repo_path = tmp_path / "test-repo"
    repo_path.mkdir()
    (repo_path / "src").mkdir()
    (repo_path / "src" / "main.py").write_text("def func():\n    pass\n")
    return repo_path


@pytest.fixture
def service_ripgrep(test_repo):
    """Create a RegexSearchService using ripgrep."""
    with patch("code_indexer.global_repos.regex_search.shutil.which") as mock_which:
        mock_which.return_value = "/usr/bin/rg"
        svc = RegexSearchService(test_repo)
    return svc


@pytest.fixture
def service_grep(test_repo):
    """Create a RegexSearchService using grep (no ripgrep)."""

    def which_side_effect(cmd):
        if cmd == "rg":
            return None
        if cmd == "grep":
            return "/usr/bin/grep"
        return None

    with patch(
        "code_indexer.global_repos.regex_search.shutil.which",
        side_effect=which_side_effect,
    ):
        svc = RegexSearchService(test_repo)
    return svc


@pytest.fixture
def ripgrep_executor_fixture(tmp_path):
    """Patch SubprocessExecutor to capture commands sent to ripgrep.

    Yields a list; each element is the command list passed to execute_with_limits.
    """
    captured_commands = []
    temp_path = str(tmp_path / "rg_output.txt")
    open(temp_path, "w").close()

    mock_result = MagicMock()
    mock_result.timed_out = False
    mock_result.status = ExecutionStatus.SUCCESS

    mock_executor = MagicMock()

    def capture_and_return(**kwargs):
        captured_commands.append(kwargs.get("command", []))
        return mock_result

    mock_executor.execute_with_limits = AsyncMock(side_effect=capture_and_return)
    mock_executor.shutdown = MagicMock()

    patches = [
        patch(
            "code_indexer.global_repos.regex_search.SubprocessExecutor",
            return_value=mock_executor,
        ),
        patch("tempfile.mkstemp", return_value=(0, temp_path)),
        patch("os.close"),
        patch("os.path.exists", return_value=True),
        patch("os.remove"),
    ]

    for p in patches:
        p.start()

    yield captured_commands

    for p in patches:
        p.stop()


# ============================================================================
# _detect_pcre2_support() Tests
# ============================================================================


class TestDetectPcre2Support:
    """Test _detect_pcre2_support() method."""

    def test_detect_pcre2_support_returns_true_when_available(self, service_ripgrep):
        """Should return True when rg --pcre2-version exits with return code 0."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "PCRE2 10.43 is available (JIT is available)\n"

        with patch("subprocess.run", return_value=mock_result):
            result = service_ripgrep._detect_pcre2_support()

        assert result is True

    def test_detect_pcre2_support_returns_false_when_unavailable(self, service_ripgrep):
        """Should return False when rg --pcre2-version exits non-zero."""
        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""

        with patch("subprocess.run", return_value=mock_result):
            result = service_ripgrep._detect_pcre2_support()

        assert result is False

    def test_detect_pcre2_support_caches_result(self, service_ripgrep):
        """Should cache detection result so subprocess is only called once."""
        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "PCRE2 10.43 is available"

        with patch("subprocess.run", return_value=mock_result) as mock_run:
            first = service_ripgrep._detect_pcre2_support()
            second = service_ripgrep._detect_pcre2_support()

        assert first is True
        assert second is True
        assert mock_run.call_count == 1

    def test_detect_pcre2_support_exception_returns_false(self, service_ripgrep):
        """Should return False when subprocess raises an exception."""
        with patch("subprocess.run", side_effect=FileNotFoundError("rg not found")):
            result = service_ripgrep._detect_pcre2_support()

        assert result is False


# ============================================================================
# search() signature — backward compatibility
# ============================================================================


class TestSearchBackwardCompatibility:
    """Default parameters must not change existing behavior."""

    def test_search_signature_has_multiline_false_default(self, service_ripgrep):
        """search() must declare multiline=False as default."""
        sig = inspect.signature(service_ripgrep.search)
        assert sig.parameters["multiline"].default is False

    def test_search_signature_has_pcre2_false_default(self, service_ripgrep):
        """search() must declare pcre2=False as default."""
        sig = inspect.signature(service_ripgrep.search)
        assert sig.parameters["pcre2"].default is False

    @pytest.mark.asyncio
    async def test_search_default_call_passes_false_flags_to_ripgrep(
        self, service_ripgrep
    ):
        """search() with defaults must propagate multiline=False, pcre2=False."""
        captured, mock_rg = _make_capture_ripgrep()
        service_ripgrep._search_ripgrep = mock_rg

        from code_indexer.server.services.api_metrics_service import api_metrics_service

        api_metrics_service.increment_regex_search = MagicMock()

        await service_ripgrep.search(pattern="test")

        assert captured["multiline"] is False
        assert captured["pcre2"] is False


# ============================================================================
# _search_ripgrep() flag tests
# ============================================================================


class TestRipgrepFlags:
    """Test that correct ripgrep flags are added for multiline/pcre2."""

    @pytest.mark.asyncio
    async def test_multiline_adds_multiline_flags(
        self, service_ripgrep, test_repo, ripgrep_executor_fixture
    ):
        """multiline=True must add --multiline and --multiline-dotall to ripgrep cmd."""
        await service_ripgrep._search_ripgrep(
            "pattern.*\nmore",
            test_repo,
            None,
            None,
            True,
            0,
            100,
            None,
            multiline=True,
            pcre2=False,
        )

        cmd = ripgrep_executor_fixture[0]
        assert "--multiline" in cmd
        assert "--multiline-dotall" in cmd
        assert "--pcre2" not in cmd

    @pytest.mark.asyncio
    async def test_pcre2_adds_pcre2_flag(
        self, service_ripgrep, test_repo, ripgrep_executor_fixture
    ):
        """pcre2=True must add --pcre2 to ripgrep cmd."""
        await service_ripgrep._search_ripgrep(
            "(?<=def )\\w+",
            test_repo,
            None,
            None,
            True,
            0,
            100,
            None,
            multiline=False,
            pcre2=True,
        )

        cmd = ripgrep_executor_fixture[0]
        assert "--pcre2" in cmd
        assert "--multiline" not in cmd

    @pytest.mark.asyncio
    async def test_multiline_and_pcre2_combined(
        self, service_ripgrep, test_repo, ripgrep_executor_fixture
    ):
        """multiline=True and pcre2=True must add all three flags."""
        await service_ripgrep._search_ripgrep(
            "pattern",
            test_repo,
            None,
            None,
            True,
            0,
            100,
            None,
            multiline=True,
            pcre2=True,
        )

        cmd = ripgrep_executor_fixture[0]
        assert "--multiline" in cmd
        assert "--multiline-dotall" in cmd
        assert "--pcre2" in cmd

    @pytest.mark.asyncio
    async def test_no_extra_flags_when_both_false(
        self, service_ripgrep, test_repo, ripgrep_executor_fixture
    ):
        """multiline=False, pcre2=False must not add any extra flags."""
        await service_ripgrep._search_ripgrep(
            "pattern",
            test_repo,
            None,
            None,
            True,
            0,
            100,
            None,
            multiline=False,
            pcre2=False,
        )

        cmd = ripgrep_executor_fixture[0]
        assert "--multiline" not in cmd
        assert "--multiline-dotall" not in cmd
        assert "--pcre2" not in cmd


# ============================================================================
# PCRE2 unavailable error
# ============================================================================


class TestPcre2UnavailableError:
    """Test clear error when PCRE2 requested but unavailable."""

    @pytest.mark.asyncio
    async def test_pcre2_unavailable_raises_value_error(self, service_ripgrep):
        """When pcre2=True but PCRE2 not supported, search() must raise ValueError."""
        service_ripgrep._pcre2_supported = False  # Force cached value to False

        from code_indexer.server.services.api_metrics_service import api_metrics_service

        api_metrics_service.increment_regex_search = MagicMock()

        with pytest.raises(ValueError, match="[Pp][Cc][Rr][Ee]2"):
            await service_ripgrep.search(pattern="(?<=def )\\w+", pcre2=True)


# ============================================================================
# Multiline ripgrep JSON parse tests
# ============================================================================


class TestParseRipgrepMultilineOutput:
    """Test _parse_ripgrep_json_output handles multi-line match entries."""

    def test_multiline_match_line_number_is_first_line(self, service_ripgrep):
        """Multiline match: line_number must be the first line of the match."""
        path = str(service_ripgrep.repo_path / "src/main.py")
        entry = _mk_match(path, "def func():\n    pass\n", 1, "def func():\n    pass")
        matches, total = service_ripgrep._parse_ripgrep_json_output(
            json.dumps(entry), 100, 0
        )

        assert total == 1
        assert matches[0].line_number == 1
        assert "def func():" in matches[0].line_content

    def test_multiline_match_content_contains_full_text(self, service_ripgrep):
        """Multiline match: line_content must contain the full matched text."""
        path = str(service_ripgrep.repo_path / "src/main.py")
        text = "class Foo:\n    def bar(self):\n        pass\n"
        entry = _mk_match(path, text, 5, "class Foo:\n    def bar")
        matches, total = service_ripgrep._parse_ripgrep_json_output(
            json.dumps(entry), 100, 0
        )

        assert total == 1
        assert "class Foo:" in matches[0].line_content

    def test_multiple_multiline_matches_all_counted(self, service_ripgrep):
        """Multiple multiline matches must all be counted and returned."""
        path = str(service_ripgrep.repo_path / "src/main.py")
        entries = [
            _mk_match(path, "def foo():\n    pass\n", 1, "def foo()"),
            _mk_match(path, "def bar():\n    return 1\n", 5, "def bar()"),
        ]
        output = "\n".join(json.dumps(e) for e in entries)
        matches, total = service_ripgrep._parse_ripgrep_json_output(output, 100, 0)

        assert total == 2
        assert len(matches) == 2


# ============================================================================
# Python/grep fallback multiline tests
# ============================================================================


class TestGrepMultilineFallback:
    """Test Python re.DOTALL fallback for multiline search when engine=grep."""

    @pytest.mark.asyncio
    async def test_grep_multiline_finds_cross_line_match(self, service_grep, test_repo):
        """When engine=grep and multiline=True, use Python re.DOTALL to find cross-line patterns."""
        (test_repo / "src" / "multi.py").write_text(
            "class Foo:\n    def bar(self):\n        pass\n\nclass Baz:\n    pass\n"
        )

        result = await service_grep.search(
            pattern="class Foo.*def bar",
            multiline=True,
        )

        assert result.total_matches >= 1
        assert any("multi.py" in m.file_path for m in result.matches)

    @pytest.mark.asyncio
    async def test_grep_non_multiline_standard_behavior(self, service_grep, test_repo):
        """When engine=grep and multiline=False (default), standard grep behavior."""
        (test_repo / "src" / "single.py").write_text("def hello(): pass\n")

        result = await service_grep.search(
            pattern="def hello",
            multiline=False,
        )

        assert result.total_matches >= 1

    @pytest.mark.asyncio
    async def test_grep_multiline_line_number_is_first_matched_line(
        self, service_grep, test_repo
    ):
        """Python multiline match: line_number must be the first line of the match."""
        (test_repo / "src" / "multi2.py").write_text(
            "line1\nclass Foo:\n    def bar(self):\n        pass\nline5\n"
        )

        result = await service_grep.search(
            pattern="class Foo.*def bar",
            multiline=True,
        )

        assert result.total_matches >= 1
        match = next(m for m in result.matches if "multi2.py" in m.file_path)
        assert match.line_number == 2  # "class Foo:" is on line 2


# ============================================================================
# search() parameter routing tests
# ============================================================================


class TestSearchParameterRouting:
    """Test that search() correctly routes multiline/pcre2 to engine methods."""

    @pytest.mark.asyncio
    async def test_search_passes_multiline_true_to_ripgrep(self, service_ripgrep):
        """search(multiline=True) must propagate multiline=True to _search_ripgrep."""
        captured, mock_rg = _make_capture_ripgrep()
        service_ripgrep._search_ripgrep = mock_rg

        from code_indexer.server.services.api_metrics_service import api_metrics_service

        api_metrics_service.increment_regex_search = MagicMock()

        await service_ripgrep.search(pattern="test", multiline=True)

        assert captured["multiline"] is True
        assert captured["pcre2"] is False

    @pytest.mark.asyncio
    async def test_search_passes_pcre2_true_to_ripgrep(self, service_ripgrep):
        """search(pcre2=True) must propagate pcre2=True to _search_ripgrep."""
        captured, mock_rg = _make_capture_ripgrep()
        service_ripgrep._search_ripgrep = mock_rg
        service_ripgrep._pcre2_supported = True  # Simulate PCRE2 available

        from code_indexer.server.services.api_metrics_service import api_metrics_service

        api_metrics_service.increment_regex_search = MagicMock()

        await service_ripgrep.search(pattern="test", pcre2=True)

        assert captured["multiline"] is False
        assert captured["pcre2"] is True
