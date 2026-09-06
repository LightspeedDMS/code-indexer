//! Symbol/file identity and cache keys (Story #1787, S2, AC3).
//!
//! `SymbolId = (file_id << 32) | local_index` is assembled by callers (later
//! extraction slices) from a `file_id` computed here plus a per-file local
//! counter -- no cross-thread coordination is needed because `file_id` is a
//! pure function of the repo-relative path, never a positional index into a
//! file list or a call-order-derived counter.
//!
//! Cache keys (`per_file_cache_key`, `repo_snapshot_identity`) are
//! dirty-working-tree-aware: they hash the bytes actually on disk, never a
//! git blob or commit hash, because X-Ray's whole purpose is analyzing repos
//! mid-edit.

use sha2::{Digest, Sha256};
use std::collections::HashMap;
use std::io;
use std::path::Path;

/// Computes a stable, path-derived file identifier for use as the upper 32
/// bits of a `SymbolId`.
///
/// This is a PURE function of `repo_relative_path` alone: it takes no file
/// list, index, or counter, so it cannot shift when files are added,
/// removed, or discovered in a different order (parallel walks visit files
/// in no guaranteed order). That is what makes it safe to call from many
/// extraction threads with no cross-thread coordination -- two threads
/// hashing the same path always agree, and a path never seen before gets an
/// id independent of everything else in the scan.
pub fn file_id(repo_relative_path: &str) -> u32 {
    let digest = Sha256::digest(repo_relative_path.as_bytes());
    u32::from_be_bytes([digest[0], digest[1], digest[2], digest[3]])
}

/// Reports that two DISTINCT repo-relative paths produced the same 32-bit
/// `file_id`. `file_id` truncates a SHA-256 digest to 32 bits, so at the
/// documented ~900-repo production fleet scale a genuine collision is not
/// a remote tail risk (birthday bound: ~4.2% for a single
/// Elasticsearch-sized ~19,090-file repo). This error exists so that event
/// is detected and reported explicitly (Rule 13, anti-silent-failure)
/// instead of the two files' symbol namespaces silently merging.
#[derive(Debug, Clone, PartialEq, Eq)]
pub struct FileIdCollisionError {
    pub existing_path: String,
    pub new_path: String,
    pub file_id: u32,
}

/// Run-scoped registry detecting `file_id` collisions across the set of
/// paths seen so far. This does NOT attempt to resolve a collision by
/// reassigning either path to a different id -- see `assign`'s doc comment
/// for why a full resolution scheme is deliberately out of scope for this
/// data-substrate slice, and what would be needed to add one later.
pub struct FileIdRegistry {
    assigned: HashMap<u32, String>,
}

impl FileIdRegistry {
    pub fn new() -> Self {
        FileIdRegistry { assigned: HashMap::new() }
    }

    /// Assigns (or, for an already-seen path, re-confirms) `path`'s
    /// `file_id`. Idempotent: calling this again with the SAME path always
    /// returns the same `Ok(id)`, and never touches any other path's
    /// entry -- this is what preserves AC3's stability property (adding a
    /// new, non-colliding path never changes an existing id).
    ///
    /// Returns `Err(FileIdCollisionError)` the moment a NEW, distinct path
    /// hashes to an id some OTHER path already holds. This registry
    /// deliberately does NOT attempt to resolve that collision by probing
    /// to a different slot: doing so safely requires a registry that
    /// persists across indexing runs (so that resolution order is fixed by
    /// history rather than by whichever run/thread happens to discover the
    /// colliding file first), which is cache-layer state that belongs to
    /// AC9 (out of scope for this data-substrate slice) -- not something
    /// this in-memory, run-scoped registry can honestly guarantee. Fail
    /// loud instead (Rule 13, anti-silent-failure; Rule 2, anti-fallback):
    /// let the caller decide how to handle a real collision (e.g. surface
    /// it as an indexing error) rather than silently reassigning an id in
    /// a way that is not provably stable.
    pub fn assign(&mut self, path: &str) -> Result<u32, FileIdCollisionError> {
        let id = file_id(path);
        match self.assigned.get(&id) {
            Some(existing_path) if existing_path == path => Ok(id),
            Some(existing_path) => Err(FileIdCollisionError {
                existing_path: existing_path.clone(),
                new_path: path.to_string(),
                file_id: id,
            }),
            None => {
                self.assigned.insert(id, path.to_string());
                Ok(id)
            }
        }
    }
}

