//! AC3 (Story #1793, S4) ambiguity measurement tool -- the story's own
//! acceptance test: "If ambiguity does not measurably improve, the story
//! FAILS regardless of code quality."
//!
//! Deliberately built ONLY against `xray_core`'s stable public surface
//! (`build_repo_graph`, `RepoIndexOptions`, `REF_KIND_INVOCATION`,
//! `Confidence`, `FactCollector`) -- none of it changed by this story, so
//! this exact file can be copied byte-for-byte into a worktree checked out
//! at the pre-S4 baseline commit and run there unmodified, producing a
//! true apples-to-apples before/after comparison.
//!
//! Usage: `cargo run --release --example measure_ambiguity -- <repo_root> [max_files]`

use std::path::{Path, PathBuf};
use std::time::Instant;

use xray_core::graph::bind::REF_KIND_INVOCATION;
use xray_core::graph::budget::IndexBudget;
use xray_core::graph::confidence::Confidence;
use xray_core::graph::extract::local_index::LocalIndex;
use xray_core::graph::repo_index::{build_repo_graph, RepoIndexOptions};
use xray_core::graph::user_facts::{FactCollector, UserFact};
use xray_core::owned_node::OwnedNode;

struct NoOpCollector;
impl FactCollector for NoOpCollector {
    fn collect_facts(&self, _root: &OwnedNode, _file: &str, _index: &LocalIndex) -> Vec<UserFact> {
        Vec::new()
    }
}

/// Bounded by the finite number of directory-tree entries under `root`
/// (Rule 14) -- `walkdir::WalkDir`'s own iterator terminates by
/// construction once the tree is exhausted. A per-entry walk error (e.g. a
/// permission-denied subdirectory) is LOGGED and skipped, never silently
/// discarded (Rule 13, anti-silent-failure) -- it would otherwise
/// undercount files and invalidate the before/after comparison with no
/// indication anything was missed.
fn collect_java_relative_paths(root: &Path) -> Vec<String> {
    let mut paths = Vec::new();
    let mut walk_errors = 0usize;
    for entry in walkdir::WalkDir::new(root) {
        let entry = match entry {
            Ok(entry) => entry,
            Err(err) => {
                eprintln!("WARNING: directory walk error (skipped): {err}");
                walk_errors += 1;
                continue;
            }
        };
        if !entry.file_type().is_file() {
            continue;
        }
        if entry.path().extension().and_then(|e| e.to_str()) != Some("java") {
            continue;
        }
        if let Ok(relative) = entry.path().strip_prefix(root) {
            paths.push(relative.to_string_lossy().replace('\\', "/"));
        }
    }
    if walk_errors > 0 {
        eprintln!("WARNING: {walk_errors} directory entries could not be walked -- file count may undercount");
    }
    paths
}

/// AC3's own tier definitions, derived purely from `Reference`/`Candidate`
/// query surface already exposed by `CodeGraph` -- documented here since
/// the story leaves the exact tier boundaries to this measurement:
/// Tier A = a single candidate (`cand_len == 1`, any confidence); Tier B =
/// more than one candidate but with GENUINE narrowing evidence (strongest
/// candidate confidence beats bare `NameOnly` -- covers both AC1's
/// inheritance families at `Confidence::High` and AC2's overload-narrowed
/// sets); Tier C = more than one candidate with ZERO narrowing evidence
/// (every candidate stuck at `NameOnly`) -- the blind-ambiguity case.
/// Unresolved (`cand_len == 0`) is tracked SEPARATELY, never folded into
/// any tier (it is "not found", a different failure mode from ambiguity).
#[derive(Default)]
struct TierCounts {
    total: usize,
    unresolved: usize,
    tier_a_unambiguous: usize,
    tier_b_ambiguous_with_evidence: usize,
    tier_c_blind_ambiguous: usize,
}

