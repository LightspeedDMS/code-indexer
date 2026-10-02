"""Process entry point: ``python3 -m tests.fixtures.secops_sidecar``.

Prints exactly one ``SIDECAR READY ingest=N control=M`` line to stdout once
both listeners accept connections; logs go to stderr.  SIGTERM (or SIGINT)
closes both listeners and exits 0.
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from pathlib import Path
from typing import List, Optional

from . import netguard
from .server import READY_LINE_PREFIX, Sidecar, SidecarConfig
from .state import SELF_TEST_DEFECTS, SidecarState

# The main thread polls in short steps (instead of one unbounded wait) so the
# Python-level signal handlers get a chance to run between waits.
_SIGNAL_POLL_SECONDS = 0.5


def _positive_int(raw: str) -> int:
    value = int(raw)
    if value <= 0:
        raise argparse.ArgumentTypeError(f"must be a positive integer: {raw}")
    return value


def _port(raw: str) -> int:
    value = int(raw)
    if not 1 <= value <= 65535:
        raise argparse.ArgumentTypeError(f"not a TCP port: {raw}")
    return value


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="python3 -m tests.fixtures.secops_sidecar",
        description="Mock Google SecOps (Chronicle) receiver for tests.",
    )
    p.add_argument("--ingest-port", type=_port, required=True)
    p.add_argument("--control-port", type=_port, required=True)
    p.add_argument("--project", required=True)
    p.add_argument("--location", required=True)
    p.add_argument("--instance", required=True)
    p.add_argument("--api-version", required=True)
    p.add_argument("--token-public-key", type=Path, required=True)
    p.add_argument("--max-request-bytes", type=_positive_int, default=4_000_000)
    p.add_argument("--token-ttl-seconds", type=_positive_int, default=3600)
    p.add_argument(
        "--self-test-defect",
        choices=SELF_TEST_DEFECTS,
        default=None,
        help="Deliberately break one rule. ONLY for the negative-control self-tests.",
    )
    return p


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    if args.ingest_port == args.control_port:
        print("ingest and control ports must differ", file=sys.stderr)
        return 2
    logging.basicConfig(
        stream=sys.stderr,
        level=logging.INFO,
        format="%(asctime)s secops-sidecar %(levelname)s %(message)s",
    )
    config = SidecarConfig(
        ingest_port=args.ingest_port,
        control_port=args.control_port,
        project=args.project,
        location=args.location,
        instance=args.instance,
        api_version=args.api_version,
        public_key_pem=args.token_public_key.read_bytes(),
        max_request_bytes=args.max_request_bytes,
        token_ttl_seconds=args.token_ttl_seconds,
    )
    sidecar = Sidecar(config, SidecarState(args.self_test_defect))
    stop_requested = threading.Event()

    def _on_signal(signum: int, frame: object) -> None:
        stop_requested.set()

    signal.signal(signal.SIGTERM, _on_signal)
    signal.signal(signal.SIGINT, _on_signal)
    netguard.install()  # refuse (and count) any non-loopback connection
    sidecar.start()
    try:
        print(
            f"{READY_LINE_PREFIX} ingest={args.ingest_port} "
            f"control={args.control_port}",
            flush=True,
        )
        # Runs until SIGTERM/SIGINT sets stop_requested (the only exit path).
        while not stop_requested.wait(_SIGNAL_POLL_SECONDS):
            pass
    finally:
        sidecar.stop()
        logging.getLogger("secops_sidecar").info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
