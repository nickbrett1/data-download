"""Pricing: the one question this service asks a vendor, and who answers it.

Everything else here is vendor-free: the binder emits a `quote` step and never
performs it, the interpreter takes a `Driver` it is given, and `dryrun.py`
supplies a fake. Pricing is the exception, because a request must carry a number
before anyone can honestly approve it (§5.2), and at that moment there is no
Attempt, no claim and no Job in flight.

Two answers, in order of preference:

* **The driver for that vendor** (`DriverPricing`). `cost` is one of the eight
  verbs, so the entity that knows what a vendor charges is the driver installed
  for it -- in the isolated container, at the version an operator installed.
  This service then needs no SDK and no vendor key, and a new vendor is priceable
  the moment its driver is, with no change here.
* **A local pricer** (`databento_pricer`), while a vendor has no artefact yet.
  It is the bridge, not the design: the SDK is imported at call time, not at
  import time, so the service boots without the package, without a key and
  without egress. Install the driver and this stops being used.

What survives either way:

* **It is a read.** A price buys nothing and downloads nothing. So a quote that
  cannot be obtained is a refused *submit*, not a failed request -- nothing has
  been created that would need undoing.
* **It never guesses.** No pricer, a Job with no `quote` step, a vendor
  exception, a zero or non-finite price: all of them are `QuoteUnavailable`.
  Zero is refused rather than accepted because "free" and "the vendor answered
  nothing" are the same number and only one of them is a fact.

What this module is *not*: an estimator (§5.2 -- no price is verified), a budget,
or a second opinion about arithmetic. The number it returns is recorded on the
request, compared once to the ceiling the caller set, and never re-read as
evidence.
"""

from __future__ import annotations

import json
import logging
import math
from collections.abc import Callable
from pathlib import Path
from typing import Any

from . import driver_client

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


class Pricing:
    """Which pricer answers for which vendor (§5.3, revised).

    A bare `Pricer` is a **one-vendor** assumption: `price()` would take a single
    callable, so a second vendor could only be priced by a second deployment.
    Resolving by vendor is what lets each vendor's own driver answer the quote --
    the same `cost` verb the interpreter already calls at run time
    (`interpreter.Driver.cost`), which is the point: the entity that knows what a
    vendor charges is the driver for that vendor, not this service.

    `resolve` is a function rather than a dict because a driver-backed pricer
    must look at what is *installed now*; a boot-time dict would freeze the
    answer and put adding a vendor back behind a redeploy.
    """

    __slots__ = ("_resolve",)

    def __init__(self, resolve: Callable[[str], Pricer | None]) -> None:
        self._resolve = resolve

    def for_vendor(self, vendor: str | None) -> Pricer | None:
        return self._resolve(" ".join(str(vendor or "").split()))

    @classmethod
    def one(cls, pricer: Pricer) -> Pricing:
        """The same pricer for every vendor -- what a single-vendor host is."""
        return cls(lambda _vendor: pricer)


def _fallback_of(fallback: Pricing | Pricer | None, vendor: str) -> Pricer | None:
    if fallback is None:
        return None
    if isinstance(fallback, Pricing):
        return fallback.for_vendor(vendor)
    return fallback


class DriverPricing(Pricing):
    """Price any vendor whose driver is installed, by asking the driver.

    `cost` is a read (§5.3): it buys nothing and downloads nothing, which is why
    a refused quote is a refused *submit* rather than a failed request. So the
    session this opens is throwaway -- opened for one verb and closed in a
    `finally`, because a leaked session holds a child process in the other
    container until its deadline.

    The root is `<datasets_dir>/_quote`, which does not exist and is never
    created: the runtime only checks that a session's root lies under its own
    dataset store, and a price writes nothing, so a directory nobody asked for
    is not created for it.

    `fallback` is the bridge, and it should be short-lived: while a vendor has
    no installed artefact, a local pricer (today's Databento SDK) may still
    answer for it. An installed driver always wins, so installing the artefact
    is how the fallback stops being used.
    """

    def __init__(
        self,
        *,
        datasets_dir: Path | str,
        url: str | None = None,
        token: str | None = None,
        fallback: Pricing | Pricer | None = None,
    ) -> None:
        self.datasets_dir = Path(datasets_dir)
        self.url = url
        self.token = token
        self.fallback = fallback

    def for_vendor(self, vendor: str | None) -> Pricer | None:
        wanted = " ".join(str(vendor or "").split())
        if not wanted:
            # An unclaimed dataset has no vendor, so no artefact can speak for
            # it: naming the install would be inventing a vendor.
            return _fallback_of(self.fallback, wanted)
        try:
            entries = driver_client.installed(url=self.url, token=self.token)
        except driver_client.DriverUnavailable as exc:
            log.warning(
                "cannot price %s from its driver (%s); falling back", wanted, exc
            )
            return _fallback_of(self.fallback, wanted)
        artifact = driver_client.driver_for(wanted, artifacts=entries)
        if artifact is None:
            log.warning(
                "no driver is installed for vendor %r; falling back", wanted
            )
            return _fallback_of(self.fallback, wanted)
        return _DriverCost(self, artifact)


class _DriverCost:
    """One `cost` call, over the driver's own session. Never reused across verbs."""

    def __init__(self, pricing: DriverPricing, artifact: Any) -> None:
        self.pricing = pricing
        self.artifact = artifact

    def __call__(self, kwargs: dict[str, Any]) -> float:
        root = self.pricing.datasets_dir / "_quote"
        driver = driver_client.RemoteDriver(
            root,
            "clean",
            url=self.pricing.url,
            token=self.pricing.token,
            driver=self.artifact.driver,
            driver_version=self.artifact.version,
        )
        try:
            return driver.cost(**kwargs)
        except driver_client.DriverUnavailable as exc:
            raise QuoteUnavailable(
                f"{self.artifact.vendor}: the driver runtime is unreachable ({exc})"
            ) from exc
        finally:
            driver.close()


def price(
    job_text: str, pricer: Pricing | Pricer | None, vendor: str | None = None
) -> float:
    """Price a bound Job for `vendor`, or raise `QuoteUnavailable`.

    `pricer is None` is the unconfigured deployment: nothing can quote, so an
    unpriced request is not one anybody can approve honestly. The message names
    both repairs, because "unpriced" is a one-line fix for the operator and a
    mystery otherwise.
    """
    if pricer is None:
        raise QuoteUnavailable(
            "nothing can be priced: install a driver for the vendor, or set "
            "DATA_DOWNLOAD_DATABENTO_KEY, and an unpriced request is not one "
            "anybody can approve honestly"
        )
    resolved = pricer.for_vendor(vendor) if isinstance(pricer, Pricing) else pricer
    if resolved is None:
        raise QuoteUnavailable(
            f"nothing can price vendor {vendor!r}: no driver is installed for it "
            "on the driver runtime, and no fallback pricer answers for it"
        )
    kwargs = quote_kwargs(job_text)
    try:
        estimate = float(resolved(kwargs))
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
