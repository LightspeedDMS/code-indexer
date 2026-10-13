# syntax=docker/dockerfile:1
#
# cidx (code-indexer) multi-user server image for EKS (cluster / multi-pod mode).
#
# Builds the CUSTOM hnswlib (with check_integrity(), Story #54) from the VENDORED
# third_party/hnswlib source, then installs the code_indexer package, and runs the
# FastAPI server via uvicorn (single worker — HNSW index is a process-local in-RAM
# singleton; horizontal scaling is via Postgres cluster mode + leader election, not
# uvicorn workers).
#
# Runtime data dir (config.json, .jwt_secret, caches) = /data via CIDX_SERVER_DATA_DIR;
# the k8s Deployment mounts a volume there and seeds config.json (storage_mode=postgres,
# postgres_dsn, cluster.node_id) to enable cluster mode.

# ---- Build stage ----
# Pin bookworm on BOTH stages so the copied site-packages/glibc stay consistent.
# (It was additionally pinned for openjdk-17; that reason is gone -- the Java SCIP
# toolchain now lives in sandbox-mcp. See the runtime stage.)
FROM python:3.11-slim-bookworm AS builder

ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive

# Build toolchain for the hnswlib native extension (+ any sdist-only deps).
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential cmake git pkg-config python3-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /build

# Install cidx. v11.30 declares the custom hnswlib fork + psycopg as direct deps
# in pyproject (git+https hnswlib, psycopg[binary]/psycopg-pool), so a plain
# `pip install .` pulls everything (git + gcc/g++ present above for the git dep).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[cluster,cohere]"

# EVO: run cidx's LLM prompts as agent-orchestrator jobs instead of a local
# `claude` subprocess. The CLI is deliberately NOT in this image -- LLM
# execution belongs in the orchestrator's sandboxed worker, not in a
# long-lived service pod. Without this plugin every lifecycle_registration job
# dies with exit 127. The plugin registers itself on the upstream
# `code_indexer.llm_invoker` entry point, so installing it is enough to enable
# it (see plugins/cidx_orchestrator_invoker).
COPY plugins/cidx_orchestrator_invoker ./plugins/cidx_orchestrator_invoker
RUN pip install ./plugins/cidx_orchestrator_invoker

# EVO-64616 Phase 3: delegate cidx's internal git to the git-mcp spoke instead of
# running git in this image. Like the invoker above, the plugin registers itself
# on an entry point (`code_indexer.git_backend`), so installing it is what makes
# `git_backend=delegated` resolvable — without it the delegated wiring fails loud
# (by design) and the server can only run in-process `local` git. Default config
# is still `local`, so this install is inert until the config flips (see
# plugins/cidx_git_backend). stdlib-only, no extra deps.
COPY plugins/cidx_git_backend ./plugins/cidx_git_backend
RUN pip install ./plugins/cidx_git_backend

# Content seam: delegate cidx's repository reads (manifest + fetch) to
# workspace-mcp, so a pod can index with no repo mount. The plugin lives in
# evo-sandbox and is published to its PyPI package registry; this installs a
# PINNED version. Like the git backend it registers on an entry point
# (`code_indexer.content_backend`), so installing it is what makes
# `content_backend=delegated` resolvable; default config is `local`, so it is
# inert until the config flips. Endpoint and token at runtime come from
# CIDX_CONTENT_MCP_URL / CIDX_CONTENT_MCP_TOKEN. From 0.7.0 the same package
# also registers `code_indexer.xray_backend` (X-Ray evaluators run in
# sandbox-mcp under `xray_backend=delegated`; CIDX_XRAY_SANDBOX_MCP_URL /
# CIDX_XRAY_SANDBOX_MCP_TOKEN).
#
# The registry is private: the index URL (with credentials) arrives as a
# BuildKit SECRET, never a build arg, so it is not written into any layer or the
# image history. CI's build:image always supplies it, and its in-image step
# asserts the entry point resolves -- so a CI image can never ship without the
# plugin. A local `docker build` without the secret skips the plugin, loudly.
ARG CIDX_CONTENT_BACKEND_VERSION=0.7.0
RUN --mount=type=secret,id=plugin_index,required=false \
    if [ -s /run/secrets/plugin_index ]; then \
      pip install --no-cache-dir --no-deps \
        --index-url "$(cat /run/secrets/plugin_index)" \
        "cidx-content-backend==${CIDX_CONTENT_BACKEND_VERSION}"; \
    else \
      echo "WARNING: no plugin_index secret -- cidx-content-backend NOT installed;" \
           "content_backend=delegated will fail loud in this image" >&2; \
    fi

