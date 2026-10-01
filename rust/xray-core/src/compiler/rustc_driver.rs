//! Issue #1934: the rustc subprocess invocation primitives, extracted
//! verbatim out of `compiler.rs` (pure move -- no behaviour change, see the
//! module doc comment on `super`). This is the "rustc driver/compile" seam
//! -- building and running the actual `rustc` command that compiles an
//! evaluator's assembled source into a `.so`.
//!
//! The three timing constants below (`RUSTC_COMPILE_TIMEOUT`/
//! `RUSTC_POLL_INTERVAL`/`RUSTC_DRAIN_TIMEOUT`) are deliberately hardcoded,
//! NOT Web-UI/config settings, per this project's standing rule against
//! adding configuration to gate a bug fix (see CLAUDE.md and memory
//! `feedback_no_settings_to_gate_bug_fixes.md`) -- unchanged from the
//! original `compiler.rs`.
//!
//! `run_rustc_with_timeout` is decomposed here into `spawn_rustc_child` +
//! `poll_rustc_until_done` + the drain step, purely to keep each function
//! under this repository's clean-code length guideline -- the control flow,
//! error paths, and return values are UNCHANGED from the original single
//! function; this is a mechanical extraction, not a logic change.

use super::types::{CompileError, CompileErrorKind};
use std::path::Path;
use std::process::{Child, Stdio};
use std::sync::mpsc::Receiver;
use std::time::Instant;

/// H2: upper bound on how long a single rustc invocation may run before
/// `run_rustc_with_timeout` kills its whole process group. Real evaluator
/// compiles take well under a second; this is a generous ceiling that
/// only fires for a genuinely pathological (compile-bomb) payload.
///
/// Hardcoded, like `graph::analyze::process::POLL_INTERVAL`/
/// `STDOUT_DRAIN_TIMEOUT`/`REAP_RETRY_COUNT` are for the identical class
/// of problem in that sibling module -- this project's standing rule is
/// to never add configuration to gate a bug fix.
pub(crate) const RUSTC_COMPILE_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(120);

/// How often `run_rustc_with_timeout` polls `try_wait()` while under budget.
const RUSTC_POLL_INTERVAL: std::time::Duration = std::time::Duration::from_millis(20);

/// Upper bound on waiting for the stdout/stderr reader threads to hand
/// back their collected bytes once rustc has already exited (or been
/// killed) -- a safety margin, not the expected wait (the pipe's write
/// end closes the moment the process, and anything it spawned holding the
/// fd open, is gone).
const RUSTC_DRAIN_TIMEOUT: std::time::Duration = std::time::Duration::from_secs(5);

/// Spawns a dedicated thread that drains `pipe` (a child's stdout or
/// stderr) into a `Vec<u8>` and sends it once the pipe closes. Mirrors
/// `graph::analyze::process::spawn_stdout_reader`'s established pattern
/// (Rule 4, anti-duplication) generalized over BOTH pipes so
/// `run_rustc_with_timeout` needs only one call site per pipe instead of
/// two near-identical closures.
///
/// `read_to_end`'s error and the channel `send`'s error are both
/// deliberately discarded (`let _ = ...`), matching
/// `spawn_stdout_reader`'s own already-reviewed rationale: a broken pipe
/// read has no corrective action available on a background thread, and a
/// disconnected receiver only happens when the caller itself gave up
/// waiting (see `RUSTC_DRAIN_TIMEOUT`) -- in both cases there is nothing
/// left to report to from INSIDE this thread; the caller-side drain
/// timeout is instead observably logged at the `recv_timeout` call site.
fn spawn_pipe_reader<R: std::io::Read + Send + 'static>(mut pipe: R) -> std::sync::mpsc::Receiver<Vec<u8>> {
    let (tx, rx) = std::sync::mpsc::channel();
    std::thread::spawn(move || {
        let mut buf = Vec::new();
        let _ = pipe.read_to_end(&mut buf);
        let _ = tx.send(buf);
    });
    rx
}