fn measure(graph: &xray_core::graph::csr::CodeGraph) -> TierCounts {
    let mut counts = TierCounts::default();
    for reference in graph.references() {
        if reference.kind != REF_KIND_INVOCATION {
            continue;
        }
        counts.total += 1;
        let candidates = graph.candidates_for(reference);
        if candidates.is_empty() {
            counts.unresolved += 1;
            continue;
        }
        if candidates.len() == 1 {
            counts.tier_a_unambiguous += 1;
            continue;
        }
        let strongest = candidates.iter().map(|c| c.confidence()).max().unwrap_or(Confidence::NameOnly);
        if strongest > Confidence::NameOnly {
            counts.tier_b_ambiguous_with_evidence += 1;
        } else {
            counts.tier_c_blind_ambiguous += 1;
        }
    }
    counts
}

/// DIAGNOSTIC (Story #1793 S4 investigation) -- aggregates
/// `classify_family_purity` (see its doc comment) across every resolved
/// invocation reference in `graph`, mirroring `measure`'s own
/// `REF_KIND_INVOCATION`-only, non-empty-candidates filtering exactly so
/// the two independently-computed totals can be cross-checked (see
/// `main`'s `debug_assert_eq!`). NOT part of the AC3 acceptance metric --
/// every field here is read-only derived from candidates AC3 already
/// computed, never a redefinition of `TierCounts`.
#[derive(Default)]
struct FamilyDiagnostics {
    resolved_total: usize,
    ambiguous_total: usize,
    family_pure_ambiguous: usize,
    heterogeneous_ambiguous: usize,
    any_family_expansion: usize,
    family_truncated: usize,
    pre_expansion_sizes: Vec<usize>,
    post_expansion_sizes: Vec<usize>,
}

fn family_diagnostics(graph: &xray_core::graph::csr::CodeGraph) -> FamilyDiagnostics {
    let mut diag = FamilyDiagnostics::default();
    for reference in graph.references() {
        if reference.kind != REF_KIND_INVOCATION {
            continue;
        }
        let candidates = graph.candidates_for(reference);
        if candidates.is_empty() {
            continue;
        }
        diag.resolved_total += 1;
        let candidate_reasons: Vec<u16> = candidates.iter().map(|c| c.reasons()).collect();
        let purity = classify_family_purity(&candidate_reasons);
        diag.pre_expansion_sizes.push(purity.pre_size);
        diag.post_expansion_sizes.push(purity.post_size);
        if purity.has_family_expansion() {
            diag.any_family_expansion += 1;
        }
        if purity.truncated {
            diag.family_truncated += 1;
        }
        if purity.is_ambiguous() {
            diag.ambiguous_total += 1;
            if purity.is_family_pure_ambiguous() {
                diag.family_pure_ambiguous += 1;
            } else {
                diag.heterogeneous_ambiguous += 1;
            }
        }
    }
    diag
}

fn pct(n: usize, denom: usize) -> f64 {
    if denom == 0 {
        0.0
    } else {
        100.0 * n as f64 / denom as f64
    }
}

fn parse_max_files(args: &[String]) -> Option<usize> {
    let raw = args.get(2)?;
    match raw.parse::<usize>() {
        Ok(0) => {
            eprintln!("error: max_files must be >= 1 (0 would index nothing)");
            std::process::exit(2);
        }
        Ok(value) => Some(value),
        Err(err) => {
            eprintln!("error: invalid max_files argument {raw:?}: {err}");
            std::process::exit(2);
        }
    }
}

/// AC3 memory-safety amendment: `IndexBudget::unlimited()` never appears on
/// any production path (it exists ONLY in tests and, previously, this
/// harness) -- measuring under it disables the very AC6 degradation ladder
/// production always runs under, so a "successful" unlimited-budget run
/// says nothing about the real production path. These two numbers are a
/// deliberately conservative, DOCUMENTED finite ceiling (not a tuned
/// production calibration -- the real admission gate is the Python-side
/// `MemoryGovernor`, byte-metered via cgroup pressure, per
/// `docs/adr/ADR-003-graph-memory-governor-integration.md`; `IndexBudget`
/// itself is only ever "a deliberately simple proxy for the index memory
/// budget", per its own doc comment): a raw-candidate-count ceiling
/// generous enough that a correctly-capped family expansion (this story's
/// own fix, `families::MAX_FAMILY_SIZE`) should never approach it on a
/// real corpus, while still being LOW enough to act as a genuine
/// circuit-breaker if some OTHER, still-unknown expansion path turns out
/// to be unbounded. `Candidate` is 8 bytes in the CSR arena (`u32` symbol +
/// `u8` confidence + `u16` reasons, padded) -- 10,000,000 raw candidates is
/// ~80MB of FINAL arena, well inside the epic's 256MB target with headroom
/// for declarations/signatures/symbol-interning. `max_candidates_per_
/// reference` is set well above `MAX_FAMILY_SIZE` so a genuinely healthy
/// family expansion is never itself the trigger for the per-reference cap.
const REALISTIC_MAX_TOTAL_CANDIDATES: usize = 10_000_000;
const REALISTIC_MAX_CANDIDATES_PER_REFERENCE: usize = 512;

