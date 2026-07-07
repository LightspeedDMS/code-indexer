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

# 1) Build + install the custom hnswlib FIRST. cidx imports hnswlib but deliberately
#    does NOT list the PyPI package as a dependency, so this must be present before
#    installing cidx.
COPY third_party/hnswlib ./third_party/hnswlib
RUN pip install ./third_party/hnswlib

# 2) Install the cidx package WITH the `cluster` extra (hatchling backend). The cluster
#    extra pulls psycopg[binary] + psycopg-pool — REQUIRED for postgres/cluster mode
#    (StorageFactory lazy-imports psycopg; without it multi-pod boot ImportErrors).
#    hnswlib already satisfied above.
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[cluster]"

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
# Pin JDK 17 (LTS), NOT the distro default (21 on bookworm): the JDK that RUNS
# the project's Gradle must be old enough for that Gradle version. Gradle 7.x
# (still the most common) only supports Java <=17; on Java 21 the build fails
# with "Unsupported class file major version 65". Java 17 runs Gradle 7.x and
# 8.x and scip-java itself, so it is the broadly-compatible choice.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgomp1 tini openjdk-17-jdk-headless curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && curl -fsSL https://github.com/coursier/launchers/raw/master/cs-x86_64-pc-linux.gz \
         | gzip -d > /usr/local/bin/cs \
    && chmod +x /usr/local/bin/cs

# JAVA_HOME so scip-java / gradle reliably find the JDK.
ENV JAVA_HOME=/usr/lib/jvm/java-17-openjdk-amd64

# Bring over the installed site-packages + the `cidx` console script from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Non-root runtime. /data is the server data dir — the Deployment mounts a volume here.
RUN useradd -m -u 1000 cidx && mkdir -p /data && chown -R cidx:cidx /data
USER cidx
WORKDIR /home/cidx

# C11: pre-fetch the scip-java artifact into the runtime user's Coursier cache so
# index-time `cs launch` is offline-safe (no Maven Central round-trip per index).
# Non-fatal: if the build host can't reach Maven Central, index-time falls back
# to an on-demand download.
RUN cs fetch com.sourcegraph:scip-java_2.13:0.11.1 || true

EXPOSE 8090

# uvicorn binds 0.0.0.0 (the CLI default is 127.0.0.1, which is unreachable in a pod).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "code_indexer.server.main", "--host", "0.0.0.0", "--port", "8090"]