/// Drains `rx` (a `spawn_pipe_reader` receiver), bounded by
/// `RUSTC_DRAIN_TIMEOUT`. On a timeout or a disconnected channel (the
/// reader thread panicked before sending), emits an observable warning
/// naming `pipe_name` and degrades to empty bytes -- deliberately NEVER a
/// `CompileError`: `success` (this function's other return value) already
/// comes from the process's real exit status, independent of this drain,
/// so a slow/stuck drain must never mask a genuine compile failure behind
/// a different "could not read output" error -- it can only ever make
/// that failure's own message less detailed.
fn drain_pipe_or_warn(rx: std::sync::mpsc::Receiver<Vec<u8>>, pipe_name: &str) -> Vec<u8> {
    match rx.recv_timeout(RUSTC_DRAIN_TIMEOUT) {
        Ok(bytes) => bytes,
        Err(e) => {
            eprintln!(
                "Warning: failed to drain rustc's {} within {}s ({}); \
                 continuing with empty {} for this compile",
                pipe_name,
                RUSTC_DRAIN_TIMEOUT.as_secs(),
                e,
                pipe_name
            );
            Vec::new()
        }
    }
}

/// Bug #1816: builds the `rustc` invocation that compiles an evaluator's
/// assembled source into the `.so` `xray-cli` later loads across the
/// `GraphHandle` FFI boundary, pinned via `RUSTUP_TOOLCHAIN` to
/// `cache::pinned_toolchain_channel()` -- the SAME toolchain `cargo build`
/// uses to compile `xray-cli` itself, since both read the identical
/// `rust-toolchain.toml`. See `cache::pinned_toolchain_channel`'s doc
/// comment for the full root-cause writeup: without this pin, this
/// subprocess's own `rustc` binary resolution depended on the CALLING
/// PROCESS's current working directory, which in production sits outside
/// this repository entirely, letting the evaluator compile under a
/// DIFFERENT rustc version than the one that built `xray-cli` -- an ABI
/// mismatch that manifested as heap corruption (`free(): double free
/// detected in tcache 2` / SIGSEGV) the moment `analyze_graph` called both
/// `signature_for` and `shortest_path_to_any` in the same evaluator.
/// Extracted into its own function so the pin itself is directly testable
/// via `Command::get_envs()`, independent of ever actually invoking rustc.
pub(crate) fn evaluator_rustc_command(build_rs_path: &Path, build_so_path: &Path) -> std::process::Command {
    let mut command = std::process::Command::new("rustc");
    command
        .env("RUSTUP_TOOLCHAIN", crate::cache::pinned_toolchain_channel())
        .args([
            "--edition", "2021",
            "--crate-type", "cdylib",
            "-C", "opt-level=2",
            "-o", build_so_path.to_str().unwrap(),
            build_rs_path.to_str().unwrap(),
        ]);
    command
}

/// Clippy type-complexity escape hatch for `spawn_rustc_child`'s return
/// type: the spawned child, its pid, and the stdout/stderr drain-thread
/// receivers `run_rustc_with_timeout` needs after `poll_rustc_until_done`
/// resolves.
type SpawnedRustcChild = (Child, i32, Receiver<Vec<u8>>, Receiver<Vec<u8>>);

/// Spawns `command` (rustc) as its own process-group leader with piped
/// stdout/stderr, starting the background drain threads for both pipes.
/// Extracted out of `run_rustc_with_timeout` (mechanical split, no logic
/// change) -- see that function's doc comment for the full rationale
/// (process-group isolation, no bare `.output()`).
fn spawn_rustc_child(mut command: std::process::Command) -> Result<SpawnedRustcChild, CompileError> {
    use std::os::unix::process::CommandExt;

    command.process_group(0);
    command.stdout(Stdio::piped());
    command.stderr(Stdio::piped());

    let mut child = command.spawn().map_err(|e| CompileError {
        message: format!("Failed to invoke rustc: {}", e),
        details: vec!["Is rustc installed and on PATH?".to_string()],
        kind: CompileErrorKind::Infrastructure,
    })?;
    let pid = child.id() as i32;
    let stdout_rx = spawn_pipe_reader(child.stdout.take().expect("stdout piped above"));
    let stderr_rx = spawn_pipe_reader(child.stderr.take().expect("stderr piped above"));
    Ok((child, pid, stdout_rx, stderr_rx))
}