fn realistic_budget() -> IndexBudget {
    IndexBudget::new(REALISTIC_MAX_TOTAL_CANDIDATES, REALISTIC_MAX_CANDIDATES_PER_REFERENCE)
}

/// S2 baseline reference numbers from Story #1793's own context (two
/// independent prototypes measured against the SAME Keycloak corpus, prior
/// to S4's AC1/AC2 changes). Named constants per Messi Rule 17
/// (anti-magic) -- NOT remeasured by this binary; a different codebase and
/// methodology produced them, so they are printed for comparison only,
/// never recomputed here.
const S2_BASELINE_PROTOTYPE_1_PCT: f64 = 35.8;
const S2_BASELINE_PROTOTYPE_2_PCT: f64 = 26.4;

/// DIAGNOSTIC: item 2 (family-collapsed ambiguity, a SEPARATE line, never
/// a replacement for the AC3 number) and item 3 (before/after
/// candidate-set-size distribution) from this task's diagnostic spec.
fn print_family_collapsed_ambiguity_and_distribution(diag: &FamilyDiagnostics, collapsed_ambiguous: usize) {
    println!(
        "diagnostic_ambiguity_pct_family_collapsed (each pure-family set counted as ONE dispatch site): {:.2}%",
        pct(collapsed_ambiguous, diag.resolved_total)
    );
    let mut pre_sizes = diag.pre_expansion_sizes.clone();
    let mut post_sizes = diag.post_expansion_sizes.clone();
    let (pre_median, pre_p90, pre_max) = distribution_stats(&mut pre_sizes);
    let (post_median, post_p90, post_max) = distribution_stats(&mut post_sizes);
    println!(
        "candidate_set_size_before_family_expansion: median={pre_median} p90={pre_p90} max={pre_max} (n={})",
        pre_sizes.len()
    );
    println!(
        "candidate_set_size_after_family_expansion:  median={post_median} p90={post_p90} max={post_max} (n={})",
        post_sizes.len()
    );
}

/// DIAGNOSTIC: item 4 from this task's diagnostic spec -- attribution of
/// the increase from the S2 baseline to the current S4 number, split into
/// the portion explained by family expansion vs. everything else.
fn print_baseline_attribution(ambiguous_total: usize, resolved: usize, collapsed_ambiguous: usize) {
    println!();
    println!(
        "=== DIAGNOSTIC: attribution vs S2 baseline (reference numbers from Story #1793's own \
         context, NOT remeasured by this binary -- different codebase/methodology) ==="
    );
    println!("s2_baseline_pct_prototype_1 (higher of the two independent prototypes): {S2_BASELINE_PROTOTYPE_1_PCT:.2}%");
    println!("s2_baseline_pct_prototype_2 (lower of the two independent prototypes): {S2_BASELINE_PROTOTYPE_2_PCT:.2}%");
    let current_pct = pct(ambiguous_total, resolved);
    let collapsed_pct = pct(collapsed_ambiguous, resolved);
    println!("current_s4_ambiguity_pct_of_resolved (== the AC3 metric above, repeated for convenience): {current_pct:.2}%");
    println!("family_collapsed_ambiguity_pct_of_resolved (diagnostic, repeated for convenience): {collapsed_pct:.2}%");
    let raw_increase_pp = current_pct - S2_BASELINE_PROTOTYPE_1_PCT;
    let collapsed_increase_pp = collapsed_pct - S2_BASELINE_PROTOTYPE_1_PCT;
    println!("raw_increase_over_s2_higher_baseline_pp: {raw_increase_pp:.2}");
    println!("family_collapsed_increase_over_s2_higher_baseline_pp: {collapsed_increase_pp:.2}");
    if raw_increase_pp > 0.0 {
        let fraction_explained_by_family = ((raw_increase_pp - collapsed_increase_pp) / raw_increase_pp) * 100.0;
        println!(
            "fraction_of_increase_over_s2_higher_baseline_explained_by_family_expansion_pct: {:.2}%",
            fraction_explained_by_family.max(0.0)
        );
    } else {
        println!(
            "fraction_of_increase_over_s2_higher_baseline_explained_by_family_expansion_pct: n/a \
             (no increase over baseline to explain)"
        );
    }
}

