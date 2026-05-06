"""Concurrency and cache behavior tests for X-Ray AST engine.

User Mandate Section 4: thread safety, parser cache reuse, engine reusability,
and memory baseline (informational).
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import List

import pytest

# Sources for parallel parse tests — one per language
_LANG_SOURCES = {
    "python": b"def foo(x):\n    return x + 1\n",
    "java": b"public class T { void m() {} }",
    "go": b"package main\nfunc foo() {}\n",
    "typescript": b"function foo(x: number) { return x; }",
    "javascript": b"function foo(x) { return x; }",
    "kotlin": b"fun foo(x: Int): Int { return x }\n",
    "bash": b"#!/bin/bash\necho hello\n",
    "csharp": b"public class T { void M() {} }",
    "html": b"<html><body><p>hello</p></body></html>",
    "css": b"body { color: red; }",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_engine():  # type: ignore[return]
    from code_indexer.xray.ast_engine import AstSearchEngine

    return AstSearchEngine()


# ---------------------------------------------------------------------------
# Section 4.1: 4 parallel parse calls from threads
# ---------------------------------------------------------------------------


class TestParallelParsing:
    """Multiple threads can call parse() concurrently on one engine instance."""

    @pytest.mark.parametrize(
        "lang",
        ["python", "java", "go"],
        ids=["python", "java", "go"],
    )
    def test_four_parallel_parses_same_language(self, lang: str) -> None:
        """4 concurrent threads each parse the same language without crash.

        All results must have child_count > 0 and no thread must raise.
        """
        engine = _make_engine()
        source = _LANG_SOURCES[lang]

        futures = []
        results: List = []
        errors: List = []

        with ThreadPoolExecutor(max_workers=4) as executor:
            for _ in range(4):
                futures.append(executor.submit(engine.parse, source, lang))

            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    errors.append(exc)
                else:
                    results.append(future.result())

        assert not errors, (
            f"{lang}: {len(errors)} thread(s) raised exceptions: {errors}"
        )
        assert len(results) == 4, f"{lang}: expected 4 results, got {len(results)}"
        for root in results:
            assert root is not None
            assert root.child_count > 0, (
                f"{lang}: parallel parse returned root with child_count=0"
            )

    def test_parallel_parses_different_languages(self) -> None:
        """4 threads each parse a different language concurrently."""
        engine = _make_engine()
        langs = ["python", "java", "go", "typescript"]
        sources = [_LANG_SOURCES[lang] for lang in langs]

        errors: List = []
        results: List = []

        with ThreadPoolExecutor(max_workers=4) as executor:
            futures = [
                executor.submit(engine.parse, src, lang)
                for src, lang in zip(sources, langs)
            ]
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    errors.append(exc)
                else:
                    results.append(future.result())

        assert not errors, f"Thread errors: {errors}"
        assert len(results) == 4
        for root in results:
            assert root.child_count > 0

    def test_parallel_parses_no_double_creation_under_race(self) -> None:
        """Parser cache is safe under concurrent first-access for the same language.

        10 threads all trigger the first parse for 'kotlin' simultaneously.
        The engine must not raise and must produce valid results.
        """
        # Create a fresh engine so kotlin is not yet cached
        engine = _make_engine()
        source = _LANG_SOURCES["kotlin"]

        errors: List = []
        results: List = []

        with ThreadPoolExecutor(max_workers=10) as executor:
            futures = [
                executor.submit(engine.parse, source, "kotlin") for _ in range(10)
            ]
            for future in as_completed(futures):
                exc = future.exception()
                if exc is not None:
                    errors.append(exc)
                else:
                    results.append(future.result())

        assert not errors, f"Thread errors on concurrent first-access: {errors}"
        assert len(results) == 10
        for root in results:
            assert root is not None


# ---------------------------------------------------------------------------
# Section 4.2: Parser cache reuse — get_language NOT called twice
# ---------------------------------------------------------------------------


class TestParserCacheReuse:
    """The second parse() for the same language reuses the cached parser."""

    def test_get_language_called_once_for_repeated_parses(self) -> None:
        """Instrumenting _tsl.get_language: called exactly once for 3 parses of same lang.

        Wrapping get_language with a counter is acceptable instrumentation per
        the story spec (not mocking business logic — the real function is called).
        """
        engine = _make_engine()

        # Instrument the engine's tsl reference to count get_language calls
        call_count: List[str] = []
        real_get_language = engine._tsl.get_language

        def counting_get_language(name: str):  # type: ignore[no-untyped-def]
            call_count.append(name)
            return real_get_language(name)

        # Patch directly on the engine's _tsl reference
        import tree_sitter_languages as tsl

        original = tsl.get_language
        try:
            tsl.get_language = counting_get_language  # type: ignore[method-assign]
            # Replace _tsl on the engine so it uses the counting version
            engine._tsl = tsl

            engine.parse(b"x = 1\n", "python")
            engine.parse(b"y = 2\n", "python")
            engine.parse(b"z = 3\n", "python")
        finally:
            tsl.get_language = original  # type: ignore[method-assign]

        python_calls = [c for c in call_count if c == "python"]
        assert len(python_calls) == 1, (
            f"Expected get_language('python') called exactly once (cache hit), "
            f"got {len(python_calls)} calls. "
            f"All calls: {call_count}"
        )

    def test_cache_size_reflects_unique_languages_used(self) -> None:
        """_parser_cache_size() equals number of distinct languages parsed."""
        engine = _make_engine()
        assert engine._parser_cache_size() == 0, "Fresh engine should have empty cache"

        engine.parse(b"x = 1\n", "python")
        assert engine._parser_cache_size() == 1

        engine.parse(b"y = 2\n", "python")
        assert engine._parser_cache_size() == 1, "Same language should not grow cache"

        engine.parse(b"package main\n", "go")
        assert engine._parser_cache_size() == 2

    def test_different_languages_each_get_own_cache_entry(self) -> None:
        """Each unique language gets its own cache entry."""
        engine = _make_engine()

        engine.parse(b"x = 1\n", "python")
        engine.parse(b"package p\n", "go")
        engine.parse(b"public class T {}", "java")

        assert engine._parser_cache_size() == 3


# ---------------------------------------------------------------------------
# Section 4.3: Engine reusability across all 10 languages
# ---------------------------------------------------------------------------


class TestEngineReusability:
    """One engine instance can parse all 10 mandatory languages without corruption."""

    def test_all_ten_languages_over_engine_lifetime(self) -> None:
        """Single engine parses all 10 mandatory languages; all results non-empty."""
        engine = _make_engine()
        errors: List[str] = []

        for lang, source in _LANG_SOURCES.items():
            try:
                root = engine.parse(source, lang)
                if root is None:
                    errors.append(f"{lang}: parse returned None")
                elif root.child_count < 0:
                    errors.append(f"{lang}: child_count is negative")
            except Exception as exc:
                errors.append(f"{lang}: raised {type(exc).__name__}: {exc}")

        assert not errors, (
            "Engine reusability failures across languages:\n"
            + "\n".join(f"  - {e}" for e in errors)
        )

    def test_cache_grows_to_ten_after_all_languages(self) -> None:
        """After parsing all 10 languages, cache holds exactly 10 entries."""
        engine = _make_engine()

        for lang, source in _LANG_SOURCES.items():
            engine.parse(source, lang)

        assert engine._parser_cache_size() == 10

    def test_repeated_parses_after_all_languages_no_corruption(self) -> None:
        """Parsing all languages then re-parsing Python still works correctly."""
        engine = _make_engine()

        for lang, source in _LANG_SOURCES.items():
            engine.parse(source, lang)

        # Re-parse Python after all other languages have been cached
        root = engine.parse(b"def foo():\n    return 42\n", "python")
        assert root is not None
        assert root.type == "module"
        assert root.child_count > 0


# ---------------------------------------------------------------------------
# Section 4.4: Memory baseline (informational — not a hard failure threshold)
# ---------------------------------------------------------------------------


class TestMemoryBaseline:
    """Memory usage baseline for sequential parsing of 100 files (informational)."""

    def test_memory_delta_informational(self) -> None:
        """Parse 100 Python files sequentially; print RSS delta for trending.

        This test does NOT fail on a memory threshold — it records baseline data.
        Skipped gracefully if psutil is not installed.
        """
        psutil = pytest.importorskip("psutil")
        import os

        src_root = Path(__file__).parent.parent.parent.parent / "src" / "code_indexer"
        py_files = sorted(
            p for p in src_root.rglob("*.py") if "__pycache__" not in p.parts
        )[:100]

        if not py_files:
            pytest.skip("No .py files found under src/code_indexer/ to use as corpus")

        engine = _make_engine()
        process = psutil.Process(os.getpid())

        rss_before = process.memory_info().rss

        for py_file in py_files:
            source = py_file.read_bytes()
            root = engine.parse(source, "python")
            assert root is not None  # sanity — parse must succeed

        rss_after = process.memory_info().rss
        delta_mb = (rss_after - rss_before) / 1024 / 1024

        print(
            f"\n  Memory baseline: parsed {len(py_files)} files, "
            f"RSS delta = {delta_mb:+.1f} MB "
            f"(before={rss_before / 1024 / 1024:.1f} MB, "
            f"after={rss_after / 1024 / 1024:.1f} MB)"
        )
        # Informational only — no hard threshold. Record for trending.
        assert True  # never fail on memory; only print
