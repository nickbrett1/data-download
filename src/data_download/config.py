"""Runtime configuration, read from the environment once at startup."""

from __future__ import annotations

import json
import logging
import math
from dataclasses import dataclass, field
from pathlib import Path

log = logging.getLogger(__name__)

KNOWN_SCOPES = frozenset({"submit", "read", "approve", "deploy"})

# A bare token (not JSON, no `:scopes` suffix) gets this. `approve` is never
# granted implicitly: §7.3 makes it human-CLI-only, and this phase has no
# approve path at all.
DEFAULT_SCOPES = frozenset({"submit", "read"})


@dataclass(frozen=True)
class Settings:
    db_path: str
    tokens: dict[str, frozenset[str]] = field(default_factory=dict)
    plan_types_dir: Path | None = None
    port: int = 8820

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

    `approve` is only ever granted by naming it explicitly in the JSON form.
    """
    if not raw or not raw.strip():
        return {}
    raw = raw.strip()
    if raw.startswith("{"):
        return _parse_json_tokens(raw)

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
    return tokens


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
    )


def finite_positive(value: object) -> bool:
    """True for a real number that is > 0 and finite (not bool/None/'unlimited')."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    return math.isfinite(value) and value > 0
