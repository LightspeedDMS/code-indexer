//! Story #1787 AC15 (amendment): OS-level containment for the analyze
//! child. Bounds the child with a per-process cgroup v2 `memory.max`
//! derived from the admitted estimate (computed by the Python-side
//! admission gate -- see `docs/adr/ADR-003-graph-memory-governor-integration.md`).
//!
//! `RLIMIT_AS` is deliberately NOT used: it caps virtual address space
//! and trips spuriously on allocator arenas and mmap reservations that
//! are never resident -- AC7's graph handoff IS mmap, so `RLIMIT_AS`
//! would abort healthy runs. `MemoryGovernor` already reads cgroup v2
//! `memory.max`/`memory.current` (and the v1 equivalents), so containing
//! the child via the SAME mechanism is consistent rather than a second,
//! divergent one.
//!
//! `MemoryCeiling` is injectable, mirroring `MemoryGovernor`'s own
//! `_MemoryReaders` pattern: cgroup v2 delegation is not guaranteed in
//! every environment (this dev sandbox included), so tests use a fake
//! that simulates a genuine OOM-kill deterministically rather than
//! requiring real cgroup permissions in CI.

use std::io;
use std::path::{Path, PathBuf};

/// Abstracts cgroup v2 memory-ceiling setup so the analyze child's
/// containment logic is testable without real cgroup delegation.
pub trait MemoryCeiling {
    /// Creates a containment boundary allowing at most `limit_bytes`
    /// resident for anything later added via `add_pid`. A failure here
    /// (no delegation, not mounted, permission denied) must DEGRADE the
    /// caller -- proceed without containment -- never abort the job:
    /// the estimate being wrong is exactly what this containment exists
    /// to survive; containment setup itself failing must not become a
    /// second way to lose the job.
    fn create(&self, limit_bytes: u64) -> io::Result<()>;
    /// Adds `pid` to the containment boundary created by `create`.
    fn add_pid(&self, pid: i32) -> io::Result<()>;
    /// True iff the kernel genuinely OOM-killed a process inside this
    /// boundary (cgroup v2 `memory.events`'s `oom_kill` counter > 0) --
    /// NEVER inferred from a bare SIGKILL exit alone, which a
    /// cancellation kill (AC7) also produces and must not be conflated
    /// with this.
    fn oom_killed(&self) -> bool;
    /// Best-effort teardown. Must never panic -- containment teardown
    /// failing must not fail the analyze job either.
    fn cleanup(&self);
}

/// Real cgroup v2 implementation. Creates
/// `<cgroup_root>/xray-analyze-<pid>/`, writes `memory.max`, and reads
/// `memory.events` for `oom_kill`.
pub struct CgroupV2MemoryCeiling {
    dir: PathBuf,
}

impl CgroupV2MemoryCeiling {
    /// `cgroup_root` must be the CURRENT process's own delegated cgroup
    /// v2 directory (e.g. the directory `/proc/self/cgroup` resolves
    /// to under `/sys/fs/cgroup`), NOT the top-level `/sys/fs/cgroup` --
    /// writing there requires delegation the caller is responsible for
    /// having verified. `name` should be unique per analyze invocation
    /// (e.g. `xray-analyze-<pid>`).
    pub fn new(cgroup_root: &Path, name: &str) -> Self {
        CgroupV2MemoryCeiling { dir: cgroup_root.join(name) }
    }
}

impl MemoryCeiling for CgroupV2MemoryCeiling {
    fn create(&self, limit_bytes: u64) -> io::Result<()> {
        std::fs::create_dir(&self.dir)?;
        std::fs::write(self.dir.join("memory.max"), limit_bytes.to_string())?;
        Ok(())
    }

    fn add_pid(&self, pid: i32) -> io::Result<()> {
        std::fs::write(self.dir.join("cgroup.procs"), pid.to_string())
    }

    fn oom_killed(&self) -> bool {
        let events = match std::fs::read_to_string(self.dir.join("memory.events")) {
            Ok(contents) => contents,
            Err(_) => return false,
        };
        events
            .lines()
            .find_map(|line| line.strip_prefix("oom_kill "))
            .and_then(|value| value.trim().parse::<u64>().ok())
            .map(|count| count > 0)
            .unwrap_or(false)
    }

