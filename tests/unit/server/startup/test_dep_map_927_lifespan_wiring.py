"""Story #927 CRITICAL #2: repair invoker refactor and lifespan wiring."""

from __future__ import annotations

from pathlib import Path


_ROUTES_PATH = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "dependency_map_routes.py"
)


def _extract_function_body(source: str, func_name: str) -> str:
    """Extract the body of a top-level function from source."""
    start = source.find(f"def {func_name}(")
    assert start != -1, f"{func_name} not found in source"
    # Find next top-level function or end of file
    next_def = source.find("\ndef ", start + 1)
    if next_def == -1:
        return source[start:]
    return source[start:next_def]


# Depth from this test file to the repository root:
# test_dep_map_927_lifespan_wiring.py -> parents[0]=startup -> [1]=server -> [2]=unit -> [3]=tests -> [4]=repo
_REPO_ROOT_DEPTH = 4
_REPO_ROOT = Path(__file__).resolve().parents[_REPO_ROOT_DEPTH]

_LIFESPAN_PATH = (
    _REPO_ROOT / "src" / "code_indexer" / "server" / "startup" / "lifespan.py"
)


def _extract_dep_map_service_call(source: str) -> str:
    """Extract the DependencyMapService(...) call block from lifespan source.

    Uses rfind to locate the LAST occurrence of 'DependencyMapService(' so that
    docstring examples (which also contain 'DependencyMapService(...)') are
    skipped in favour of the actual constructor call site.

    Fails explicitly if the call is not found or the paren is unterminated.
    """
    # rfind picks the last occurrence — the actual constructor call, not the
    # docstring placeholder that reads 'DependencyMapService(...)'.
    start = source.rfind("DependencyMapService(")
    assert start != -1, "DependencyMapService( not found in lifespan.py"
    depth = 0
    for i, ch in enumerate(source[start:]):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return source[start : start + i + 1]
    raise AssertionError("Unterminated DependencyMapService(...) call in lifespan.py")


def _assert_in_dep_map_service_call(kwarg: str) -> None:
    """Assert that kwarg= appears inside the DependencyMapService(...) call in lifespan.py."""
    call_block = _extract_dep_map_service_call(_LIFESPAN_PATH.read_text())
    assert f"{kwarg}=" in call_block, (
        f"{kwarg}= missing from DependencyMapService() in lifespan.py (Story #927)"
    )


class TestLifespanCollaboratorWiring:
    """lifespan.py DependencyMapService instantiation must wire Story #927 collaborators."""

    def test_health_check_fn_wired(self):
        _assert_in_dep_map_service_call("health_check_fn")

    def test_repair_invoker_fn_wired(self):
        """Story #927 Pass 4: repair_invoker_fn must be None in the constructor.

        The Pass 4 refactor changed from passing the invoker directly to the
        constructor to a late-binding pattern: constructor receives None, then
        set_repair_invoker_fn() is called afterwards. This test verifies the
        constructor uses repair_invoker_fn=None (not a non-None value).
        The late-binding itself is verified by TestLifespanCallSiteOrdering.
        """
        call_block = _extract_dep_map_service_call(_LIFESPAN_PATH.read_text())
        assert "repair_invoker_fn=None" in call_block, (
            "Story #927 Pass 4: DependencyMapService() constructor must receive "
            "repair_invoker_fn=None — late-binding via set_repair_invoker_fn() "
            f"is used instead. Got call block: {call_block!r}"
        )

    def test_pg_pool_wired(self):
        _assert_in_dep_map_service_call("pg_pool")