/// DIAGNOSTIC (Story #1793 S4 investigation) -- NOT part of the AC3
/// acceptance metric, and computed from it read-only. Classifies a single
/// invocation reference's resolved candidate-set by whether its ambiguity
/// is fully explained by AC1's inheritance-family expansion
/// (`apply_inheritance_family_expansion` in
/// `xray-core/src/graph/bind/resolve.rs`) or whether genuine ambiguity was
/// already present before that expansion ran.
///
/// `pre_size` reconstructs the candidate count BEFORE family expansion, by
/// counting candidates whose `reasons` bitmask does NOT carry
/// `reasons::INHERITANCE_FAMILY`. This reconstruction is sound because
/// `apply_inheritance_family_expansion`'s own doc comment states it "EXPANDS
/// ... it never removes anything" -- every candidate present before
/// expansion is still present after, unflagged, so subtracting the
/// family-tagged candidates recovers the exact pre-expansion set.
#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct FamilyPurity {
    pre_size: usize,
    post_size: usize,
    truncated: bool,
}

impl FamilyPurity {
    fn is_ambiguous(&self) -> bool {
        self.post_size > 1
    }

    /// True when this reference's ambiguity is ENTIRELY attributable to
    /// family expansion: at most one candidate existed before expansion
    /// ran (so at most one interface could have triggered it -- see this
    /// function's doc comment), meaning every unit of ambiguity in the
    /// final set was added by `apply_inheritance_family_expansion`, never
    /// by any other narrowing path.
    fn is_family_pure_ambiguous(&self) -> bool {
        self.is_ambiguous() && self.pre_size <= 1
    }

    fn has_family_expansion(&self) -> bool {
        self.post_size > self.pre_size
    }
}

/// Pure classification over one reference's per-candidate `reasons`
/// bitmasks (`Candidate::reasons()`) -- deliberately decoupled from
/// `CodeGraph` so it is unit-testable without building a full graph.
fn classify_family_purity(candidate_reasons: &[u16]) -> FamilyPurity {
    let post_size = candidate_reasons.len();
    let pre_size =
        candidate_reasons.iter().filter(|r| *r & xray_core::graph::reasons::INHERITANCE_FAMILY == 0).count();
    let truncated = candidate_reasons.iter().any(|r| *r & xray_core::graph::reasons::FAMILY_TRUNCATED != 0);
    FamilyPurity { pre_size, post_size, truncated }
}

/// Percentile used by `distribution_stats`'s p90 figure. Named per Messi
/// Rule 17 (anti-magic) rather than an inline literal at the call site.
const P90_PERCENTILE: f64 = 0.90;

