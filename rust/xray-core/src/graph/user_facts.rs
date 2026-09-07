//! The fact-collection SEAM the fused pipeline calls into (Story #1787, S2,
//! AC2). `collect_facts` here is a plain Rust trait method, not yet a
//! dylib-loaded ABI export -- AC8 (out of scope for this slice) is what
//! will later wire a real user-authored callback into this exact seam,
//! mirroring how `crate::scanner::Evaluator` is a plain trait later backed
//! by `crate::dynlib::DynlibEvaluator`.
//!
//! This file defines the trait itself. `crate::graph::fused` (next module
//! added in this slice) is what actually PROVES the sequencing contract
//! AC2 mandates: `collect_facts` invoked EXACTLY ONCE per file, AFTER
//! extraction has fully populated the `LocalIndex`, on the SAME parsed
//! tree extraction just walked -- never interleaved with extraction, never
//! on a partial index. The end-to-end discriminating tests for that
//! contract live in `tests/ac2_fused_extract_collect.rs` against
//! `crate::graph::fused::process_file_fused`, not here.

use crate::graph::extract::local_index::LocalIndex;
use crate::graph::identity::SymbolId;
use crate::graph::string_table::StringTable;
use crate::owned_node::OwnedNode;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::Path;

/// One fact produced by a `FactCollector` for one file. `Serialize`/
/// `Deserialize` back the host-only `write_facts_file`/`read_facts_file`
/// disk format below (dual-review defect H2) -- unrelated to, and never
/// crossing, the dylib FFI boundary `dynlib::GraphDynlibEvaluator::
/// call_collect_facts` uses, which passes a real in-memory `Vec<UserFact>`
/// by value.
///
/// `custom_key` (Story #1785): `None` (the default shape every existing
/// collector already produces) means "attribute this fact to whichever
/// symbol encloses `line`" -- the pre-#1785 behavior, unchanged.
/// `Some(name)` means this fact names a genuinely non-symbol key (a config
/// key, event topic, or structural clone hash) and must be recorded under
/// `FactKey::Custom` ONLY, at the `repo_index::record_fused_result`
/// aggregation site -- never ALSO attributed to an enclosing symbol, which
/// would be a false identity (ADR-001). `name` is a plain `String`, not
/// `InternedStr`: a `FactCollector` never sees the graph's interning
/// substrate (`StringTable`/`CsrBuilder`) directly, mirroring how it never
/// constructs a `SymbolId` either -- the aggregation site is the one place
/// that owns interning, exactly like it already owns `enclosing_symbol`.
#[derive(Debug, Clone, PartialEq, Eq, Serialize, Deserialize)]
pub struct UserFact {
    pub kind: String,
    pub line: usize,
    pub message: String,
    pub custom_key: Option<String>,
}

/// Mirrors `crate::scanner::Evaluator`'s shape (`Send + Sync`, called
/// across rayon threads with no per-thread cloning). Implementors are
/// invoked by `crate::graph::fused` exactly once per file -- never
/// per-node -- with the file's WHOLE completed `LocalIndex` already built.
pub trait FactCollector: Send + Sync {
    fn collect_facts(&self, root: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact>;
}

/// ADR-001: an interned id into a `StringTable` for a genuinely non-symbol
/// fact key (config keys, event topics, structural hashes) -- named
/// distinctly from a bare `u32` so `FactKey::Custom`'s contract reads as
/// "an interned string id", exactly mirroring how `identity::SymbolId`
/// names `u64` for the same documentation reason.
pub type InternedStr = u32;

/// AC8 / ADR-001: "The graph fact key is the closed sum type:
/// `FactKey::Symbol(SymbolId) | FactKey::Custom(InternedStr)`. Symbol-
/// shaped facts use SymbolId and never use formatted strings; Custom is
/// reserved for genuinely non-symbol values." Closed (no third variant,
/// no catch-all) so a caller can never smuggle a symbol reference through
/// as a formatted string -- the exact anti-pattern ADR-001 replaces.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
pub enum FactKey {
    Symbol(SymbolId),
    Custom(InternedStr),
}

/// The fact store `analyze_graph(g: &CodeGraph, facts: &FactIndex)` reads
/// alongside the bound graph (AC7). Built by the caller from every
/// `UserFact` a `collect_facts` invocation (or, once dylib-loaded per AC8,
/// a compiled graph-mode evaluator's `collect_facts` export) produced
/// across the whole repository, keyed by `FactKey` -- never re-derived
/// from formatted text.
#[derive(Debug, Default)]
pub struct FactIndex {
    facts: HashMap<FactKey, Vec<UserFact>>,
    /// Story #1785: ONE shared string table for this index's
    /// `FactKey::Custom` names, owned here (never a bare caller-supplied
    /// `InternedStr`) so `insert_custom`/`get_custom` can round-trip a
    /// human-readable name (config key, event topic, structural hash)
    /// without a `FactCollector` or a graph-mode evaluator ever having to
    /// intern the string itself. Reuses the exact interning substrate
    /// `StringTable::intern`/`CsrBuilder::intern_string` already establish
    /// (Rule 4, anti-duplication) rather than inventing a second one.
    custom_keys: StringTable,
}

impl FactIndex {
    pub fn new() -> Self {
        FactIndex { facts: HashMap::new(), custom_keys: StringTable::new() }
    }

