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
