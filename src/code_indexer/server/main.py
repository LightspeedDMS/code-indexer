"""
Main entry point for CIDX Server.

Runs FastAPI server using uvicorn with proper configuration.
"""

import argparse
import json
import os
import uvicorn
from pathlib import Path


def _resolve_workers(cli_workers):
    """Resolve the uvicorn worker count (bootstrap knob for C1).

    Precedence: --workers CLI flag, else the ``workers`` field in the bootstrap
    config.json (CIDX_SERVER_DATA_DIR/config.json, falling back to
    ~/.cidx-server/config.json), else 1. Values < 1 are clamped to 1.

    Running >1 uvicorn worker lets a pod use more than one core -- a single
    process is GIL-bound at ~1.6 cores even though tantivy/hnswlib release the
    GIL during search. Each worker is its own process with its own in-RAM index
    cache, so raise this only when the per-pod working set fits in
    workers x per-worker memory.
    """
    if cli_workers is not None:
        return max(1, cli_workers)
    data_dir = os.environ.get("CIDX_SERVER_DATA_DIR", str(Path.home() / ".cidx-server"))
    cfg_path = Path(data_dir) / "config.json"
    try:
        if cfg_path.exists():
            with open(cfg_path) as f:
                workers = int(json.load(f).get("workers", 1))
                return max(1, workers)
    except Exception:
        pass
    return 1


def main():
    """Main entry point for CIDX server."""
    parser = argparse.ArgumentParser(description="CIDX Multi-User Server")
    parser.add_argument(
        "--port", type=int, default=8090, help="Port to run server on (default: 8090)"
    )
    parser.add_argument(
        "--host",
        type=str,
        default="127.0.0.1",
        help="Host to bind server to (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--reload", action="store_true", help="Enable auto-reload for development"
    )
    parser.add_argument(
        "--workers",
        type=int,
        default=None,
        help="Number of uvicorn worker processes (default: from config.json "
        "'workers', else 1). >1 lets a pod use more than one core.",
    )

    args = parser.parse_args()
    workers = _resolve_workers(args.workers)

    # Ensure ~/.cidx-server directory exists
    server_dir = Path.home() / ".cidx-server"
    server_dir.mkdir(exist_ok=True)

    print(f"Starting CIDX Server on {args.host}:{args.port}")
    print(f"Server directory: {server_dir}")
    print(f"Documentation available at: http://{args.host}:{args.port}/docs")
    print(f"Worker processes: {workers}")
    print("Press Ctrl+C to stop the server")

    # Run server. uvicorn requires the app as an import string when workers > 1
    # (each worker imports it independently); --reload and --workers are mutually
    # exclusive, so reload forces a single worker. Startup is worker-safe:
    # migrations run under a PG advisory lock and admin seeding is idempotent.
    run_kwargs = dict(
        host=args.host,
        port=args.port,
        access_log=True,
    )
    if args.reload:
        run_kwargs["reload"] = True
    elif workers > 1:
        run_kwargs["workers"] = workers
    uvicorn.run("code_indexer.server.app:app", **run_kwargs)


if __name__ == "__main__":
    main()
