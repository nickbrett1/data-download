"""The binder for `market_data_window`: params in, Job out (§5.2).

Deterministic and pure. No clock, no random, no vendor client, no price
arithmetic -- a `quote` step is *emitted*, not performed; performing it is the
interpreter's job at run time (§3).

The step sequence is the batch shape: resolve the symbology, price it, submit,
poll the vendor job, download the per-day files, convert to parquet, verify
against the vendor's own manifest. `split_duration: day` is the convention the
dataset store already holds, which is why the download step is per-day rather
than one archive.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

PLAN_NAME = "market_data_window"

# One poll interval, in the Job rather than in the interpreter, because how
# often to ask a vendor is a vendor fact (§3: the kind is the abstraction, the
# vendor's fields are payload).
POLL_INTERVAL_S = 30

# Where the interpreter stages files inside the Attempt's output directory.
# Relative on purpose: the absolute directory is keyed on the *request id* and
# created at run time (phase 5), and `job_hash` must never be a path segment
# (§5.7).
DEST = "files/"


def _required(params: Mapping[str, Any], name: str) -> Any:
    """Params are schema-validated before binding, so a miss is a code defect."""
    if name not in params:
        raise ValueError(f"params.{name} is required for {PLAN_NAME}")
    return params[name]


def bind(
    params: Mapping[str, Any], verify: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Bind one Request's params into a Job (§5.1: params always in, Job always out)."""
    if not isinstance(verify, Mapping) or not verify.get("on_failure"):
        # §5.4: `verify.on_failure` is mandatory on every Request, and a Job
        # cannot widen it past what its Plan declared. Failing here rather than
        # defaulting is what keeps "a Job always states its failure policy"
        # true; `models.validate_plan` has already refused most of these.
        raise ValueError("verify.on_failure is required before a Job can be bound")

    # Who is being asked. Required, because one plan type serves every seller
    # and the driver is bound by vendor -- a Job with no vendor has no driver to
    # call, and `driver_not_ready` at run time would be a late, expensive answer.
    # Checked here rather than emitted: the Job is the *steps*, and the vendor is
    # the request's fact, not a step. The runner reads it from the row.
    _required(params, "vendor")
    dataset = _required(params, "dataset")
    schema = _required(params, "schema")
    symbols = list(_required(params, "symbols"))
    stype_in = _required(params, "stype_in")
    start = _required(params, "start")
    end = _required(params, "end")
    encoding = params.get("encoding", "dbn")
    compression = params.get("compression", "zstd")
    split_duration = params.get("split_duration", "day")

    steps = [
        # Symbology first: every later step quotes and submits against what this
        # resolves to, and an unresolvable symbol is the cheapest failure there
        # is.
        #
        # The window travels with it. A resolve without dates means "resolve
        # over the recent past", and the recent past is the one boundary a
        # subscription dataset's licence is guaranteed to be near: Databento
        # answers `422 dataset_unavailable_range` for a window ending today
        # when the entitlement ends yesterday evening, so a Job that resolved
        # without its own dates failed *before* quoting -- every time, for
        # exactly the datasets that need a licence. The driver already accepts
        # `start`/`end` and uses them when given; the binder was the layer
        # dropping them.
        {
            "kind": "resolve",
            "kwargs": {
                "dataset": dataset,
                "symbols": symbols,
                "stype_in": stype_in,
                "start": start,
                "end": end,
            },
        },
        # A quote step, emitted rather than performed. Nothing ever compares its
        # result to `estimate_usd` (§5.2): prices move, and a stored estimate
        # silently prices today's fetch at last month's rate.
        {
            "kind": "quote",
            "kwargs": {
                "dataset": dataset,
                "schema": schema,
                "symbols": symbols,
                "stype_in": stype_in,
                "start": start,
                "end": end,
            },
        },
        {
            "kind": "submit",
            "kwargs": {
                "dataset": dataset,
                "schema": schema,
                "symbols": symbols,
                "stype_in": stype_in,
                "start": start,
                "end": end,
                "encoding": encoding,
                "compression": compression,
                "split_duration": split_duration,
            },
        },
        {"kind": "poll", "kwargs": {"interval_s": POLL_INTERVAL_S}},
        {"kind": "download", "kwargs": {"dest": DEST}},
        # Never omitted as a no-op: the registry's encodings are dbn/csv/json and
        # the landing format is parquet, so the conversion always does something.
        # If a parquet encoding is ever added, this step must be *omitted* rather
        # than kept as a step that asserts nothing (§5.2).
        {"kind": "convert", "kwargs": {"from": encoding, "to": "parquet"}},
        {"kind": "verify", "kwargs": {"on_failure": verify["on_failure"]}},
    ]

    return {"plan": PLAN_NAME, "steps": steps}
