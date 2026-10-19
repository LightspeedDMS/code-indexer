//! Child process groups (the evaluator's `rustc` and the linker it starts)
//! never outlive `xray-cli`.
//!
//! A child that leads its own process group is out of reach of a caller
//! that kills `xray-cli`'s group, so three mechanisms cover every way
//! `xray-cli` can end:
//!
//! - `die_with_parent`: the kernel SIGKILLs the child when `xray-cli`
//!   dies, even by SIGKILL (nothing in `xray-cli` can run then);
//! - `install_termination_handler`: on SIGTERM/SIGINT/SIGHUP, every tracked
//!   group is SIGKILLed and its leader reaped before `xray-cli` exits, so a
//!   caller that saw `xray-cli` exit knows the leader is gone;
//! - `TrackedGroup`'s drop: a panic that unwinds past a running child kills
//!   its group.

use std::os::unix::process::CommandExt;
use std::process::Command;
use std::sync::atomic::{AtomicI32, Ordering};

/// Upper bound on child groups tracked at once; one beyond it is still
/// covered by `die_with_parent`, only not by the termination handler.
const MAX_TRACKED_GROUPS: usize = 16;

/// Leader pids of the live tracked groups; 0 = free slot. A plain static
/// array of atomics so the signal handler reads it without locking.
static LIVE_GROUPS: [AtomicI32; MAX_TRACKED_GROUPS] = [const { AtomicI32::new(0) }; MAX_TRACKED_GROUPS];

/// Reap attempts per leader inside the termination handler, `REAP_POLL_NANOS`
/// apart: SIGKILL makes a leader reapable almost at once, so 200 x 10 ms is
/// a bound, not an expected wait.
const REAP_POLLS: u32 = 200;
const REAP_POLL_NANOS: libc::c_long = 10_000_000;

/// The signals after which `xray-cli` kills its tracked groups and exits.
const TERMINATION_SIGNALS: [libc::c_int; 3] = [libc::SIGTERM, libc::SIGINT, libc::SIGHUP];

/// Makes the child `command` spawns receive SIGKILL when this process dies.
/// If this process died before the child armed the signal, the child exits
/// at once instead of running unparented.
pub fn die_with_parent(command: &mut Command) {
    let parent = std::process::id() as libc::pid_t;
    //^ ext PR_SET_PDEATHSIG=fires when the spawning THREAD exits;spawn and wait stay on one thread
    // SAFETY: prctl/getppid/_exit are async-signal-safe raw syscalls; the
    // closure allocates nothing, so it is sound between fork and exec.
    unsafe {
        command.pre_exec(move || {
            if libc::prctl(libc::PR_SET_PDEATHSIG, libc::SIGKILL) != 0 {
                return Err(std::io::Error::last_os_error());
            }
            if libc::getppid() != parent {
                libc::_exit(1);
            }
            Ok(())
        });
    }
}

/// A live child group, killed on drop unless `mark_finished` was called.
pub struct TrackedGroup {
    leader: i32,
    slot: Option<usize>,
    finished: bool,
}

impl TrackedGroup {
    /// Starts tracking the group led by `leader` (a child spawned with
    /// `process_group(0)`).
    pub fn track(leader: i32) -> Self {
        let slot = LIVE_GROUPS
            .iter()
            .position(|s| s.compare_exchange(0, leader, Ordering::SeqCst, Ordering::SeqCst).is_ok());
        if slot.is_none() {
            eprintln!(
                "Warning: more than {} child process groups at once; group {} is covered only by \
                 the parent-death signal",
                MAX_TRACKED_GROUPS, leader
            );
        }
        TrackedGroup { leader, slot, finished: false }
    }

    /// The leader exited or was killed and reaped: dropping must not signal
    /// its (possibly reused) group id.
    pub fn mark_finished(&mut self) {
        self.finished = true;
    }
}

impl Drop for TrackedGroup {
    fn drop(&mut self) {
        if let Some(slot) = self.slot {
            LIVE_GROUPS[slot].store(0, Ordering::SeqCst);
        }
        if !self.finished {
            // SAFETY: kill(2) on the group this process spawned and still owns.
            unsafe {
                libc::kill(-self.leader, libc::SIGKILL);
            }
        }
    }
}

/// Async-signal-safe: only atomics, kill, waitpid, nanosleep and _exit.
extern "C" fn on_termination_signal(sig: libc::c_int) {
    for slot in LIVE_GROUPS.iter() {
        let leader = slot.load(Ordering::SeqCst);
        if leader > 0 {
            // SAFETY: kill(2) is async-signal-safe.
            unsafe {
                libc::kill(-leader, libc::SIGKILL);
            }
        }
    }
    for slot in LIVE_GROUPS.iter() {
        let leader = slot.load(Ordering::SeqCst);
        if leader > 0 {
            reap_leader(leader);
        }
    }
    // SAFETY: _exit(2) is async-signal-safe and never returns.
    unsafe { libc::_exit(128 + sig) }
}

/// Waits (bounded) until `leader` is reaped here or by the thread that owns
/// it: waitpid returns the pid once reaped, -1 when another thread already
/// reaped it, 0 while it is still running.
fn reap_leader(leader: i32) {
    let pause = libc::timespec { tv_sec: 0, tv_nsec: REAP_POLL_NANOS };
    for _ in 0..REAP_POLLS {
        // SAFETY: waitpid/nanosleep are async-signal-safe; null status and
        // remainder pointers are allowed by both.
        let reaped = unsafe { libc::waitpid(leader, std::ptr::null_mut(), libc::WNOHANG) };
        if reaped != 0 {
            return;
        }
        unsafe {
            libc::nanosleep(&pause, std::ptr::null_mut());
        }
    }
}

/// Installs the termination handler for SIGTERM, SIGINT and SIGHUP. Call
/// once, early in `main`.
pub fn install_termination_handler() -> std::io::Result<()> {
    for sig in TERMINATION_SIGNALS {
        // SAFETY: a zeroed sigaction is a valid starting value; every field
        // the kernel reads is set below before the call.
        let mut action: libc::sigaction = unsafe { std::mem::zeroed() };
        action.sa_sigaction = on_termination_signal as *const () as libc::sighandler_t;
        // SAFETY: sa_mask is a valid sigset_t owned by `action`.
        unsafe {
            libc::sigemptyset(&mut action.sa_mask);
            for blocked in TERMINATION_SIGNALS {
                libc::sigaddset(&mut action.sa_mask, blocked);
            }
        }
        // SAFETY: `action` is fully initialised; the old action is not wanted.
        if unsafe { libc::sigaction(sig, &action, std::ptr::null_mut()) } != 0 {
            return Err(std::io::Error::last_os_error());
        }
    }
    Ok(())
}
