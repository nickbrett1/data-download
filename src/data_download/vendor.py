"""The one place a vendor client is constructed (phase 4, §5.3).

Everything in this repo until now was vendor-free: the binder emits a `quote`
step and never performs it, the interpreter takes a `Driver` it is given, and
`dryrun.py` supplies a fake. This module is the exception, and it is deliberately
the *only* one:

* **It is a read.** `metadata.get_cost` prices a window; it buys nothing and
  downloads nothing (§5.3). So a quote that cannot be obtained is a refused
  submit, not a failed request -- nothing has been created that would need
  undoing.
* **It never guesses.** A missing key, a Job with no `quote` step, a vendor
  exception, a zero or non-finite price: all of them are `QuoteUnavailable`.
  Zero is refused rather than accepted because "free" and "the vendor answered
  nothing" are the same number and only one of them is a fact.
* **The SDK is imported at call time**, not at import time, so the service boots
  without the package, without a key and without egress -- and says so at
  startup rather than at the first submit.

What this module is *not*: an estimator (§5.2 -- no price is verified), a
budget, or a second opinion about arithmetic. The number it returns is recorded
on the request, compared once to the ceiling the caller set, and never re-read
as evidence.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from typing import Any

log = logging.getLogger(__name__)

# The `quote` step's kwargs in, dollars out. The same shape as
# `interpreter.Driver.cost`, narrowed to one operation: a pricer answers one
# question, and an object that could also submit would be a spend surface
# pretending to be a lookup.
Pricer = Callable[[dict[str, Any]], float]

QUOTE_STEP = "quote"


class QuoteUnavailable(Exception):
    """No price could be obtained. Never a zero, never a guess (§5.2)."""


def quote_kwargs(job_text: str) -> dict[str, Any]:
    """The `quote` step's kwargs in a bound Job.

    Refuses a Job with no quote step rather than inventing the call: a Plan that
    did not emit a quote did not say what it would cost, and reconstructing one
    here would be this module authoring a Plan's vendor call.
    """
    try:
        job = json.loads(job_text)
    except (TypeError, ValueError) as exc:
        raise QuoteUnavailable(f"the stored Job is not JSON: {exc}") from None
    steps = job.get("steps") if isinstance(job, dict) else None
    for step in steps or []:
        if isinstance(step, dict) and step.get("kind") == QUOTE_STEP:
            kwargs = step.get("kwargs")
            if not isinstance(kwargs, dict):
                raise QuoteUnavailable("the Job's quote step carries no kwargs")
            return kwargs
    raise QuoteUnavailable("this Job has no quote step, so there is nothing to price")


def price(job_text: str, pricer: Pricer | None) -> float:
    """Price a bound Job, or raise `QuoteUnavailable`.

    `pricer is None` is the unconfigured deployment: no key, so no price, so no
    approvable request. The message names the variable, because "unpriced" is a
    repair the operator can make in one line and a mystery otherwise.
    """
    if pricer is None:
        raise QuoteUnavailable(
            "nothing can be priced: DATA_DOWNLOAD_DATABENTO_KEY is unset, and an "
            "unpriced request is not one anybody can approve honestly"
        )
    kwargs = quote_kwargs(job_text)
    try:
        estimate = float(pricer(kwargs))
    except QuoteUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001 - the vendor's own exception types
        raise QuoteUnavailable(f"the vendor did not return a price: {exc}") from exc
    if not math.isfinite(estimate) or estimate <= 0:
        raise QuoteUnavailable(
            f"the vendor returned an unusable price ({estimate!r}); a request is "
            "not priced at zero or at infinity"
        )
    return estimate


def databento_pricer(key: str) -> Pricer:
    """A pricer backed by Databento's `metadata.get_cost`.

    One client per process, constructed on the first quote. `Historical` is the
    only client this service ever builds, and it lives inside this closure --
    nothing else in the process can reach it (§5.3: untrusted code never gets a
    client, and neither does an unrelated module).
    """
    client: Any | None = None

    def cost(kwargs: dict[str, Any]) -> float:
        nonlocal client
        if client is None:
            import databento as db  # imported here: see the module docstring

            client = db.Historical(key)
        return float(client.metadata.get_cost(**kwargs))

    return cost
