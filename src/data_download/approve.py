"""Approve a request (`python -m data_download.approve`).

**A verb, not a route** (§7.4). `approve` scope exists and a bare token never
gets it, so an HTTP endpoint would be one token away from being an agent's
approve. A CLI has no route table entry, no token and no egress.

    python -m data_download.approve <request_id> --actor nick --note "cheap window"

Exit codes: `0` approved (or already approved), `1` refused, `2` not found --
the same three `bind.py` and `dryrun.py` use, so a script learns one convention.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from .config import load_settings
from .db import ApproveRefused, Store


async def run(args: argparse.Namespace) -> int:
    store = Store(args.db or load_settings().db_path)
    await store.connect()
    try:
        row, changed = await store.approve(args.request_id, args.actor, args.note)
    except ApproveRefused as exc:
        if exc.reason == "not_found":
            print(f"not_found: no request {args.request_id!r}", file=sys.stderr)
            return 2
        print(exc.as_text(), file=sys.stderr)
        return 1
    finally:
        await store.close()

    if changed:
        print(f"approved {row['id']} (job_hash={row['job_hash']})")
    else:
        print(f"already approved {row['id']} - nothing written")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m data_download.approve",
        description="Approve one filed request. There is no HTTP approve endpoint.",
    )
    parser.add_argument("request_id")
    parser.add_argument("--actor", required=True, help="who is approving, recorded verbatim")
    parser.add_argument("--note", default=None, help="why, recorded verbatim")
    parser.add_argument("--db", default=None, help="the store (default: DATA_DOWNLOAD_DB)")
    return asyncio.run(run(parser.parse_args(argv)))


if __name__ == "__main__":
    raise SystemExit(main())
