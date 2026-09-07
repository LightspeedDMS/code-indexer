#!/bin/bash

# Rust automation script - CIDX X-Ray Rust workspace (rust/xray-core, rust/xray-cli)
#
# Runs the full Rust test suite (cargo test --workspace) and the clippy lint
# gate (cargo clippy --all-targets -- -D warnings) against the Rust workspace
# rooted at rust/Cargo.toml.
#
# Before this script existed, NO gate anywhere (local or CI) ran `cargo test`
# or `cargo clippy` -- the entire 447-test Rust suite, and in particular the
# AC18 structural-parity check (rust/xray-core/src/preamble_ac18_parity.rs,
# gated `#[cfg(test)] mod preamble_ac18_parity;` in lib.rs), was executed only
# by a human running cargo manually. AC18 is the only thing preventing a
# repeat of Bug #1795 -- a PREAMBLE mirror of OwnedNode/EvalFinding silently
# diverging from the real types, which per ADR-002 is memory-unsafe across
# the dylib boundary, not merely incorrect. This is the same defect class as
# Bug #1798 (@pytest.mark.slow tests routed into a lane no gate executed).
#
# Discoverability: this script is listed in CLAUDE.md's testing table
# ("Rust Gate" row) alongside fast-automation.sh/server-fast-automation.sh/
# e2e-automation.sh, and CLAUDE.md is loaded into every Claude Code session
# for this project. It is ALSO run in CI as its own job (see the `rust` job
# in .github/workflows/main.yml), which gates tag/release creation the same
# way the `lint` and `test` jobs do -- so a Rust regression cannot reach
# staging/production even if a developer forgets to run this script locally.
#
# Required whenever rust/ is touched -- same trigger shape as
# server-fast-automation.sh being required when src/code_indexer/server/ is
# touched.
#
# TOOLCHAIN PIN: rust/rust-toolchain.toml pins the exact toolchain (currently
# 1.98.0) this script's cargo/clippy invocations use -- rustup auto-detects
# that file and auto-installs the pinned version+components on first run
# whenever cwd is inside rust/. This exists because a bare floating toolchain
# silently drifts ahead of CI's own pin (.github/workflows/main.yml's `rust`
# job uses `dtolnay/rust-toolchain@1.98.0`, not a floating tag), which is the
# identical drift problem CLAUDE.md's "Lint and CI" section documents for
# ruff/mypy. Concretely: CI run 34135704952 failed `cargo clippy -D warnings`
# on clippy::filter_next while this script had just reported a clean pass
# locally, because local clippy was 0.1.91 (rustc 1.91.0) against CI's
# 1.98.0. A green run of THIS script now means the SAME clippy version CI
# runs actually passed -- keep rust/rust-toolchain.toml's `channel` and this
# workflow's `dtolnay/rust-toolchain@<version>` ref in sync going forward.

set -e  # Exit on any error -- no `|| true` anywhere in this script. A cargo
        # failure MUST fail this gate.

echo "Starting Rust automation pipeline (rust/ workspace)..."
echo "==========================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

print_step() {
    echo -e "\n${BLUE}==> $1${NC}"
}

print_success() {
    echo -e "${GREEN}[OK] $1${NC}"
}

print_error() {
    echo -e "${RED}[FAIL] $1${NC}"
}

# Check if we're in the right directory (project root, one level above rust/)
if [[ ! -f "pyproject.toml" ]]; then
    print_error "Not in project root directory (pyproject.toml not found)"
    exit 1
fi

if [[ ! -f "rust/Cargo.toml" ]]; then
    print_error "rust/Cargo.toml not found -- Rust workspace missing"
    exit 1
fi

if ! command -v cargo &> /dev/null; then
    print_error "cargo not found on PATH -- install Rust (https://rustup.rs) to run this gate"
    exit 1
fi

print_step "Checking cargo/rustc/clippy toolchain (pinned via rust/rust-toolchain.toml)"
(cd rust && cargo --version && rustc --version && cargo clippy --version)
print_success "Toolchain checked"

print_step "Running cargo test --workspace (rust/xray-core + rust/xray-cli, includes AC18 PREAMBLE parity check)"
(cd rust && cargo test --workspace)
print_success "Rust test suite passed"

print_step "Running cargo clippy --workspace --all-targets -- -D warnings"
(cd rust && cargo clippy --workspace --all-targets -- -D warnings)
print_success "Rust clippy check passed (zero warnings)"

echo -e "\n${GREEN}Rust automation completed successfully!${NC}"
echo "==========================================="
echo "[OK] cargo test --workspace passed (rust/xray-core + rust/xray-cli)"
echo "[OK] cargo clippy --all-targets -- -D warnings passed (zero warnings)"
echo ""
echo "Run this script whenever rust/ (the X-Ray Rust engine) is touched -- see"
echo "the 'Rust Gate' row in CLAUDE.md's testing table. It is also wired into"
echo "CI as the 'rust' job in .github/workflows/main.yml."