impl Default for FileIdRegistry {
    fn default() -> Self {
        Self::new()
    }
}

/// Globally-unique symbol identifier: `(file_id << 32) | local_index`.
pub type SymbolId = u64;

/// Assembles a `SymbolId` from a `file_id` (see `file_id` above) and a
/// per-file `local_index`. Pure bit composition -- no coordination, no
/// shared state -- so parallel extraction threads can each assign
/// `local_index` values 0, 1, 2, ... within their own file independently of
/// every other thread and never collide.
pub fn make_symbol_id(file: u32, local_index: u32) -> SymbolId {
    ((file as u64) << 32) | (local_index as u64)
}

/// Computes a dirty-working-tree-aware cache key for one file's extraction
/// output: a hash of the repo-relative path together with the CONTENT bytes
/// actually on disk right now. There is no git object (blob or commit)
/// anywhere in this computation, so an uncommitted edit -- the normal state
/// of a repo X-Ray analyzes -- always changes the key.
pub fn per_file_cache_key(repo_relative_path: &str, content: &[u8]) -> String {
    let mut hasher = Sha256::new();
    hasher.update(repo_relative_path.as_bytes());
    hasher.update([0u8]); // separator: prevents "ab"+"c" colliding with "a"+"bc"
    hasher.update(content);
    format!("{:x}", hasher.finalize())
}

/// Computes a whole-repository snapshot identity from the CURRENT
/// working-tree content of `repo_relative_paths`, resolved against
/// `repo_root`. Like `per_file_cache_key`, this reads real bytes off disk --
/// never a git blob or commit hash -- so any uncommitted edit to any covered
/// file changes the identity.
///
/// Per-file keys are sorted before hashing so the result does not depend on
/// the order `repo_relative_paths` happens to list files in.
///
/// # Errors
/// Returns an `io::Error` (fails loud, per anti-silent-failure) if any
/// listed path resolves outside `repo_root` -- whether via lexical `..`, an
/// absolute path, or a symlink inside the repo pointing elsewhere -- or
/// cannot be read. There is no fallback to a stale or partial identity.
pub fn repo_snapshot_identity(
    repo_root: &Path,
    repo_relative_paths: &[String],
) -> io::Result<String> {
    // Canonicalized ONCE: every candidate file's resolved (symlink-following)
    // path is checked against this below, so a symlink inside the repo that
    // points outside it is caught the same way a lexical `..` is.
    let canonical_repo_root = repo_root.canonicalize()?;

    let mut per_file_keys: Vec<String> = Vec::with_capacity(repo_relative_paths.len());
    for relative_path in repo_relative_paths {
        let candidate = repo_root.join(relative_path);
        let canonical_candidate = candidate.canonicalize()?;
        if !canonical_candidate.starts_with(&canonical_repo_root) {
            return Err(io::Error::new(
                io::ErrorKind::InvalidInput,
                format!("path escapes repo root: {relative_path}"),
            ));
        }

        let content = std::fs::read(&canonical_candidate)?;
        per_file_keys.push(per_file_cache_key(relative_path, &content));
    }
    per_file_keys.sort_unstable();

    let mut hasher = Sha256::new();
    for key in &per_file_keys {
        hasher.update(key.as_bytes());
        hasher.update([0u8]);
    }
    Ok(format!("{:x}", hasher.finalize()))
}

#[cfg(test)]
mod tests {
    use super::*;

