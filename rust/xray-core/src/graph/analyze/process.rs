//! AC7: the killable-child-process container. `run_analyze_child` spawns
//! the given `Command` as its OWN process group (so a hang that itself
//! forked grandchildren is killable as a unit), polls for completion up
//! to `timeout`, and on timeout SIGKILLs the whole group -- never the
//! single child pid alone (`std::process::Child::kill()` cannot reach
//! grandchildren). A panic INSIDE the child is expected to be caught by
//! the child's own `catch_unwind` boundary (the `--analyze-graph`
//! subcommand) and self-reported as JSON on stdout; this parent-side
//! function never needs to distinguish "child panicked" from "child
//! finished normally" via its OWN exit-code inspection except as a
//! fallback when the child's self-report is missing/malformed.
//!
//! Stdout is drained by a DEDICATED reader thread, started immediately
//! after spawn -- never read only after the child exits. The OS pipe
//! buffer is finite (commonly 64KB on Linux); a child writing more than
//! that while nobody is reading would block in its own `write()` call
//! forever, which from the poll loop's point of view is
//! indistinguishable from a genuine hang and would be misreported as
//! `TimedOut` instead of the child's real (successful) outcome.

use super::result::AnalyzeStatus;
use super::result::GraphResult;
use serde::{Deserialize, Serialize};
use std::io::Read;
use std::ffi::CString;
use std::os::unix::ffi::OsStrExt;
use std::os::unix::process::CommandExt;
use std::process::{Child, Command, Stdio};
use std::sync::mpsc::{self, Receiver};
use std::time::{Duration, Instant};

/// How often `run_analyze_child` polls `try_wait()` while under budget,
/// and the retry interval `reap_after_kill` uses while waiting for a
/// SIGKILLed process to become reapable.
const POLL_INTERVAL: Duration = Duration::from_millis(20);

/// Upper bound on `reap_after_kill`'s retry loop: `SIGKILL` cannot be
/// caught or blocked, so a process that was really sent it becomes
/// reapable almost immediately -- this bound exists only so a failed
/// `kill(2)` call (e.g. `ESRCH`, the child already exited on its own)
/// can never turn into an unbounded blocking wait (Rule 14).
const REAP_RETRY_COUNT: u32 = 50;

/// Upper bound on waiting for the stdout-reader thread to hand back the
/// bytes it collected, once the child process itself has already exited
/// (or been killed) -- the reader's `read_to_end` returns as soon as the
/// pipe's write end closes, which happens the moment the child (and any
/// process it spawned holding the fd open) is gone, so this is a safety
/// margin, not the expected wait.
const STDOUT_DRAIN_TIMEOUT: Duration = Duration::from_secs(2);

/// The JSON envelope the analyze CHILD process writes to stdout to
/// self-report its own outcome -- the wire format `run_analyze_child`
/// parses on a normal (non-timeout) exit.
#[derive(Debug, Serialize, Deserialize)]
pub struct ChildReport {
    pub status: AnalyzeStatus,
    pub result: Option<GraphResult>,
}

/// Sends `SIGKILL` to the WHOLE process group led by `pid` -- never just
/// the single pid. `command.process_group(0)` (called by
/// `run_analyze_child` before spawning) is what makes `pid` a genuine
/// process-group leader, so this also reaches any grandchild the analyze
/// child itself spawned before it needed to be killed. Returns whether
/// the syscall itself reported success -- `reap_after_kill` uses this to
/// decide whether to retry, rather than discarding it.
fn kill_process_group(pid: i32) -> bool {
    // SAFETY: `libc::kill` with a negative pid targets a process GROUP
    // rather than a single process, per POSIX `kill(2)`. Sending SIGKILL
    // to a group this process itself just spawned (and is the leader of)
    // has no memory-safety implications -- it is a plain syscall wrapper.
    let result = unsafe { libc::kill(-pid, libc::SIGKILL) };
    result == 0
}

/// Kills `child`'s process group and waits for it to become reapable, via
/// a BOUNDED retry loop (`REAP_RETRY_COUNT * POLL_INTERVAL`, never a
/// blocking `child.wait()`). Retries the kill syscall itself (at most
/// once per poll iteration) whenever the most recent attempt reported
/// failure and the child still hasn't been reaped -- most commonly
/// meaningless (the process already exited, `try_wait` will confirm that
/// on the next check), but never silently ignored either.
fn reap_after_kill(child: &mut Child, pid: i32) {
    let mut killed = kill_process_group(pid);
    for _ in 0..REAP_RETRY_COUNT {
        if matches!(child.try_wait(), Ok(Some(_))) {
            return;
        }
        if !killed {
            killed = kill_process_group(pid);
        }
        std::thread::sleep(POLL_INTERVAL);
    }
}

