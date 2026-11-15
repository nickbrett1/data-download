"""The dry run: the whole path, offline (§4.2 of the phase-2 order).

A filed Request goes in one end and a typed verdict comes out the other:
`registry.resolve` -> the Plan's binder -> a Job -> the interpreter -> a driver
-> a verdict. Nothing touches the network, the store, or the Request's state.

The driver here is a **fake, and it is honest about it**. It makes no network
call and holds no credential. It writes placeholder files (not parquet, not the
store) into a temp directory it created and hashes them, so `verify` runs its
real sha256 and row-count comparisons against real bytes.

Three modes, because a fake that can only fail proves only half of what this
phase is for:

| mode      | driver does                                    | verdict        |
|-----------|------------------------------------------------|----------------|
| `nothing` | claims two files, writes none                  | contradiction  |
| `short`   | publishes one of the two, sha matches          | shortfall      |
| `clean`   | publishes both, shas and rows match            | pass           |

`clean` is the one that earns its keep: without it, "verify passes" is first
demonstrated on real money.

What it proves is the **wiring** -- and nothing about Databento. It encodes our
belief about the vendor's shapes; it is not evidence that a real download will
work, and it must not grow into a fixture by another name (§4.1).

Kept out of the service's import graph on purpose: no route imports this, and
`tests/test_dryrun.py` asserts that.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
import sys
import tempfile
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from . import interpreter
from . import job as job_mod
from .profiles import resolve as resolve_plan
from .registry import load_registry

MODES = ("nothing", "short", "clean")

# A fixed number, and nothing compares it to `estimate_usd` (§4.2). It exists so
# the `quote` step has something to return, not so it can be checked.
FAKE_QUOTE_USD = 12.34

# Placeholder rows per file. Big enough that a row count is a real comparison,
# small enough that the file is 4 kB.
ROWS_PER_FILE = 1000


class OfflineDatabentoDriver:
    """A driver that believes it is Databento and touches nothing outside `root`."""

    def __init__(self, mode: str, root: Path) -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        self.mode = mode
        self.root = Path(root)
        self._submitted: dict[str, Any] = {}
        self.calls: list[str] = []

    # -- the vendor protocol -------------------------------------------------

    def cost(self, **kwargs: Any) -> float:
        self.calls.append("cost")
        return FAKE_QUOTE_USD

    def resolve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("resolve")
        symbols = kwargs.get("symbols") or []
        return {"symbols": list(symbols), "resolved": len(symbols)}

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("submit")
        self._submitted = dict(kwargs)
        seed = json.dumps(kwargs, sort_keys=True, default=str).encode("utf-8")
        return {"vendor_job_id": "vjob_" + hashlib.sha256(seed).hexdigest()[:12]}

    def poll(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("poll")
        # Instant, and always done: there is no clock in this phase, and a fake
        # that needed time would make the dry run's result depend on it.
        return {"state": "done"}

    def list_files(self, **kwargs: Any) -> list[dict[str, Any]]:
        self.calls.append("list_files")
        return [dict(entry) for entry in self._claimed()]

    def fetch(self, **kwargs: Any) -> list[str]:
        self.calls.append("fetch")
        dest = self._dest(kwargs["dest"])
        claimed = self._claimed()
        if self.mode == "nothing":
            published: list[dict[str, Any]] = []
        elif self.mode == "short":
            published = claimed[:1]
        else:
            published = claimed
        written: list[str] = []
        for entry in published:
            path = dest / entry["name"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(_content(entry["name"]))
            written.append(entry["name"])
        return written

    def convert(self, **kwargs: Any) -> list[str]:
        self.calls.append("convert")
        return sorted(kwargs.get("staged") or [])

    def verify(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("verify")
        claimed = [dict(entry) for entry in self._claimed()]
        shipped = self._shipped_manifest(claimed)
        staged = _staged_files(self._dest(kwargs["dest"]))
        return {"claimed": [e["name"] for e in claimed], "manifest": shipped, "staged": staged}

    def _dest(self, dest: Any) -> Path:
        """A Job's `dest` is relative on purpose (§5.7), so it resolves under the
        temp root -- never against the process's working directory."""
        path = Path(str(dest))
        return path if path.is_absolute() else self.root / path

    # -- the shape this fake believes ---------------------------------------

    def _claimed(self) -> list[dict[str, Any]]:
        """One file per day of the submitted window, with a real sha256."""
        start = _as_date(self._submitted.get("start"))
        end = _as_date(self._submitted.get("end"))
        dataset = self._submitted.get("dataset") or "DATASET"
        encoding = self._submitted.get("encoding") or "dbn"
        compression = self._submitted.get("compression") or "zstd"
        suffix = f".{encoding}" + (".zst" if compression != "none" else "")

        days: list[date] = []
        if start and end and end > start:
            cursor = start
            while cursor < end:
                days.append(cursor)
                cursor += timedelta(days=1)
                if len(days) > 31:  # a bounded fake, not a bounded vendor
                    break
        if not days:
            days = [start or date(2026, 1, 1)]

        return [
            {
                "name": f"{dataset}-{day.isoformat()}{suffix}",
                "sha256": hashlib.sha256(_content(f"{dataset}-{day.isoformat()}{suffix}")).hexdigest(),
                "rows": ROWS_PER_FILE,
            }
            for day in days
        ]

    def _shipped_manifest(self, claimed: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """The `manifest.json` shipped with the data.

        `nothing` ships a manifest claiming the files it never wrote (a
        contradiction); `short` ships a manifest that agrees only one file was
        published, which is what makes it a legitimate shortfall rather than a
        lie.
        """
        if self.mode == "short":
            return claimed[:1]
        return claimed


def _content(name: str) -> bytes:
    """Deterministic bytes for a placeholder file, with a countable row per line."""
    header = f"placeholder:{name}\n".encode()
    return header + b"row\n" * ROWS_PER_FILE


def _as_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _staged_files(dest: Path) -> dict[str, dict[str, Any]]:
    staged: dict[str, dict[str, Any]] = {}
    if not dest.is_dir():
        return staged
    for path in sorted(dest.iterdir()):
        if not path.is_file():
            continue
        payload = path.read_bytes()
        staged[path.name] = {
            "sha256": hashlib.sha256(payload).hexdigest(),
            "rows": payload.count(b"\n") - 1,  # the header line is not a row
        }
    return staged


def _read_request(db_path: str, request_id: str) -> dict[str, Any] | None:
    """Read the row **read-only**: the dry run must not be able to change state."""
    path = Path(db_path)
    if not path.is_file():
        return None
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        row = conn.execute("SELECT * FROM requests WHERE id = ?", (request_id,)).fetchone()
    finally:
        conn.close()
    return dict(row) if row else None


def _state_snapshot(db_path: str, request_id: str) -> tuple[Any, ...]:
    """Everything the dry run promises not to touch."""
    row = _read_request(db_path, request_id) or {}
    return (
        row.get("state"),
        row.get("updated_at"),
        row.get("job_hash"),
        row.get("errors_json"),
        row.get("landed_sha256"),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data_download.dryrun",
        description=(
            "Take a filed request through resolve -> bind -> interpret -> verify "
            "against an offline driver. No network, no store write, no state change."
        ),
    )
    parser.add_argument("--request", required=True, help="the request id to dry-run")
    parser.add_argument(
        "--plan",
        default=None,
        help="resolve this Plan name instead of the request's (a refusal test)",
    )
    parser.add_argument("--mode", choices=MODES, default="nothing")
    parser.add_argument(
        "--db",
        default=None,
        help="the request store (default: $DATA_DOWNLOAD_DB, or ./requests.db)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="explicit opt-in, kept for the order's wording; the module entry is already one",
    )
    parser.add_argument(
        "--keep",
        action="store_true",
        help="leave the temp directory behind for inspection",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    import os

    args = build_parser().parse_args(argv)
    db_path = args.db or os.environ.get("DATA_DOWNLOAD_DB") or "requests.db"

    before = _state_snapshot(db_path, args.request)
    row = _read_request(db_path, args.request)
    if row is None:
        print(f"error: no request {args.request!r} in {db_path}", file=sys.stderr)
        return 2

    plan_name = args.plan or row.get("plan_type")
    registry = load_registry(_plan_types_dir())
    plan = resolve_plan(registry, plan_name)
    if plan is None:
        # The refusal the exit test asks for: nothing is bound and nothing runs.
        print(f"error: plan_type_unknown: {plan_name!r} is not in the registry")
        return 2

    params = json.loads(row["params_json"]) if row.get("params_json") else {}
    verify = {"on_failure": _on_failure_of(row.get("job_json"))}
    bound = plan.binder(params, verify)
    try:
        text, digest = job_mod.bind_and_seal(bound)
    except job_mod.JobInvalid as exc:
        print(f"error: plan_not_valid: {exc.message}", file=sys.stderr)
        return 2

    stored = row.get("job_json")
    if stored is not None and stored != text:
        # Byte-stability, checked against what was actually stored: the binder is
        # deterministic, so a difference is drift or a pre-phase-2 row.
        print(
            "error: the stored job_json is not what this binder produces "
            "(drift, or a row filed before phase 2)",
            file=sys.stderr,
        )
        return 2

    workdir = Path(tempfile.mkdtemp(prefix="data-download-dryrun-"))
    try:
        driver = OfflineDatabentoDriver(args.mode, workdir)
        job = json.loads(text)
        result = interpreter.run(job, driver)
        print(
            f"request: {args.request}  plan: {plan.name}  mode: {args.mode}  "
            f"interpreter: {result.interpreter_version}"
        )
        print(f"job: sha256 {digest} ({len(job['steps'])} steps)")
        print(f"verdict: {result.verdict} ({result.disposition})")
        print(f"detail: {json.dumps(result.detail, sort_keys=True)}")
        print(f"worker: {workdir} ({', '.join(driver.calls)})")
        after = _state_snapshot(db_path, args.request)
        if after != before:
            print("error: the run changed the request's state", file=sys.stderr)
            return 2
        print("state: unchanged")
        return 0 if result.verdict == interpreter.PASS else 1
    except interpreter.InterpreterError as exc:
        print(f"error: interpreter: {exc}", file=sys.stderr)
        return 2
    finally:
        if args.keep:
            print(f"kept: {workdir}")
        else:
            shutil.rmtree(workdir, ignore_errors=True)


def _on_failure_of(job_json: str | None) -> str:
    """The failure policy this request was filed with, read back off its Job."""
    if job_json:
        try:
            steps = json.loads(job_json).get("steps") or []
        except (json.JSONDecodeError, AttributeError):
            steps = []
        for step in steps:
            if isinstance(step, dict) and step.get("kind") == "verify":
                value = (step.get("kwargs") or {}).get("on_failure")
                if value in {"commit_marked", "quarantine"}:
                    return str(value)
    return "quarantine"


def _plan_types_dir() -> Path | None:
    from .config import find_plan_types_dir

    return find_plan_types_dir()


if __name__ == "__main__":
    raise SystemExit(main())