    fn cleanup(&self) {
        // Best-effort: a cgroup dir can only be rmdir'd once EMPTY of
        // member processes; by the time cleanup() runs the child has
        // already been reaped, so this is expected to succeed. Any
        // failure (already removed, permission, lingering member) is
        // deliberately swallowed -- containment teardown must never fail
        // the analyze job.
        let _ = std::fs::remove_dir(&self.dir);
    }
}

/// A ceiling that never contains anything -- used when no memory limit
/// was admitted for this build (`memory_limit_bytes: None`), so
/// `run_analyze_child` (the unbounded entry point) can delegate to
/// `run_analyze_child_with_memory_limit` without a special-cased "no
/// ceiling" branch.
pub struct NoopMemoryCeiling;

impl MemoryCeiling for NoopMemoryCeiling {
    fn create(&self, _limit_bytes: u64) -> io::Result<()> {
        Ok(())
    }

    fn add_pid(&self, _pid: i32) -> io::Result<()> {
        Ok(())
    }

    fn oom_killed(&self) -> bool {
        false
    }

    fn cleanup(&self) {}
}

/// Test double letting `process.rs`'s tests exercise
/// `run_analyze_child_with_memory_limit`'s containment/degrade/OOM-override
/// logic deterministically, without real cgroup permissions -- mirrors
/// `MemoryGovernor`'s own injectable `_MemoryReaders` pattern. Every call
/// is recorded via `Cell`s so a test can assert exactly what was invoked.
#[cfg(test)]
pub(crate) struct FakeMemoryCeiling {
    pub(crate) create_fails: bool,
    pub(crate) add_pid_fails: bool,
    pub(crate) simulate_oom: bool,
    pub(crate) create_called: std::cell::Cell<bool>,
    pub(crate) add_pid_called_with: std::cell::Cell<Option<i32>>,
    pub(crate) oom_killed_called: std::cell::Cell<bool>,
    pub(crate) cleanup_called: std::cell::Cell<bool>,
}

#[cfg(test)]
impl Default for FakeMemoryCeiling {
    fn default() -> Self {
        FakeMemoryCeiling {
            create_fails: false,
            add_pid_fails: false,
            simulate_oom: false,
            create_called: std::cell::Cell::new(false),
            add_pid_called_with: std::cell::Cell::new(None),
            oom_killed_called: std::cell::Cell::new(false),
            cleanup_called: std::cell::Cell::new(false),
        }
    }
}

#[cfg(test)]
impl MemoryCeiling for FakeMemoryCeiling {
    fn create(&self, _limit_bytes: u64) -> io::Result<()> {
        self.create_called.set(true);
        if self.create_fails {
            return Err(io::Error::new(io::ErrorKind::PermissionDenied, "fake: cgroup delegation unavailable"));
        }
        Ok(())
    }

    fn add_pid(&self, pid: i32) -> io::Result<()> {
        self.add_pid_called_with.set(Some(pid));
        if self.add_pid_fails {
            return Err(io::Error::other("fake: cgroup.procs write failed"));
        }
        Ok(())
    }

    fn oom_killed(&self) -> bool {
        self.oom_killed_called.set(true);
        self.simulate_oom
    }

