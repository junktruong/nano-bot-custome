"""Run nanobot extension worker from module entrypoint."""

from __future__ import annotations

import argparse
import os

from nanobot.extensions.worker import run_extension_worker_server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run nanobot extension worker service.")
    parser.add_argument("--host", default="127.0.0.1", help="Bind host (default: 127.0.0.1)")
    parser.add_argument("--port", type=int, default=7091, help="Bind port (default: 7091)")
    parser.add_argument("--workers", type=int, default=2, help="Worker thread count (default: 2)")
    parser.add_argument(
        "--token",
        default=os.environ.get("NANOBOT_EXTENSION_TOKEN", ""),
        help="Bearer token for API auth (default: NANOBOT_EXTENSION_TOKEN)",
    )
    return parser


def main() -> int:
    args = _build_parser().parse_args()
    run_extension_worker_server(
        host=args.host,
        port=args.port,
        token=args.token,
        worker_count=args.workers,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

