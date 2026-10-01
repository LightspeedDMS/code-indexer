//! Issue #1934: the compile pipeline (validate -> hash -> cache check ->
//! assemble -> compile -> save), extracted verbatim out of `compiler.rs`
//! (pure move -- no behaviour change, see the module doc comment on
//! `super`).
//!
//! `compile_evaluator_impl` (appended in a follow-up edit) is decomposed
//! into helpers matching its own pre-existing numbered "Step 0..8"
//! comments, purely to keep each function under this repository's
//! clean-code length guideline -- the control flow, error paths, and
//! return values are UNCHANGED from the original single function; this is
//! a mechanical extraction, not a logic change.

use super::assemble::{
    assemble_evaluator_source_with_preamble_and_bounds, assemble_graph_evaluator_source_and_bounds,
    cache_identity_info_from_source, detect_evaluator_mode, CacheIdentityInfo, EvaluatorMode,
};
use super::diagnostics::adjust_error_lines;
use super::preamble::PREAMBLE;
use super::rustc_driver::{evaluator_rustc_command, run_rustc_with_timeout, RUSTC_COMPILE_TIMEOUT};
use super::types::{CompileError, CompileErrorKind, CompileResult};
use crate::cache::{self, CacheMetadata};
use crate::validator;
use std::path::{Path, PathBuf};
use std::time::Instant;
use tempfile::TempDir;

const MAX_CACHE_ENTRIES: usize = 100;

/// H2 (consolidated review, Issue #1811/Bug #1812, Codex): a source-size
/// cap, checked BEFORE validation/compilation ever runs. Real evaluator
/// sources are at most a few hundred lines; this is a generous ceiling
/// (2 MiB) whose only purpose is to reject a pathological payload cheaply
/// rather than let it reach rustc at all.
///
/// Deliberately a hardcoded constant, NOT a Web-UI/config setting -- per
/// this project's standing rule against adding configuration to gate a
/// bug fix, and matching the sibling constants below.
const MAX_USER_CODE_BYTES: usize = 2 * 1024 * 1024;

/// Compile user evaluator code into a cached .so file.
///
/// Pipeline: validate → hash → cache check → assemble → compile → save
pub fn compile_evaluator(user_code: &str, cache_dir: &Path) -> Result<CompileResult, CompileError> {
    compile_evaluator_impl(user_code, cache_dir, PREAMBLE)
}

/// Test-only seam (Bug #1784 review MAJOR-1): compiles using a
/// caller-supplied preamble instead of the hardcoded PREAMBLE constant, so
/// a genuine integration test can prove -- through the REAL compile_evaluator
/// pipeline -- that changing ONLY the preamble text (user code, ABI version,
/// and rustc toolchain held fixed) forces a fresh compile (cached == false),
/// never a stale-artifact reuse. This is the test class that would catch a
/// future regression where the cache identity is again derived from raw
/// user_code alone while the composite-identity helper functions are left
/// in place unchanged. Exists ONLY under #[cfg(test)]; production code can
/// never call it.
#[cfg(test)]
pub(crate) fn compile_evaluator_with_preamble(
    user_code: &str,
    cache_dir: &Path,
    preamble: &str,
) -> Result<CompileResult, CompileError> {
    compile_evaluator_impl(user_code, cache_dir, preamble)
}

/// Step 3 helper: assembles `user_code` into a complete compilable source
/// using the assembler matching `mode` -- Legacy uses the caller-supplied
/// `preamble` + legacy EPILOGUE; Graph always uses the full
/// GRAPH_PREAMBLE_EXTRA_* mirror + GRAPH_EPILOGUE (AC8). Mechanical
/// extraction from `compile_evaluator_impl`'s original match expression.
fn assemble_for_mode(mode: EvaluatorMode, preamble: &str, user_code: &str) -> (String, (usize, usize)) {
    match mode {
        EvaluatorMode::Legacy => assemble_evaluator_source_with_preamble_and_bounds(preamble, user_code),
        EvaluatorMode::Graph => assemble_graph_evaluator_source_and_bounds(user_code),
    }
}

/// Step 4 helper: cache check. The filename itself already encodes
/// source_hash + abi_version + rustc_version, so a mismatch on any of them
/// means the file simply won't be found. The per-field checks below are
/// defense in depth against a corrupted/hand-edited .meta sitting at a
/// colliding filename — a mismatch on ANY field is ALWAYS a MISS, never a
/// fallback match. Returns `Some` only on a genuine, fresh cache hit.
fn check_cache_hit(
    so_path: &Path,
    meta_path: &Path,
    identity_info: &CacheIdentityInfo,
) -> Option<CompileResult> {
    if !so_path.exists() {
        return None;
    }
    let meta = cache::read_metadata(meta_path)?;
    if meta.rustc_version == identity_info.rustc_version
        && meta.abi_version == identity_info.abi_version
        && meta.source_hash == identity_info.source_hash
        && cache::is_fresh(&meta.compiled_at, cache::LOCAL_CACHE_TTL_SECS)
    {
        Some(CompileResult {
            so_path: so_path.to_path_buf(),
            compile_ms: 0,
            cached: true,
        })
    } else {
        None
    }
}

