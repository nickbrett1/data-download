"""Entry point for data_download (python -m data_download).

The container image runs this with `--host 0.0.0.0 --port 8820` and uvicorn
serves the FastAPI app; the Dockerfile's HEALTHCHECK polls `/healthz`. Running
the module with no arguments prints that it is importable, which is what a
local client or a sanity check expects.
"""

from __future__ import annotations

import argparse
import os
import sys

DEFAULT_PORT = int(os.environ.get("PORT", 8820))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data_download",
        description="data_download service entry point (uvicorn over FastAPI).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="streamable-http",
        help="Kept for compatibility with the generated image; stdio only reports readiness.",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port to serve on (default: {DEFAULT_PORT}, or $PORT).",
    )
    return parser


def main() -> int:
    args = build_parser().parse_args()
    if args.transport == "stdio":
        print(f"{__package__} is installed and importable.", file=sys.stderr)
        return 0

    from .app import serve

    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
