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
pub fn run_analyze_child(mut command: Command, timeout: Duration) -> (AnalyzeStatus, Option<GraphResult>) {
    command.process_group(0);
    command.stdout(Stdio::piped());
    let mut child = match command.spawn() {
        Ok(c) => c,
        Err(_) => return (AnalyzeStatus::LoadFailed, None),
    };
    let pid = child.id() as i32;
    let stdout_rx = spawn_stdout_reader(&mut child);
    let start = Instant::now();

    loop {
        match child.try_wait() {
            Ok(Some(exit_status)) => return finish(exit_status.success(), &stdout_rx),
            Ok(None) => {
                if start.elapsed() >= timeout {
                    reap_after_kill(&mut child, pid);
                    return (AnalyzeStatus::TimedOut, None);
                }
                std::thread::sleep(POLL_INTERVAL);
            }
            Err(_) => {
                reap_after_kill(&mut child, pid);
                return (AnalyzeStatus::Panicked, None);
            }
        }
    }
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
}
