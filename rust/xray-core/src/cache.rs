/// Cache management for compiled evaluator .so files.
///
/// Cache directory: ~/.cidx-server/xray-cache/
/// Each entry: {hash}.so + {hash}.meta (key=value text)
use std::path::{Path, PathBuf};

/// Metadata stored alongside each cached .so file.
#[derive(Debug, Clone, PartialEq)]
pub struct CacheMetadata {
    pub source_hash: String,
    pub rustc_version: String,
    /// Bug #1784: the XRAY_ABI_VERSION the compiled .so was built against.
    /// Recorded explicitly (not just folded into an opaque combined hash) so
    /// a stale artifact's metadata is human-readable/debuggable. A .meta file
    /// missing this field (written before this fix) fails to parse -- see
    /// parse_metadata() -- which is always treated as a cache MISS.
    pub abi_version: u64,
    pub compiled_at: String, // ISO 8601
    pub compile_ms: u128,
}

/// Returns the cache directory: $CIDX_DATA_DIR/xray-cache/ when CIDX_DATA_DIR is set
/// (absolute path required), otherwise ~/.cidx-server/xray-cache/.
///
/// Must match Python's _get_cache_dir() in rust_backend.py (Bug #879).
pub fn get_cache_dir() -> PathBuf {
    // CIDX_DATA_DIR is the server-level override for all data paths (Bug #879).
    // Must match Python's _get_cache_dir() in rust_backend.py.
    if let Ok(data_dir) = std::env::var("CIDX_DATA_DIR") {
        let p = PathBuf::from(&data_dir);
        if p.is_absolute() {
            return p.join("xray-cache");
        }
    }
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".cidx-server").join("xray-cache")
}

/// Reads and deserialises CacheMetadata from a .meta file.
/// Returns None if the file cannot be read or parsed.
pub fn read_metadata(meta_path: &Path) -> Option<CacheMetadata> {
    let content = std::fs::read_to_string(meta_path).ok()?;
    parse_metadata(&content)
}

/// Serialises CacheMetadata and writes it to `meta_path` atomically.
///
/// Writes to a per-call-unique temp file in the same directory first, then
/// atomically persists (renames) it to the final path so that concurrent
/// readers never observe a partial write. Creates parent directories if
/// needed.
///
/// The temp file MUST be per-call-unique (not a deterministic name derived
/// from `meta_path`) — Bug #1425 exposed a race where concurrent callers
/// writing the SAME `meta_path` (e.g. two threads finishing a concurrent
/// compile of the identical evaluator hash) all computed the identical
/// `{meta_path}.meta.tmp` scratch path; whichever caller's rename() ran
/// second failed with "No such file or directory" because the first caller
/// had already renamed that shared scratch file away.
pub fn write_metadata(meta_path: &Path, meta: &CacheMetadata) -> Result<(), std::io::Error> {
    use std::io::Write as _;

    let parent = meta_path.parent().unwrap_or_else(|| Path::new("."));
    std::fs::create_dir_all(parent)?;
    let content = format_metadata(meta);
    let mut tmp = tempfile::Builder::new()
        .prefix(".meta-tmp-")
        .tempfile_in(parent)?;
    tmp.write_all(content.as_bytes())?;
    tmp.persist(meta_path).map_err(|persist_err| persist_err.error)?;
    Ok(())
}

