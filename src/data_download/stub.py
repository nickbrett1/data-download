"""The stub vendor: phase 5's runner against files that are not data (§3).

Phase 5 is the runner -- claim a row, interpret its Job, put bytes under
`/datasets`, settle the row. This module is the **driver half** of that, and it
is a stub for one reason: the runner's correctness (lease, settlement, the
landing digest, the path a request lands in) can be built and tested without a
credential, a vendor job, or a cent of spend. What it cannot prove is anything
about Databento, and it does not pretend to:

* every file it writes begins with `STUB:` and the vendor job id, so no byte it
  produces can be mistaken for market data;
* `cost()` returns a fixed number and the runner compares it to nothing (§5.2:
  the one comparison happened at submit);
* `convert()` does not convert. A real parquet conversion writes files the
  vendor's manifest does not list, and the shared verifier would correctly call
  that a contradiction -- so the stub leaves the source files alone rather than
  teaching `classify` a lie.

The shapes it believes (one file per day, `.dbn.zst`, a shipped manifest) are
the same beliefs `dryrun.py` holds, deliberately: `tests/test_stub_and_runner.py`
runs both and asserts they agree step for step, so a change to one that is not a
change to the other is caught there rather than on the first real dollar.

No network and no vendor import -- `tests/test_store_and_egress.py` bans both
under `src/`, and `vendor.py` remains the only module allowed a client.
"""

from __future__ import annotations

import hashlib
from datetime import date, timedelta
from pathlib import Path
from typing import Any

MODES = ("clean", "short", "nothing")

# A fixed number so `quote` has something to return. Never compared to
# `estimate_usd`; that comparison is phase 4's, at submit.
FAKE_QUOTE_USD = 12.34

# Placeholder rows per file: big enough that a row count is a real comparison,
# small enough that a day is a few kilobytes.
ROWS_PER_FILE = 1000


class StubDatabentoDriver:
    """A driver that answers like Databento and touches nothing outside `root`."""

    def __init__(self, root: Path, mode: str = "clean") -> None:
        if mode not in MODES:
            raise ValueError(f"unknown mode {mode!r}; expected one of {', '.join(MODES)}")
        self.root = Path(root)
        self.mode = mode
        self._submitted: dict[str, Any] = {}
        self._vendor_job_id = ""
        self.calls: list[str] = []

    # -- the driver protocol (interpreter.Driver) ----------------------------

    def cost(self, **kwargs: Any) -> float:
        self.calls.append("cost")
        return FAKE_QUOTE_USD

    def resolve(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("resolve")
        symbols = kwargs.get("symbols") or []
        return {"symbols": list(symbols), "resolved": len(symbols), "stub": True}

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("submit")
        self._submitted = dict(kwargs)
        seed = _canonical(kwargs).encode("utf-8")
        # Remembered, not returned-and-forgotten: the day files embed the job id,
        # so `_claimed()` and `fetch()` must hash the same text.
        self._vendor_job_id = "stub_" + hashlib.sha256(seed).hexdigest()[:12]
        return {"vendor_job_id": self._vendor_job_id}

    def poll(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("poll")
        # Instant and always done: the stub has no clock, so its result must not
        # depend on one.
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
            path.write_bytes(_content(entry["name"], self._vendor_job_id))
            written.append(entry["name"])
        return written

    def convert(self, **kwargs: Any) -> list[str]:
        self.calls.append("convert")
        # Deliberately a no-op: see the module docstring. The real driver replaces
        # the `.dbn.zst` with a `.parquet`; the stub leaves the source in place so
        # `staged` still matches the vendor manifest exactly.
        return sorted(kwargs.get("staged") or [])

    def verify(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append("verify")
        claimed = [dict(entry) for entry in self._claimed()]
        shipped = claimed[:1] if self.mode == "short" else claimed
        return {
            "claimed": [entry["name"] for entry in claimed],
            "manifest": shipped,
            "staged": staged_files(self._dest(kwargs["dest"])),
        }

    # -- where the bytes go --------------------------------------------------

    def _dest(self, dest: Any) -> Path:
        """A Job's `dest` is relative on purpose (§5.7): it resolves under the
        request's root, never against the process's working directory."""
        path = Path(str(dest))
        return path if path.is_absolute() else self.root / path

    # -- the shape this stub believes ---------------------------------------

    def _claimed(self) -> list[dict[str, Any]]:
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
                if len(days) > 31:  # a bounded stub, not a bounded vendor
                    break
        if not days:
            days = [start or date(2026, 1, 1)]

        job_id = self._vendor_job_id
        return [
            {
                "name": f"{dataset}-{day.isoformat()}{suffix}",
                "sha256": hashlib.sha256(
                    _content(f"{dataset}-{day.isoformat()}{suffix}", job_id)
                ).hexdigest(),
                "rows": ROWS_PER_FILE,
            }
            for day in days
        ]


def _content(name: str, vendor_job_id: str) -> bytes:
    """Deterministic bytes that announce themselves as a stub."""
    header = f"STUB: not market data. file={name} job={vendor_job_id}\n".encode()
    return header + b"row\n" * ROWS_PER_FILE


def staged_files(dest: Path) -> dict[str, dict[str, Any]]:
    """What is actually on disk, hashed -- the same reading the verifier does."""
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


def _as_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _canonical(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)