/// Step 5 + 5b helper: creates `cache_dir` if needed, then an isolated,
/// per-invocation build directory inside it (Bug #1425 -- two concurrent
/// compiles of the SAME evaluator hash use the identical crate name, so
/// rustc's codegen-unit intermediate object files collide by filename when
/// both processes target `cache_dir` directly). `tempdir_in(cache_dir)`
/// guarantees the build dir is on the SAME filesystem as `cache_dir`, so
/// publishing the finished .so via `rename()` is a single atomic syscall.
/// Writes `assembled_source` into the build dir's `.rs` file. Returns the
/// `TempDir` guard (the CALLER must keep it alive until publish -- it
/// recursively removes the build directory on drop) plus the build `.rs`/
/// `.so` paths.
fn prepare_build(
    cache_dir: &Path,
    identity: &str,
    assembled_source: &str,
) -> Result<(TempDir, PathBuf, PathBuf), CompileError> {
    std::fs::create_dir_all(cache_dir).map_err(|e| CompileError {
        message: format!("Failed to create cache directory '{}': {}", cache_dir.display(), e),
        details: vec![],
        kind: CompileErrorKind::Infrastructure,
    })?;

    let build_dir = tempfile::Builder::new()
        .prefix(&format!("build-{}-", &identity[..identity.len().min(16)]))
        .tempdir_in(cache_dir)
        .map_err(|e| CompileError {
            message: format!(
                "Failed to create isolated build directory in '{}': {}",
                cache_dir.display(),
                e
            ),
            details: vec![],
            kind: CompileErrorKind::Infrastructure,
        })?;
    let build_rs_path = build_dir.path().join(format!("{}.rs", identity));
    let build_so_path = build_dir.path().join(format!("{}.so", identity));

    std::fs::write(&build_rs_path, assembled_source).map_err(|e| CompileError {
        message: format!("Failed to write evaluator source: {}", e),
        details: vec![],
        kind: CompileErrorKind::Infrastructure,
    })?;

    Ok((build_dir, build_rs_path, build_so_path))
}

/// Step 6 helper: compiles the assembled source at `build_rs_path` into
/// `build_so_path` -- bounded via `run_rustc_with_timeout` -- see that
/// function's docs for why a bare `.output()` (no timeout, no
/// process-group isolation) let a compile-expensive evaluator escape the
/// pipeline's advertised timeout entirely. On failure, rustc's
/// PREAMBLE-shifted line numbers are adjusted back into the user's own
/// source range before returning the `CompileError`. Returns the elapsed
/// compile time (ms) on success.
fn run_compile(
    build_rs_path: &Path,
    build_so_path: &Path,
    user_code_bounds: (usize, usize),
) -> Result<u128, CompileError> {
    let compile_start = Instant::now();
    let rustc_command = evaluator_rustc_command(build_rs_path, build_so_path);
    let (success, _stdout, stderr_bytes) =
        run_rustc_with_timeout(rustc_command, RUSTC_COMPILE_TIMEOUT)?;
    let compile_ms = compile_start.elapsed().as_millis();

    if !success {
        let stderr = String::from_utf8_lossy(&stderr_bytes).to_string();
        // Bug #1929 rework (Codex P2): `user_code_bounds` is computed
        // STRUCTURALLY by the assembler itself (`assemble_for_mode`),
        // never re-derived by parsing the assembled text for marker
        // strings -- see `assemble_with_epilogue`'s own doc comment for
        // why that was unsound. `adjust_error_lines` remaps ONLY a
        // location strictly inside the user-code span -- a preamble- or
        // epilogue-origin diagnostic is left with its original line
        // number, clearly labelled as evaluator support code, never
        // silently rewritten into a plausible-but-wrong "user" line.
        let (user_code_start_exclusive, user_code_end_inclusive) = user_code_bounds;
        let adjusted = adjust_error_lines(&stderr, user_code_start_exclusive, user_code_end_inclusive);
        return Err(CompileError {
            message: "Evaluator compilation failed".to_string(),
            details: adjusted,
            kind: CompileErrorKind::Compile,
        });
    }
    Ok(compile_ms)
}

/// Step 6b helper: atomically publishes the compiled .so from the
/// isolated build dir into the shared `cache_dir`. A concurrent compile of
/// the SAME hash may publish first — that's fine, since both compiles
/// started from byte-identical source; `rename()` simply replaces
/// `so_path` with an equally-valid artifact.
fn publish_artifact(build_so_path: &Path, so_path: &Path) -> Result<(), CompileError> {
    std::fs::rename(build_so_path, so_path).map_err(|e| CompileError {
        message: format!(
            "Failed to publish compiled evaluator to '{}': {}",
            so_path.display(),
            e
        ),
        details: vec![],
        kind: CompileErrorKind::Infrastructure,
    })
}