# ---- xray-cli build stage ----
# WHY THIS STAGE EXISTS: xray_search / xray_explore / xray_search_batch /
# xray_analyze_graph have NO Python fallback -- they shell out to the native
# `xray-cli` binary unconditionally. Nothing ever built or shipped that binary
# into this image, and `pip install` cannot: it is a Rust artifact, and
# pyproject's `packages = ["src/code_indexer"]` would not carry it even if it
# existed. The Python-side default location (`<project root>/rust/target/
# release/xray-cli`, found by walking up from the installed package looking for
# a `rust/` directory) cannot resolve under /usr/local/lib/python3.11/
# site-packages either, because `rust/` is not there and never was. Result
# before this stage: every xray call in the cluster failed to find a binary.
#
# `rust:slim-bookworm` deliberately, NOT a pinned `rust:<version>-slim` tag:
# the authoritative pin is rust/rust-toolchain.toml (channel = "1.98.0"), and
# rustup -- which the official rust images ship -- auto-detects that file for
# any cargo invocation at or below its directory and auto-installs the pinned
# toolchain. Naming a version in this Dockerfile too would be a second copy of
# the pin that can silently drift from the first; that file's own header
# already documents what drift costs (CI run 34135704952). The base tag only
# supplies rustup + a cc/linker; which rustc actually compiles the binary is
# decided by rust-toolchain.toml. `bookworm` matches both other stages so the
# glibc the binary links against is the glibc it runs on.
FROM rust:slim-bookworm AS xray-builder

ENV DEBIAN_FRONTEND=noninteractive \
    CARGO_TERM_COLOR=never