    fn cleanup(&self) {
        self.cleanup_called.set(true);
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    /// Real filesystem, fake "cgroup" directory (a plain tempdir, since
    /// this dev sandbox / CI cannot assume real cgroup v2 delegation):
    /// `create` must write `memory.max` with the exact requested limit,
    /// and `oom_killed` must report `false` when no `memory.events` file
    /// exists yet (the boundary was created but nothing has run in it).
    #[test]
    fn cgroup_v2_memory_ceiling_writes_memory_max_and_reports_no_oom_when_events_absent() {
        let root = tempfile::tempdir().unwrap();
        let ceiling = CgroupV2MemoryCeiling::new(root.path(), "xray-analyze-test");

        ceiling.create(256 * 1024 * 1024).unwrap();

        let written = std::fs::read_to_string(root.path().join("xray-analyze-test/memory.max")).unwrap();
        assert_eq!(written, (256 * 1024 * 1024).to_string());
        assert!(!ceiling.oom_killed(), "no memory.events file yet must report no OOM, not panic or default to true");
    }

    /// `cleanup()`'s contract is "best-effort, never panics" -- on REAL
    /// cgroupfs, `memory.max`/`cgroup.procs`/`memory.events` are kernel
    /// pseudo-files that vanish automatically on `rmdir`, so cleanup
    /// always succeeds once the boundary is empty of member processes.
    /// A plain tempdir (this test's stand-in, since real cgroup v2
    /// delegation isn't guaranteed here) cannot emulate that -- `rmdir`
    /// on a directory that still contains real files genuinely fails.
    /// This test proves cleanup() swallows that failure rather than
    /// panicking, which is the actual contract this method promises.
    #[test]
    fn cgroup_v2_memory_ceiling_cleanup_never_panics_even_when_removal_fails() {
        let root = tempfile::tempdir().unwrap();
        let ceiling = CgroupV2MemoryCeiling::new(root.path(), "xray-analyze-test");
        ceiling.create(1024).unwrap();

        ceiling.cleanup();

        assert!(
            root.path().join("xray-analyze-test/memory.max").exists(),
            "on this fake filesystem the directory genuinely cannot be removed while non-empty -- \
             this test exists to prove cleanup() did not panic despite that, not that removal succeeded"
        );
    }

    /// THE core AC15 discriminator: `oom_killed` must report `true` ONLY
    /// when `memory.events`'s `oom_kill` counter is genuinely nonzero --
    /// never inferred from anything else.
    #[test]
    fn cgroup_v2_memory_ceiling_reports_oom_killed_when_events_file_shows_a_nonzero_oom_kill_count() {
        let root = tempfile::tempdir().unwrap();
        let ceiling = CgroupV2MemoryCeiling::new(root.path(), "xray-analyze-test");
        ceiling.create(1024).unwrap();

        std::fs::write(
            root.path().join("xray-analyze-test/memory.events"),
            "low 0\nhigh 0\nmax 3\noom 1\noom_kill 1\noom_group_kill 0\n",
        )
        .unwrap();

        assert!(ceiling.oom_killed());
    }

    /// A `memory.events` file with `oom_kill 0` (the boundary existed and
    /// was queried, but the kernel never actually killed anything in it)
    /// must report `false` -- proving this isn't just "file exists ->
    /// true".
    #[test]
    fn cgroup_v2_memory_ceiling_reports_no_oom_when_oom_kill_count_is_zero() {
        let root = tempfile::tempdir().unwrap();
        let ceiling = CgroupV2MemoryCeiling::new(root.path(), "xray-analyze-test");
        ceiling.create(1024).unwrap();

        std::fs::write(root.path().join("xray-analyze-test/memory.events"), "oom_kill 0\n").unwrap();

        assert!(!ceiling.oom_killed());
    }

    /// A cgroup ceiling creation failure (e.g. no delegation, dir already
    /// exists) must surface as a real `Err`, never panic -- the caller
    /// (AC15's degrade-not-abort contract) depends on being able to
    /// observe and swallow this.
    #[test]
    fn cgroup_v2_memory_ceiling_create_fails_loudly_when_the_directory_cannot_be_created() {
        let root = tempfile::tempdir().unwrap();
        // Pre-create the target directory so the real create() -- which
        // uses create_dir (not create_dir_all) -- fails with AlreadyExists,
        // simulating an unavailable/already-occupied containment boundary.
        std::fs::create_dir(root.path().join("xray-analyze-test")).unwrap();
        let ceiling = CgroupV2MemoryCeiling::new(root.path(), "xray-analyze-test");

        assert!(ceiling.create(1024).is_err(), "create() must return Err, not panic, on a pre-existing directory");
    }

    /// `NoopMemoryCeiling` must never claim an OOM and must accept every
    /// operation as a silent success -- it exists purely to give
    /// `run_analyze_child` (no memory limit) a ceiling implementation
    /// with zero containment behavior.
    #[test]
    fn noop_memory_ceiling_never_reports_oom_and_never_fails() {
        let ceiling = NoopMemoryCeiling;
        assert!(ceiling.create(1).is_ok());
        assert!(ceiling.add_pid(1).is_ok());
        assert!(!ceiling.oom_killed());
        ceiling.cleanup();
    }
}