    /// Appends `fact` under `key`, preserving insertion order among facts
    /// sharing the same key. A key may legitimately accumulate more than
    /// one fact (e.g. several `deprecated` annotations on overloads of the
    /// same symbol).
    pub fn insert(&mut self, key: FactKey, fact: UserFact) {
        self.facts.entry(key).or_default().push(fact);
    }

    /// Every fact recorded under `key`, or an EMPTY slice if `key` was
    /// never inserted -- never a panic, and indistinguishable from "the
    /// key exists with zero facts" by design (both mean "nothing to
    /// report" to a caller).
    pub fn get(&self, key: &FactKey) -> &[UserFact] {
        self.facts.get(key).map(|v| v.as_slice()).unwrap_or(&[])
    }

    /// Story #1785: the name-based write surface for a genuinely non-symbol
    /// fact (config key, event topic, structural clone hash). Interns
    /// `name` into this index's OWN `custom_keys` table (same-name calls
    /// dedup to the same id, per `StringTable::intern`'s contract) and
    /// inserts `fact` under `FactKey::Custom(id)` -- the caller never
    /// constructs a `FactKey::Custom` or an `InternedStr` by hand.
    pub fn insert_custom(&mut self, name: &str, fact: UserFact) {
        let id = self.custom_keys.intern(name);
        self.insert(FactKey::Custom(id), fact);
    }

    /// Story #1785: the name-based read surface `FactsHandle::for_custom`
    /// delegates to. Resolves `name` against this index's OWN `custom_keys`
    /// table via `StringTable::find` -- a NON-mutating lookup, so probing a
    /// name nobody ever inserted under returns an EMPTY slice (mirroring
    /// `get`'s own absent-key contract) without silently interning a new,
    /// meaningless id as a side effect of the failed probe.
    pub fn get_custom(&self, name: &str) -> &[UserFact] {
        match self.custom_keys.find(name) {
            Some(id) => self.get(&FactKey::Custom(id)),
            None => &[],
        }
    }
}

/// On-disk shape `write_facts_file`/`read_facts_file` (de)serialize --
/// factored into its own borrowed/owned pair (rather than serializing
/// `FactIndex` itself) purely to let `write_facts_file` keep borrowing out
/// of `facts` instead of cloning it, exactly like the pre-#1785 `entries`
/// tuple already did. Story #1785 extends the H2-era `entries`-only format
/// with `custom_keys`: without it, a `FactKey::Custom` fact reloaded by a
/// SEPARATE `--analyze-graph` process would carry only an opaque id --
/// `FactIndex::get_custom`'s name-based lookup depends on the SAME
/// `StringTable` name->id assignment the writing process used, which lives
/// nowhere else on disk.
#[derive(Serialize)]
struct FactsFileWrite<'a> {
    entries: Vec<(&'a FactKey, &'a Vec<UserFact>)>,
    custom_keys: &'a StringTable,
}

