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

/// Returns the cache directory: ~/.cidx-server/xray-cache/
pub fn get_cache_dir() -> PathBuf {
    let home = std::env::var("HOME").unwrap_or_else(|_| ".".to_string());
    PathBuf::from(home).join(".cidx-server").join("xray-cache")
}

/// Reads and deserialises CacheMetadata from a .meta file.
/// Returns None if the file cannot be read or parsed.
pub fn read_metadata(meta_path: &Path) -> Option<CacheMetadata> {
    let content = std::fs::read_to_string(meta_path).ok()?;
    parse_metadata(&content)
}

/// Serialises CacheMetadata and writes it to `meta_path`.
/// Creates parent directories if needed.
pub fn write_metadata(meta_path: &Path, meta: &CacheMetadata) {
    if let Some(parent) = meta_path.parent() {
        let _ = std::fs::create_dir_all(parent);
    }
    let content = format_metadata(meta);
    let _ = std::fs::write(meta_path, content);
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
        let _ = std::fs::remove_file(so_path);
        // Remove corresponding .meta file
        let meta_path = so_path.with_extension("meta");
        let _ = std::fs::remove_file(meta_path);
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

    #[test]
    fn test_get_cache_dir_contains_expected_path() {
        let dir = get_cache_dir();
        let s = dir.to_string_lossy();
        assert!(s.contains(".cidx-server"), "expected .cidx-server in {}", s);
        assert!(s.contains("xray-cache"), "expected xray-cache in {}", s);
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
        write_metadata(&meta_path, &meta);
        let read_back = read_metadata(&meta_path);
        assert_eq!(read_back, Some(meta));
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
