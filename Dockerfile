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
# Pin bookworm: it ships openjdk-17 (trixie, the current `slim` default, does
# not) which the runtime stage needs for scip-java + Gradle 7.x compatibility.
# Both stages must match so the copied site-packages/glibc are consistent.
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
# The second command is a build-time smoke test: it fails the image here,
# where the error is readable, rather than in a pod at query time, if the
# binary did not link or cannot execute on this glibc. It must be a REAL flag
# that exits non-zero when broken -- xray-cli treats an unrecognised argument
# as a scan target and still exits 0, so `--help` would pass unconditionally.
# `--print-graph-extractor-extensions` does no compilation and no repo walk,
# so it is cheap as well as genuinely discriminating.
RUN cargo build --release -p xray-cli \
    && ./target/release/xray-cli --print-graph-extractor-extensions > /dev/null

# ---- Runtime stage ----
FROM python:3.11-slim-bookworm

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CIDX_SERVER_DATA_DIR=/data \
    DEBIAN_FRONTEND=noninteractive

# Runtime deps: git (GitPython + golden-repo clone/refresh), libgomp1 (hnswlib OpenMP),
# tini (PID 1 signal handling / zombie reaping).
#
# C11: a headless JDK + Coursier (`cs`) so Java/Kotlin SCIP works. scip-java is
# launched on demand via Coursier and runs the target project's gradle/maven
# build, both of which need a JDK. curl fetches the Coursier launcher.
#
# Java SCIP needs TWO JDKs:
#  - JDK 17 (openjdk-17, apt) is the Gradle LAUNCHER. The JDK that runs a
#    project's Gradle must be old enough for that Gradle version: Gradle 7.x
#    (still common) only supports Java <=17 (Java 21 -> "Unsupported class file
#    major version 65"), and Java 17 also runs Gradle 8.x and scip-java itself.
#  - JDK 21 (Temurin, tarball at /opt/jdk-21) is available as a Gradle TOOLCHAIN
#    for projects that compile at languageVersion=21 (e.g. the Evolution
#    monolith). Gradle launched with 17 picks 21 up via installations.paths
#    (set in the cidx user's gradle.properties below).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgomp1 tini openjdk-17-jdk-headless curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://github.com/coursier/launchers/raw/master/cs-x86_64-pc-linux.gz \
         | gzip -d > /usr/local/bin/cs \
    && chmod +x /usr/local/bin/cs \
    && mkdir -p /opt/jdk-21 \
    && curl -fsSL "https://api.adoptium.net/v3/binary/latest/21/ga/linux/x64/jdk/hotspot/normal/eclipse" \
         | tar -xz -C /opt/jdk-21 --strip-components=1

# Bake Gradle so scip-java can build + index Gradle projects. The image had
# scip-java + JDK but NO build tool (gradle/maven), so scip-java's build step
# failed and Java SCIP generation produced empty .scip.db -- the observed
# "SCIP fast-but-empty" pathology. Gradle 8.5 runs on the JDK 17 launcher and
# supports the JDK 21 toolchain (installations.paths, set below).
RUN apt-get update && apt-get install -y --no-install-recommends unzip \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://services.gradle.org/distributions/gradle-8.5-bin.zip -o /tmp/gradle.zip \
    && unzip -q /tmp/gradle.zip -d /opt \
    && ln -sf /opt/gradle-8.5/bin/gradle /usr/local/bin/gradle \
    && rm /tmp/gradle.zip

# ripgrep (rg) is REQUIRED for /api/regex/search: RegexSearchService prefers rg
# (rg --json over a gitignored tree is sub-second) and silently falls back to a
# linear `grep -r` scan when `which("rg")` is falsy. On a 14G working tree that
# fallback always hits the 30s search timeout, so the binary must be present.
# Kept as its own layer (after the heavy JDK/coursier/gradle steps) so adding it
# does not invalidate their build cache.
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep \
    && rm -rf /var/lib/apt/lists/*

# JAVA_HOME points at the 17 launcher; 21 is a toolchain (installations.paths).
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# Bring over the installed site-packages + the `cidx` console script from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# The xray-cli binary ONLY -- not cargo, not rustc, not ~/.cargo, not the
# target/ directory (hundreds of MB of intermediate artifacts for a single
# ~10MB executable). MUST come after the `COPY --from=builder /usr/local/bin`
# above, which replaces the whole directory and would otherwise delete it.
COPY --from=xray-builder /xray/rust/target/release/xray-cli /usr/local/bin/xray-cli

# Tell the Python side where the binary is. Without this the backend falls back
# to looking for `<some ancestor of the installed package>/rust/target/release/
# xray-cli`, which does not exist in a site-packages install -- see
# code_indexer/xray/rust_backend.py::resolve_xray_cli_path for the full
# resolution order. This is the deployment half of that override: the path is a
# property of how THIS IMAGE was built, so it belongs next to the COPY that
# decides it, not in a runtime config file.
ENV CIDX_XRAY_CLI_PATH=/usr/local/bin/xray-cli

# KNOWN GAP (not fixed here, deliberately): xray-cli compiles each evaluator at
# QUERY time by spawning `rustc` (rust/xray-core/src/compiler/rustc_driver.rs,
# with RUSTUP_TOOLCHAIN pinned to the channel embedded from
# rust/rust-toolchain.toml). This stage ships the binary but no rustc, so a
# scan here reaches xray-cli and then fails with an Infrastructure-kind
# CompileError ("Failed to invoke rustc ... Is rustc installed and on PATH?").
# That is a loud, correct, actionable failure rather than the silent
# "scanned everything, found nothing" this image used to produce -- but it is
# NOT yet a working xray. Closing it means either adding the pinned rustup
# toolchain to this stage (image size cost) or moving evaluator compilation out
# of the serving pod; that is a deployment decision, not a Dockerfile detail.

# Non-root runtime. /data is the server data dir — the Deployment mounts a volume here.
RUN useradd -m -u 1000 cidx && mkdir -p /data && chown -R cidx:cidx /data
USER cidx
WORKDIR /home/cidx

# Gradle setup for the cidx user so scip-java indexing works out-of-the-box:
#  - installations.paths=/opt/jdk-21: expose the Java 21 toolchain (see above) so
#    projects requiring languageVersion=21 compile without "No matching toolchains".
#  - init.d repositoriesMode=PREFER_PROJECT: let scip-java's SemanticdbGradlePlugin
#    add its own repository (to fetch semanticdb-javac) on projects that set
#    dependencyResolutionManagement FAIL_ON_PROJECT_REPOS (e.g. Evolution). cidx
#    only runs Gradle for SCIP indexing, so relaxing this globally is safe.
RUN mkdir -p /home/cidx/.gradle/init.d \
    && printf 'org.gradle.java.installations.paths=/opt/jdk-21\n' \
         > /home/cidx/.gradle/gradle.properties \
    && printf 'settingsEvaluated { s -> s.dependencyResolutionManagement { repositoriesMode.set(RepositoriesMode.PREFER_PROJECT) } }\n' \
         > /home/cidx/.gradle/init.d/cidx-scip.gradle

# C11: pre-fetch the scip-java artifact into the runtime user's Coursier cache so
# index-time `cs launch` is offline-safe (no Maven Central round-trip per index).
# Non-fatal: if the build host can't reach Maven Central, index-time falls back
# to an on-demand download.
RUN cs fetch com.sourcegraph:scip-java_2.13:0.11.1 || true

EXPOSE 8090

# uvicorn binds 0.0.0.0 (the CLI default is 127.0.0.1, which is unreachable in a pod).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "code_indexer.server.main", "--host", "0.0.0.0", "--port", "8090"]