#[derive(Deserialize)]
struct FactsFileRead {
    entries: Vec<(FactKey, Vec<UserFact>)>,
    custom_keys: StringTable,
}

/// Dual-review defect H2: persists `facts` to `path` as JSON -- the
/// `(FactKey, Vec<UserFact>)` entries as a plain array (since `FactKey` is
/// an enum and `serde_json` cannot use a non-string type as an object key),
/// plus (Story #1785) `facts`'s own `custom_keys` `StringTable` alongside
/// them so a `FactKey::Custom` fact survives the round trip resolvable BY
/// NAME, not just by its now-meaningless-across-processes id. This is the
/// counterpart `repo_index::build_repo_graph`'s real, aggregated
/// `FactIndex` needs so the separate `--analyze-graph` process (`xray-cli`'s
/// `run_analyze_graph`) can load real facts instead of always constructing
/// an empty one.
pub fn write_facts_file(facts: &FactIndex, path: &Path) -> std::io::Result<()> {
    let entries: Vec<(&FactKey, &Vec<UserFact>)> = facts.facts.iter().collect();
    let on_disk = FactsFileWrite { entries, custom_keys: &facts.custom_keys };
    let json = serde_json::to_vec(&on_disk)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    std::fs::write(path, json)
}

/// Reads a `FactIndex` previously written by `write_facts_file` back from
/// `path`. Fails loud (`io::Error`) on a missing file or malformed JSON --
/// never silently degrades to an empty `FactIndex`, which would be
/// indistinguishable from "this build genuinely collected no facts"
/// (Rule 13, anti-silent-failure). Callers that want to treat a missing/
/// invalid facts file as optional (e.g. an older graph with no
/// accompanying facts) decide that at the call site, not here.
pub fn read_facts_file(path: &Path) -> std::io::Result<FactIndex> {
    let bytes = std::fs::read(path)?;
    let on_disk: FactsFileRead = serde_json::from_slice(&bytes)
        .map_err(|e| std::io::Error::new(std::io::ErrorKind::InvalidData, e))?;
    Ok(FactIndex { facts: on_disk.entries.into_iter().collect(), custom_keys: on_disk.custom_keys })
}

/// ADR-002 / Story #1787 AC8: the SAME opaque-handle principle
/// `graph::csr::handle::GraphHandle` applies to `CodeGraph` extended to
/// `FactIndex`. `FactIndex`'s real field is a `HashMap<FactKey,
/// Vec<UserFact>>` -- mirroring that layout into the evaluator PREAMBLE
/// would reopen exactly the memory-layout-mismatch risk ADR-002 rejected
/// for `CodeGraph`, for the same underlying reason (a private std-collection
/// field, not a small/stable public shape). `FactsHandle` instead carries an
/// opaque context pointer plus accessor function pointers, scoped to the two
/// queries graph-mode evaluators need: symbol-keyed facts (`for_symbol`)
/// and, as of Story #1785, name-based `FactKey::Custom` facts (`for_custom`)
/// -- config keys, event topics, structural clone hashes that genuinely
/// have no `SymbolId` and would be a false identity if forced into one
/// (ADR-001). `for_custom` was scoped OUT of the original AC8 slice as a
/// deliberate deferral, not an oversight; #1785 is that deferred accessor
/// landing.
#[derive(Clone, Copy)]
pub struct FactsHandle<'facts> {
    ctx: *const (),
    for_symbol_fn: fn(*const (), u64) -> Vec<UserFact>,
    for_custom_fn: fn(*const (), &str) -> Vec<UserFact>,
    _facts: std::marker::PhantomData<&'facts ()>,
}

fn facts_from_ctx<'a>(ctx: *const ()) -> &'a FactIndex {
    unsafe { &*(ctx as *const FactIndex) }
}

fn thunk_facts_for_symbol(ctx: *const (), symbol: SymbolId) -> Vec<UserFact> {
    facts_from_ctx(ctx).get(&FactKey::Symbol(symbol)).to_vec()
}