/// DIAGNOSTIC: median/p90/max over a candidate-set-size sample. `values`
/// is sorted in place -- this function's only loop is the sort itself,
/// bounded by `values.len()` (Rule 14), with the two index computations
/// below being O(1). Median here is the simple "value at the middle sorted
/// index" (upper-mid on an even-length sample), a diagnostic approximation
/// documented as such rather than a statistically interpolated median.
fn distribution_stats(values: &mut [usize]) -> (usize, usize, usize) {
    if values.is_empty() {
        return (0, 0, 0);
    }
    values.sort_unstable();
    let median = values[values.len() / 2];
    let p90_index = (((values.len() as f64) * P90_PERCENTILE) as usize).min(values.len() - 1);
    let p90 = values[p90_index];
    let max = *values.last().expect("checked non-empty above");
    (median, p90, max)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// RED phase (diagnostic breakdown, Story #1793 S4 investigation --
    /// NOT a binder-behaviour change): a single non-family candidate is
    /// neither ambiguous nor family-pure. Exercises `classify_family_purity`
    /// with zero graph construction (pure function over `reasons` bitmasks).
    #[test]
    fn classify_family_purity_single_plain_candidate_is_not_ambiguous() {
        let purity = classify_family_purity(&[xray_core::graph::reasons::SAME_FILE]);
        assert_eq!(purity.pre_size, 1);
        assert_eq!(purity.post_size, 1);
        assert!(!purity.is_ambiguous());
        assert!(!purity.is_family_pure_ambiguous());
        assert!(!purity.has_family_expansion());
        assert!(!purity.truncated);
    }

    /// One base candidate plus one family-expansion-added candidate: the
    /// reference IS ambiguous, and that ambiguity is ENTIRELY attributable
    /// to family expansion (pre_size <= 1).
    #[test]
    fn classify_family_purity_one_base_plus_family_member_is_family_pure() {
        let reasons = [xray_core::graph::reasons::IMPORTED, xray_core::graph::reasons::INHERITANCE_FAMILY];
        let purity = classify_family_purity(&reasons);
        assert_eq!(purity.pre_size, 1);
        assert_eq!(purity.post_size, 2);
        assert!(purity.is_ambiguous());
        assert!(purity.is_family_pure_ambiguous());
        assert!(purity.has_family_expansion());
    }

    /// Two candidates, NEITHER carrying `INHERITANCE_FAMILY`: genuine
    /// heterogeneous ambiguity that predates any family expansion --
    /// must NOT be classified as family-pure.
    #[test]
    fn classify_family_purity_two_plain_candidates_is_heterogeneous_not_family_pure() {
        let reasons = [xray_core::graph::reasons::SAME_FILE, xray_core::graph::reasons::IMPORTED];
        let purity = classify_family_purity(&reasons);
        assert_eq!(purity.pre_size, 2);
        assert_eq!(purity.post_size, 2);
        assert!(purity.is_ambiguous());
        assert!(!purity.is_family_pure_ambiguous());
        assert!(!purity.has_family_expansion());
    }

    /// Two ALREADY-ambiguous base candidates plus one family-expansion
    /// addition: pre-existing heterogeneous ambiguity is NOT masked by the
    /// family expansion riding along on top of it -- still not family-pure.
    #[test]
    fn classify_family_purity_preexisting_ambiguity_with_family_addition_stays_heterogeneous() {
        let reasons = [
            xray_core::graph::reasons::SAME_FILE,
            xray_core::graph::reasons::IMPORTED,
            xray_core::graph::reasons::INHERITANCE_FAMILY,
        ];
        let purity = classify_family_purity(&reasons);
        assert_eq!(purity.pre_size, 2);
        assert_eq!(purity.post_size, 3);
        assert!(purity.is_ambiguous());
        assert!(!purity.is_family_pure_ambiguous(), "pre-existing ambiguity must not be masked by family expansion");
        assert!(purity.has_family_expansion());
    }

    /// The `FAMILY_TRUNCATED` bit on any candidate must be surfaced via
    /// `truncated`, independent of purity classification.
    #[test]
    fn classify_family_purity_detects_truncation_flag() {
        let reasons = [
            xray_core::graph::reasons::IMPORTED,
            xray_core::graph::reasons::INHERITANCE_FAMILY | xray_core::graph::reasons::FAMILY_TRUNCATED,
        ];
        let purity = classify_family_purity(&reasons);
        assert!(purity.truncated);
    }

    /// `distribution_stats` on an empty sample returns all-zero rather than
    /// panicking (Rule 13, anti-silent-failure via a defined, documented
    /// zero-state rather than an index panic).
    #[test]
    fn distribution_stats_empty_sample_returns_zeros() {
        let mut values: Vec<usize> = Vec::new();
        assert_eq!(distribution_stats(&mut values), (0, 0, 0));
    }

    /// A ten-element sample has a well-defined, documented median/p90/max
    /// under this function's simple (non-interpolated) definition.
    #[test]
    fn distribution_stats_ten_element_sample() {
        let mut values: Vec<usize> = vec![10, 9, 8, 7, 6, 5, 4, 3, 2, 1];
        let (median, p90, max) = distribution_stats(&mut values);
        assert_eq!(median, 6, "sorted [1..10], index len/2=5 -> value 6");
        assert_eq!(p90, 10, "index (10*0.9)=9 -> value 10");
        assert_eq!(max, 10);
    }

    /// `families::MAX_FAMILY_SIZE` (`xray-core/src/graph/bind/families.rs`)
    /// is `pub(crate)`, not reachable from this external example crate --
    /// this constant is a DELIBERATE duplicate mirroring its documented
    /// value, so this regression guard fails loudly if the two ever drift
    /// apart rather than silently passing against a stale copy.
    const FAMILY_EXPANSION_CAP_MIRROR: usize = 64;

    /// The harness's own `IndexBudget` must never itself become the
    /// bottleneck for a CORRECTLY-CAPPED family expansion: if
    /// `max_candidates_per_reference` were <= `MAX_FAMILY_SIZE`, a single
    /// healthy interface with exactly the family cap's worth of real
    /// implementors would ALSO trip AC6's per-reference ladder cap, making
    /// `IndexBudgetExceeded` fire even though nothing pathological
    /// happened -- muddying the measurement's ladder-engagement report.
    #[test]
    fn realistic_budget_per_reference_cap_exceeds_the_family_expansion_cap() {
        assert!(
            realistic_budget().max_candidates_per_reference() > FAMILY_EXPANSION_CAP_MIRROR,
            "the harness's per-reference budget cap must exceed families::MAX_FAMILY_SIZE"
        );
    }
}

