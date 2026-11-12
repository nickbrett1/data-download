"""Entry point for data_download (python -m data_download).

Transport selection: `stdio` (the default, so a local client can spawn the
module and inherit its own filesystem) or `streamable-http` (so the container
image serves HTTP and answers the `http:/...` healthcheck its Dockerfile
declares). The generated Dockerfile selects the HTTP transport by default; a
local client runs the module with no arguments and gets stdio.
"""

import argparse
import os
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

DEFAULT_PORT = int(os.environ.get("PORT", 8820))
HEALTH_PATH = "/healthz"


class _HealthHandler(BaseHTTPRequestHandler):
    """Answer GET HEALTH_PATH with `{"status": "ok"}`; 404 everything else."""

    def do_GET(self):
        if self.path.split("?", 1)[0] == HEALTH_PATH:
            body = b'{"status": "ok"}'
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_error(404)

    def log_message(self, fmt, *args):
        """Keep the default one-line-per-request stderr logging quiet."""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="data_download",
        description="data_download entry point (stdio by default).",
    )
    parser.add_argument(
        "--transport",
        choices=["stdio", "streamable-http"],
        default="stdio",
        help="Transport to serve on (default: stdio).",
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Bind address for streamable-http (default: 0.0.0.0).",
    )
    parser.add_argument(
        "--port",
        type=int,
        default=DEFAULT_PORT,
        help=f"Port for streamable-http (default: {DEFAULT_PORT}).",
    )
    return parser


def serve_http(host: str, port: int) -> None:
    server = ThreadingHTTPServer((host, port), _HealthHandler)
    print(f"serving http://{host}:{port}{HEALTH_PATH}", flush=True)
    server.serve_forever()


def main() -> int:
    args = build_parser().parse_args()
    if args.transport == "streamable-http":
        serve_http(args.host, args.port)
        return 0
    print(f"{__package__} is installed and importable.", file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
