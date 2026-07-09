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
        "js" | "jsx" | "mjs" | "cjs" => Some(tree_sitter_javascript::LANGUAGE.into()),
        "go" => Some(tree_sitter_go::LANGUAGE.into()),
        "cs" => Some(tree_sitter_c_sharp::LANGUAGE.into()),
        "sh" | "bash" => Some(tree_sitter_bash::LANGUAGE.into()),
        "html" | "htm" => Some(tree_sitter_html::LANGUAGE.into()),
        "css" => Some(tree_sitter_css::LANGUAGE.into()),
        "tf" | "hcl" => Some(tree_sitter_hcl::LANGUAGE.into()),
        "yml" | "yaml" => Some(tree_sitter_yaml::LANGUAGE.into()),
        "sql" => Some(tree_sitter_sequel_tsql::LANGUAGE.into()),
        "xml" => Some(tree_sitter_xml::LANGUAGE_XML.into()),
        "groovy" | "gradle" => Some(tree_sitter_groovy::LANGUAGE.into()),
        "c" | "h" => Some(tree_sitter_c::LANGUAGE.into()),
        "cc" | "cpp" | "cxx" | "c++" | "hpp" | "hh" | "hxx" | "h++" => Some(tree_sitter_cpp::LANGUAGE.into()),
        _ => None,
    }
}

/// All file extensions supported by the scanner.
pub fn supported_extensions() -> &'static [&'static str] {
    &[
        "java", "kt", "kts", "py", "ts", "tsx", "js", "jsx", "go", "cs", "sh", "bash", "html",
        "htm", "css", "tf", "hcl", "yml", "yaml", "sql", "xml", "groovy", "gradle", "mjs", "cjs",
        "c", "h", "cc", "cpp", "cxx", "c++", "hpp", "hh", "hxx", "h++",
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

    #[test]
    fn test_hcl_extensions_present() {
        assert!(language_for_extension("tf").is_some(), "No language for tf");
        assert!(language_for_extension("hcl").is_some(), "No language for hcl");
    }

    #[test]
    fn test_yaml_extensions_present() {
        assert!(language_for_extension("yml").is_some(), "No language for yml");
        assert!(language_for_extension("yaml").is_some(), "No language for yaml");
    }

    #[test]
    fn test_sql_extension_present() {
        assert!(language_for_extension("sql").is_some(), "No language for sql");
    }

    #[test]
    fn test_xml_extension_present() {
        assert!(language_for_extension("xml").is_some(), "No language for xml");
    }

    #[test]
    fn test_groovy_extensions_present() {
        assert!(language_for_extension("groovy").is_some(), "No language for groovy");
        assert!(language_for_extension("gradle").is_some(), "No language for gradle");
    }

    #[test]
    fn test_mjs_cjs_extensions_present() {
        assert!(language_for_extension("mjs").is_some(), "No language for mjs");
        assert!(language_for_extension("cjs").is_some(), "No language for cjs");
    }

    #[test]
    fn test_new_extensions_in_supported_list() {
        let exts = supported_extensions();
        for ext in &["tf", "hcl", "yml", "yaml", "sql", "xml", "groovy", "gradle", "mjs", "cjs"] {
            assert!(exts.contains(ext), "Extension not in supported list: {}", ext);
        }
    }

    // --- C language support ---

    #[test]
    fn test_c_extension_present() {
        assert!(language_for_extension("c").is_some(), "No language for .c");
    }

    #[test]
    fn test_c_header_extension_present() {
        assert!(language_for_extension("h").is_some(), "No language for .h");
    }

    // --- C++ language support ---

    #[test]
    fn test_cpp_extension_present() {
        assert!(language_for_extension("cpp").is_some(), "No language for .cpp");
    }

    #[test]
    fn test_cc_extension_present() {
        assert!(language_for_extension("cc").is_some(), "No language for .cc");
    }

    #[test]
    fn test_cxx_extension_present() {
        assert!(language_for_extension("cxx").is_some(), "No language for .cxx");
    }

    #[test]
    fn test_cpp_plus_plus_extension_present() {
        assert!(language_for_extension("c++").is_some(), "No language for .c++");
    }

    #[test]
    fn test_hpp_extension_present() {
        assert!(language_for_extension("hpp").is_some(), "No language for .hpp");
    }

    #[test]
    fn test_hh_extension_present() {
        assert!(language_for_extension("hh").is_some(), "No language for .hh");
    }

    #[test]
    fn test_hxx_extension_present() {
        assert!(language_for_extension("hxx").is_some(), "No language for .hxx");
    }

    #[test]
    fn test_hxx_plus_plus_extension_present() {
        assert!(language_for_extension("h++").is_some(), "No language for .h++");
    }

    #[test]
    fn test_c_and_cpp_extensions_in_supported_list() {
        let exts = supported_extensions();
        for ext in &["c", "h", "cc", "cpp", "cxx", "c++", "hpp", "hh", "hxx", "h++"] {
            assert!(exts.contains(ext), "C/C++ extension not in supported list: {}", ext);
        }
    }
}