class TestLifespanRepairInvokerRefactor:
    """_execute_repair_body must be defined and called by _run_repair_with_feedback."""

    def test_execute_repair_body_defined(self):
        """dependency_map_routes.py must define _execute_repair_body function."""
        source = _ROUTES_PATH.read_text()
        assert "def _execute_repair_body(" in source, (
            "_execute_repair_body not found in dependency_map_routes.py"
        )

    def test_run_repair_with_feedback_delegates_to_execute_body(self):
        """_run_repair_with_feedback function body must call _execute_repair_body."""
        source = _ROUTES_PATH.read_text()
        body = _extract_function_body(source, "_run_repair_with_feedback")
        assert "_execute_repair_body(" in body, (
            "_run_repair_with_feedback must delegate to _execute_repair_body"
        )

    def test_execute_repair_body_accepts_job_id_param(self):
        """_execute_repair_body signature must include job_id parameter."""
        source = _ROUTES_PATH.read_text()
        idx = source.find("def _execute_repair_body(")
        assert idx != -1, "_execute_repair_body not found"
        sig_end = source.find(")", idx)
        signature = source[idx : sig_end + 1]
        assert "job_id" in signature, (
            f"_execute_repair_body must accept job_id param, got: {signature!r}"
        )


def _invoke_repair_closure(dep_map_dir: "Path", job_id: str = "test-job-id") -> dict:
    """Shared helper: build a repair invoker via the lifespan factory, invoke it, return captured kwargs.

    Patches _execute_repair_body to capture runtime kwargs without actually running
    the repair executor. Returns the dict of kwargs that the closure passed, plus
    __mock_* keys for identity assertions in callers.

    dep_map_dir must come from pytest's tmp_path fixture — no hardcoded paths.
    """
    from unittest.mock import MagicMock, patch

    mock_dep_map_service = MagicMock(name="dep_map_service")
    mock_tracking_backend = MagicMock(name="tracking_backend")
    mock_job_tracker = MagicMock(name="job_tracker")

    captured: dict = {}

    def spy_execute_repair_body(**kwargs):
        captured.update(kwargs)

    from code_indexer.server.startup.lifespan import _make_dep_map_repair_invoker_fn

    with patch(
        "code_indexer.server.web.dependency_map_routes._execute_repair_body",
        side_effect=spy_execute_repair_body,
    ):
        invoker = _make_dep_map_repair_invoker_fn(
            dep_map_dir=dep_map_dir,
            tracking_backend=mock_tracking_backend,
            job_tracker=mock_job_tracker,
            dep_map_service=mock_dep_map_service,
        )
        invoker(job_id)

    # Attach stubs so callers can assert identity without re-creating them
    captured["__mock_dep_map_service"] = mock_dep_map_service
    captured["__mock_tracking_backend"] = mock_tracking_backend
    captured["__mock_job_tracker"] = mock_job_tracker
    return captured


class TestLifespanClosureRuntimeKwargCapture:
    """Story #927 Codex Pass 3 fix: RUNTIME kwarg inspection replaces source-text grep.

    The old TestLifespanClosureForwardsDependencies used source.find() on lifespan.py
    and would NOT catch regressions where the closure forwarded the wrong variable,
    passed None, or had any other runtime-only failure. These tests invoke the actual
    factory function, call the returned closure, and assert on what _execute_repair_body
    actually receives at runtime — catching the exact regression class Codex Pass 2 found.
    """

    def test_repair_invoker_fn_passes_dep_map_service_at_runtime(self, tmp_path):
        """Closure produced by _make_dep_map_repair_invoker_fn must forward dep_map_service at runtime.

        Regression guard: Story #927 Pass 1 omitted dep_map_service= from the closure,
        breaking auto-repair. The spy on _execute_repair_body captures runtime kwargs —
        this test would have failed in Pass 1 because dep_map_service would be absent.
        """
        captured = _invoke_repair_closure(tmp_path, "test-job-dep-map-service")

        assert "dep_map_service" in captured, (
            f"Closure failed to forward dep_map_service to _execute_repair_body. "
            f"Captured kwargs: {sorted(k for k in captured if not k.startswith('__'))}"
        )
        assert captured["dep_map_service"] is captured["__mock_dep_map_service"], (
            "dep_map_service forwarded is not the expected instance"
        )

    def test_repair_invoker_fn_forwards_all_collaborators_at_runtime(self, tmp_path):
        """Closure must forward job_id, output_dir, tracking_backend, and job_tracker at runtime.

        Verifies the complete kwarg surface of the closure so any future regression that
        drops a collaborator is caught immediately.
        """
        captured = _invoke_repair_closure(tmp_path, "test-job-all-collabs")

        assert captured.get("job_id") == "test-job-all-collabs", (
            f"job_id not forwarded: {captured.get('job_id')!r}"
        )
        assert captured.get("output_dir") == tmp_path, (
            f"output_dir not forwarded: {captured.get('output_dir')!r}"
        )
        assert (
            captured.get("tracking_backend") is captured["__mock_tracking_backend"]
        ), "tracking_backend not forwarded to _execute_repair_body"
        assert captured.get("job_tracker") is captured["__mock_job_tracker"], (
            "job_tracker not forwarded to _execute_repair_body"
        )


