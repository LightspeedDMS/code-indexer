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
/// Writes to `{meta_path}.tmp` first, then renames to the final path so that
/// concurrent readers never observe a partial write.
/// Creates parent directories if needed.
pub fn write_metadata(meta_path: &Path, meta: &CacheMetadata) -> Result<(), std::io::Error> {
    if let Some(parent) = meta_path.parent() {
        std::fs::create_dir_all(parent)?;
    }
    let tmp_path = meta_path.with_extension("meta.tmp");
    let content = format_metadata(meta);
    std::fs::write(&tmp_path, content)?;
    std::fs::rename(&tmp_path, meta_path)?;
    Ok(())
}

/// Returns the current rustc version string by running `rustc --version`.
/// Falls back to "unknown" if rustc is not on PATH.
pub fn get_rustc_version() -> String {
    let output = std::process::Command::new("rustc")
        .arg("--version")
        .output();
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
        "source_hash={}\nrustc_version={}\ncompiled_at={}\ncompile_ms={}\n",
        meta.source_hash, meta.rustc_version, meta.compiled_at, meta.compile_ms
    )
}

fn parse_metadata(content: &str) -> Option<CacheMetadata> {
    let mut source_hash = None;
    let mut rustc_version = None;
    let mut compiled_at = None;
    let mut compile_ms = None;

    for line in content.lines() {
        if let Some((key, value)) = line.split_once('=') {
            match key {
                "source_hash" => source_hash = Some(value.to_string()),
                "rustc_version" => rustc_version = Some(value.to_string()),
                "compiled_at" => compiled_at = Some(value.to_string()),
                "compile_ms" => compile_ms = value.parse::<u128>().ok(),
                _ => {}
            }
        }
    }

    Some(CacheMetadata {
        source_hash: source_hash?,
        rustc_version: rustc_version?,
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
            compiled_at: "2025-01-01T00:00:00Z".to_string(),
            compile_ms: 252,
        };
        write_metadata(&meta_path, &meta).expect("write_metadata must succeed");
        let read_back = read_metadata(&meta_path);
        assert_eq!(read_back, Some(meta));
    }

    #[test]
    fn test_write_metadata_returns_result() {
        let dir = TempDir::new().unwrap();
        let meta_path = dir.path().join("result_test.meta");
        let meta = CacheMetadata {
            source_hash: "deadbeef".to_string(),
            rustc_version: "rustc 1.91.0".to_string(),
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