/// Bug #1816: the exact Rust toolchain channel this workspace is pinned to
/// (`rust/rust-toolchain.toml`'s `channel` field), embedded at BUILD TIME
/// via `include_str!` so a fully-deployed `xray-cli` binary carries the
/// value with no runtime file dependency -- the same `include_str!` pattern
/// `preamble_ac18_parity.rs` already establishes elsewhere in this crate
/// for pulling a real source file's content into the binary.
///
/// WHY THIS EXISTS: every `rustc` subprocess this crate spawns (this
/// module's own `rustc --version` probe below, and `compiler.rs`'s
/// evaluator-compiling `rustc` invocation) previously ran with NO explicit
/// toolchain selection, so rustup resolved a toolchain by walking UP from
/// the CALLING PROCESS's own current working directory looking for a
/// `rust-toolchain.toml`. That resolution is correct only when `xray-cli`
/// happens to be invoked from inside this repository's `rust/` tree (true
/// for `cargo test`/manual dev-shell runs) -- production invokes `xray-cli`
/// from wherever the MCP server process itself runs, which has no such file
/// above it, so rustup silently fell back to `rustup default`, a version
/// that can differ from whatever toolchain actually compiled the
/// statically-linked `xray-cli` binary (via `cargo build`, which DOES honor
/// this same `rust-toolchain.toml`).
///
/// Bug #1816's root cause: a `.so` compiled by a DIFFERENT rustc/LLVM
/// version than the one that built `xray-cli` corrupted the heap across the
/// `GraphHandle` FFI boundary the moment `analyze_graph` called both
/// `signature_for` and `shortest_path_to_any` in the same evaluator
/// (`free(): double free detected in tcache 2` / SIGSEGV depending on call
/// order) -- reproduced directly by compiling the SAME two-call evaluator
/// once from inside `rust/` (no crash: both sides land on the SAME pinned
/// toolchain) and once from `/tmp` (crashes every time: the evaluator
/// compiles under whatever `rustup default` resolves to, while the release
/// `xray-cli` binary itself was built with the pinned channel). Plain Rust
/// `fn` pointers have an explicitly UNSPECIFIED ABI across compiler
/// versions (see the `graph::csr::handle` module doc comment's own defense
/// of using them, which assumes -- and this fix now GUARANTEES -- "both
/// sides are compiled by the identical rustc invocation"); no change to the
/// FFI thunk code itself is needed once that assumption is actually true.
const RUST_TOOLCHAIN_TOML: &str = include_str!("../../rust-toolchain.toml");

/// Extracts the `channel = "..."` value from `RUST_TOOLCHAIN_TOML`. A
/// deliberately narrow, single-purpose parse (Rule 3, KISS) rather than a
/// full TOML parser dependency: this file has exactly one meaningful line
/// and is fully controlled by this repository.
pub(crate) fn pinned_toolchain_channel() -> &'static str {
    RUST_TOOLCHAIN_TOML
        .lines()
        .find_map(|line| {
            let (key, rest) = line.trim().split_once('=')?;
            if key.trim() != "channel" {
                return None;
            }
            rest.trim().strip_prefix('"')?.split('"').next()
        })
        .unwrap_or_else(|| panic!("rust-toolchain.toml has no parsable `channel = \"...\"` line"))
}

/// Builds the `rustc --version` probe `Command`, pinned via
/// `RUSTUP_TOOLCHAIN` to `pinned_toolchain_channel()` -- see that function's
/// doc comment for why this must never be left to rustup's own cwd-based
/// resolution. Extracted from `get_rustc_version()` so the pinning itself
/// (not just the version string it produces) is directly, deterministically
/// testable via `Command::get_envs()`.
pub(crate) fn rustc_version_command() -> std::process::Command {
    let mut command = std::process::Command::new("rustc");
    command.arg("--version").env("RUSTUP_TOOLCHAIN", pinned_toolchain_channel());
    command
}

/// Returns the current rustc version string by running `rustc --version`
/// under the pinned toolchain (`rustc_version_command`). Falls back to
/// "unknown" if rustc is not on PATH.
pub fn get_rustc_version() -> String {
    let output = rustc_version_command().output();
    match output {
        Ok(o) if o.status.success() => {
            String::from_utf8_lossy(&o.stdout).trim().to_string()
        }
        _ => "unknown".to_string(),
    }
}

/// TTL for local cached .so files (seconds).
pub const LOCAL_CACHE_TTL_SECS: u64 = 300;

/// Check whether the `compiled_at` timestamp is within `ttl_secs` of now.
/// `compiled_at` format: "{epoch}s-since-epoch" (written by `chrono_now_iso()`).
/// Returns false if the format is unrecognised (forces recompile — safe default).
///
/// Boundary: entries aged exactly `ttl_secs` seconds are considered stale (strict `<`).
pub fn is_fresh(compiled_at: &str, ttl_secs: u64) -> bool {
    let epoch_str = match compiled_at.strip_suffix("s-since-epoch") {
        Some(s) if !s.is_empty() => s,
        _ => return false, // unrecognised format → stale → recompile
    };
    let compiled_epoch: u64 = match epoch_str.parse() {
        Ok(v) => v,
        Err(_) => return false, // non-numeric → stale
    };
    // If system clock is before UNIX_EPOCH, treat as stale (safe default: force recompile).
    let now = match std::time::SystemTime::now().duration_since(std::time::UNIX_EPOCH) {
        Ok(d) => d.as_secs(),
        Err(_) => return false, // clock error → stale → recompile
    };
    // Strict <: entries aged exactly ttl_secs are considered stale.
    now.saturating_sub(compiled_epoch) < ttl_secs
}