    /// AC3: `SymbolId = (file_id << 32) | local_index`. A wrong
    /// implementation that swapped the halves, used addition instead of a
    /// shift+or (which would corrupt bits whenever `local_index` overflows
    /// past what addition would carry into), or truncated either half would
    /// fail this bit-exact check.
    #[test]
    fn make_symbol_id_packs_file_id_high_and_local_index_low() {
        let symbol = make_symbol_id(0x1234_5678, 0x0000_0042);
        assert_eq!(symbol, (0x1234_5678u64 << 32) | 0x0000_0042u64);
        assert_eq!((symbol >> 32) as u32, 0x1234_5678);
        assert_eq!((symbol & 0xFFFF_FFFF) as u32, 0x0000_0042);
    }

    #[test]
    fn make_symbol_id_is_distinct_for_different_files_or_local_indices() {
        let a = make_symbol_id(1, 0);
        let b = make_symbol_id(1, 1); // same file, different local index
        let c = make_symbol_id(2, 0); // different file, same local index
        assert_ne!(a, b);
        assert_ne!(a, c);
        assert_ne!(b, c);
    }

    /// AC3 forbids a positional-index `file_id` because it shifts whenever a
    /// file is added, silently invalidating the whole cache. The realistic
    /// wrong implementation this guards against is a `HashMap<String, u32>`
    /// with an insertion-order counter: assigning ids in whatever order
    /// files are *discovered* is positional indexing in disguise -- touch
    /// one new file earlier in the scan and every id assigned after it
    /// shifts. A pure hash-of-path function cannot exhibit this because it
    /// has no notion of "earlier".
    #[test]
    fn file_id_is_path_derived_not_position_or_call_order_derived() {
        let reference_id = file_id("src/b.rs");

        // Simulate a differently-ordered scan (as parallel/unordered
        // directory walks naturally produce) touching many other paths
        // first, including one that would sort/insert before "src/b.rs".
        for other in ["src/z.rs", "src/000_new.rs", "src/a.rs", "src/nested/x.rs"] {
            let _ = file_id(other);
        }

        assert_eq!(file_id("src/b.rs"), reference_id);
        // Different paths must map to different ids -- a constant function
        // would also pass the equality check above, for the wrong reason.
        assert_ne!(file_id("src/b.rs"), file_id("src/a.rs"));
    }

    #[test]
    fn file_id_is_deterministic_for_the_same_path() {
        assert_eq!(file_id("pkg/Foo.java"), file_id("pkg/Foo.java"));
    }

    /// AC3: cache keys must be dirty-working-tree-aware, never a git blob or
    /// commit hash. A wrong implementation that hashed `git show HEAD:path`
    /// or a commit SHA would return the SAME key here even though the
    /// on-disk content changed, silently serving stale symbols. This test
    /// never touches git at all -- it writes real bytes to a real temp file
    /// and edits them without any commit, which a git-blob-keyed
    /// implementation could not distinguish (there is no git repo here).
    #[test]
    fn per_file_cache_key_reflects_uncommitted_working_tree_edit() {
        let dir = tempfile::tempdir().expect("create temp dir");
        let file_path = dir.path().join("Foo.java");
        std::fs::write(&file_path, b"class Foo {}").expect("write initial content");
        let content_before = std::fs::read(&file_path).expect("read initial content");
        let key_before = per_file_cache_key("Foo.java", &content_before);

        // Edit WITHOUT committing.
        std::fs::write(&file_path, b"class Foo { void bar() {} }").expect("write edited content");
        let content_after = std::fs::read(&file_path).expect("read edited content");
        let key_after = per_file_cache_key("Foo.java", &content_after);

        assert_ne!(
            key_before, key_after,
            "cache key must change when working-tree content changes"
        );
    }

    #[test]
    fn per_file_cache_key_is_deterministic_for_identical_path_and_content() {
        let a = per_file_cache_key("Foo.java", b"class Foo {}");
        let b = per_file_cache_key("Foo.java", b"class Foo {}");
        assert_eq!(a, b);
    }

