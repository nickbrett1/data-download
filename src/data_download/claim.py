"""Claim an approved request (`python -m data_download.claim`).

This is the phase 5 runner's entry point with `argv` in front of it, not a
second surface: it calls the same `Store.claim` the runner will, so the exit
test can be a shell transcript.

    python -m data_download.claim <request_id> --owner a
    python -m data_download.claim <request_id> --owner b     # refused: leased
    python -m data_download.claim <request_id> --owner a --lease-seconds 1
    sleep 2
    python -m data_download.claim <request_id> --owner b     # reclaimed

Exit codes: `0` claimed, `1` refused, `2` not found.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid

from .config import load_settings
from .db import ClaimRefused, Store


def _hostname() -> str:
    """Not `socket.gethostname()`: `tests/test_store_and_egress.py` forbids a
    socket import anywhere under `src/`, and the ban is worth more than the
    convenience. `uname` answers the same question without an import that can
    open a connection."""
    try:
        return os.uname().nodename
    except AttributeError:  # not POSIX
        return os.environ.get("HOSTNAME") or "unknown"


def default_owner() -> str:
    """`<host>:<pid>:<uuid>` -- opaque, but legible in an event.

    A bare uuid would answer "is this lease mine?" only if the claimant still
    remembers it; host and pid make a reclaim readable as well as correct.
    """
    return f"{_hostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


async def run(args: argparse.Namespace) -> int:
    store = Store(args.db or load_settings().db_path)
    await store.connect()
    try:
        row = await store.claim(
            args.request_id,
            args.owner,
            lease_seconds=args.lease_seconds,
        )
    except ClaimRefused as exc:
        if exc.reason == "not_found":
            print(f"not_found: no request {args.request_id!r}", file=sys.stderr)
            return 2
        print(exc.as_text(), file=sys.stderr)
        return 1
    finally:
        await store.close()

    print(f"claimed {row['id']} owner={row['owner']} lease_until={row['lease_until']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data_download.claim",
        description="Claim one approved request: the CAS in §8.2.",
    )
    parser.add_argument("request_id")
    parser.add_argument("--owner", default=None, help="the claimant (default: host:pid:uuid)")
    parser.add_argument(
        "--lease-seconds", type=int, default=None, help="override the default lease"
    )
    parser.add_argument("--db", default=None, help="the store (default: DATA_DOWNLOAD_DB)")
    args = parser.parse_args(argv)
    if args.owner is None:
        args.owner = default_owner()
    return asyncio.run(run(args))


if __name__ == "__main__":
    raise SystemExit(main())
