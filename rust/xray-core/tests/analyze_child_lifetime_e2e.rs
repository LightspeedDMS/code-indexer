//! The killable analyze child (`run_analyze_child`) never outlives the
//! process that started it.
//!
//! The child leads its own process group, out of reach of a caller killing
//! the parent's group. Each test re-runs THIS test binary, filtered to the
//! `parent_role` helper, as the parent: it installs the termination handler
//! (as `xray-cli`'s `main` does) and runs a child that records its pid and
//! sleeps. The parent is then SIGKILLed (the parent-death signal must stop
//! the child) or SIGTERMed (the handler must stop it before the parent
//! exits).

use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const ROLE_ENV: &str = "XRAY_ANALYZE_LIFETIME_PARENT_PIDFILE";
const SIGKILL: i32 = 9;
const SIGTERM: i32 = 15;
/// Far longer than any test: the child only ends by being killed.
const CHILD_SLEEP_SECONDS: u64 = 300;
const PARENT_BUDGET: Duration = Duration::from_secs(CHILD_SLEEP_SECONDS);
const WAIT: Duration = Duration::from_secs(20);
const POLL: Duration = Duration::from_millis(20);

/// Only acts when re-run as the parent; a no-op in a normal test run.
#[test]
fn parent_role() {
    let Ok(pidfile) = std::env::var(ROLE_ENV) else {
        return;
    };
    xray_core::child_group_reaper::install_termination_handler().expect("install handler");
    let mut command = Command::new("sh");
    command.args([
        "-c",
        &format!("echo $$ > {pidfile}.tmp && mv {pidfile}.tmp {pidfile} && exec sleep {CHILD_SLEEP_SECONDS}"),
    ]);
    let (status, _) = xray_core::graph::analyze::process::run_analyze_child::<serde_json::Value>(command, PARENT_BUDGET);
    panic!("the parent is always killed first; run_analyze_child returned {:?}", status);
}

fn sys_kill(pid: i32, sig: i32) -> i32 {
    extern "C" {
        fn kill(pid: i32, sig: i32) -> i32;
    }
    // SAFETY: plain kill(2) syscall wrapper.
    unsafe { kill(pid, sig) }
}

fn alive(pid: i32) -> bool {
    match std::fs::read_to_string(format!("/proc/{}/stat", pid)) {
        Ok(stat) => stat.rsplit(')').next().and_then(|r| r.split_whitespace().next()) != Some("Z"),
        Err(_) => false,
    }
}

fn wait_dead(pid: i32) -> bool {
    let start = Instant::now();
    while start.elapsed() < WAIT {
        if !alive(pid) {
            return true;
        }
        std::thread::sleep(POLL);
    }
    false
}

/// (parent process, pid of the analyze child it started)
fn start_parent(dir: &std::path::Path) -> (Child, i32) {
    let pidfile = dir.join("child.pid");
    let mut parent = Command::new(std::env::current_exe().unwrap())
        .args(["--exact", "parent_role", "--nocapture", "--test-threads=1"])
        .env(ROLE_ENV, &pidfile)
        .stdout(Stdio::null())
        .stderr(Stdio::null())
        .spawn()
        .expect("spawn parent");
    let start = Instant::now();
    while start.elapsed() < WAIT {
        if let Ok(text) = std::fs::read_to_string(&pidfile) {
            if let Ok(pid) = text.trim().parse() {
                return (parent, pid);
            }
        }
        std::thread::sleep(POLL);
    }
    let _ = parent.kill();
    let _ = parent.wait();
    panic!("the analyze child never started");
}

#[test]
fn sigkill_of_the_parent_kills_the_analyze_child() {
    let dir = tempfile::tempdir().unwrap();
    let (mut parent, child) = start_parent(dir.path());
    assert_eq!(sys_kill(parent.id() as i32, SIGKILL), 0);
    parent.wait().unwrap();
    let died = wait_dead(child);
    sys_kill(child, SIGKILL);
    assert!(died, "analyze child {} outlived a SIGKILLed parent", child);
}

#[test]
fn sigterm_of_the_parent_kills_the_analyze_child_before_exit() {
    let dir = tempfile::tempdir().unwrap();
    let (mut parent, child) = start_parent(dir.path());
    assert_eq!(sys_kill(parent.id() as i32, SIGTERM), 0);
    parent.wait().unwrap();
    let gone_at_exit = !alive(child);
    sys_kill(child, SIGKILL);
    assert!(gone_at_exit, "analyze child {} still running after the parent exited on SIGTERM", child);
}
