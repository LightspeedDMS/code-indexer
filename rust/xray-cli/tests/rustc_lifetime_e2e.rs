//! The evaluator's `rustc` never outlives `xray-cli`.
//!
//! `xray-cli` starts `rustc` in its own process group (so its own compile
//! timeout can kill rustc together with the linker rustc starts). A caller
//! that kills `xray-cli` -- the server on its timeout or on a job cancel --
//! must not leave that group running. These tests put a stand-in `rustc`
//! first on PATH: it answers `--version` like the real one and otherwise
//! records its pid and the pid of a child it starts (standing in for the
//! linker), then sleeps. `xray-cli` is then killed with SIGKILL (nothing in
//! it can run; the kernel's parent-death signal must stop rustc) or SIGTERM
//! (its handler must kill rustc's whole group before exiting).

use std::io::Write as _;
use std::path::PathBuf;
use std::process::{Child, Command, Stdio};
use std::time::{Duration, Instant};

const WAIT_FOR_RUSTC: Duration = Duration::from_secs(30);
const WAIT_FOR_DEATH: Duration = Duration::from_secs(5);
const POLL: Duration = Duration::from_millis(20);

const FAKE_RUSTC: &str = r#"#!/bin/bash
if [ "$1" = "--version" ] || [ "$1" = "-vV" ]; then
  echo "rustc 1.98.0 (stand-in 2026-01-01)"
  echo "release: 1.98.0"
  exit 0
fi
sleep 300 &
echo "$$ $!" > "$FAKE_RUSTC_LOG.tmp"
mv "$FAKE_RUSTC_LOG.tmp" "$FAKE_RUSTC_LOG"
wait
"#;

struct Scenario {
    _dir: tempfile::TempDir,
    root: PathBuf,
    log: PathBuf,
}

impl Scenario {
    fn new() -> Self {
        let dir = tempfile::tempdir().expect("tempdir");
        let root = dir.path().to_path_buf();
        let bin = root.join("bin");
        std::fs::create_dir_all(&bin).unwrap();
        let rustc = bin.join("rustc");
        std::fs::write(&rustc, FAKE_RUSTC).unwrap();
        std::fs::set_permissions(&rustc, std::os::unix::fs::PermissionsExt::from_mode(0o755)).unwrap();
        let sample = root.join("sample.py");
        std::fs::write(&sample, "x = 1\n").unwrap();
        let mut files = std::fs::File::create(root.join("files.txt")).unwrap();
        writeln!(files, "{}", sample.display()).unwrap();
        // A unique source per scenario so the compile cache can never hit.
        let marker = format!("{:?}", root);
        let code = format!(
            "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> {{ let _ = {:?}; Vec::new() }}",
            marker
        );
        std::fs::write(root.join("ev.rs"), code).unwrap();
        let log = root.join("rustc.pids");
        Scenario { _dir: dir, root, log }
    }

    fn spawn_xray_cli(&self) -> Child {
        let path = format!("{}:{}", self.root.join("bin").display(), std::env::var("PATH").unwrap_or_default());
        Command::new(env!("CARGO_BIN_EXE_xray-cli"))
            .args(["--dynlib", self.root.join("ev.rs").to_str().unwrap()])
            .args(["--files-from", self.root.join("files.txt").to_str().unwrap()])
            .arg("--json")
            .env("PATH", path)
            .env("CIDX_DATA_DIR", self.root.join("data"))
            .env("FAKE_RUSTC_LOG", &self.log)
            .stdout(Stdio::null())
            .stderr(Stdio::null())
            .spawn()
            .expect("spawn xray-cli")
    }

    /// (rustc pid, pid of the child rustc started), once the stand-in ran.
    fn wait_for_rustc(&self) -> (i32, i32) {
        let start = Instant::now();
        while start.elapsed() < WAIT_FOR_RUSTC {
            if let Ok(text) = std::fs::read_to_string(&self.log) {
                let pids: Vec<i32> = text.split_whitespace().filter_map(|s| s.parse().ok()).collect();
                if pids.len() == 2 {
                    return (pids[0], pids[1]);
                }
            }
            std::thread::sleep(POLL);
        }
        panic!("stand-in rustc never started (log {:?})", self.log);
    }
}

/// Alive = present in /proc and not a zombie.
fn alive(pid: i32) -> bool {
    match std::fs::read_to_string(format!("/proc/{}/stat", pid)) {
        Ok(stat) => {
            let state = stat.rsplit(')').next().and_then(|rest| rest.split_whitespace().next());
            state != Some("Z")
        }
        Err(_) => false,
    }
}

fn wait_dead(pid: i32) -> bool {
    let start = Instant::now();
    while start.elapsed() < WAIT_FOR_DEATH {
        if !alive(pid) {
            return true;
        }
        std::thread::sleep(POLL);
    }
    false
}

fn sys_kill(pid: i32, sig: i32) -> i32 {
    extern "C" {
        fn kill(pid: i32, sig: i32) -> i32;
    }
    // SAFETY: plain kill(2) syscall wrapper.
    unsafe { kill(pid, sig) }
}

fn cleanup(pids: &[i32]) {
    for pid in pids {
        sys_kill(*pid, 9);
    }
}

fn signal(child: &Child, sig: i32) {
    let rc = sys_kill(child.id() as i32, sig);
    assert_eq!(rc, 0, "kill({}, {}) failed", child.id(), sig);
}

#[test]
fn sigkill_of_xray_cli_kills_rustc() {
    let scenario = Scenario::new();
    let mut xray = scenario.spawn_xray_cli();
    let (rustc, linker) = scenario.wait_for_rustc();
    signal(&xray, 9);
    xray.wait().unwrap();
    let rustc_died = wait_dead(rustc);
    cleanup(&[rustc, linker]);
    assert!(rustc_died, "rustc pid {} outlived a SIGKILLed xray-cli", rustc);
}

#[test]
fn sigterm_of_xray_cli_kills_rustc_group_before_exit() {
    let scenario = Scenario::new();
    let mut xray = scenario.spawn_xray_cli();
    let (rustc, linker) = scenario.wait_for_rustc();
    signal(&xray, 15);
    xray.wait().unwrap();
    // xray-cli has exited: its handler must already have killed AND reaped
    // rustc, and SIGKILLed rustc's group (the linker stand-in).
    let rustc_gone_at_exit = !alive(rustc);
    let linker_died = wait_dead(linker);
    cleanup(&[rustc, linker]);
    assert!(rustc_gone_at_exit, "rustc pid {} still running after xray-cli exited on SIGTERM", rustc);
    assert!(linker_died, "rustc's child pid {} outlived xray-cli's SIGTERM", linker);
}