/// Evicts oldest entries from `cache_dir` (by mtime) keeping at most
/// `max_entries` .so files. Corresponding .meta files are removed too.
pub fn evict_lru(cache_dir: &Path, max_entries: usize) {
    let entries = match std::fs::read_dir(cache_dir) {
        Ok(e) => e,
        Err(_) => return,
    };

    // Collect all .so files with their modification times
    let mut so_files: Vec<(PathBuf, std::time::SystemTime)> = entries
        .filter_map(|e| e.ok())
        .filter(|e| {
            e.path()
                .extension()
                .and_then(|s| s.to_str())
                .map(|ext| ext == "so")
                .unwrap_or(false)
        })
        .filter_map(|e| {
            let path = e.path();
            let mtime = e.metadata().ok()?.modified().ok()?;
            Some((path, mtime))
        })
        .collect();

    if so_files.len() <= max_entries {
        return;
    }

    // Sort oldest first (smallest mtime)
    so_files.sort_by_key(|(_, mtime)| *mtime);

    let to_remove = so_files.len() - max_entries;
    for (so_path, _) in so_files.iter().take(to_remove) {
        if let Err(e) = std::fs::remove_file(so_path) {
            if e.kind() != std::io::ErrorKind::NotFound {
                // Best-effort eviction: log nothing, continue
            }
        }
        // Remove corresponding .meta file — missing is fine (best-effort)
        let meta_path = so_path.with_extension("meta");
        if let Err(e) = std::fs::remove_file(&meta_path) {
            if e.kind() != std::io::ErrorKind::NotFound {
                // Best-effort eviction: continue silently
            }
        }
    }
}

// ---- Internal serialisation (simple key=value text format) ----

fn format_metadata(meta: &CacheMetadata) -> String {
    format!(
        "source_hash={}\nrustc_version={}\nabi_version={}\ncompiled_at={}\ncompile_ms={}\n",
        meta.source_hash, meta.rustc_version, meta.abi_version, meta.compiled_at, meta.compile_ms
    )
}

fn parse_metadata(content: &str) -> Option<CacheMetadata> {
    let mut source_hash = None;
    let mut rustc_version = None;
    let mut abi_version = None;
    let mut compiled_at = None;
    let mut compile_ms = None;

    for line in content.lines() {
        if let Some((key, value)) = line.split_once('=') {
            match key {
                "source_hash" => source_hash = Some(value.to_string()),
                "rustc_version" => rustc_version = Some(value.to_string()),
                "abi_version" => abi_version = value.parse::<u64>().ok(),
                "compiled_at" => compiled_at = Some(value.to_string()),
                "compile_ms" => compile_ms = value.parse::<u128>().ok(),
                _ => {}
            }
        }
    }

    Some(CacheMetadata {
        source_hash: source_hash?,
        rustc_version: rustc_version?,
        // Missing/unparseable abi_version -> None here -> the whole
        // Option<CacheMetadata> short-circuits to None via `?` -- a legacy
        // .meta file (pre-Bug-#1784) is always a MISS, never a silent match.
        abi_version: abi_version?,
        compiled_at: compiled_at?,
        compile_ms: compile_ms?,
    })
}

#[cfg(test)]
mod tests {
    use super::*;
    use tempfile::TempDir;

    // ---- is_fresh() tests ----

    #[test]
    fn test_is_fresh_within_ttl() {
        // compiled 10s ago, TTL 300 — must be fresh
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let compiled_epoch = now - 10;
        let compiled_at = format!("{}s-since-epoch", compiled_epoch);
        assert!(is_fresh(&compiled_at, 300), "10s-old entry with TTL=300 must be fresh");
    }

    #[test]
    fn test_is_fresh_expired() {
        // compiled 600s ago, TTL 300 — must be stale
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let compiled_epoch = now - 600;
        let compiled_at = format!("{}s-since-epoch", compiled_epoch);
        assert!(!is_fresh(&compiled_at, 300), "600s-old entry with TTL=300 must be stale");
    }

    #[test]
    fn test_is_fresh_invalid_format() {
        // unrecognised format → safe default (stale → recompile)
        assert!(!is_fresh("not-a-timestamp", 300), "invalid format must return false");
        assert!(!is_fresh("garbage", 300), "garbage string must return false");
        assert!(!is_fresh("", 300), "empty string must return false");
        assert!(!is_fresh("2025-01-01T00:00:00Z", 300), "ISO 8601 format must return false (wrong suffix)");
    }