/// Story #1785: the `FactsHandle::for_custom` thunk, mirroring
/// `thunk_facts_for_symbol`'s exact shape one level down (`FactIndex::
/// get_custom` instead of `get(&FactKey::Symbol(..))`). `name: &str` crosses
/// this fn-pointer boundary as a plain borrowed input parameter -- the same
/// established pattern `GraphHandle::reachable_from_fn`'s `&[u32]` input
/// already proves safe on this exact plain-Rust-ABI fn-pointer convention
/// (see `graph::csr::mod`'s module doc comment); only a RETURNED borrow
/// needs the raw-`(ptr, len)` workaround `resolve_string_raw_fn` uses, which
/// does not apply here since `Vec<UserFact>` is returned by value.
fn thunk_facts_for_custom(ctx: *const (), name: &str) -> Vec<UserFact> {
    facts_from_ctx(ctx).get_custom(name).to_vec()
}

impl<'facts> FactsHandle<'facts> {
    /// Builds a handle bound to `facts`. Mirrors `GraphHandle::from_graph`'s
    /// lifetime contract exactly: the `'facts` parameter is enforced by the
    /// borrow checker via the `PhantomData` marker, so the returned handle
    /// cannot outlive `facts`.
    pub fn from_facts(facts: &'facts FactIndex) -> FactsHandle<'facts> {
        FactsHandle {
            ctx: facts as *const FactIndex as *const (),
            for_symbol_fn: thunk_facts_for_symbol,
            for_custom_fn: thunk_facts_for_custom,
            _facts: std::marker::PhantomData,
        }
    }

    /// Every fact recorded under `FactKey::Symbol(symbol)`, or an empty
    /// `Vec` if none were -- mirrors `FactIndex::get`'s own "absent key"
    /// contract exactly.
    pub fn for_symbol(&self, symbol: SymbolId) -> Vec<UserFact> {
        (self.for_symbol_fn)(self.ctx, symbol)
    }

    /// Story #1785: every fact recorded under the `FactKey::Custom` named
    /// `name`, or an empty `Vec` if none were -- mirrors `for_symbol`'s own
    /// "absent key" contract exactly. `name` is a literal the evaluator's
    /// own source already knows (e.g. `facts.for_custom("db.host")`), never
    /// a value derived from graph data -- there is no accessor that turns a
    /// `SymbolId` into a custom-key name, which is precisely what keeps
    /// this surface from becoming a second way to smuggle a symbol
    /// reference through as a formatted string (ADR-001).
    pub fn for_custom(&self, name: &str) -> Vec<UserFact> {
        (self.for_custom_fn)(self.ctx, name)
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    struct CountingCollector;

    impl FactCollector for CountingCollector {
        fn collect_facts(&self, _root: &OwnedNode, file: &str, index: &LocalIndex) -> Vec<UserFact> {
            vec![UserFact {
                kind: "declaration_count".to_string(),
                line: 1,
                message: format!("{file}:{}", index.declarations.len()),
                custom_key: None,
            }]
        }
    }

    #[test]
    fn a_fact_collector_receives_the_file_name_and_index_it_was_called_with() {
        let root = OwnedNode::new_leaf_for_test("program", "", 1, true);
        let index = LocalIndex::new();
        let facts = CountingCollector.collect_facts(&root, "Foo.java", &index);
        assert_eq!(facts.len(), 1);
        assert_eq!(facts[0].message, "Foo.java:0");
    }

    // --- AC8 / ADR-001: FactKey::Symbol(SymbolId) | FactKey::Custom(InternedStr) ---
    // and the FactIndex analyze_graph receives alongside &CodeGraph.

    /// ADR-001: "Symbol-shaped facts use SymbolId and never use formatted
    /// strings. Custom is reserved for genuinely non-symbol values." A
    /// `FactIndex::get` for a key nobody inserted under must return an
    /// EMPTY slice, never panic and never be indistinguishable from "the
    /// key was inserted with zero facts" -- both are legitimately "no
    /// facts recorded for this key" from a caller's point of view.
    ///
    /// `Custom` keys are constructed by INTERNING a real config-key/event-
    /// topic string through `StringTable` -- exactly the closed
    /// `InternedStr` id shape ADR-001 specifies -- never a bare magic
    /// number standing in for "some string I didn't bother to name".
    #[test]
    fn fact_index_get_returns_facts_inserted_under_the_exact_key_and_empty_for_an_absent_one() {
        use crate::graph::identity::make_symbol_id;
        use crate::graph::string_table::StringTable;

        let symbol = make_symbol_id(1, 0);
        let mut strings = StringTable::new();
        let db_host_key: InternedStr = strings.intern("db.host");
        let unused_key: InternedStr = strings.intern("never.inserted");

        let mut index = FactIndex::new();
        index.insert(
            FactKey::Symbol(symbol),
            UserFact { kind: "deprecated".to_string(), line: 10, message: "old API".to_string(), custom_key: None },
        );
        index.insert(
            FactKey::Custom(db_host_key),
            UserFact { kind: "config_key".to_string(), line: 1, message: "db.host".to_string(), custom_key: None },
        );

        let symbol_facts = index.get(&FactKey::Symbol(symbol));
        assert_eq!(symbol_facts.len(), 1);
        assert_eq!(symbol_facts[0].message, "old API");

        let custom_facts = index.get(&FactKey::Custom(db_host_key));
        assert_eq!(custom_facts.len(), 1);
        assert_eq!(custom_facts[0].message, "db.host");

        assert!(index.get(&FactKey::Symbol(make_symbol_id(99, 99))).is_empty());
        assert!(index.get(&FactKey::Custom(unused_key)).is_empty());
    }

    /// Story #1785: `insert_custom`/`get_custom` are the NAME-based
    /// write/read surface a `FactCollector` and a graph-mode evaluator
    /// actually use -- neither ever sees a raw `InternedStr`. `insert_custom`
    /// interns `name` into `FactIndex`'s OWN string table (never the raw
    /// `FactIndex::insert` a caller would have to intern for manually), and
    /// `get_custom` resolves `name` back to the same id to look the facts
    /// up. A name nobody ever inserted under must return an EMPTY slice --
    /// mirroring `get`'s own absent-key contract -- never a panic and never
    /// a side-effecting intern of a brand-new id for a probe that found
    /// nothing.
    #[test]
    fn fact_index_insert_custom_and_get_custom_round_trip_by_name() {
        let mut index = FactIndex::new();
        index.insert_custom(
            "db.host",
            UserFact { kind: "config_key".to_string(), line: 1, message: "db.host".to_string(), custom_key: None },
        );

        let facts = index.get_custom("db.host");
        assert_eq!(facts.len(), 1);
        assert_eq!(facts[0].message, "db.host");

        assert!(index.get_custom("never.inserted").is_empty());
    }

    /// Story #1785: `write_facts_file`/`read_facts_file` must preserve
    /// `FactIndex`'s OWN `custom_keys` name->id mapping, not just the raw
    /// `(FactKey, Vec<UserFact>)` entries -- otherwise a `FactKey::Custom`
    /// fact survives the disk round trip only as an opaque id nothing can
    /// resolve BY NAME any more, which is exactly the shape the CLI's
    /// `--analyze-graph --facts-in` two-process handoff (`main.rs`'s
    /// `run_analyze_graph`) depends on: the reloading process never shares
    /// the original process's in-memory `StringTable`, so `get_custom` must
    /// still find the fact after `read_facts_file` reconstructs a BRAND NEW
    /// `FactIndex` from disk.
    #[test]
    fn insert_custom_facts_written_to_disk_are_still_reachable_by_name_after_read_facts_file() {
        let mut original = FactIndex::new();
        original.insert_custom(
            "db.host",
            UserFact { kind: "config_key".to_string(), line: 1, message: "db.host".to_string(), custom_key: None },
        );

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("facts.json");
        write_facts_file(&original, &path).expect("write_facts_file must succeed");
        let reloaded = read_facts_file(&path).expect("read_facts_file must succeed");

        let facts = reloaded.get_custom("db.host");
        assert_eq!(facts.len(), 1, "a FactKey::Custom fact must still be reachable BY NAME after a disk round trip");
        assert_eq!(facts[0].message, "db.host");
        assert!(reloaded.get_custom("never.inserted").is_empty());
    }

    /// Dual-review defect H2: `main.rs`'s `--analyze-graph` subcommand can
    /// only ever hand `analyze_graph` a REAL `FactIndex` (rather than the
    /// pre-fix, permanently-empty `FactIndex::new()`) if the facts
    /// `repo_index::build_repo_graph` aggregates can be persisted alongside
    /// the graph and reloaded by the separate `--analyze-graph` process.
    /// `write_facts_file`/`read_facts_file` must round-trip every fact
    /// under its EXACT `FactKey` -- both variants (`Symbol` and `Custom`).
    #[test]
    fn a_fact_index_written_to_disk_round_trips_through_write_and_read_facts_file() {
        use crate::graph::identity::make_symbol_id;
        use crate::graph::string_table::StringTable;

        let mut strings = StringTable::new();
        let custom_key: InternedStr = strings.intern("db.host");

        let mut original = FactIndex::new();
        original.insert(
            FactKey::Symbol(make_symbol_id(1, 0)),
            UserFact { kind: "deprecated".to_string(), line: 10, message: "old API".to_string(), custom_key: None },
        );
        original.insert(
            FactKey::Custom(custom_key),
            UserFact { kind: "config_key".to_string(), line: 1, message: "db.host".to_string(), custom_key: None },
        );

        let dir = tempfile::tempdir().unwrap();
        let path = dir.path().join("facts.json");
        write_facts_file(&original, &path).expect("write_facts_file must succeed");
        let reloaded = read_facts_file(&path).expect("read_facts_file must succeed");

        assert_eq!(reloaded.get(&FactKey::Symbol(make_symbol_id(1, 0))), original.get(&FactKey::Symbol(make_symbol_id(1, 0))));
        assert_eq!(reloaded.get(&FactKey::Custom(custom_key)), original.get(&FactKey::Custom(custom_key)));
        assert!(reloaded.get(&FactKey::Symbol(make_symbol_id(9, 9))).is_empty());
    }

    /// ADR-002 extension: `FactsHandle` must delegate to the real
    /// `FactIndex` rather than reimplementing lookup logic -- proven by
    /// comparing against `FactIndex::get` directly, for both a populated
    /// key and one nobody ever inserted under.
    #[test]
    fn facts_handle_for_symbol_delegates_to_the_real_fact_index() {
        use crate::graph::identity::make_symbol_id;

        let symbol = make_symbol_id(1, 0);
        let absent_symbol = make_symbol_id(9, 9);
        let mut index = FactIndex::new();
        index.insert(
            FactKey::Symbol(symbol),
            UserFact { kind: "deprecated".to_string(), line: 10, message: "old API".to_string(), custom_key: None },
        );

        let handle = FactsHandle::from_facts(&index);

        assert_eq!(handle.for_symbol(symbol), index.get(&FactKey::Symbol(symbol)).to_vec());
        assert_eq!(handle.for_symbol(symbol).len(), 1);
        assert_eq!(handle.for_symbol(symbol)[0].message, "old API");
        assert!(handle.for_symbol(absent_symbol).is_empty());
    }

    /// Story #1785: `FactsHandle::for_custom` is the read-path counterpart
    /// to `FactIndex::insert_custom` -- a graph-mode evaluator's ONLY way
    /// to retrieve a `FactKey::Custom` fact (config key, event topic,
    /// structural hash). Mirrors `facts_handle_for_symbol_delegates_to_the_
    /// real_fact_index` exactly: must delegate to the real `FactIndex.
    /// get_custom` rather than reimplementing lookup logic, for both a
    /// populated name and one nobody ever inserted under.
    #[test]
    fn facts_handle_for_custom_delegates_to_the_real_fact_index() {
        let mut index = FactIndex::new();
        index.insert_custom(
            "db.host",
            UserFact { kind: "config_key".to_string(), line: 1, message: "db.host".to_string(), custom_key: None },
        );

        let handle = FactsHandle::from_facts(&index);

        assert_eq!(handle.for_custom("db.host"), index.get_custom("db.host").to_vec());
        assert_eq!(handle.for_custom("db.host").len(), 1);
        assert_eq!(handle.for_custom("db.host")[0].message, "db.host");
        assert!(handle.for_custom("never.inserted").is_empty());
    }
}
