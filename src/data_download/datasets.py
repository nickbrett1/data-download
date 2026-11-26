"""The datasets browser: what landed, and what is still landing (§7.8).

The store knows what was *asked for*; the datasets root knows what *exists*.
Neither alone answers the question a phone asks — "show me what I have, and
what is still coming" — so this module joins them, read-only:

* the vendor and dataset come from the row (its params, or the plan type's
  declaration) and the Job's
  `resolve` step), which is the same vocabulary `runner.destination` files
  under, so the tree on disk and the tree in the browser cannot drift;
* the files come from `landing.json` when a run settled, and from the
  filesystem when one has not — a `running` request with three of five days on
  disk must show three files, not nothing, or "in progress" looks like "not
  started";
* a terminal row whose directory is gone says so (`on_disk: false`) instead of
  pretending the bytes are there.

Nothing here writes, hashes on demand, or walks the whole share: it reads the
`landing.json` of requests the store already mentioned, and `stat`s their
files. A browser that hashed 13 GB because a phone opened a page would be a
worse bug than a stale listing.

A `failed` row is deliberately absent. Its bytes may well be on disk, but the
verifier refused them (`quarantine`), and a page answering "what do I have?"
must not count something the pipeline has already rejected. Those rows stay on
the request book, where attempts -- not holdings -- are the subject.

Read-only is a property of this module — every path goes through
`resolve_under`, which refuses anything outside the root — and of the
deployment (the share is mounted read-only where the runner does not need it).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from . import layout
from .registry import Registry

# States worth browsing: what is committed, and what is on its way.
#
# Two kinds of row are left out, for two different reasons. A `draft` or a
# blocked request has no Job, so no vendor, no dataset and no directory --
# showing it would be a row that can never have bytes. A `failed` row *can*
# have bytes on disk, but the verifier refused them (`quarantine`): listing
# them here would answer "what do I have?" with something the pipeline has
# already decided is not trustworthy. This page is what landed; the request
# book is where everything that was attempted stays visible.
VISIBLE_STATES = frozenset({"approved", "running", "succeeded", "landed_with_errors"})

# What the page calls a request whose plan type is no longer in the registry.
NO_VENDOR = "(unknown vendor)"


def resolve_under(root: Path | str | None, *parts: str) -> Path | None:
    """A path inside `root`, or `None`.

    `resolve()` first, so `..` and symlinks are both judged by where they
    actually point rather than by how the string looks. This is the only way
    a route should turn a URL segment into a filesystem path.
    """
    if root is None:
        return None
    base = Path(root).resolve()
    candidate = base.joinpath(*parts)
    try:
        resolved = candidate.resolve()
    except (OSError, RuntimeError):
        return None
    if resolved != base and base not in resolved.parents:
        return None
    return resolved


def _params_of(row: dict[str, Any]) -> dict[str, Any]:
    """The row's params, or `{}`. Used to name the vendor, which they carry."""
    text = row.get("params_json")
    if not text:
        return {}
    try:
        params = json.loads(text)
    except (TypeError, ValueError):
        return {}
    return params if isinstance(params, dict) else {}


def _job_of(row: dict[str, Any]) -> dict[str, Any] | None:
    text = row.get("job_json")
    if not text:
        return None
    try:
        job = json.loads(text)
    except (TypeError, ValueError):
        return None
    return job if isinstance(job, dict) else None


def _step_kwargs(job: dict[str, Any], kind: str) -> dict[str, Any]:
    for step in job.get("steps") or []:
        if isinstance(step, dict) and step.get("kind") == kind:
            kwargs = step.get("kwargs")
            return kwargs if isinstance(kwargs, dict) else {}
    return {}


def job_facts(job: dict[str, Any] | None) -> dict[str, Any]:
    """The handful of Job fields a reader needs to recognise a download.

    Read off the Job rather than off params because the Job is what ran; params
    are the earlier draft of the same fact (§5.1).
    """
    if not job:
        return {}
    resolve = _step_kwargs(job, "resolve")
    quote = _step_kwargs(job, "quote")
    download = _step_kwargs(job, "download")
    return {
        "dataset": resolve.get("dataset") or quote.get("dataset"),
        "symbols": resolve.get("symbols") or quote.get("symbols") or [],
        "stype_in": resolve.get("stype_in") or quote.get("stype_in"),
        "schema": quote.get("schema") or download.get("schema"),
        "start": quote.get("start"),
        "end": quote.get("end"),
    }


def _landing(request_dir: Path) -> dict[str, Any] | None:
    path = request_dir / "landing.json"
    if not path.is_file():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _files_on_disk(dest: Path) -> list[dict[str, Any]]:
    """Name and size, no hashing: this is a listing, not a verification."""
    out: list[dict[str, Any]] = []
    if not dest.is_dir():
        return out
    for path in sorted(dest.iterdir()):
        if not path.is_file():
            continue
        try:
            size = path.stat().st_size
        except OSError:
            continue
        out.append({"name": path.name, "bytes": size, "sha256": None, "rows": None})
    return out