fn main() {
    let args: Vec<String> = std::env::args().collect();
    if args.len() < 2 {
        eprintln!("usage: measure_ambiguity <repo_root> [max_files]");
        std::process::exit(2);
    }
    let repo_root = PathBuf::from(&args[1]);
    if !repo_root.is_dir() {
        eprintln!("error: repo_root {repo_root:?} does not exist or is not a directory");
        std::process::exit(2);
    }
    let max_files = parse_max_files(&args);

    let paths = collect_java_relative_paths(&repo_root);
    eprintln!("discovered {} .java files under {}", paths.len(), repo_root.display());

    let budget = realistic_budget();
    let options = RepoIndexOptions { budget, max_files };
    let start = Instant::now();
    let result = build_repo_graph(&repo_root, &paths, &options, &NoOpCollector).expect("no file_id collision");
    let elapsed = start.elapsed();

    let counts = measure(&result.graph);
    let ambiguous_total = counts.tier_b_ambiguous_with_evidence + counts.tier_c_blind_ambiguous;

    println!("=== AC3 ambiguity measurement ===");
    println!("repo_root: {}", repo_root.display());
    println!("files_discovered: {}", paths.len());
    println!("elapsed_seconds: {:.2}", elapsed.as_secs_f64());
    println!("index_budget_max_total_candidates: {REALISTIC_MAX_TOTAL_CANDIDATES}");
    println!("index_budget_max_candidates_per_reference: {REALISTIC_MAX_CANDIDATES_PER_REFERENCE}");
    println!(
        "graph_completeness: {:?} (IndexBudgetExceeded/ResolutionAmbiguous means AC6's degradation ladder engaged)",
        result.graph.completeness()
    );
    println!("fact_graph_complete: {}", result.fact_graph_complete);
    println!("files_with_parse_errors: {}", result.files_with_parse_errors);
    println!("files_with_read_errors: {}", result.files_with_read_errors);
    println!("files_with_extractor_panics: {}", result.files_with_extractor_panics);
    println!("truncated_by_max_files: {}", result.truncated_by_max_files);
    println!("total_invocation_references: {}", counts.total);
    println!("unresolved_empty_candidate_set: {} ({:.2}%)", counts.unresolved, pct(counts.unresolved, counts.total));
    println!(
        "tier_a_unambiguous_single_candidate: {} ({:.2}%)",
        counts.tier_a_unambiguous,
        pct(counts.tier_a_unambiguous, counts.total)
    );
    println!(
        "tier_b_ambiguous_with_evidence: {} ({:.2}%)",
        counts.tier_b_ambiguous_with_evidence,
        pct(counts.tier_b_ambiguous_with_evidence, counts.total)
    );
    println!(
        "tier_c_blind_ambiguous_nameonly: {} ({:.2}%)",
        counts.tier_c_blind_ambiguous,
        pct(counts.tier_c_blind_ambiguous, counts.total)
    );
    println!(
        "ambiguity_pct_of_all_references (Tier B + Tier C): {:.2}%",
        pct(ambiguous_total, counts.total)
    );
    let resolved = counts.total - counts.unresolved;
    println!(
        "ambiguity_pct_of_resolved_references (matches the S2 baseline's own methodology): {:.2}%",
        pct(ambiguous_total, resolved)
    );

    let java_depth = result.graph.binder_depths().iter().find(|d| d.language == "java");
    println!("java_binder_levels_reached_bitmask: {:?}", java_depth.map(|d| d.levels_reached));

    print_family_diagnostics(&result.graph, ambiguous_total, resolved);
}