/// Polls `child` via `try_wait()` until it exits or `timeout` (measured
/// from `start`) elapses, in which case the whole process group is killed
/// via `reap_after_kill`. Extracted out of `run_rustc_with_timeout`
/// (mechanical split, no logic change) -- see that function's doc comment
/// for the full rationale.
fn poll_rustc_until_done(
    child: &mut Child,
    pid: i32,
    start: Instant,
    timeout: std::time::Duration,
) -> Result<bool, CompileError> {
    use crate::graph::analyze::process::reap_after_kill;

    loop {
        match child.try_wait() {
            Ok(Some(status)) => return Ok(status.success()),
            Ok(None) => {
                if start.elapsed() >= timeout {
                    reap_after_kill(child, pid);
                    return Err(CompileError {
                        message: format!(
                            "rustc compilation timed out after {}s and was killed",
                            timeout.as_secs()
                        ),
                        details: vec![],
                        // A genuinely pathological (compile-bomb) payload
                        // in the USER's own evaluator source is what this
                        // timeout is designed to catch (see
                        // RUSTC_COMPILE_TIMEOUT's own doc comment) --
                        // Compile, not Infrastructure.
                        kind: CompileErrorKind::Compile,
                    });
                }
                std::thread::sleep(RUSTC_POLL_INTERVAL);
            }
            Err(e) => {
                reap_after_kill(child, pid);
                return Err(CompileError {
                    message: format!("Failed to wait for rustc: {}", e),
                    details: vec![],
                    kind: CompileErrorKind::Infrastructure,
                });
            }
        }
    }
}

/// H2 (consolidated review, Issue #1811/Bug #1812, Codex): runs `command`
/// (rustc) to completion or until `timeout` elapses, whichever is first --
/// mirroring `graph::analyze::process::run_analyze_child`'s established
/// process-group-timeout-kill pattern exactly (Rule 4, anti-duplication:
/// reuses its `reap_after_kill` primitive -- which itself calls
/// `kill_process_group` -- rather than reimplementing either).
///
/// The bare `std::process::Command::output()` this replaces had NO
/// timeout at all -- a compile-expensive or genuinely hanging evaluator
/// escaped the pipeline's advertised timeout entirely. Worse, `output()`
/// never puts the child in its own process group, so a caller (Python)
/// that kills only the xray-cli PARENT process on ITS OWN timeout leaves
/// the already-running rustc CHILD (and any linker grandchild) orphaned,
/// continuing to consume CPU/RAM/disk with nothing left to account for
/// it. `command.process_group(0)` here makes the spawned pid a genuine
/// process-group leader, so a timeout-triggered kill reaches the whole
/// tree, not just rustc itself.
///
/// Returns `(success, stdout_bytes, stderr_bytes)` on a completed process
/// (regardless of exit code -- callers inspect `success`), or a
/// `CompileError` if the command could not be spawned, timed out, or
/// `try_wait` itself failed.
pub(crate) fn run_rustc_with_timeout(
    command: std::process::Command,
    timeout: std::time::Duration,
) -> Result<(bool, Vec<u8>, Vec<u8>), CompileError> {
    let (mut child, pid, stdout_rx, stderr_rx) = spawn_rustc_child(command)?;
    let start = Instant::now();
    let success = poll_rustc_until_done(&mut child, pid, start, timeout)?;

    let stdout = drain_pipe_or_warn(stdout_rx, "stdout");
    let stderr = drain_pipe_or_warn(stderr_rx, "stderr");
    Ok((success, stdout, stderr))
}
