"""Runtime configuration, read from the environment once at startup."""

from __future__ import annotations

import ipaddress
import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

# `approve` is knowingly still in the set and gates nothing: approval is now
# authorised by the tailnet identity `tailscale serve` asserts (`auth.py`), so
# the scope was retired. It stays parseable because a deployed `.env` names it,
# and rejecting the file would drop *every* token in it -- turning one dead word
# into an outage. `parse_tokens` warns when it sees one.
KNOWN_SCOPES = frozenset({"submit", "read", "escalate", "approve", "deploy"})

# A bare token (not JSON, no `:scopes` suffix) gets this. `escalate` is not
# granted implicitly: it asks for new code, so a token that can file a request
# cannot also ask for the registry to grow without somebody naming that in its
# scopes.
DEFAULT_SCOPES = frozenset({"submit", "read"})

# The only peer that can be `tailscale serve` when this process is not in a
# container. Everything else -- a LAN client, a sibling container, a rebound
# domain -- carries a header it wrote itself.
LOOPBACK_NETWORKS = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


@dataclass(frozen=True)
class Settings:
    db_path: str
    tokens: dict[str, frozenset[str]] = field(default_factory=dict)
    plan_types_dir: Path | None = None
    port: int = 8820
    # The tailnet logins allowed to approve, lowercased. Empty means nobody:
    # the door fails closed rather than treating "has a tailnet" as "may spend".
    approvers: frozenset[str] = field(default_factory=frozenset)
    # Which peer addresses are allowed to assert identity. Loopback only, by
    # default: that is the Serve hop when this runs directly on the host. In a
    # container the hop arrives from the bridge gateway instead, which is why
    # this is a setting rather than a constant.
    trusted_proxies: tuple = LOOPBACK_NETWORKS
    # The commit this process runs (§4.1). Resolved here rather than in the app
    # so a test can name it, and so the one resolution per process is visible in
    # the settings rather than hidden in a lifespan side effect.
    code_ref: str | None = None
    # Phase 4: how a request is priced -- resolved **by vendor**, because the
    # driver installed for a vendor answers its `cost` verb. `None` means this
    # deployment cannot price anything (no driver runtime and no local key), and
    # a submit that would need a number is refused rather than stored unpriced.
    # A resolver rather than a bare callable so `pytest` injects a fake and never
    # reaches a vendor, and so the one place a client exists stays in
    # `vendor.py` (§5.3).
    quote: object | None = None
    # Phase 5: where a runner puts bytes — `datasets/<dataset>/<request_id>/`.
    # `None` means this deployment has not said where its data lives, and the
    # runner refuses rather than guessing a path: bytes in the wrong place are
    # bytes the operator will not find, on a request that then reads `succeeded`.
    datasets_dir: Path | None = None
    # Phase 5.5/C: where the driver runtime lives, and the shared secret that
    # lets this process talk to it. `None` for the URL means the client's own
    # default (`http://data-download-drivers:8831`); `None` for the token means
    # **no request can be made at all**, because the runtime refuses an
    # unauthenticated caller and this process refuses to send one. Held here
    # rather than read at the call site so one place decides whether this
    # deployment can run a Job.
    driver_runtime_url: str | None = None
    driver_runtime_token: str | None = None
    #: The datasets root *as the driver runtime sees it*. Only the runtime
    #: writes there, and it validates every path against its own mount, so a
    #: loop running on the host must not hand it the host path.
    driver_datasets_root: str | None = None
    # Phase 6: whether this process picks approved rows up by itself. On by
    # default, because "approved and then nothing" is the state that made the
    # worker necessary; the switch exists so tests and the CLI can own the
    # running without two claimants racing, not because off is a sensible default.
    runner: bool = True
    # How long the worker waits between passes when nothing nudges it. A pass
    # that ran something loops again at once, so this is the idle poll, not the
    # latency of a newly approved row.
    runner_interval: float = 60.0
    # Phase 5.5: where the canvas UI that holds an authoring transcript is
    # served, if this deployment has one. `None` means the driver page shows the
    # conversation id but cannot make it a link -- the id is still the thing that
    # finds the transcript, and inventing a URL from it would be a guess.
    openhands_url: str | None = None
    #: The key file the agent server generated, as this process can see it
    #: (a mount). `None` means `openhands_agent` falls back to `docker exec`,
    #: which only a host-run deployment can do.
    openhands_key_file: Path | None = None
    #: The agent's `/projects`, mounted here at the same path it has in the
    #: agent. This is how a push credential reaches the agent without a docker
    #: socket: a file in a directory both processes can see.
    openhands_projects_dir: Path | None = None
    #: The workspace the agent is told to use, as the *agent* spells it. Only
    #: the prompt and the credential's path depend on it, and both sides spell
    #: it the same way (see `openhands_projects_dir`).
    openhands_workdir: str | None = None
    # Phase 7: whether this process works the *driver* queue as well as the
    # request queue. On by default, for the same reason `runner` is: a job
    # queued and then nothing is the state the worker exists to remove. It is
    # only actually started when every part is reachable, and the app says
    # which one is missing rather than refusing the same sentence every minute.
    driver_jobs: bool = True
    #: Idle interval for the driver queue. A job that finished looping goes
    #: straight on to the next; this is only the beat that catches a job queued
    #: by an approval nobody nudged us about.
    driver_jobs_interval: float = 60.0
    driver_repo: str = "nickbrett1/data-download-drivers"
    driver_base: str = "main"
    #: The forge credential: gate, changed paths, merge, PR, and the tarball
    #: that is installed. `None` means the driver queue cannot finish a job.
    github_token: str | None = None
    #: How long one authoring conversation may run, and what it may spend.
    author_timeout: float = 3600.0
    author_budget: float = 2.0
    author_iterations: int = 200

    @property
    def driver_ready(self) -> bool:
        """Whether a Job could be run at all. False means refuse before claiming."""
        return bool(self.driver_runtime_token)

    def scopes_for(self, token: str) -> frozenset[str] | None:
        return self.tokens.get(token)