/// Spawns a dedicated thread that drains `child`'s piped stdout into a
/// `Vec<u8>` and sends it once the pipe closes (child exit or kill).
/// Started immediately after `spawn()` -- see module docs for why
/// reading only after exit would deadlock on a large enough payload.
fn spawn_stdout_reader(child: &mut Child) -> Receiver<Vec<u8>> {
    let mut stdout = child.stdout.take().expect("run_analyze_child always pipes stdout before spawning");
    let (tx, rx) = mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = stdout.read_to_end(&mut buf);
        let _ = tx.send(buf);
    });
    rx
}

/// Runs `command` to completion or until `timeout` elapses, whichever is
/// first. Terminates by construction (Rule 14): the poll loop runs at
/// most `ceil(timeout / POLL_INTERVAL)` iterations before the timeout
/// branch fires, `reap_after_kill` is itself bounded, and the final
/// stdout drain is bounded by `STDOUT_DRAIN_TIMEOUT`.
///
/// Returns `AnalyzeStatus::LoadFailed` ONLY when `command` could not even
/// be spawned -- every other status implies the child process genuinely
/// existed, including the rare `try_wait` OS-error path, treated as
/// `Panicked` (an abnormal termination this process could not cleanly
/// observe) rather than misreported as a spawn failure.
pub fn run_analyze_child(command: Command, timeout: Duration) -> (AnalyzeStatus, Option<GraphResult>) {
    run_analyze_child_with_memory_limit(command, timeout, None, &super::memory_ceiling::NoopMemoryCeiling)
}

/// Story #1787 AC15, step 1: creates the containment boundary when a
/// limit was admitted. Returns whether containment is genuinely active --
/// a `create` failure DEGRADES to `false` (never aborts the job).
fn activate_memory_ceiling(memory_limit_bytes: Option<u64>, ceiling: &dyn super::memory_ceiling::MemoryCeiling) -> bool {
    match memory_limit_bytes {
        Some(limit_bytes) => ceiling.create(limit_bytes).is_ok(),
        None => false,
    }
}

/// Story #1787 AC15, step 2: overrides `outcome` to
/// `AnalyzeStatus::AbortedMemoryLimit` only when containment was
/// genuinely active AND the kernel actually OOM-killed something inside
/// it -- never inferred from a bare exit code. Always tears down the
/// containment boundary, on every path.
fn finalize_with_memory_ceiling(
    containment_active: bool,
    ceiling: &dyn super::memory_ceiling::MemoryCeiling,
    outcome: (AnalyzeStatus, Option<GraphResult>),
) -> (AnalyzeStatus, Option<GraphResult>) {
    let final_outcome =
        if containment_active && ceiling.oom_killed() { (AnalyzeStatus::AbortedMemoryLimit, None) } else { outcome };
    ceiling.cleanup();
    final_outcome
}

