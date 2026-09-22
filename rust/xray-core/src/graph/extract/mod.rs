//! Per-language extraction registry (Story #1787, S2, AC2).
//!
//! The engine supports 17 languages (`crate::languages`). This slice
//! implements extraction properly for exactly ONE of them (Java -- the
//! epic's primary validation corpus is Keycloak/Elasticsearch) and
//! structures the registry so every other language plugs in later without
//! touching the fused pipeline (`crate::graph::fused`) at all: adding a
//! language is "write a `LanguageExtractor` impl, add one match arm here".
//!
//! Per Rule 13 (anti-silent-failure): a language with NO extractor yet
//! returns `ExtractorLookup::Unsupported` explicitly. It never falls back
//! to a `LanguageExtractor` that silently produces an empty `LocalIndex`,
//! which would be indistinguishable from "this file genuinely declares
//! nothing" -- a partial extractor that fabricates that appearance is
//! worse than an explicit, observable "not yet supported" signal.

pub mod java;
mod java_fields;
mod java_invocations;
mod java_methods;
mod java_receiver;
mod java_type_names;
pub mod kotlin;
pub mod local_index;

use crate::owned_node::OwnedNode;
use local_index::LocalIndex;

/// One language's extraction logic: a single, complete walk of the parsed
/// tree that populates a `LocalIndex`. Implementors must not retain `root`
/// (or any borrow derived from it) past this call -- see `local_index`
/// module docs for why `LocalIndex` holds only owned data.
pub trait LanguageExtractor: Send + Sync {
    fn extract(&self, root: &OwnedNode, file_id: u32) -> LocalIndex;
}

/// Result of looking up a language's extractor. A distinct type (rather
/// than `Option<Box<dyn LanguageExtractor>>`) so a caller cannot conflate
/// "no extractor registered for this language yet" with any other
/// `None`-shaped condition.
pub enum ExtractorLookup {
    Supported(Box<dyn LanguageExtractor>),
    Unsupported,
}

/// Looks up the `LanguageExtractor` for a file extension. Mirrors
/// `crate::languages::language_for_extension`'s per-extension dispatch
/// shape, but is a SEPARATE registry: not every extension the tree-sitter
/// grammar layer supports has an extractor implemented yet.
pub fn extractor_for_language(ext: &str) -> ExtractorLookup {
    match ext {
        "java" => ExtractorLookup::Supported(Box::new(java::JavaExtractor)),
        // Bug #1908: Kotlin at bind levels 0-2 -- see `kotlin` module docs
        // for scope (declarations/references/imports/inheritance) and what
        // is deliberately NOT covered (Java-specific levels 3-4).
        "kt" | "kts" => ExtractorLookup::Supported(Box::new(kotlin::KotlinExtractor)),
        _ => ExtractorLookup::Unsupported,
    }
}

/// Extensions with a real `LanguageExtractor` registered, paired with a
/// human-readable language name (Bug #1907, epic #1906 P0).
///
/// This is the single source of truth `xray-cli --print-graph-extractor-
/// extensions` exposes to Python's candidate-collection walk
/// (`xray_graph.py`). That walk applies `include_patterns`/
/// `exclude_patterns` BEFORE any file ever reaches Rust, so an excluded
/// file leaves no trace in any Rust-side counter -- narrowing the scope to
/// one extractable language previously made the response report
/// `fact_graph_complete: true` with every degradation counter at zero,
/// even though the graph was missing every call site in the excluded
/// language. Asking THIS function (rather than hand-maintaining a second,
/// Python-side extension list) is what keeps that honesty check correct
/// automatically the moment a new language's extractor lands here -- see
/// `graph_extractor_extensions_agrees_with_extractor_for_language_for_
/// every_known_extension` below for the anti-drift proof.
pub fn graph_extractor_extensions() -> &'static [(&'static str, &'static str)] {
    &[("java", "Java"), ("kt", "Kotlin"), ("kts", "Kotlin")]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn java_extension_is_supported() {
        assert!(matches!(
            extractor_for_language("java"),
            ExtractorLookup::Supported(_)
        ));
    }

    /// Bug #1908: both Kotlin extensions the tree-sitter grammar layer
    /// already recognizes (`crate::languages::language_for_extension`)
    /// must resolve to a real extractor, not merely be parseable.
    #[test]
    fn kotlin_extensions_are_supported() {
        assert!(matches!(
            extractor_for_language("kt"),
            ExtractorLookup::Supported(_)
        ));
        assert!(matches!(
            extractor_for_language("kts"),
            ExtractorLookup::Supported(_)
        ));
    }

    /// Python IS a supported engine language (`crate::languages`) but has
    /// no extractor implemented in THIS slice -- the lookup must say so
    /// explicitly, never silently hand back something that behaves like an
    /// extractor and produces an empty `LocalIndex`.
    #[test]
    fn an_engine_supported_language_without_an_extractor_yet_is_explicitly_unsupported() {
        assert!(matches!(
            extractor_for_language("py"),
            ExtractorLookup::Unsupported
        ));
    }

    #[test]
    fn a_totally_unknown_extension_is_also_explicitly_unsupported() {
        assert!(matches!(
            extractor_for_language("xyz"),
            ExtractorLookup::Unsupported
        ));
    }

    /// Bug #1907: the anti-drift proof. `graph_extractor_extensions()` is
    /// the sole source `xray-cli --print-graph-extractor-extensions`
    /// exposes to Python's candidate-collection walk, which uses it to
    /// decide whether an EXCLUDED file's language could have contributed a
    /// real call edge. If this list and `extractor_for_language` ever
    /// disagreed for ANY of the engine's known extensions, the file-
    /// exclusion honesty check built on top of it would silently reproduce
    /// exactly the "false fact_graph_complete: true" bug this issue exists
    /// to fix -- just for a different extension. Checked against every
    /// extension the tree-sitter grammar layer recognizes
    /// (`crate::languages::supported_extensions`), not merely the two this
    /// slice happens to implement, so a THIRD extractor landing later
    /// without a `graph_extractor_extensions` update fails this test
    /// immediately instead of silently drifting.
    #[test]
    fn graph_extractor_extensions_agrees_with_extractor_for_language_for_every_known_extension() {
        for ext in crate::languages::supported_extensions() {
            let has_extractor = matches!(extractor_for_language(ext), ExtractorLookup::Supported(_));
            let listed = graph_extractor_extensions().iter().any(|(listed_ext, _)| listed_ext == ext);
            assert_eq!(
                has_extractor, listed,
                "extension {ext:?}: extractor_for_language() has_extractor={has_extractor} \
                 but graph_extractor_extensions() listed={listed} -- these must never disagree"
            );
        }
    }

    /// Bug #1907: the list must never contain an extension `extractor_for_
    /// language` cannot actually back with a real extractor -- that would
    /// make Python's honesty check LIE in the opposite direction (treating
    /// an unsupported file's exclusion as if it mattered).
    #[test]
    fn every_listed_extension_is_really_supported() {
        for (ext, _lang) in graph_extractor_extensions() {
            assert!(
                matches!(extractor_for_language(ext), ExtractorLookup::Supported(_)),
                "graph_extractor_extensions lists {ext:?} but extractor_for_language disagrees"
            );
        }
    }
}
