"""Regression tests for Bug #1378: progress bar denominator frozen after
the first `add_task()` call.

Root cause (confirmed by code trace, more precise than the bug report's
initial guess): both `MultiThreadedProgressManager.update_complete_state`
(progress/multi_threaded_display.py -- the class actually wired to the CLI's
`cidx index --index-commits` display) and `AggregateProgressDisplay.update_progress`
(progress/aggregate_progress.py -- named directly in the bug report, though
currently unused outside its own unit tests) call Rich's
`Progress.add_task(..., total=total)` exactly ONCE (guarded by a "has the
task started" flag) and afterwards only ever call `Progress.update(task_id,
completed=..., ...)` WITHOUT ever passing `total=` again.

Rich's `Task.percentage`/ETA are computed from the task's internal `.total`,
which is therefore frozen at whatever value was supplied at creation time.
When a caller's notion of `total` legitimately changes between calls (e.g.
temporal indexing moving from one quarterly shard to the next, or from one
configured embedder's run to another), the bar % and ETA silently keep
referencing the STALE frozen total while any text field built from the live
`total` parameter (e.g. "X/Y commits") shows the fresh value -- producing
exactly the contradictory display reported in Bug #1378 (bar pegged at 100%
while the commit counter shows single-digit percent completion).

Fix: pass `total=total` on every `.update()` call too, so Rich's internal
task total can never go stale.
"""

from rich.console import Console

from code_indexer.progress.multi_threaded_display import MultiThreadedProgressManager
from code_indexer.progress.aggregate_progress import AggregateProgressDisplay


class TestMultiThreadedProgressBarDenominatorRefresh:
    """MultiThreadedProgressManager is the class actually wired to the CLI's
    temporal (`--index-commits`) progress display via cli.py's
    `update_commit_progress` -> `progress_manager.update_complete_state`.
    """

    def test_bar_total_refreshes_on_update_when_total_changes(self):
        """A second update_complete_state() call with a DIFFERENT total must
        refresh the underlying Rich task's .total -- not leave it frozen at
        whatever the first call (add_task time) supplied.
        """
        console = Console()
        manager = MultiThreadedProgressManager(console=console, max_slots=4)

        # First call: creates the task with total=154 (simulates the first,
        # small quarterly shard in Bug #1378's repro).
        manager.update_complete_state(
            current=92,
            total=154,
            files_per_second=1.0,
            kb_per_second=1.0,
            active_threads=4,
            concurrent_files=[],
            slot_tracker=None,
            info="92/154 commits (60%)",
            item_type="commits",
        )
        task = manager.progress.tasks[0]
        assert task.total == 154
        assert round(task.percentage) == 60

        # Second call: total grows to the whole-run denominator (8008).
        # Before the fix, Rich's task.total stays frozen at 154, so
        # percentage/ETA keep referencing the stale denominator even though
        # the visible "X/Y" counter text shows the fresh total.
        manager.update_complete_state(
            current=174,
            total=8008,
            files_per_second=1.0,
            kb_per_second=1.0,
            active_threads=4,
            concurrent_files=[],
            slot_tracker=None,
            info="174/8008 commits (2%)",
            item_type="commits",
        )
        task = manager.progress.tasks[0]
        assert task.total == 8008, (
            f"BUG #1378: task.total should refresh to 8008, got {task.total} "
            f"(frozen at first add_task() call)"
        )
        # Percentage must reflect the SAME denominator as the visible counter
        # text -- never pegged at/near 100% while thousands of commits remain.
        assert round(task.percentage, 1) == round(174 / 8008 * 100, 1)

    def test_bar_total_stable_when_total_unchanged(self):
        """Sanity/no-regression: repeated calls with an UNCHANGED total must
        not disturb the task -- this is the common (non-temporal, file
        indexing) case and must remain a no-op.
        """
        console = Console()
        manager = MultiThreadedProgressManager(console=console, max_slots=4)

        manager.update_complete_state(
            current=10,
            total=100,
            files_per_second=1.0,
            kb_per_second=1.0,
            active_threads=4,
            concurrent_files=[],
            slot_tracker=None,
            info="10/100 files",
            item_type="files",
        )
        manager.update_complete_state(
            current=50,
            total=100,
            files_per_second=1.0,
            kb_per_second=1.0,
            active_threads=4,
            concurrent_files=[],
            slot_tracker=None,
            info="50/100 files",
            item_type="files",
        )
        task = manager.progress.tasks[0]
        assert task.total == 100
        assert round(task.percentage) == 50


class TestAggregateProgressBarDenominatorRefresh:
    """AggregateProgressDisplay is explicitly named as an affected file in
    Bug #1378's root-cause analysis. It is not currently wired into the CLI
    temporal display path, but carries the identical structural defect --
    fixed for consistency and to prevent the same bug resurfacing if this
    class is wired up later.
    """

    def test_bar_total_refreshes_on_update_when_total_changes(self):
        console = Console()
        display = AggregateProgressDisplay(console=console)

        display.update_progress(
            current=92,
            total=154,
            elapsed_seconds=10.0,
            estimated_remaining=5.0,
            files_per_second=1.0,
            kb_per_second=1.0,
            active_threads=4,
            item_type="commits",
        )
        assert display.progress_bar.tasks[0].total == 154

        display.update_progress(
            current=174,
            total=8008,
            elapsed_seconds=20.0,
            estimated_remaining=500.0,
            files_per_second=1.0,
            kb_per_second=1.0,
            active_threads=4,
            item_type="commits",
        )
        task = display.progress_bar.tasks[0]
        assert task.total == 8008, (
            f"BUG #1378: task.total should refresh to 8008, got {task.total}"
        )
        assert round(task.percentage, 1) == round(174 / 8008 * 100, 1)