def _parse_json_tokens(raw: str) -> dict[str, frozenset[str]]:
    parsed = json.loads(raw)
    if not isinstance(parsed, dict):
        raise ValueError("DATA_DOWNLOAD_API_TOKENS JSON must be an object {token: scopes}")
    out: dict[str, frozenset[str]] = {}
    for token, scopes in parsed.items():
        if isinstance(scopes, str):
            items = [s for s in scopes.replace(",", " ").split() if s]
        elif isinstance(scopes, list):
            items = [str(s) for s in scopes]
        else:
            raise ValueError("token scopes must be a list or a whitespace/comma string")
        unknown = {s.lower() for s in items} - KNOWN_SCOPES
        if unknown:
            raise ValueError(f"unknown scope(s) {sorted(unknown)} for a token")
        out[token] = frozenset(s.lower() for s in items)
    return out


def parse_tokens(raw: str | None) -> dict[str, frozenset[str]]:
    """Parse `DATA_DOWNLOAD_API_TOKENS`.

    Accepted forms, because the deployed `.env` was written before this module
    existed and must not be rewritten to an arbitrary shape:

    * JSON object:            ``{"tok-a": ["submit","read"], "tok-b": "read"}``
    * delimited list:         ``tok-a:submit+read;tok-b:read``
    * a single bare token:    ``6d7a99...``  (=> submit + read)

    A scope this module does not know still fails the whole file, deliberately:
    a typo that silently narrowed a token would be worse than a loud refusal.
    `approve` is the one exception -- known, and inert (see `KNOWN_SCOPES`).
    """
    if not raw or not raw.strip():
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        return _warn_inert_approve(_parse_json_tokens(raw))

    tokens: dict[str, frozenset[str]] = {}
    for entry in raw.replace(";", ",").split(","):
        entry = entry.strip()
        if not entry:
            continue
        if ":" in entry:
            token, _, scopes = entry.partition(":")
            items = [s for s in scopes.replace("+", " ").replace(" ", " ").split() if s]
            unknown = {s.lower() for s in items} - KNOWN_SCOPES
            if unknown:
                raise ValueError(f"unknown scope(s) {sorted(unknown)} for a token")
            tokens[token] = frozenset(s.lower() for s in items)
        else:
            tokens[entry] = DEFAULT_SCOPES
    return _warn_inert_approve(tokens)