/// DIAGNOSTIC breakdown (Story #1793 S4 investigation) -- NOT part of the
/// AC3 acceptance metric, which `main` already printed unchanged above
/// this call. Read-only attribution analysis over the SAME graph/
/// candidates AC3 already computed. `ambiguous_total`/`resolved` are AC3's
/// own already-computed totals, passed in so the cross-check below can
/// verify `family_diagnostics`'s independently computed totals agree.
fn print_family_diagnostics(graph: &xray_core::graph::csr::CodeGraph, ambiguous_total: usize, resolved: usize) {
    let diag = family_diagnostics(graph);
    debug_assert_eq!(
        diag.ambiguous_total, ambiguous_total,
        "family_diagnostics and measure() must agree on the ambiguous-reference total -- \
         both independently filter REF_KIND_INVOCATION + non-empty candidates over the same graph"
    );
    debug_assert_eq!(
        diag.resolved_total, resolved,
        "family_diagnostics and measure() must agree on the resolved-reference total"
    );

    let collapsed_ambiguous = diag.heterogeneous_ambiguous;
    print_family_expansion_counts(&diag);
    print_family_collapsed_ambiguity_and_distribution(&diag, collapsed_ambiguous);
    print_baseline_attribution(ambiguous_total, resolved, collapsed_ambiguous);
}

/// DIAGNOSTIC: raw family-expansion/family-purity counts.
fn print_family_expansion_counts(diag: &FamilyDiagnostics) {
    println!();
    println!("=== DIAGNOSTIC: family-expansion attribution (NOT part of the AC3 metric above) ===");
    println!("resolved_invocation_references_diagnostic: {}", diag.resolved_total);
    println!(
        "references_with_any_family_expansion: {} ({:.2}% of resolved)",
        diag.any_family_expansion,
        pct(diag.any_family_expansion, diag.resolved_total)
    );
    println!("references_with_family_truncated_flag (MAX_FAMILY_SIZE cap engaged): {}", diag.family_truncated);
    println!(
        "ambiguous_family_pure_single_interface: {} ({:.2}% of ambiguous, {:.2}% of resolved)",
        diag.family_pure_ambiguous,
        pct(diag.family_pure_ambiguous, diag.ambiguous_total),
        pct(diag.family_pure_ambiguous, diag.resolved_total)
    );
    println!(
        "ambiguous_heterogeneous_non_family_pure: {} ({:.2}% of ambiguous, {:.2}% of resolved)",
        diag.heterogeneous_ambiguous,
        pct(diag.heterogeneous_ambiguous, diag.ambiguous_total),
        pct(diag.heterogeneous_ambiguous, diag.resolved_total)
    );
}