# The tree-sitter crate (and the ~20 tree-sitter-<lang> grammar crates
# xray-core depends on) compile C sources via a build script, so a C toolchain
# is a hard build requirement, not a convenience. The rust base image already
# ships gcc; installing build-essential makes the dependency explicit and
# survives a base-image change that drops it.
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential \
    && rm -rf /var/lib/apt/lists/*

# The rust/ tree was previously not COPYed into ANY stage -- the build context
# carried it, nothing consumed it. Copied whole (not member-by-member) because
# the workspace manifest + lockfile + both member crates must agree, and
# xray-core `include_str!`s rust-toolchain.toml to embed the pinned channel
# into the binary (it passes RUSTUP_TOOLCHAIN=<channel> to the rustc it invokes
# at query time) -- a partial copy would silently change the artifact.
COPY rust/ /xray/rust/

# cargo MUST run from inside the workspace: that is how it finds Cargo.toml,
# and equally how rustup finds rust-toolchain.toml (its override mechanism
# searches the cwd and its ancestors). Running from /xray would build with the
# base image's default toolchain instead of the pinned one -- or, as it did on
# the first attempt here, not build at all.
WORKDIR /xray/rust

# -p xray-cli, not a bare `cargo build --release`: the workspace also contains
# xray-core's dev/bench targets, and only this one binary is shipped.
#
# --locked: an image build must compile the dependency versions the lockfile
# names, not whatever cargo would resolve today. Without it a transitive
# semver-compatible release silently changes the shipped binary between two
# builds of the same commit, and cargo rewrites Cargo.lock inside the build
# context where nobody ever sees the diff. Verified in-tree: `cargo metadata
# --locked` succeeds against rust/Cargo.lock, so the lockfile is in sync with
# both member manifests and the flag is a no-op today -- which is exactly when
# it is cheap to add.
#
# The second command is a build-time smoke test: it fails the image here,
# where the error is readable, rather than in a pod at query time, if the
# binary did not link or cannot execute on this glibc. It must be a REAL flag
# that exits non-zero when broken -- xray-cli treats an unrecognised argument
# as a scan target and still exits 0, so `--help` would pass unconditionally.
# `--print-graph-extractor-extensions` does no compilation and no repo walk,
# so it is cheap as well as genuinely discriminating. NOTE this proves the
# binary runs on the BUILDER's glibc; the runtime stage repeats it on the
# runtime glibc, which is the one that matters.
RUN cargo build --locked --release -p xray-cli \
    && ./target/release/xray-cli --print-graph-extractor-extensions > /dev/null

# Capture the toolchain that just built xray-cli, to ship to the runtime stage.
#
# This is not "a rustc 1.98.0", it is THAT rustc. xray-core/build.rs bakes the
# build compiler's full `rustc --version` string into the binary as
# CIDX_HOST_RUSTC_VERSION, and dynlib.rs::verify_rustc_version_match compares it
# BYTE-FOR-BYTE against the version embedded in each evaluator .so, refusing to
# dlopen on any difference ("Recompile the evaluator with the host toolchain").
# A separately-installed 1.98.0 that differs only by build hash would make 100%
# of evaluator loads fail -- after compiling successfully, so the failure would
# surface at load time, not build time. Copying the sysroot out of this stage
# makes the runtime compiler the same compiler by construction; the runtime
# stage then re-asserts it against the SHIPPED BINARY rather than trusting this
# comment (see the assertion in the runtime stage).
#
# No sidecar `.rustc-version` file is produced. One used to be, and the runtime
# stage compared `rustc --version` against it -- but both sides of that
# comparison were `rustc --version` run in THIS stage, so the check could only
# fail if `cp -a` were broken. The honest reference value lives in the binary.
#
# Pruned to what compiling an evaluator at query time actually needs. Measured:
# the full rustup install costs +1.02GiB, this costs ~534MB (509MiB) of
# toolchain in the runtime image. The remainder is irreducible -- libLLVM
# (191MB) is rustc's codegen backend, librustc_driver (144MB) is rustc, and the
# std rlibs (173MB) are what a cdylib links against. Dropped: cargo (never used
# at query time -- xray-cli invokes rustc directly), rustdoc, clippy, the docs,
# and the std SOURCE tree.
RUN set -eux; \
    rustc --version; \
    cp -a "$(rustc --print sysroot)" /xray-toolchain; \
    rm -rf /xray-toolchain/share /xray-toolchain/lib/rustlib/src /xray-toolchain/etc; \
    find /xray-toolchain/bin -type f ! -name rustc -delete

# ---- Runtime stage ----
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CIDX_SERVER_DATA_DIR=/data \
    DEBIAN_FRONTEND=noninteractive

# Runtime deps, and why each one is here:
#  - git      : cidx still runs git IN THIS IMAGE on paths the git-mcp seam does
#               not cover -- git-aware indexing (services/git_topology_service.py,
#               services/file_identifier.py), temporal indexing
#               (services/temporal/), and parts of global_repos/refresh_scheduler.py
#               (see the "EVO-64678: left raw" note there: the seam's symbolic_ref()
#               cannot express refs/remotes/origin/HEAD). Those modules shell out to
#               `git` directly and hold ZERO git_backend references, so removing the
#               binary breaks indexing even with git_backend='delegated'. Removing it
#               is a seam-coverage task, not a Dockerfile one.
#  - libgomp1 : hnswlib OpenMP runtime.
#  - tini     : PID 1 signal handling / zombie reaping.
#  - ripgrep  : REQUIRED for /api/regex/search. RegexSearchService prefers rg
#               (rg --json over a gitignored tree is sub-second) and silently falls
#               back to a linear `grep -r` scan when `which("rg")` is falsy. On a 14G
#               working tree that fallback always hits the 30s search timeout.
#  - curl + ca-certificates : TLS trust store for outbound HTTPS (embedding provider);
#               curl itself is kept as the in-pod debug client.
#
# NO JAVA TOOLCHAIN HERE. The headless JDK 17, the Temurin JDK 21 toolchain at
# /opt/jdk-21, Coursier (`cs`) and Gradle 8.5 were all carried solely for local
# scip-java. SCIP generation is delegated to sandbox-mcp (scip_backend='delegated'),
# whose scip image ships JDK 17 + Coursier + scip-java 0.11.1 -- the same version
# scip/indexers/java.py pins. startup/scip_backend_wiring.py is fail-loud: an
# unreachable sandbox-mcp raises rather than falling back to a local build, so this
# image has no reachable path to a JVM. Re-adding one means re-adding a fallback.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgomp1 tini ripgrep curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# ---- xray-cli + its query-time Rust toolchain ----
# ORDERING: this whole block sits ABOVE the site-packages COPY below on
# purpose. Every instruction in it is keyed on `rust/` alone, and together they
# are by far the most expensive part of the image (measured below). Underneath
# the Python COPY -- where they used to be -- editing any one .py file
# invalidated and re-ran all of it: the gcc/libc6-dev apt install, the 534MB
# toolchain COPY and the assertion, on every single application change.
#
# Moving them up is safe because `COPY --from=<stage> <dir> <dir>` MERGES into
# the destination; it does NOT replace it. Verified by build (2026-10-01): a
# file placed in a destination directory survives a later whole-directory COPY
# over that same path, and only same-named entries are overwritten. The comment
# that used to live here claimed the opposite -- that `COPY --from=builder
# /usr/local/bin /usr/local/bin` "replaces the whole directory and would
# otherwise delete it" -- and that claim was false. It was also only a comment:
# nothing failed the build if it were violated. The one-command gate after the
# Python COPYs below now enforces the real invariant instead.
#
# WHY A COMPILER IS IN THE RUNTIME IMAGE: xray-cli compiles each evaluator at
# QUERY time (rust/xray-core/src/compiler/rustc_driver.rs spawns `rustc`), so
# shipping only the binary gives a loud "Is rustc installed and on PATH?" on
# every xray_search -- better than the silent empty scan it replaced, but still
# not a working xray.
#
# Prebuilding the evaluators instead is NOT possible: an xray query IS Rust
# source (assemble.rs::detect_evaluator_mode -- both Legacy and Graph modes
# compile to a .so, there is no interpreted path), compiled per query and cached
# under a hash of that source, so there is no fixed set to build ahead of time.
# The compile cache cannot paper over it either: cache::probe_rustc_version()
# degrades to the literal string "unknown" without rustc, so a rustc-less pod
# computes a different identity and misses every entry a seeder wrote.
#
# COST, re-measured against a build of this same Dockerfile with this block
# deleted (decimal bytes; MiB in brackets). Two different numbers are both
# correct, which is where the previously-quoted figures came from:
#
#   FILES added to the image -- `docker export <container> | wc -c`, and
#   `docker history` layer diffs, which agree to within tar padding:
#     gcc + libc6-dev ........ 188,182,528  (179.5MiB)
#     /opt/rust toolchain .... 533,831,680  (509.1MiB)
#     ---------------------------------------------------
#     flattened delta ........ 717,400,576  (684.2MiB)
#     (layer-diff sum .......  721,985,536  (688.5MiB))
#     xray-cli binary ........  33,759,232  (32.2MiB)  <- the feature, not the compiler
#
#   What `docker images` / `docker image inspect .Size` REPORTS:
#     delta .................. 971,756,548  (926.7MiB)
#
# The 254MB gap is not missing files. Under the containerd image store that
# size also counts the retained COMPRESSED layer blobs alongside the unpacked
# snapshot, and /opt/rust alone gzips to 196,437,314 bytes (measured) -- which,
# with the gcc/libc6-dev layer, is the whole difference. So the "+972MB" in
# commit 00acbe4d3 is the `docker images` number and is reproducible; the
# "+987MB" this comment used to claim is not reproducible by either method and
# is simply wrong. Quote 717MB of files, ~972MB of node disk if the content
# store is not pruned.
#
# On nodes whose root volumes are 50GiB (tools-* Karpenter general-purpose) the
# image moves from ~5% to ~7% of node disk. The alternative, moving compilation
# behind a seam into its own service, buys back that space at the price of a
# version-locked second deployable (see the byte-equality check below), a
# network hop in the cold path, a fleet-wide outage mode, and a wider trust
# boundary: evaluator source is user-supplied Rust, and the validator
# (validator.rs) is a syn AST whitelist, not a sandbox. That trade was judged
# not worth it at this fleet size; the reasoning is in
# scratchpad/XRAY_SIDECAR_SCOPE.md.
#
# gcc + libc6-dev, not build-essential: rustc shells out to a C linker (`cc`)
# and needs the crt objects and libc.so that libc6-dev provides. The toolchain's
# own bundled rust-lld is NOT a substitute -- it cannot resolve -lgcc_s/-lc/-lm
# without gcc's spec-file search paths, and wiring those up would need a rustc
# wrapper script, since evaluator_rustc_command passes no linker flags.
RUN apt-get update && apt-get install -y --no-install-recommends \
        gcc libc6-dev \
    && rm -rf /var/lib/apt/lists/*

# The xray-cli binary ONLY -- not cargo, not rustc, not ~/.cargo, not the
# target/ directory (hundreds of MB of intermediate artifacts for a single
# 32MB executable; the image grows by 33.8MB, measured).
COPY --from=xray-builder /xray/rust/target/release/xray-cli /usr/local/bin/xray-cli

# Tell the Python side where the binary is. Without this the backend falls back
# to looking for `<some ancestor of the installed package>/rust/target/release/
# xray-cli`, which does not exist in a site-packages install -- see
# code_indexer/xray/rust_backend.py::resolve_xray_cli_path for the full
# resolution order. This is the deployment half of that override: the path is a
# property of how THIS IMAGE was built, so it belongs next to the COPY that
# decides it, not in a runtime config file.
ENV CIDX_XRAY_CLI_PATH=/usr/local/bin/xray-cli

COPY --from=xray-builder /xray-toolchain /opt/rust

# /opt/rust/bin/rustc is the real compiler, not a rustup shim, so it ignores
# RUSTUP_TOOLCHAIN (which xray-cli sets from the embedded channel) and resolves
# its sysroot relative to its own path. That makes the compiler used at query
# time deterministic: it is the one copied above, whatever the environment says.
ENV PATH=/opt/rust/bin:$PATH

# Fail the BUILD on the mismatch that would otherwise fail every dlopen at
# query time, long after deploy.
#
# WHAT THIS PROVES, precisely. dynlib.rs:28 holds
# `const HOST_RUSTC_VERSION: &str = env!("CIDX_HOST_RUSTC_VERSION")`, and
# xray-core/build.rs sets that env var to the full `rustc --version` string of
# whatever compiler cargo used. `env!` is expanded at compile time, so that
# exact string is a literal in xray-cli's rodata -- and it is the string
# verify_rustc_version_match compares BYTE-FOR-BYTE against each evaluator .so
# at dlopen. So grepping the shipped binary for the runtime `rustc --version`
# output compares the runtime compiler against the value the running process
# will actually use.
#
# WHAT IT DOES NOT PROVE, and why the previous version of this check proved
# nothing at all. The check used to compare `rustc --version` against
# /opt/rust/.rustc-version -- a file written by `rustc --version` in the SAME
# xray-builder stage the sysroot was copied from. Both sides had one source, so
# it was a tautology: it could only fail if `cp -a` were broken. The residual
# gap in the current form is narrower but real: `grep -qaF` proves the string is
# PRESENT in the binary, not that it is the value bound to
# CIDX_HOST_RUSTC_VERSION. Verified by build (2026-10-01) that the live string
# occurs exactly once in the binary and that a fabricated version string does
# not match, so the check does discriminate; closing the gap entirely would need
# xray-cli to print CIDX_HOST_RUSTC_VERSION on demand, which is a change to
# rust/ and is deliberately not made here.
#
# The second half links a real cdylib, because a toolchain that is present and
# version-correct can still be unable to produce the one artifact xray needs --
# that is a missing linker or crt object, and this is the cheapest place to find
# out.
RUN set -eux; \
    test -x "$CIDX_XRAY_CLI_PATH"; \
    got="$(rustc --version)"; \
    if ! grep -qaF -- "$got" "$CIDX_XRAY_CLI_PATH"; then \
        echo "FATAL: runtime rustc is not the compiler that built xray-cli."; \
        echo "  runtime rustc: $got"; \
        echo "  that exact string is NOT embedded in $CIDX_XRAY_CLI_PATH,"; \
        echo "  so dynlib.rs's HOST_RUSTC_VERSION names a different compiler."; \
        echo "dynlib.rs compares these byte-for-byte; every evaluator would"; \
        echo "compile and then fail to load."; \
        exit 1; \
    fi; \
    printf '#[no_mangle] pub extern "C" fn evaluate_node() -> i32 { 42 }\n' > /tmp/abi_probe.rs; \
    rustc --crate-type cdylib -O /tmp/abi_probe.rs -o /tmp/abi_probe.so; \
    test -s /tmp/abi_probe.so; \
    rm -f /tmp/abi_probe.rs /tmp/abi_probe.so; \
    echo "query-time toolchain OK: $got"

# Bring over the installed site-packages + the `cidx` console script from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# The gate on everything above. Two distinct things are asserted, both of which
# were previously only asserted in prose:
#  1. xray-cli is still at CIDX_XRAY_CLI_PATH and executable after the
#     whole-directory COPY into /usr/local/bin above -- i.e. that COPY really
#     does merge rather than replace.
#  2. the SHIPPED binary actually RUNS, on the RUNTIME stage's glibc. The only
#     execution smoke test before this one ran in the xray-builder stage, on
#     the BUILDER's glibc, against a binary in a different filesystem. That
#     cannot catch an image that ships an xray-cli which dies with "Exec format
#     error" in the pod -- a green build shipping exactly that has been
#     demonstrated.
# `--print-graph-extractor-extensions` must be a REAL flag: xray-cli treats an
# unrecognised argument as a scan target and still exits 0, so `--help` would
# pass unconditionally. It does no compilation and no repo walk, so this layer
# costs well under a second.
RUN set -eux; \
    test -x "$CIDX_XRAY_CLI_PATH"; \
    "$CIDX_XRAY_CLI_PATH" --print-graph-extractor-extensions > /dev/null

# Non-root runtime. /data is the server data dir — the Deployment mounts a volume here.
RUN useradd -m -u 1000 cidx && mkdir -p /data && chown -R cidx:cidx /data
USER cidx
WORKDIR /home/cidx

EXPOSE 8090

# uvicorn binds 0.0.0.0 (the CLI default is 127.0.0.1, which is unreachable in a pod).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "code_indexer.server.main", "--host", "0.0.0.0", "--port", "8090"]
