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
        _ => ExtractorLookup::Unsupported,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn java_extension_is_supported() {
        assert!(matches!(extractor_for_language("java"), ExtractorLookup::Supported(_)));
    }

    /// Python IS a supported engine language (`crate::languages`) but has
    /// no extractor implemented in THIS slice -- the lookup must say so
    /// explicitly, never silently hand back something that behaves like an
    /// extractor and produces an empty `LocalIndex`.
    #[test]
    fn an_engine_supported_language_without_an_extractor_yet_is_explicitly_unsupported() {
        assert!(matches!(extractor_for_language("py"), ExtractorLookup::Unsupported));
    }

    #[test]
    fn a_totally_unknown_extension_is_also_explicitly_unsupported() {
        assert!(matches!(extractor_for_language("xyz"), ExtractorLookup::Unsupported));
    }
}