/// Story #1787 AC15: identical to `run_analyze_child`, plus an OS-level
/// memory ceiling derived from the admitted estimate.
/// `ceiling.add_pid(pid)` is attempted ONLY when `ceiling.create`
/// already succeeded -- if `create` failed there is no containment
/// boundary to add the pid into, so `add_pid` is deliberately skipped
/// rather than called against a nonexistent boundary. Either step
/// failing degrades containment to inactive for this run rather than
/// aborting the job -- see `activate_memory_ceiling` and
/// `finalize_with_memory_ceiling` for the two containment-specific steps
/// this wraps around the unchanged spawn/poll/timeout/reap loop.
/// Story #1787 AC15 spawn-race fix: writes `pid` (as decimal ASCII) into
/// the file at `path`, using ONLY async-signal-safe raw syscalls
/// (`open`/`write`/`close`) and a fixed-size stack buffer -- no heap
/// allocation, locking, or other libstd machinery that could deadlock
/// after `fork()` in a multithreaded parent (`std::fs::write` is NOT
/// safe to call from a `pre_exec` hook for exactly this reason). Called
/// from a `pre_exec` hook, which runs in the child strictly between
/// `fork()` and `execve()`. Every failure is swallowed (best-effort): a
/// self-add failure must degrade to uncontained execution, never abort
/// the spawn (a `pre_exec` closure returning `Err` fails the whole
/// `Command::spawn()` call in the parent).
fn write_pid_signal_safe(path: &std::ffi::CStr, pid: u32) {
    const MAX_U32_DIGITS: usize = 10;
    // Rule 14 (anti-unbounded-loop): bounds the EINTR retry loop below --
    // 10 bytes is at most 10 individual signal-interrupted write() calls
    // in the worst case (one byte transferred per interruption), so this
    // is already generous; it exists purely to make termination provable
    // rather than to reflect an expected retry count.
    const MAX_EINTR_RETRIES: u32 = 32;

    let mut buf = [0u8; MAX_U32_DIGITS];
    let mut i = buf.len();
    let mut n = pid;
    if n == 0 {
        i -= 1;
        buf[i] = b'0';
    } else {
        while n > 0 {
            i -= 1;
            buf[i] = b'0' + (n % 10) as u8;
            n /= 10;
        }
    }
    let to_write = &buf[i..];
    // SAFETY: open/write/close are async-signal-safe POSIX syscalls.
    // `path` is a valid, NUL-terminated C string built by the caller
    // BEFORE fork(); `to_write` is a stack-local slice -- no heap
    // allocation occurs anywhere in this function.
    unsafe {
        let fd = libc::open(path.as_ptr(), libc::O_WRONLY | libc::O_CREAT | libc::O_TRUNC, 0o644);
        if fd < 0 {
            return;
        }
        let mut written = 0usize;
        let mut eintr_retries = 0u32;
        while written < to_write.len() && eintr_retries < MAX_EINTR_RETRIES {
            let n = libc::write(
                fd,
                to_write[written..].as_ptr() as *const libc::c_void,
                to_write.len() - written,
            );
            if n > 0 {
                written += n as usize;
                continue;
            }
            // A signal interrupted the syscall before any bytes were
            // transferred -- retry, bounded by MAX_EINTR_RETRIES. Any
            // other outcome (n == 0, or n < 0 for a reason other than
            // EINTR) abandons the write: this is best-effort, a
            // partial/failed write degrades containment, it never
            // aborts the spawn.
            if n < 0 && *libc::__errno_location() == libc::EINTR {
                eintr_retries += 1;
                continue;
            }
            break;
        }
        // close()'s return value is intentionally discarded: this is a
        // best-effort self-add write, and a close() failure (e.g. EINTR)
        // has no corrective action available inside an async-signal-safe
        // pre_exec hook -- the fd is either already closed or will be
        // reclaimed when this process image is replaced by execve().
        let _ = libc::close(fd);
    }
}

pub fn run_analyze_child_with_memory_limit(
    mut command: Command,
    timeout: Duration,
    memory_limit_bytes: Option<u64>,
    ceiling: &dyn super::memory_ceiling::MemoryCeiling,
) -> (AnalyzeStatus, Option<GraphResult>) {
    let mut containment_active = activate_memory_ceiling(memory_limit_bytes, ceiling);

    command.process_group(0);
    command.stdout(Stdio::piped());

    // AC15 spawn-race fix: without this, the window between spawn()
    // returning and the parent's own add_pid(pid) call below lets the
    // child run (and allocate) completely uncontained. When the ceiling
    // exposes a self_add_path, install a pre_exec hook so the CHILD
    // joins the boundary itself, strictly BEFORE execve() -- closing
    // that window instead of merely narrowing it.
    if containment_active {
        if let Some(self_add_path) = ceiling.self_add_path() {
            if let Ok(path_cstring) = CString::new(self_add_path.as_os_str().as_bytes()) {
                // SAFETY: this closure runs in the freshly forked child,
                // between fork() and execve(). It calls ONLY
                // write_pid_signal_safe (async-signal-safe raw syscalls,
                // no heap allocation) and std::process::id() (a plain
                // getpid() wrapper), and always returns Ok(()) -- a
                // pre_exec closure returning Err would fail the whole
                // Command::spawn() call in the PARENT, turning a
                // self-add failure into a total spawn failure instead of
                // the intended degrade-to-uncontained behavior.
                unsafe {
                    command.pre_exec(move || {
                        write_pid_signal_safe(&path_cstring, std::process::id());
                        Ok(())
                    });
                }
            }
        }
    }

    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(_) => {
            ceiling.cleanup();
            return (AnalyzeStatus::LoadFailed, None);
        }
    };
    let pid = child.id() as i32;
    if containment_active {
        containment_active = ceiling.add_pid(pid).is_ok();
    }
    let stdout_rx = spawn_stdout_reader(&mut child);
    let start = Instant::now();

    let outcome = loop {
        match child.try_wait() {
            Ok(Some(exit_status)) => break finish(exit_status.success(), &stdout_rx),
            Ok(None) => {
                if start.elapsed() >= timeout {
                    reap_after_kill(&mut child, pid);
                    break (AnalyzeStatus::TimedOut, None);
                }
                std::thread::sleep(POLL_INTERVAL);
            }
            Err(_) => {
                reap_after_kill(&mut child, pid);
                break (AnalyzeStatus::Panicked, None);
            }
        }
    };

    finalize_with_memory_ceiling(containment_active, ceiling, outcome)
}