    #[test]
    fn test_is_fresh_boundary() {
        // compiled exactly at TTL — must be stale (strict <, not <=)
        let now = std::time::SystemTime::now()
            .duration_since(std::time::UNIX_EPOCH)
            .unwrap()
            .as_secs();
        let compiled_epoch = now - 300; // exactly at boundary
        let compiled_at = format!("{}s-since-epoch", compiled_epoch);
        assert!(!is_fresh(&compiled_at, 300), "entry at exact TTL boundary must be stale (strict <)");
    }

    // ---- get_cache_dir() tests ----

    // Mutex to serialize all tests that touch CIDX_DATA_DIR.
    // std::env is process-global mutable state; Rust runs tests in parallel by
    // default, so this mutex makes isolation explicit and safe regardless of
    // --test-threads setting.
    static ENV_MUTEX: std::sync::OnceLock<std::sync::Mutex<()>> = std::sync::OnceLock::new();

    fn env_mutex() -> &'static std::sync::Mutex<()> {
        ENV_MUTEX.get_or_init(|| std::sync::Mutex::new(()))
    }

    /// RAII guard: saves the previous CIDX_DATA_DIR value on construction,
    /// restores it on drop (or removes the var if it was absent).
    /// Guarantees restoration even when the test panics.
    struct CidxDataDirGuard {
        previous: Option<std::ffi::OsString>,
    }
    impl CidxDataDirGuard {
        fn new() -> Self {
            Self {
                previous: std::env::var_os("CIDX_DATA_DIR"),
            }
        }
    }
    impl Drop for CidxDataDirGuard {
        fn drop(&mut self) {
            match &self.previous {
                Some(v) => std::env::set_var("CIDX_DATA_DIR", v),
                None => std::env::remove_var("CIDX_DATA_DIR"),
            }
        }
    }

    #[test]
    fn test_get_cache_dir_contains_expected_path() {
        // Serialize against other tests that set CIDX_DATA_DIR.
        let _lock = env_mutex().lock().unwrap_or_else(|e| e.into_inner());
        let _guard = CidxDataDirGuard::new();
        // Ensure the var is absent so we exercise the default path.
        std::env::remove_var("CIDX_DATA_DIR");
        let dir = get_cache_dir();
        let s = dir.to_string_lossy();
        assert!(s.contains(".cidx-server"), "expected .cidx-server in {}", s);
        assert!(s.contains("xray-cache"), "expected xray-cache in {}", s);
    }

    #[test]
    fn test_get_cache_dir_respects_cidx_data_dir() {
        // Serialize against other tests that touch CIDX_DATA_DIR.
        let _lock = env_mutex().lock().unwrap_or_else(|e| e.into_inner());
        let _guard = CidxDataDirGuard::new();
        let base = std::env::temp_dir().join("cidx-test-data");
        // Pass path directly as OsStr — no UTF-8 assumption.
        std::env::set_var("CIDX_DATA_DIR", &base);
        let dir = get_cache_dir();
        // Assert before drop so the failure message is meaningful.
        assert_eq!(dir, base.join("xray-cache"));
        // _guard restores previous CIDX_DATA_DIR state on drop.
    }

    #[test]
    fn test_write_and_read_metadata_roundtrip() {
        let dir = TempDir::new().unwrap();
        let meta_path = dir.path().join("abc123.meta");
        let meta = CacheMetadata {
            source_hash: "abc123".to_string(),
            rustc_version: "rustc 1.91.0".to_string(),
            abi_version: 2,
            compiled_at: "2025-01-01T00:00:00Z".to_string(),
            compile_ms: 252,
        };
        write_metadata(&meta_path, &meta).expect("write_metadata must succeed");
        let read_back = read_metadata(&meta_path);
        assert_eq!(read_back, Some(meta));
    }

    #[test]
    fn test_metadata_missing_abi_version_field_is_none() {
        // Bug #1784: a pre-fix .meta file (written before abi_version existed)
        // must parse as None -- never silently default to a value that could
        // spuriously match the current ABI. A missing field is ALWAYS a MISS.
        let dir = TempDir::new().unwrap();
        let meta_path = dir.path().join("legacy.meta");
        std::fs::write(
            &meta_path,
            "source_hash=abc123\nrustc_version=rustc 1.91.0\ncompiled_at=2025-01-01T00:00:00Z\ncompile_ms=252\n",
        )
        .unwrap();
        let read_back = read_metadata(&meta_path);
        assert_eq!(
            read_back, None,
            "a .meta file missing abi_version must fail to parse (forces a MISS), not default"
        );
    }

    #[test]
    fn test_concurrent_write_metadata_same_path_all_succeed() {
        // Secondary race exposed by Bug #1425's compiler build-isolation fix:
        // once concurrent compiles of the SAME evaluator hash all succeed at
        // the rustc step, they all reach write_metadata() for the SAME
        // meta_path concurrently. The pre-fix implementation computes a
        // deterministic tmp path via meta_path.with_extension("meta.tmp") —
        // identical for every caller — so when thread A renames its tmp file
        // away first, thread B's later rename() fails with "No such file or
        // directory" because the tmp file it wrote to no longer exists under
        // that shared name.
        let dir = TempDir::new().unwrap();
        let meta_path = dir.path().join("concurrent1425.meta");

        const THREAD_COUNT: usize = 8;
        let written_metas: Vec<CacheMetadata> = (0..THREAD_COUNT)
            .map(|i| CacheMetadata {
                source_hash: "concurrent1425".to_string(),
                rustc_version: "rustc 1.91.0".to_string(),
                abi_version: 2,
                compiled_at: format!("{}s-since-epoch", 1_700_000_000 + i),
                compile_ms: 100 + i as u128,
            })
            .collect();

        let handles: Vec<_> = written_metas
            .iter()
            .cloned()
            .map(|meta| {
                let meta_path = meta_path.clone();
                std::thread::spawn(move || write_metadata(&meta_path, &meta))
            })
            .collect();

        let results: Vec<_> = handles
            .into_iter()
            .map(|h| h.join().expect("write_metadata thread must not panic"))
            .collect();

        for (i, result) in results.iter().enumerate() {
            assert!(
                result.is_ok(),
                "concurrent write_metadata #{} must succeed, got: {:?}",
                i,
                result.as_ref().err()
            );
        }

        // The file must end up holding EXACTLY one of the per-thread payloads
        // written above (never a torn/partial/foreign write).
        let final_meta = read_metadata(&meta_path)
            .expect("meta file must be readable and well-formed after concurrent writes");
        assert!(
            written_metas.contains(&final_meta),
            "final metadata {:?} must be one of the exact values written by a thread: {:?}",
            final_meta,
            written_metas
        );
    }

    #[test]
    fn test_write_metadata_returns_result() {
        let dir = TempDir::new().unwrap();
        let meta_path = dir.path().join("result_test.meta");
        let meta = CacheMetadata {
            source_hash: "deadbeef".to_string(),
            rustc_version: "rustc 1.91.0".to_string(),
            abi_version: 2,
            compiled_at: "2025-01-01T00:00:00Z".to_string(),
            compile_ms: 100,
        };
        let result: Result<(), std::io::Error> = write_metadata(&meta_path, &meta);
        assert!(result.is_ok(), "write_metadata must return Ok on success");
    }

    #[test]
    fn test_evict_lru_tolerates_missing_meta_file() {
        // evict_lru must not panic when .so exists but .meta was already deleted
        let dir = TempDir::new().unwrap();
        for i in 0..4u32 {
            std::fs::write(dir.path().join(format!("hash{}.so", i)), b"so").unwrap();
            // Intentionally omit some .meta files
            if i % 2 == 0 {
                std::fs::write(dir.path().join(format!("hash{}.meta", i)), b"meta").unwrap();
            }
        }
        // Must complete without panic even when some .meta files are absent
        evict_lru(dir.path(), 2);
        let so_count = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| {
                e.path().extension().and_then(|s| s.to_str()).map(|ext| ext == "so").unwrap_or(false)
            })
            .count();
        assert_eq!(so_count, 2);
    }

    #[test]
    fn test_read_metadata_missing_file_returns_none() {
        let dir = TempDir::new().unwrap();
        let result = read_metadata(&dir.path().join("nonexistent.meta"));
        assert!(result.is_none());
    }

    #[test]
    fn test_read_metadata_malformed_content_returns_none() {
        let dir = TempDir::new().unwrap();
        let path = dir.path().join("bad.meta");
        std::fs::write(&path, "not a valid meta file\n").unwrap();
        assert!(read_metadata(&path).is_none());
    }

    #[test]
    fn test_get_rustc_version_returns_nonempty_string() {
        let v = get_rustc_version();
        assert!(!v.is_empty());
        // Should contain "rustc" or fall back to "unknown"
        assert!(v.starts_with("rustc") || v == "unknown");
    }

    /// Bug #1816: `pinned_toolchain_channel()` must parse the EXACT channel
    /// this workspace is pinned to out of the real, `include_str!`-embedded
    /// `rust/rust-toolchain.toml` -- not a hardcoded duplicate. This test
    /// intentionally hardcodes the current pin value: if the pin is ever
    /// bumped, this test must be updated in the SAME commit, which is
    /// exactly the kind of drift-detector this bug fix exists to prevent
    /// (see the sync-constraint-3 note in the project's own CLAUDE.md about
    /// keeping this file and CI's toolchain action in lockstep).
    #[test]
    fn pinned_toolchain_channel_matches_the_workspace_pin() {
        assert_eq!(pinned_toolchain_channel(), "1.98.0");
    }

    /// Bug #1816 (THE fix's discriminating test): the `rustc --version`
    /// probe `Command` must carry `RUSTUP_TOOLCHAIN` pinned to
    /// `pinned_toolchain_channel()`. Before this fix, `get_rustc_version()`
    /// spawned a bare `rustc --version` with no env override, so its
    /// reported version silently tracked whatever toolchain rustup resolved
    /// from the CALLING PROCESS's current working directory -- correct only
    /// by coincidence when that cwd happened to sit under this workspace's
    /// `rust-toolchain.toml`. Production invokes `xray-cli` from directories
    /// with no such file above them, so the resolved toolchain there
    /// silently drifted from whatever toolchain actually compiled the
    /// `xray-cli` binary itself. Inspecting `Command::get_envs()` proves the
    /// mechanism directly and deterministically, without spawning a second
    /// real toolchain (which the CI machine is not guaranteed to have
    /// installed) or mutating the test process's own working directory
    /// (unsafe under `cargo test`'s parallel execution).
    #[test]
    fn rustc_version_command_pins_rustup_toolchain_env_var() {
        let command = rustc_version_command();
        let envs: std::collections::HashMap<_, _> = command.get_envs().collect();
        assert_eq!(
            envs.get(std::ffi::OsStr::new("RUSTUP_TOOLCHAIN")),
            Some(&Some(std::ffi::OsStr::new(pinned_toolchain_channel()))),
            "the rustc --version probe command must pin RUSTUP_TOOLCHAIN to the workspace channel"
        );
    }

    #[test]
    fn test_evict_lru_keeps_max_entries() {
        let dir = TempDir::new().unwrap();

        // Create 5 .so files
        for i in 0..5u32 {
            let so_path = dir.path().join(format!("hash{}.so", i));
            std::fs::write(&so_path, b"fake so").unwrap();
        }

        evict_lru(dir.path(), 3);

        let remaining: Vec<_> = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| {
                e.path()
                    .extension()
                    .and_then(|s| s.to_str())
                    .map(|ext| ext == "so")
                    .unwrap_or(false)
            })
            .collect();

        assert_eq!(remaining.len(), 3);
    }

    #[test]
    fn test_evict_lru_removes_corresponding_meta_files() {
        let dir = TempDir::new().unwrap();

        // Create 4 .so + .meta pairs
        for i in 0..4u32 {
            let so_path = dir.path().join(format!("hash{}.so", i));
            let meta_path = dir.path().join(format!("hash{}.meta", i));
            std::fs::write(&so_path, b"fake so").unwrap();
            std::fs::write(&meta_path, b"fake meta").unwrap();
        }

        evict_lru(dir.path(), 2);

        let so_count = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| {
                e.path()
                    .extension()
                    .and_then(|s| s.to_str())
                    .map(|ext| ext == "so")
                    .unwrap_or(false)
            })
            .count();
        let meta_count = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .filter(|e| {
                e.path()
                    .extension()
                    .and_then(|s| s.to_str())
                    .map(|ext| ext == "meta")
                    .unwrap_or(false)
            })
            .count();

        assert_eq!(so_count, 2);
        assert_eq!(meta_count, 2);
    }

    #[test]
    fn test_evict_lru_noop_when_under_limit() {
        let dir = TempDir::new().unwrap();
        for i in 0..3u32 {
            std::fs::write(dir.path().join(format!("hash{}.so", i)), b"").unwrap();
        }
        evict_lru(dir.path(), 10);
        let count = std::fs::read_dir(dir.path())
            .unwrap()
            .filter_map(|e| e.ok())
            .count();
        assert_eq!(count, 3);
    }
}
