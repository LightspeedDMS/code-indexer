//! Bug #1855 drift guard: the project's rustc toolchain pin is documented
//! in THREE places that must never silently drift apart --
//! `rust-toolchain.toml` at the repository root (Layer 2 of Bug #1855,
//! makes the host build use the pinned toolchain regardless of cwd),
//! `rust/rust-toolchain.toml` (the pre-existing pin `cache::
//! pinned_toolchain_channel` reads, which governs the evaluator `.so`
//! compile and the pinned `rustc --version` probe), and the
//! `dtolnay/rust-toolchain@<version>` step in `.github/workflows/main.yml`'s
//! `rust` CI job. The project's CLAUDE.md already documents the two-way
//! rust/ <-> CI sync constraint (broken once already: CI run 34135704952
//! failed clippy on a tree that had just passed locally, because CI's
//! toolchain had floated apart from the local pin). Adding the root file
//! turns that into a three-way constraint with the same failure mode if
//! nothing enforces it -- this module is that enforcement.

const ROOT_RUST_TOOLCHAIN_TOML: &str = include_str!("../../../rust-toolchain.toml");
const RUST_DIR_RUST_TOOLCHAIN_TOML: &str = include_str!("../../rust-toolchain.toml");
const CI_WORKFLOW_YAML: &str = include_str!("../../../.github/workflows/main.yml");

/// Narrow, single-purpose `channel = "..."` extractor for a
/// `rust-toolchain.toml`-shaped TOML fragment. Deliberately NOT a full TOML
/// parser (Rule 3, KISS) -- mirrors `cache::pinned_toolchain_channel`'s own
/// algorithm, but is kept as an independent parse here rather than calling
/// that function directly: it must extract the SAME line shape from the
/// (not-yet-existing) ROOT file too, and a test that re-derives the value
/// instead of trusting the one function under test is the more
/// discriminating design (see `verify_rustc_version_match` in dynlib.rs for
/// the same principle applied to Layer 1).
fn extract_toml_channel(toml_text: &str) -> Option<&str> {
    toml_text.lines().find_map(|line| {
        let (key, rest) = line.trim().split_once('=')?;
        if key.trim() != "channel" {
            return None;
        }
        rest.trim().strip_prefix('"')?.split('"').next()
    })
}

/// Narrow extractor for the CI workflow's
/// `uses: dtolnay/rust-toolchain@<version>` step line.
fn extract_ci_toolchain_ref(workflow_text: &str) -> Option<&str> {
    let refs: Vec<&str> = workflow_text
        .lines()
        .filter_map(|line| line.trim().strip_prefix("uses: dtolnay/rust-toolchain@"))
        .collect();
    let first = refs
        .first()
        .copied()
        .expect("CI workflow must contain at least one dtolnay/rust-toolchain ref");
    assert!(
        refs.iter().all(|candidate| *candidate == first),
        "all CI rust-toolchain refs must agree"
    );
    Some(first)
}

#[cfg(test)]
mod tests {
    use super::*;

    /// THE real-file drift guard: all three pinned values must agree. This
    /// is what actually runs in the Rust gate (`rust-automation.sh` /
    /// CI's `rust` job) on every real commit.
    #[test]
    fn three_way_toolchain_pin_stays_in_sync() {
        let root_channel = extract_toml_channel(ROOT_RUST_TOOLCHAIN_TOML)
            .expect("root rust-toolchain.toml must have a parsable `channel = \"...\"` line");
        let rust_dir_channel = extract_toml_channel(RUST_DIR_RUST_TOOLCHAIN_TOML)
            .expect("rust/rust-toolchain.toml must have a parsable `channel = \"...\"` line");
        let ci_ref = extract_ci_toolchain_ref(CI_WORKFLOW_YAML)
            .expect("rust job in .github/workflows/main.yml must pin dtolnay/rust-toolchain@<version>");

        assert_eq!(
            root_channel, rust_dir_channel,
            "root rust-toolchain.toml and rust/rust-toolchain.toml channels have drifted apart: '{}' vs '{}'",
            root_channel, rust_dir_channel
        );
        assert_eq!(
            root_channel, ci_ref,
            "root rust-toolchain.toml channel ('{}') and CI's dtolnay/rust-toolchain ref ('{}') have drifted apart",
            root_channel, ci_ref
        );
    }

    /// Bug #1855 (drift test, RED phase): proves `extract_toml_channel`
    /// actually DISCRIMINATES a divergent channel value from a matching
    /// one, using the identical parser the real-file test above relies on.
    /// Without this, `three_way_toolchain_pin_stays_in_sync` passing would
    /// prove nothing more than "today's three files happen to agree" --
    /// exactly the failure mode the mission and working agreement warn
    /// against (a RED that is not genuinely discriminating is a defect).
    #[test]
    fn extract_toml_channel_rejects_divergent_synthetic_values() {
        let pinned = "[toolchain]\nchannel = \"1.98.0\"\ncomponents = [\"clippy\"]\n";
        let same_channel_different_formatting = "[toolchain]\n  channel = \"1.98.0\"\n";
        let drifted = "[toolchain]\nchannel = \"1.91.0\"\n";

        assert_eq!(
            extract_toml_channel(pinned),
            extract_toml_channel(same_channel_different_formatting),
            "identical channel values (modulo whitespace) must compare equal"
        );
        assert_ne!(
            extract_toml_channel(pinned),
            extract_toml_channel(drifted),
            "a synthetically drifted channel value must be detected as a mismatch, \
             not silently accepted"
        );
    }

    /// Companion synthetic-mutation proof for the CI workflow side of the
    /// three-way comparison.
    #[test]
    fn extract_ci_toolchain_ref_rejects_divergent_synthetic_values() {
        let pinned_ref_line = "      uses: dtolnay/rust-toolchain@1.98.0\n";
        let drifted_ref_line = "      uses: dtolnay/rust-toolchain@1.91.0\n";

        assert_ne!(
            extract_ci_toolchain_ref(pinned_ref_line),
            extract_ci_toolchain_ref(drifted_ref_line),
            "a synthetically drifted CI toolchain ref must be detected as a mismatch, \
             not silently accepted"
        );
    }

    /// A second CI job must not be ignored merely because the first job has
    /// the expected toolchain. This is deliberately a multi-job-shaped input
    /// so a first-match implementation cannot pass by inspecting only the
    /// first reference.
    #[test]
    #[should_panic(expected = "all CI rust-toolchain refs must agree")]
    fn extract_ci_toolchain_ref_rejects_divergent_second_job_reference() {
        let workflow = "jobs:\n  rust:\n    steps:\n      uses: dtolnay/rust-toolchain@1.98.0\n  other:\n    steps:\n      uses: dtolnay/rust-toolchain@1.91.0\n";

        extract_ci_toolchain_ref(workflow);
    }
}