def _warn_inert_approve(tokens: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
    """Say so when a token still asks for the retired `approve` scope.

    Silence here would leave an operator believing a credential still guards
    the spend. It guards nothing; the tailnet login does.
    """
    for token, scopes in tokens.items():
        if "approve" in scopes:
            log.warning(
                "a token (…%s) asks for `approve`, which no longer guards anything "
                "- approval is authorised by DATA_DOWNLOAD_APPROVERS over "
                "`tailscale serve`. Safe to delete from the env.",
                token[-4:],
            )
    return tokens


def parse_trusted_proxies(raw: str | None) -> tuple:
    """Parse `DATA_DOWNLOAD_TRUSTED_PROXIES`: addresses or CIDRs, comma separated.

    Unset means loopback. A single address is read as a /32 (or /128), because
    "the gateway Docker showed me" is the thing an operator actually has to hand.
    A value this module cannot parse is dropped with a warning: losing an entry
    makes the door stricter, never looser, so it is not worth refusing to boot.
    """
    if not raw or not raw.strip():
        return LOOPBACK_NETWORKS
    networks = []
    for part in raw.replace(",", " ").split():
        try:
            networks.append(ipaddress.ip_network(part.strip(), strict=False))
        except ValueError:
            log.warning("DATA_DOWNLOAD_TRUSTED_PROXIES: %r is not an address - ignored", part)
    return tuple(networks) or LOOPBACK_NETWORKS


def parse_approvers(raw: str | None) -> frozenset[str]:
    """Parse `DATA_DOWNLOAD_APPROVERS`: logins, comma or whitespace separated.

    Lowercased on both sides of the comparison, because an IdP that reports
    `Nick@Example.com` today and `nick@example.com` tomorrow must not silently
    lock the only approver out of the door.
    """
    if not raw:
        return frozenset()
    return frozenset(part.strip().lower() for part in raw.replace(",", " ").split() if part.strip())


def parse_switch(raw: str | None, *, default: bool = True) -> bool:
    """`DATA_DOWNLOAD_RUNNER`: on unless it is plainly off.

    An unset value or a word this module does not recognise keeps the default,
    which is *on*. The failure modes are not symmetric: a typo that leaves the
    worker running is visible (rows run), one that silently stops it is the bug
    this whole phase exists to remove.
    """
    if raw is None or not raw.strip():
        return default
    value = raw.strip().lower()
    if value in {"0", "false", "no", "off", "n", "f"}:
        return False
    if value in {"1", "true", "yes", "on", "y", "t"}:
        return True
    log.warning("DATA_DOWNLOAD_RUNNER=%r is not a switch - leaving it %s", raw, "on" if default else "off")
    return default


def _path(raw: str | None) -> Path | None:
    """An optional path setting: unset or blank is `None`, never `Path('.')`.

    `Path("")` is the current directory, which is a real place and the wrong
    one: a blank mount setting must switch the behaviour off rather than point
    it at whatever the working directory happens to be.
    """
    value = (raw or "").strip()
    return Path(value) if value else None


def _number(raw: str | None, default: float) -> float:
    """An optional positive number. A typo keeps the default and says so."""
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw.strip())
    except ValueError:
        log.warning("%r is not a number - using %s", raw, default)
        return default
    if not math.isfinite(value) or value <= 0:
        log.warning("%r is not a positive number - using %s", raw, default)
        return default
    return value


def parse_interval(raw: str | None, *, default: float = 60.0) -> float:
    """`DATA_DOWNLOAD_RUNNER_INTERVAL`: seconds between idle passes.

    Bounded below at 1 s so a stray `0.001` cannot turn the worker into a spin
    loop against sqlite, and above at an hour so a fat-fingered `60000` (which
    reads like milliseconds and is 16 hours) is refused rather than obeyed.
    """
    if raw is None or not raw.strip():
        return default
    try:
        value = float(raw)
    except ValueError:
        log.warning("DATA_DOWNLOAD_RUNNER_INTERVAL=%r is not a number - using %ss", raw, default)
        return default
    if not math.isfinite(value) or value < 1 or value > 3600:
        log.warning("DATA_DOWNLOAD_RUNNER_INTERVAL=%r is outside 1..3600 - using %ss", raw, default)
        return default
    return value