def _request_entry(
    row: dict[str, Any],
    *,
    root: Path | None,
    registry: Registry,
) -> dict[str, Any] | None:
    job = _job_of(row)
    facts = job_facts(job)
    dataset = facts.get("dataset")
    # Same order as everywhere else: the request's params name the seller
    # (the plan type is generic), the type may pin one, the dataset code may be
    # claimed by a registered type. Without the first step every row of a
    # generic plan would land under "(unknown vendor)".
    vendor = registry.vendor_of_request(row.get("plan_type"), _params_of(row))
    if not dataset:
        # No Job, no dataset: nothing this page could ever show. (An approved
        # row always has one; this is the belt to the claim's braces.)
        return None

    vendor_slug = layout.vendor_slug(vendor)
    # The directory a run writes today is named by its window and a short id
    # (`layout.landing_name`); anything landed before that layout is named by
    # the request id. The new name is the answer, and the old one is the
    # fallback -- a layout change must not take history off the page.
    name = layout.landing_name(str(row["id"]), facts.get("start"), facts.get("end"))
    request_dir = (
        resolve_under(root, vendor_slug, str(dataset), name)
        if root is not None and vendor_slug
        else None
    )
    legacy = (
        resolve_under(root, vendor_slug, str(dataset), layout.legacy_name(str(row["id"])))
        if root is not None and vendor_slug
        else None
    )
    if (request_dir is None or not request_dir.is_dir()) and legacy is not None:
        if legacy.is_dir():
            request_dir, name = legacy, layout.legacy_name(str(row["id"]))
    landing = _landing(request_dir) if request_dir else None
    dest = (request_dir / "files") if request_dir else None

    files: list[dict[str, Any]] = []
    if landing and isinstance(landing.get("files"), list):
        for entry in landing["files"]:
            if not isinstance(entry, dict) or not isinstance(entry.get("name"), str):
                continue
            files.append(
                {
                    "name": entry["name"],
                    "bytes": entry.get("bytes"),
                    "sha256": entry.get("sha256"),
                    "rows": entry.get("rows"),
                }
            )
    if not files and dest is not None:
        # A run still in flight: the manifest does not exist yet, but the bytes
        # that have arrived do, and "in progress" should look like progress.
        files = _files_on_disk(dest)

    for entry in files:
        entry["href"] = (
            f"/datasets/file/{vendor_slug}/{dataset}/{name}/{entry['name']}"
            if vendor_slug
            else None
        )

    total = sum(int(f["bytes"] or 0) for f in files if f.get("bytes") is not None)
    driver = (landing or {}).get("driver")
    return {
        "request_id": row["id"],
        "state": row["state"],
        "plan_type": row.get("plan_type"),
        "vendor": vendor or NO_VENDOR,
        "vendor_slug": vendor_slug,
        "dataset": dataset,
        **{k: facts.get(k) for k in ("symbols", "stype_in", "schema", "start", "end")},
        "estimate_usd": row.get("estimate_usd"),
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "landed_sha256": row.get("landed_sha256"),
        "verdict": (landing or {}).get("verdict"),
        "disposition": (landing or {}).get("disposition"),
        # Which driver wrote the landing. Reported, not judged: whether those
        # bytes are real is a fact about the driver, and the page shows the
        # landing, not a verdict on it.
        "driver": driver,
        "settled_at": (landing or {}).get("settled_at"),
        "on_disk": bool(files) or bool(landing),
        "files": files,
        "total_bytes": total,
    }


def tree(
    rows: list[dict[str, Any]],
    *,
    root: Path | str | None,
    registry: Registry,
) -> dict[str, Any]:
    """Rows in, a vendor -> dataset -> request tree out, plus totals."""
    base = Path(root) if root is not None else None
    entries = [
        entry
        for entry in (
            _request_entry(row, root=base, registry=registry)
            for row in rows
            if row.get("state") in VISIBLE_STATES
        )
        if entry is not None
    ]

    vendors: dict[str, dict[str, Any]] = {}
    for entry in entries:
        bucket = vendors.setdefault(
            entry["vendor_slug"] or "", {"vendor": entry["vendor"], "datasets": {}}
        )
        dataset = bucket["datasets"].setdefault(
            entry["dataset"], {"dataset": entry["dataset"], "requests": []}
        )
        dataset["requests"].append(entry)

    ordered = []
    for slug in sorted(vendors, key=lambda s: (s == "", s)):
        bucket = vendors[slug]
        datasets = []
        for name in sorted(bucket["datasets"]):
            group = bucket["datasets"][name]
            group["requests"].sort(key=lambda r: str(r["created_at"]), reverse=True)
            group["files"] = sum(len(r["files"]) for r in group["requests"])
            group["bytes"] = sum(int(r["total_bytes"]) for r in group["requests"])
            datasets.append(group)
        ordered.append(
            {
                "vendor": bucket["vendor"],
                "vendor_slug": slug,
                "datasets": datasets,
                "requests": sum(len(d["requests"]) for d in datasets),
                "files": sum(d["files"] for d in datasets),
                "bytes": sum(d["bytes"] for d in datasets),
            }
        )

    return {
        "root": str(base) if base is not None else None,
        "configured": base is not None,
        "vendors": ordered,
        "totals": {
            "vendors": len(ordered),
            "datasets": sum(len(v["datasets"]) for v in ordered),
            "requests": len(entries),
            "files": sum(v["files"] for v in ordered),
            "bytes": sum(v["bytes"] for v in ordered),
            "in_flight": sum(1 for e in entries if e["state"] in {"approved", "running"}),
        },
    }


def human_bytes(value: int | None) -> str:
    """`4.1 kB` — only used in the page, kept beside the tree so the API and the
    renderer agree on what a size rounds to."""
    size = float(value or 0)
    for unit in ("B", "kB", "MB", "GB", "TB"):
        if size < 1000 or unit == "TB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1000
    return f"{size:.1f} TB"