def _build_real_dep_map_service():
    """Build a real DependencyMapService with MagicMock collaborators.

    Used by TestLifespanCallSiteOrdering to exercise the actual setter implementation
    rather than a stub. Collaborators are MagicMock so no real infrastructure is needed.
    """
    from unittest.mock import MagicMock
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    return DependencyMapService(
        golden_repos_manager=MagicMock(name="golden_repos_manager"),
        config_manager=MagicMock(name="config_manager"),
        tracking_backend=MagicMock(name="tracking_backend"),
        analyzer=MagicMock(name="analyzer"),
        job_tracker=MagicMock(name="job_tracker"),
        repair_invoker_fn=None,  # Not yet bound — will be set via setter
    )


def _invoke_late_bound_repair(service_instance, dep_map_dir: Path) -> dict:
    """Build repair invoker, late-bind to service, invoke it, return captured kwargs.

    Simulates the corrected lifespan pattern:
      1. Construct service (done by caller)
      2. Call factory with real service instance
      3. Bind via set_repair_invoker_fn
      4. Invoke and capture what _execute_repair_body receives

    Returns kwargs dict captured from _execute_repair_body.
    """
    from unittest.mock import MagicMock, patch
    from code_indexer.server.startup.lifespan import _make_dep_map_repair_invoker_fn

    captured: dict = {}

    def spy_execute_repair_body(**kwargs):
        captured.update(kwargs)

    with patch(
        "code_indexer.server.web.dependency_map_routes._execute_repair_body",
        side_effect=spy_execute_repair_body,
    ):
        invoker = _make_dep_map_repair_invoker_fn(
            dep_map_dir=dep_map_dir,
            tracking_backend=MagicMock(name="tracking_backend"),
            job_tracker=MagicMock(name="job_tracker"),
            dep_map_service=service_instance,
        )
        service_instance.set_repair_invoker_fn(invoker)
        service_instance._repair_invoker_fn("test-job-late-bind")

    return captured


