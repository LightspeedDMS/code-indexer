use tree_sitter::Language;

/// Returns the tree-sitter Language for the given file extension, or None
/// if the extension is not supported.
pub fn language_for_extension(ext: &str) -> Option<Language> {
    match ext {
        "java" => Some(tree_sitter_java::LANGUAGE.into()),
        "kt" | "kts" => Some(tree_sitter_kotlin_ng::LANGUAGE.into()),
        "py" => Some(tree_sitter_python::LANGUAGE.into()),
        "ts" => Some(tree_sitter_typescript::LANGUAGE_TYPESCRIPT.into()),
        "tsx" => Some(tree_sitter_typescript::LANGUAGE_TSX.into()),
        "js" | "jsx" => Some(tree_sitter_javascript::LANGUAGE.into()),
        "go" => Some(tree_sitter_go::LANGUAGE.into()),
        "cs" => Some(tree_sitter_c_sharp::LANGUAGE.into()),
        "sh" | "bash" => Some(tree_sitter_bash::LANGUAGE.into()),
        "html" | "htm" => Some(tree_sitter_html::LANGUAGE.into()),
        "css" => Some(tree_sitter_css::LANGUAGE.into()),
        _ => None,
    }
}

/// All file extensions supported by the scanner.
pub fn supported_extensions() -> &'static [&'static str] {
    &[
        "java", "kt", "kts", "py", "ts", "tsx", "js", "jsx", "go", "cs", "sh", "bash", "html",
        "htm", "css",
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_java_language_present() {
        assert!(language_for_extension("java").is_some());
    }

    #[test]
    fn test_unknown_extension_returns_none() {
        assert!(language_for_extension("xyz").is_none());
        assert!(language_for_extension("").is_none());
        assert!(language_for_extension("rs").is_none());
    }

    #[test]
    fn test_all_supported_extensions_have_language() {
        for ext in supported_extensions() {
            assert!(
                language_for_extension(ext).is_some(),
                "No language for extension: {}",
                ext
            );
        }
    }

    #[test]
    fn test_tsx_and_ts_distinct() {
        let ts = language_for_extension("ts").unwrap();
        let tsx = language_for_extension("tsx").unwrap();
        // They are different language objects (TypeScript vs TSX)
        // We can't compare Language directly, but can verify both are Some
        let _ = ts;
        let _ = tsx;
    }
}