/// Drains the stdout-reader thread's collected bytes (bounded wait) and
/// parses the child's JSON self-report. A non-zero exit, a drain
/// timeout, or a malformed/missing report all fail loud as `Panicked` --
/// exactly what an UNCAUGHT abort (a double panic, or a signal the
/// child's own `catch_unwind` boundary cannot intercept) looks like from
/// here. Never silently mapped onto `RanOk` with an empty result, which
/// would look identical to "analyzed a graph with zero findings".
fn finish(exited_successfully: bool, stdout_rx: &Receiver<Vec<u8>>) -> (AnalyzeStatus, Option<GraphResult>) {
    let stdout_bytes = stdout_rx.recv_timeout(STDOUT_DRAIN_TIMEOUT).unwrap_or_default();
    if !exited_successfully {
        return (AnalyzeStatus::Panicked, None);
    }
    match serde_json::from_slice::<ChildReport>(&stdout_bytes) {
        Ok(report) => (report.status, report.result),
        Err(_) => (AnalyzeStatus::Panicked, None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn shell_command(script: &str) -> Command {
        let mut command = Command::new("sh");
        command.arg("-c").arg(script);
        command
    }

    /// AC7's core discriminating requirement: a child that never
    /// terminates (here, a shell spawning `sleep 100` as a grandchild)
    /// must be KILLED -- process group and all -- once `timeout` elapses,
    /// and `run_analyze_child` must return promptly (well under the
    /// hung child's own 100s sleep) with the DISTINCT `TimedOut` status,
    /// never confused with a successful empty result.
    #[test]
    fn a_hanging_child_is_killed_at_timeout_and_reported_as_timed_out() {
        let command = shell_command("sleep 100");
        let start = Instant::now();
        let (status, result) = run_analyze_child(command, Duration::from_millis(200));
        let elapsed = start.elapsed();

        assert_eq!(status, AnalyzeStatus::TimedOut);
        assert!(result.is_none());
        assert!(
            elapsed < Duration::from_secs(5),
            "run_analyze_child must return promptly after killing the hung child, took {elapsed:?}"
        );
    }

    /// A child that exits successfully with a valid `ran_ok` self-report
    /// must be reported EXACTLY as that status with its real result --
    /// never re-interpreted or discarded.
    #[test]
    fn a_child_that_reports_ran_ok_is_passed_through_verbatim() {
        let json = r#"{"status":"ran_ok","result":{"findings":[],"refine":[]}}"#;
        let command = shell_command(&format!("printf '%s' '{json}'"));
        let (status, result) = run_analyze_child(command, Duration::from_secs(5));

        assert_eq!(status, AnalyzeStatus::RanOk);
        assert_eq!(result, Some(GraphResult { findings: vec![], refine: vec![] }));
    }

    /// A child that self-reports `panicked` (its OWN `catch_unwind`
    /// caught a real panic inside the loaded evaluator) must be reported
    /// as `Panicked`, DISTINCT from `TimedOut` and from `RanOk` -- never
    /// collapsed into either.
    #[test]
    fn a_child_that_self_reports_panicked_is_reported_as_panicked_not_timed_out_or_ran_ok() {
        let json = r#"{"status":"panicked","result":null}"#;
        let command = shell_command(&format!("printf '%s' '{json}'"));
        let (status, result) = run_analyze_child(command, Duration::from_secs(5));

        assert_eq!(status, AnalyzeStatus::Panicked);
        assert!(result.is_none());
    }

    /// A child that dies abnormally (nonzero exit, e.g. an uncaught
    /// SIGSEGV/SIGABRT the child's own `catch_unwind` boundary could
    /// never intercept) with no usable stdout must ALSO be reported as
    /// `Panicked` -- never silently treated as `RanOk` with no findings.
    #[test]
    fn a_child_that_exits_nonzero_with_no_report_is_reported_as_panicked() {
        let command = shell_command("exit 1");
        let (status, result) = run_analyze_child(command, Duration::from_secs(5));

        assert_eq!(status, AnalyzeStatus::Panicked);
        assert!(result.is_none());
    }

    /// Spawning a nonexistent binary must be a distinct `LoadFailed`,
    /// never confused with any status that implies the child process
    /// actually existed.
    #[test]
    fn a_command_that_cannot_even_be_spawned_is_reported_as_load_failed() {
        let command = Command::new("/definitely/does/not/exist/xray-analyze-child");
        let (status, result) = run_analyze_child(command, Duration::from_secs(5));

        assert_eq!(status, AnalyzeStatus::LoadFailed);
        assert!(result.is_none());
    }

    use super::super::memory_ceiling::FakeMemoryCeiling;

    /// Shared scaffolding for the AC15 memory-limit tests below: a child
    /// that always self-reports a real, successful `ran_ok` with an
    /// empty result. Deduplicates the identical JSON/shell-command setup
    /// every memory-limit test otherwise needs.
    fn ran_ok_command() -> Command {
        let json = r#"{"status":"ran_ok","result":{"findings":[],"refine":[]}}"#;
        shell_command(&format!("printf '%s' '{json}'"))
    }

    fn ran_ok_result() -> Option<GraphResult> {
        Some(GraphResult { findings: vec![], refine: vec![] })
    }

    /// A successful, non-OOM run under an active memory limit must
    /// report its REAL status unchanged (never overridden), and must
    /// have exercised the full containment lifecycle: create, add_pid
    /// (with the real child pid), and cleanup.
    #[test]
    fn memory_limit_run_that_completes_normally_reports_real_status_and_exercises_full_containment_lifecycle() {
        let ceiling = FakeMemoryCeiling::default();

        let (status, result) = run_analyze_child_with_memory_limit(
            ran_ok_command(),
            Duration::from_secs(5),
            Some(256 * 1024 * 1024),
            &ceiling,
        );

        assert_eq!(status, AnalyzeStatus::RanOk);
        assert_eq!(result, ran_ok_result());
        assert!(ceiling.create_called.get());
        assert!(ceiling.add_pid_called_with.get().is_some(), "the real child pid must have been added to containment");
        assert!(ceiling.oom_killed_called.get());
        assert!(ceiling.cleanup_called.get(), "cleanup must run on the normal-completion path");
    }

    /// THE central AC15 discriminating test: when containment was active
    /// and the fake ceiling reports a genuine OOM-kill, the final status
    /// must be the DISTINCT `AbortedMemoryLimit` -- even though the child
    /// itself exited with a self-reported `ran_ok` (simulating a kernel
    /// SIGKILL racing with a child that had already begun writing its
    /// report; the containment signal must win).
    #[test]
    fn memory_limit_run_that_was_oom_killed_is_reported_as_aborted_memory_limit() {
        let ceiling = FakeMemoryCeiling { simulate_oom: true, ..FakeMemoryCeiling::default() };

        let (status, result) =
            run_analyze_child_with_memory_limit(ran_ok_command(), Duration::from_secs(5), Some(1024), &ceiling);

        assert_eq!(status, AnalyzeStatus::AbortedMemoryLimit);
        assert!(result.is_none());
        assert!(ceiling.cleanup_called.get(), "cleanup must run on the OOM-override path");
    }

    /// AC15 degrade contract: when `ceiling.create` fails (no cgroup
    /// delegation), containment must be treated as INACTIVE -- `add_pid`
    /// must never be called against a boundary that was never created,
    /// `oom_killed` must never be consulted to override the outcome
    /// (even if the fake were configured to claim one), and the job's
    /// REAL status must be reported unchanged.
    #[test]
    fn memory_limit_create_failure_degrades_to_no_containment_and_never_calls_add_pid_or_overrides_status() {
        let ceiling = FakeMemoryCeiling { create_fails: true, simulate_oom: true, ..FakeMemoryCeiling::default() };

        let (status, result) =
            run_analyze_child_with_memory_limit(ran_ok_command(), Duration::from_secs(5), Some(1024), &ceiling);

        assert_eq!(status, AnalyzeStatus::RanOk, "create() failing must degrade to the real, unoverridden status");
        assert_eq!(result, ran_ok_result());
        assert!(ceiling.create_called.get());
        assert!(ceiling.add_pid_called_with.get().is_none(), "add_pid must never be called when create() failed");
        assert!(ceiling.cleanup_called.get(), "cleanup must run on the create-failure degrade path");
    }

    /// AC15 degrade contract, second half: `create` succeeds but
    /// `add_pid` fails (e.g. the child raced ahead of the write) --
    /// containment must ALSO degrade to inactive, so a later
    /// `simulate_oom` is never consulted to override the real status.
    #[test]
    fn memory_limit_add_pid_failure_also_degrades_to_no_containment_and_never_overrides_status() {
        let ceiling = FakeMemoryCeiling { add_pid_fails: true, simulate_oom: true, ..FakeMemoryCeiling::default() };

        let (status, result) =
            run_analyze_child_with_memory_limit(ran_ok_command(), Duration::from_secs(5), Some(1024), &ceiling);

        assert_eq!(status, AnalyzeStatus::RanOk, "add_pid() failing must ALSO degrade to the real, unoverridden status");
        assert_eq!(result, ran_ok_result());
        assert!(ceiling.create_called.get());
        assert!(ceiling.add_pid_called_with.get().is_some(), "add_pid must have been attempted since create() succeeded");
        assert!(ceiling.cleanup_called.get(), "cleanup must run on the add_pid-failure degrade path");
    }

    /// THE spawn-race discriminator: when the ceiling exposes a
    /// `self_add_path`, the CHILD must write its own pid into that path
    /// via a `pre_exec` hook -- executed strictly BEFORE `execve()` --
    /// closing the fork-to-add_pid race window where an uncontained
    /// child could otherwise run and allocate freely between spawn()
    /// returning and the parent's own add_pid(pid) call landing.
    ///
    /// The exec'd program's FIRST action is to compare self_add_path's
    /// contents against its own `$$` (unchanged across exec) and only
    /// self-report `ran_ok` if they already match -- reporting
    /// `panicked` otherwise. This proves ORDERING (the write landed
    /// before this program ever ran), not merely that the file
    /// eventually held the right value after the fact.
    #[test]
    fn memory_limit_self_add_path_receives_the_real_child_pid_before_exec() {
        const TEST_TIMEOUT: Duration = Duration::from_secs(5);
        const TEST_MEMORY_LIMIT_BYTES: u64 = 1024;

        let tmp = tempfile::tempdir().unwrap();
        let self_add_path = tmp.path().join("cgroup.procs");

        let ceiling =
            FakeMemoryCeiling { self_add_path: Some(self_add_path.clone()), ..FakeMemoryCeiling::default() };

        let ran_ok_json = r#"{"status":"ran_ok","result":{"findings":[],"refine":[]}}"#;
        let panicked_json = r#"{"status":"panicked","result":null}"#;
        let command = shell_command(&format!(
            "[ \"$(cat '{path}' 2>/dev/null)\" = \"$$\" ] && printf '%s' '{ran_ok_json}' || printf '%s' '{panicked_json}'",
            path = self_add_path.display()
        ));

        let (status, result) =
            run_analyze_child_with_memory_limit(command, TEST_TIMEOUT, Some(TEST_MEMORY_LIMIT_BYTES), &ceiling);

        assert_eq!(
            status,
            AnalyzeStatus::RanOk,
            "self_add_path must already contain the child's own pid BEFORE this exec'd program's \
             first instruction ran (pre_exec ordering) -- a Panicked status here means the write \
             had not landed in time, i.e. the AC15 spawn race is still open"
        );
        assert_eq!(result, ran_ok_result());
        assert!(
            ceiling.add_pid_called_with.get().is_some(),
            "the parent-side add_pid(pid) backstop must still run even when self_add_path is used"
        );
    }
}
