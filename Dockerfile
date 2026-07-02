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
FROM python:3.11-slim AS builder

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
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CIDX_SERVER_DATA_DIR=/data \
    DEBIAN_FRONTEND=noninteractive

# Runtime deps: git (GitPython + golden-repo clone/refresh), libgomp1 (hnswlib OpenMP),
# tini (PID 1 signal handling / zombie reaping).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git libgomp1 tini \
    && rm -rf /var/lib/apt/lists/*

# Bring over the installed site-packages + the `cidx` console script from the builder.
COPY --from=builder /usr/local/lib/python3.11/site-packages /usr/local/lib/python3.11/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin

# Non-root runtime. /data is the server data dir — the Deployment mounts a volume here.
RUN useradd -m -u 1000 cidx && mkdir -p /data && chown -R cidx:cidx /data
USER cidx
WORKDIR /home/cidx

EXPOSE 8090

# uvicorn binds 0.0.0.0 (the CLI default is 127.0.0.1, which is unreachable in a pod).
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "code_indexer.server.main", "--host", "0.0.0.0", "--port", "8090"]