class TestLifespanCallSiteOrdering:
    """Story #927 Codex Pass 4: lifespan must construct DependencyMapService BEFORE
    binding the repair invoker, so the closure captures the real instance, not None.

    The Pass 3->4 refactor introduced a use-before-assignment bug where the factory
    was called before the service was constructed — the closure permanently captured
    None. This test class catches that regression via three complementary guards:

    1. Existence check: set_repair_invoker_fn must exist on DependencyMapService.
    2. Runtime behaviour: late-binding via set_repair_invoker_fn must propagate the
       real service instance — NOT None — into the closure. Uses a real
       DependencyMapService instance with MagicMock collaborators.
    3. Source-order guard: lifespan.py must construct DependencyMapService BEFORE
       calling _make_dep_map_repair_invoker_fn.
    """

    def test_set_repair_invoker_fn_exists_on_service(self):
        """DependencyMapService must expose set_repair_invoker_fn for late-binding."""
        from code_indexer.server.services.dependency_map_service import (
            DependencyMapService,
        )

        assert hasattr(DependencyMapService, "set_repair_invoker_fn"), (
            "DependencyMapService.set_repair_invoker_fn missing — "
            "late-binding pattern (Story #927 Codex Pass 4) cannot work without it"
        )
        assert callable(getattr(DependencyMapService, "set_repair_invoker_fn")), (
            "DependencyMapService.set_repair_invoker_fn must be callable"
        )

    def test_set_repair_invoker_fn_binds_and_captures_service_not_none(self, tmp_path):
        """Late-binding via set_repair_invoker_fn must give the closure the real instance.

        Uses a real DependencyMapService (with MagicMock collaborators) so the actual
        setter implementation is exercised. Regression guard: factory called with
        dep_map_service=None (old broken order) would fail this assertion.
        """
        service_instance = _build_real_dep_map_service()
        captured = _invoke_late_bound_repair(service_instance, tmp_path)

        assert "dep_map_service" in captured, (
            "Closure failed to forward dep_map_service to _execute_repair_body"
        )
        assert captured["dep_map_service"] is service_instance, (
            f"dep_map_service captured wrong instance: {captured['dep_map_service']!r}"
        )
        assert captured["dep_map_service"] is not None, (
            "Closure captured None — call-site ordering bug regressed"
        )

    def test_lifespan_ordering_service_built_before_invoker_factory_called(self):
        """lifespan.py must construct DependencyMapService BEFORE _make_dep_map_repair_invoker_fn.

        Source-order guard: 'dependency_map_service = DependencyMapService(' must appear
        before '_dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn(' in the
        lifespan source. Regression here means the closure permanently captures None.
        """
        source = _LIFESPAN_PATH.read_text()

        service_pos = source.find("dependency_map_service = DependencyMapService(")
        factory_pos = source.find(
            "_dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn("
        )

        assert service_pos != -1, (
            "'dependency_map_service = DependencyMapService(' not found in lifespan.py"
        )
        assert factory_pos != -1, (
            "'_dep_map_repair_invoker_fn = _make_dep_map_repair_invoker_fn(' "
            "not found in lifespan.py"
        )
        assert service_pos < factory_pos, (
            f"Call-site ordering bug: _make_dep_map_repair_invoker_fn (pos {factory_pos}) "
            f"appears BEFORE DependencyMapService construction (pos {service_pos}). "
            f"The closure will capture None permanently."
        )

    def test_lifespan_ordering_setter_called_before_start_scheduler(self):
        """Story #927 Codex Pass 5: source-order guard for the FULL ordering contract.

        The pre-binding window between construct and bind is closed by the late-binding
        fix. The post-binding window between bind and start_scheduler must also be
        correct: if start_scheduler() is called before set_repair_invoker_fn(...),
        the scheduler daemon could fire with _repair_invoker_fn still None.

        This test asserts the full ordering: construct -> factory -> setter -> start.
        """
        import inspect
        from code_indexer.server.startup import lifespan

        source = inspect.getsource(lifespan.make_lifespan)

        construct_pos = source.find("DependencyMapService(")
        factory_pos = source.find("_make_dep_map_repair_invoker_fn(")
        setter_pos = source.find(".set_repair_invoker_fn(")
        start_pos = source.find(".start_scheduler(")

        assert construct_pos != -1, (
            "DependencyMapService(...) not found in make_lifespan"
        )
        assert factory_pos != -1, (
            "_make_dep_map_repair_invoker_fn(...) not found in make_lifespan"
        )
        assert setter_pos != -1, (
            ".set_repair_invoker_fn(...) not found in make_lifespan"
        )
        assert start_pos != -1, ".start_scheduler(...) not found in make_lifespan"

        # Full ordering: construct < factory < setter < start
        assert construct_pos < factory_pos, (
            "DependencyMapService(...) must be constructed BEFORE "
            "_make_dep_map_repair_invoker_fn(...) so the closure captures the real instance"
        )
        assert factory_pos < setter_pos, (
            "Factory must be called BEFORE .set_repair_invoker_fn(...) so we have a "
            "closure to bind"
        )
        assert setter_pos < start_pos, (
            "Story #927 Codex Pass 5: .set_repair_invoker_fn(...) MUST be called "
            "BEFORE .start_scheduler(...). Otherwise the scheduler daemon can fire "
            "with _repair_invoker_fn=None and crash auto-repair."
        )