/// Step 7 helper: writes cache metadata best-effort -- the `.so` already
/// exists on disk, so a metadata write failure is only warned about, never
/// allowed to fail the whole compile.
fn write_cache_metadata_best_effort(
    meta_path: &Path,
    identity_info: &CacheIdentityInfo,
    compile_ms: u128,
) {
    let now = chrono_now_iso();
    if let Err(e) = cache::write_metadata(meta_path, &CacheMetadata {
        source_hash: identity_info.source_hash.clone(),
        rustc_version: identity_info.rustc_version.clone(),
        abi_version: identity_info.abi_version,
        compiled_at: now,
        compile_ms,
    }) {
        eprintln!("xray: warning: failed to write cache metadata {}: {}", meta_path.display(), e);
    }
}

/// Simple timestamp without external dependency.
pub(crate) fn chrono_now_iso() -> String {
    let duration = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default();
    format!("{}s-since-epoch", duration.as_secs())
}

/// Steps 0-2 helper: rejects an oversized source (H2), validates it
/// (sandbox rules), then classifies its evaluator mode SYNCHRONOUSLY --
/// `detect_evaluator_mode` subsumes the old "must define evaluate_node"
/// check: it IS that check for the Legacy case, plus the symmetric
/// Graph-mode check ADR-001 requires, with mixed/incomplete callback
/// families rejected as errors rather than silently guessed at.
fn validate_and_classify(user_code: &str) -> Result<EvaluatorMode, CompileError> {
    if user_code.len() > MAX_USER_CODE_BYTES {
        return Err(CompileError {
            message: format!(
                "Evaluator source exceeds the maximum allowed size of {} bytes (got {} bytes)",
                MAX_USER_CODE_BYTES,
                user_code.len()
            ),
            details: vec![],
            kind: CompileErrorKind::Compile,
        });
    }
    if let Err(errors) = validator::validate_evaluator_source(user_code) {
        return Err(CompileError {
            message: "Evaluator validation failed".to_string(),
            details: errors.iter().map(|e| e.to_string()).collect(),
            kind: CompileErrorKind::Compile,
        });
    }
    detect_evaluator_mode(user_code)
}

/// Step 3 helper: computes the ONE shared cache identity (Bug #1784) —
/// sensitive to the full assembled source (PREAMBLE + user code +
/// EPILOGUE), the ABI version, and the rustc toolchain. Computed from the
/// assembled source ONCE here and reused for the actual compile, so
/// PREAMBLE/EPILOGUE text is never re-derived redundantly.
fn assemble_and_identify(
    mode: EvaluatorMode,
    preamble: &str,
    user_code: &str,
    cache_dir: &Path,
) -> (String, (usize, usize), CacheIdentityInfo, PathBuf, PathBuf) {
    let (assembled_source, bounds) = assemble_for_mode(mode, preamble, user_code);
    let identity_info = cache_identity_info_from_source(&assembled_source);
    let so_path = cache_dir.join(format!("{}.so", identity_info.identity));
    let meta_path = cache_dir.join(format!("{}.meta", identity_info.identity));
    (assembled_source, bounds, identity_info, so_path, meta_path)
}

/// Orchestrates the full compile pipeline (Steps 0-8), threading state
/// through the helpers above in the SAME order as the original single
/// function: validate/classify -> assemble+identify -> cache check ->
/// isolated build -> compile -> publish -> metadata -> LRU eviction.
fn compile_evaluator_impl(user_code: &str, cache_dir: &Path, preamble: &str) -> Result<CompileResult, CompileError> {
    let mode = validate_and_classify(user_code)?;
    let (assembled_source, bounds, identity_info, so_path, meta_path) =
        assemble_and_identify(mode, preamble, user_code, cache_dir);

    if let Some(hit) = check_cache_hit(&so_path, &meta_path, &identity_info) {
        return Ok(hit);
    }

    // Bug #1425: isolated per-invocation build dir. The TempDir guard
    // recursively removes it (source + any rustc scratch files) on every
    // exit path -- success or the early '?' returns below.
    let (build_dir, build_rs_path, build_so_path) =
        prepare_build(cache_dir, &identity_info.identity, &assembled_source)?;

    let compile_ms = run_compile(&build_rs_path, &build_so_path, bounds)?;
    publish_artifact(&build_so_path, &so_path)?;
    write_cache_metadata_best_effort(&meta_path, &identity_info, compile_ms);
    cache::evict_lru(cache_dir, MAX_CACHE_ENTRIES);

    // build_dir's TempDir guard stays alive until here, matching the
    // original single-function scope exactly (source deleted on drop).
    drop(build_dir);

    Ok(CompileResult {
        so_path,
        compile_ms,
        cached: false,
    })
}