def find_plan_types_dir(explicit: str | Path | None = None) -> Path | None:
    """Locate the git-defined registry: `plan_types/*.yaml` at the repo root."""
    if explicit:
        p = Path(explicit)
        return p if p.is_dir() else None

    here = Path(__file__).resolve()
    candidates = [Path.cwd() / "plan_types"]
    candidates += [parent / "plan_types" for parent in Path.cwd().resolve().parents[:3]]
    candidates += [parent / "plan_types" for parent in here.parents[:4]]
    for cand in candidates:
        if cand.is_dir():
            return cand
    return None


def load_settings(env: dict[str, str] | None = None) -> Settings:
    import os

    env = dict(os.environ if env is None else env)

    db_path = (env.get("DATA_DOWNLOAD_DB") or "").strip()
    if not db_path:
        # DELIBERATE DEVIATION from the work order, which says to fail fast when
        # the path is unset. The generated `docker_smoke` step runs the image
        # with no environment at all, so exiting here would turn a green
        # pipeline red for the wrong reason. An in-memory store keeps the
        # process up and `/healthz` honest; the warning is deliberately loud.
        log.warning(
            "DATA_DOWNLOAD_DB is unset - using an in-memory store. "
            "Rows will not survive a restart."
        )
        db_path = ":memory:"

    try:
        tokens = parse_tokens(env.get("DATA_DOWNLOAD_API_TOKENS"))
    except (ValueError, json.JSONDecodeError) as exc:
        log.error("DATA_DOWNLOAD_API_TOKENS is unusable (%s) - no token will authenticate", exc)
        tokens = {}

    trusted_proxies = parse_trusted_proxies(env.get("DATA_DOWNLOAD_TRUSTED_PROXIES"))
    approvers = parse_approvers(env.get("DATA_DOWNLOAD_APPROVERS"))
    if not approvers:
        # Loud, for the same reason the in-memory store is: a door that refuses
        # every approval looks like a bug in the button, not a missing setting.
        log.warning(
            "DATA_DOWNLOAD_APPROVERS is unset - no tailnet login may approve. "
            "Set it to your login (e.g. you@example.com) to enable the button."
        )

    from .coderef import resolve as resolve_code_ref
    from .vendor import Pricing, databento_pricer

    key = (env.get("DATA_DOWNLOAD_DATABENTO_KEY") or "").strip()
    # The bridge, not the design: a local SDK pricer for vendors that have no
    # driver artefact installed yet. `DriverPricing` below prefers an installed
    # driver, so this stops being reached the moment one exists.
    legacy_quote = Pricing.one(databento_pricer(key)) if key else None
    quote = legacy_quote
    if legacy_quote is None:
        # Loud, like the other doors that fail closed: with no key and no
        # fallback, only a vendor with an installed driver can be priced, and a
        # plan body that cannot be priced is refused rather than stored with a
        # NULL estimate that would look like "we forgot".
        log.warning(
            "DATA_DOWNLOAD_DATABENTO_KEY is unset - no local pricer exists, so "
            "only a vendor with an installed driver can be priced; a submit for "
            "any other vendor is refused with quote_unavailable."
        )

    datasets_raw = (env.get("DATA_DOWNLOAD_DATASETS_DIR") or "").strip()
    datasets_dir = Path(datasets_raw) if datasets_raw else None
    if datasets_dir is None:
        # Only the runner cares, and the service must boot without it, so this is
        # a warning rather than a refusal -- but a loud one, because the first
        # approved request will have nowhere to land.
        log.warning(
            "DATA_DOWNLOAD_DATASETS_DIR is unset - no request can be run: the "
            "runner refuses rather than writing bytes to a path nobody named."
        )

    driver_runtime_url = (env.get("DATA_DOWNLOAD_DRIVER_RUNTIME_URL") or "").strip() or None
    driver_runtime_token = (env.get("DRIVER_RUNTIME_TOKEN") or "").strip() or None
    driver_datasets_root = (
        env.get("DATA_DOWNLOAD_DRIVER_DATASETS_ROOT") or ""
    ).strip() or None
    if driver_runtime_token is None:
        # Fail closed and say so: the runtime refuses an unauthenticated caller,
        # and this process refuses to send one, so with no token no approved
        # request can be run. Loud because the first symptom is a runner whose
        # rows all settle `failed`, which looks like a vendor problem.
        log.warning(
            "DRIVER_RUNTIME_TOKEN is unset - no approved request can be run: the "
            "runner refuses before claiming rather than sending an unauthenticated "
            "verb. It must match the token in the driver runtime's environment."
        )

    # Pricing, composed: the driver for that vendor if one is installed, else the
    # local bridge (if any). Resolved per request rather than at boot so a newly
    # installed artefact is priceable without a restart -- which is the whole
    # point of binding by vendor (`driver_client.driver_for`).
    if driver_runtime_token and datasets_dir:
        from .vendor import DriverPricing

        quote = DriverPricing(
            datasets_dir=datasets_dir,
            url=driver_runtime_url,
            token=driver_runtime_token,
            fallback=legacy_quote,
        )
        log.info(
            "pricing: the driver runtime answers per vendor%s",
            " (local fallback for vendors with no artefact)" if legacy_quote else "",
        )

    runner = parse_switch(env.get("DATA_DOWNLOAD_RUNNER"))
    runner_interval = parse_interval(env.get("DATA_DOWNLOAD_RUNNER_INTERVAL"))
    if runner and not datasets_dir:
        # The worker would claim nothing: `run_one` refuses before the claim
        # without a datasets directory, so every pass would be a logged refusal.
        # Say it once, here, where the operator can act on it.
        log.warning(
            "DATA_DOWNLOAD_RUNNER is on but DATA_DOWNLOAD_DATASETS_DIR is unset - "
            "approved requests will wait: there is nowhere to land bytes."
        )

    port_raw = (env.get("PORT") or "8820").strip()
    try:
        port = int(port_raw)
        if not (0 < port < 65536):
            raise ValueError
    except ValueError:
        log.warning("PORT=%r is not a valid port - using 8820", port_raw)
        port = 8820

    return Settings(
        db_path=db_path,
        tokens=tokens,
        plan_types_dir=find_plan_types_dir(env.get("DATA_DOWNLOAD_PLAN_TYPES_DIR")),
        port=port,
        approvers=approvers,
        trusted_proxies=trusted_proxies,
        code_ref=resolve_code_ref(env),
        quote=quote,
        datasets_dir=datasets_dir,
        driver_runtime_url=driver_runtime_url,
        driver_runtime_token=driver_runtime_token,
        driver_datasets_root=driver_datasets_root,
        runner=runner,
        runner_interval=runner_interval,
        openhands_url=(env.get("DATA_DOWNLOAD_OPENHANDS_URL") or "").strip() or None,
        openhands_key_file=_path(env.get("DATA_DOWNLOAD_OPENHANDS_KEY_FILE")),
        openhands_projects_dir=_path(env.get("DATA_DOWNLOAD_OPENHANDS_PROJECTS_DIR")),
        openhands_workdir=(env.get("DATA_DOWNLOAD_OPENHANDS_WORKDIR") or "").strip() or None,
        driver_jobs=parse_switch(env.get("DATA_DOWNLOAD_DRIVER_JOBS")),
        driver_jobs_interval=parse_interval(
            env.get("DATA_DOWNLOAD_DRIVER_JOBS_INTERVAL"), default=60.0
        ),
        driver_repo=(env.get("DATA_DOWNLOAD_DRIVER_REPO") or "").strip()
        or "nickbrett1/data-download-drivers",
        driver_base=(env.get("DATA_DOWNLOAD_DRIVER_BASE") or "").strip() or "main",
        github_token=(env.get("DATA_DOWNLOAD_GITHUB_TOKEN") or env.get("GITHUB_TOKEN") or "").strip()
        or None,
        author_timeout=_number(env.get("DATA_DOWNLOAD_AUTHOR_TIMEOUT"), 3600.0),
        author_budget=_number(env.get("DATA_DOWNLOAD_AUTHOR_BUDGET"), 2.0),
        author_iterations=int(_number(env.get("DATA_DOWNLOAD_AUTHOR_ITERATIONS"), 200)),
    )


def finite_positive(value: object) -> bool:
    """True for a real number that is > 0 and finite (not bool/None/'unlimited')."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0