    /// AC3: `repo_snapshot_identity` carries the same dirty-tree-aware
    /// requirement as the per-file key. Edits to any file the snapshot
    /// covers must change the identity even with no git commit involved.
    #[test]
    fn repo_snapshot_identity_reflects_uncommitted_edit() {
        let dir = tempfile::tempdir().expect("create temp dir");
        std::fs::write(dir.path().join("a.rs"), b"fn a() {}").expect("write a.rs");
        std::fs::write(dir.path().join("b.rs"), b"fn b() {}").expect("write b.rs");
        let files = vec!["a.rs".to_string(), "b.rs".to_string()];

        let id_before =
            repo_snapshot_identity(dir.path(), &files).expect("compute snapshot identity");

        std::fs::write(dir.path().join("a.rs"), b"fn a() { /* edited */ }")
            .expect("edit a.rs without committing");

        let id_after =
            repo_snapshot_identity(dir.path(), &files).expect("recompute snapshot identity");

        assert_ne!(id_before, id_after);
    }

    /// A 32-bit `file_id` has a non-negligible collision probability at the
    /// documented ~900-repo production fleet scale (birthday bound over
    /// 2^32 slots: ~4.2% for a single Elasticsearch-sized ~19,090-file
    /// repo). An undetected collision would silently MERGE two distinct
    /// files' symbol namespaces -- every downstream candidate set and
    /// dead-code tier would inherit wrong resolution with no signal
    /// anything went wrong. `FileIdRegistry` must refuse to let that
    /// happen silently.
    ///
    /// These two paths are not hypothetical: both were independently
    /// verified (via a standalone SHA-256 computation, not this crate) to
    /// hash to the SAME file_id (0x7432e22f) under this exact scheme, so
    /// this test exercises a REAL collision rather than hoping for one or
    /// mocking the hash function.
    #[test]
    fn file_id_registry_detects_a_real_collision_between_distinct_paths_loudly() {
        const PATH_A: &str = "src/generated/file_10960.rs";
        const PATH_B: &str = "src/generated/file_24311.rs";
        assert_eq!(
            file_id(PATH_A),
            file_id(PATH_B),
            "test fixture assumption broken: these two paths must actually collide"
        );

        let mut registry = FileIdRegistry::new();
        registry.assign(PATH_A).expect("first registration never collides");

        let result: Result<u32, FileIdCollisionError> = registry.assign(PATH_B);
        match result {
            Err(collision) => {
                assert_eq!(collision.file_id, file_id(PATH_A));
                assert_eq!(collision.existing_path, PATH_A);
                assert_eq!(collision.new_path, PATH_B);
            }
            Ok(_) => panic!(
                "a real file_id collision between two DISTINCT paths must be reported, \
                 never silently merged into the same id"
            ),
        }
    }

    #[test]
    fn file_id_registry_is_idempotent_for_the_same_path() {
        let mut registry = FileIdRegistry::new();
        let first = registry.assign("src/a.rs").expect("first assignment succeeds");
        let second = registry.assign("src/a.rs").expect("re-assigning the same path succeeds");
        assert_eq!(first, second);
    }

    /// AC3's stability property, now proven through the registry: adding a
    /// brand-new, NON-colliding path must never change any id already
    /// handed out. This is the common case (the collision case above is
    /// the deliberately narrow exception).
    #[test]
    fn file_id_registry_assigning_a_new_non_colliding_path_does_not_change_existing_ids() {
        let mut registry = FileIdRegistry::new();
        let id_a = registry.assign("src/a.rs").expect("assign a.rs");
        let id_b = registry.assign("src/b.rs").expect("assign b.rs");

        let id_c = registry.assign("src/c.rs").expect("assign a brand-new file");
        let _ = id_c;

        assert_eq!(registry.assign("src/a.rs").expect("re-check a.rs"), id_a);
        assert_eq!(registry.assign("src/b.rs").expect("re-check b.rs"), id_b);
    }
}
