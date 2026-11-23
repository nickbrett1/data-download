"""`interpreter.Driver` over the wire: eight verbs, one session, no retries.

Phase 5 could hand `interpreter.run` a driver that lived in this process. The
split moves the driver to its own container, and this module is the whole cost
of that move: a proxy that implements the same eight methods by POSTing them.

Three things are deliberate:

1. **The interpreter does not change.** It takes a `Driver` and calls verbs.
   `RemoteDriver` is a `Driver`; `interpreter.py` was not touched to make this
   work, which is the proof the seam was already in the right place.

2. **A session is opened, used, and closed.** The wire is stateful -- `poll`
   needs the vendor job `submit` created -- so the eight verbs share a session
   that the runtime holds open as a child process. `RemoteDriver` opens it on
   the first verb and closes it when told; `with` does both. A driver that is
   never closed leaks a process in the other container, so the runner closes it
   in a `finally`.

3. **Nothing is retried, here or below.** `submit` is not idempotent against a
   vendor, and a retry that duplicates a purchase is worse than a failure the
   caller can see. `runtime_http.request` makes one attempt and this module
   turns a non-2xx into `InterpreterError` -- the interpreter's existing
   vocabulary for "the Job did not get that far" -- so `runner.run_one` needs no
   new branch.

4. **Discovery is a route, not a directory read.** What is installed is the
   runtime's fact, because the artefacts live in *its* filesystem. The app does
   not mount that directory -- mounting it would give the trusted half a second
   opinion about what is installed, and opinions about installed code are how a
   half-copied artefact gets run. `installed()` asks `GET /v1/drivers` and
   `driver_for(vendor)` picks the newest artefact that vendor declares.

The deadline table duplicates `wire.DEADLINES` in `data-download-drivers` on
purpose: the client's patience must not be dictated by the thing it is waiting
for. The conformance suite (phase D) asserts the two tables agree, so the copy
cannot drift silently.

Agreeing is not the same as being equal, and `CLIENT_GRACE_S` is the difference.
The runtime enforces its deadline and answers `timeout`; the client waits
*longer* so that answer can arrive. With both at the same number the client
wins the race half the time and records `TimeoutError: timed out` -- a local
socket error that says nothing about which verb was too slow, instead of the
runtime's named refusal. Seen live, 2026-11-23.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import interpreter, runtime_http

#: The wire's contract version. An integer, and it is *not*
#: `interpreter.INTERPRETER_VERSION`: which transport carried the verbs and which
#: flow ran are different facts.
PROTOCOL_VERSION = 1

#: Per-verb deadlines, in seconds. Mirrors `wire.DEADLINES`.
DEADLINES: dict[str, float] = {
    "resolve": 30,
    # Pricing scans the window server-side: Databento measured 0.1-28 s for one
    # day of ES trades (2026-11-23), and a price is free and idempotent, so the
    # patience to let a slow quote finish costs nothing. Both tables carry this.
    "cost": 120,
    "submit": 60,
    "poll": 30,
    "list_files": 120,
    "fetch": 300,
    "convert": 300,
    "verify": 120,
}

#: How much longer than the runtime's own deadline this client waits. The
#: runtime is the one that decides a verb is too slow; this only has to outlast
#: that decision, so that its answer -- `timeout: poll exceeded 60s` -- is what
#: reaches the operator rather than a bare socket timeout.
CLIENT_GRACE_S = 15.0

def budget(verb: str) -> float:
    """How long this client waits for `verb`: the runtime's deadline, plus grace."""
    return DEADLINES.get(verb, DEFAULT_TIMEOUT) + CLIENT_GRACE_S


DEFAULT_PORT = 8831
DEFAULT_URL = f"http://data-download-drivers:{DEFAULT_PORT}"
DEFAULT_TIMEOUT = 30.0


class DriverUnavailable(interpreter.InterpreterError):
    """No session could be opened: no runtime, no credential, no such driver.

    An `InterpreterError` subclass so the runner settles `failed` with a reason
    instead of crashing -- the Job did not get as far as a verdict, which is
    exactly what that exception means.
    """


@dataclass(frozen=True)
class Artifact:
    """One installed driver, as the runtime describes it in `GET /v1/drivers`."""

    driver: str
    version: str
    vendor: str = ""
    protocol: int = PROTOCOL_VERSION

    def __str__(self) -> str:
        return f"{self.driver} v{self.version}" + (f" ({self.vendor})" if self.vendor else "")


def _endpoint(url: str | None) -> tuple[str, int]:
    """`http://host:port` (or `host:port`) -> `(host, port)`.

    A scheme other than http is refused: the hop is a compose network, so TLS
    here would be decoration. Module-level so discovery and the session verbs
    cannot disagree about how a URL is read.
    """
    text = url or os.environ.get("DATA_DOWNLOAD_DRIVER_RUNTIME_URL") or DEFAULT_URL
    parts = urlsplit(text if "//" in text else f"//{text}")
    if parts.scheme not in ("http", ""):
        raise DriverUnavailable(f"driver runtime url {url!r} must be http, not {parts.scheme}")
    if not parts.hostname:
        raise DriverUnavailable(f"driver runtime url {url!r} names no host")
    return parts.hostname, parts.port or DEFAULT_PORT


def _token(token: str | None) -> str:
    """The bearer token, resolved once. Empty is left empty so `runtime_http`
    refuses it *before* a socket -- the failure names the setting instead of
    turning into a 401 that reads like a wrong token."""
    return token if token is not None else (os.environ.get("DRIVER_RUNTIME_TOKEN") or "")


def installed(
    *,
    url: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
) -> list[Artifact]:
    """Every driver the runtime has installed. Read-only; creates no session.

    Raises `DriverUnavailable` when there is no conversation to have (no token,
    unreachable runtime, a protocol version this client does not speak). An
    *empty list* is not an error: a healthy runtime with nothing installed is a
    real state, and the caller can tell the operator what to install.
    """
    host, port = _endpoint(url)
    try:
        status, payload = runtime_http.request(
            host,
            port,
            "GET",
            "/v1/drivers",
            token=_token(token),
            timeout=timeout or DEFAULT_TIMEOUT,
        )
    except runtime_http.RuntimeUnreachable as exc:
        raise DriverUnavailable(f"could not list the drivers: {exc}") from exc

    if not 200 <= status < 300:
        error = payload.get("error") if isinstance(payload, dict) else {}
        code = (error or {}).get("code") or "unknown"
        detail = (error or {}).get("detail") or ""
        raise DriverUnavailable(
            f"could not list the drivers: {status} {code}" + (f" ({detail})" if detail else "")
        )

    protocol = payload.get("protocol")
    if protocol is not None and protocol != PROTOCOL_VERSION:
        # Refuse rather than guess: a runtime speaking version 2 may describe
        # artefacts differently, and half-understanding discovery is how a
        # driver gets addressed by fields that no longer mean what they meant.
        raise DriverUnavailable(
            f"the driver runtime speaks protocol {protocol}, this client speaks "
            f"{PROTOCOL_VERSION}; upgrade whichever is older"
        )

    out: list[Artifact] = []
    for entry in payload.get("drivers") or []:
        if not isinstance(entry, dict) or not entry.get("name"):
            continue
        out.append(
            Artifact(
                driver=str(entry["name"]),
                version=str(entry.get("version") or "1"),
                vendor=str(entry.get("vendor") or ""),
                protocol=int(entry.get("protocol") or PROTOCOL_VERSION),
            )
        )
    return out


def _version_key(version: str) -> tuple:
    """`1.2.0` sorts above `1`; anything unparseable sorts below numbers.

    Directory names are the runtime's to choose, so this must not raise on a
    string it does not recognise -- a newer artefact with an odd version is
    still the one to run, and refusing to pick would make an install invisible.
    """
    parts: list[tuple[int, int, str]] = []
    for chunk in str(version).replace("-", ".").split("."):
        if chunk.isdigit():
            parts.append((1, int(chunk), ""))
        else:
            parts.append((0, 0, chunk))
    return tuple(parts)


def driver_for(
    vendor: str,
    *,
    url: str | None = None,
    token: str | None = None,
    timeout: float | None = None,
    artifacts: list[Artifact] | None = None,
) -> Artifact | None:
    """The newest installed driver that declares `vendor`, or `None`.

    Binding is by **vendor**, not by plan type: `plan_types/*.yaml` already says
    who sells the data, so an install needs no entry in this repo and no merge.
    A vendor nobody has installed a driver for is `None` -- a refusal the caller
    turns into a sentence, not an exception thrown across the boundary.
    """
    entries = installed(url=url, token=token, timeout=timeout) if artifacts is None else artifacts
    wanted = " ".join(str(vendor or "").split()).lower()
    if not wanted:
        return None
    matches = [
        entry for entry in entries if " ".join(entry.vendor.split()).lower() == wanted
    ]
    if not matches:
        return None
    return max(matches, key=lambda entry: _version_key(entry.version))


class RemoteDriver:
    """`interpreter.Driver`, sent over HTTP to the driver-runtime container.

    `root` is the request directory this Job is allowed to write into. Both
    containers mount the dataset store at the **same absolute path** (the spec's
    one deployment invariant), so the path is passed through rather than
    translated -- and the runtime re-checks that it lies under its own
    `datasets_dir`, so a wrong mount is a `400` rather than bytes in the wrong
    place.
    """

    def __init__(
        self,
        root: Any,
        mode: str = "clean",
        *,
        url: str | None = None,
        token: str | None = None,
        driver: str = "stub",
        driver_version: str = "1",
        protocol: int = PROTOCOL_VERSION,
        timeout: float | None = None,
    ) -> None:
        self.root = str(root)
        self.mode = mode
        self.driver = driver
        self.driver_version = driver_version
        self.protocol = protocol
        self._url = url or os.environ.get("DATA_DOWNLOAD_DRIVER_RUNTIME_URL") or DEFAULT_URL
        # The credential is read here but *not* defaulted to "": an unset token
        # must fail at the first verb with a sentence naming the setting.
        self._token = token if token is not None else os.environ.get("DRIVER_RUNTIME_TOKEN")
        self._timeout = timeout
        self._host, self._port = self._split(self._url)
        self._session_id: str | None = None
        self.session_driver_version: str | None = None
        self.calls: list[str] = []

    # -- transport -----------------------------------------------------------

    @staticmethod
    def _split(url: str) -> tuple[str, int]:
        """`http://host:port` -> `(host, port)`. The same reading discovery uses."""
        return _endpoint(url)

    def _post(
        self, verb: str, body: dict[str, Any], timeout: float | None = None
    ) -> dict[str, Any]:
        """POST one verb and return its body, or raise on a non-2xx.

        The error code the runtime chose (`driver_missing`, `budget_refused`,
        `timeout`, ...) goes into the message verbatim. Inventing our own words
        for the runtime's refusals is how two vocabularies start.
        """
        self.calls.append(verb)
        if timeout is not None:
            limit = timeout
        elif self._timeout:
            limit = self._timeout
        else:
            limit = budget(verb)
        try:
            status, payload = runtime_http.request(
                self._host,
                self._port,
                "POST",
                f"/v1/{verb}",
                token=self._token or "",
                body=body,
                timeout=limit,
            )
        except runtime_http.RuntimeUnreachable as exc:
            raise DriverUnavailable(f"{verb}: {exc}") from exc

        if 200 <= status < 300:
            return payload
        error = payload.get("error") if isinstance(payload, dict) else None
        code = (error or {}).get("code") if isinstance(error, dict) else None
        detail = (error or {}).get("detail") if isinstance(error, dict) else None
        raise interpreter.InterpreterError(
            f"{verb} refused: {status} {code or 'unknown'}"
            + (f" ({detail})" if detail else "")
        )

    # -- the session ---------------------------------------------------------

    def open(self) -> str:
        """Create the session the eight verbs share. Idempotent."""
        if self._session_id is not None:
            return self._session_id
        try:
            status, payload = runtime_http.request(
                self._host,
                self._port,
                "POST",
                "/v1/sessions",
                token=self._token or "",
                body={
                    "protocol": self.protocol,
                    "driver": self.driver,
                    "driver_version": self.driver_version,
                    "root": self.root,
                    "driver_options": {"mode": self.mode},
                },
                timeout=self._timeout or DEFAULT_TIMEOUT,
            )
        except runtime_http.RuntimeUnreachable as exc:
            raise DriverUnavailable(f"could not open a driver session: {exc}") from exc

        if not 200 <= status < 300:
            error = payload.get("error") if isinstance(payload, dict) else {}
            code = (error or {}).get("code") or "unknown"
            detail = (error or {}).get("detail") or ""
            raise DriverUnavailable(
                f"could not open a driver session: {status} {code}"
                + (f" ({detail})" if detail else "")
                + f"; is driver {self.driver!r} v{self.driver_version} installed?"
            )

        session_id = payload.get("session_id")
        if not session_id:
            raise DriverUnavailable("the runtime opened a session but returned no session_id")
        self._session_id = str(session_id)
        self.session_driver_version = payload.get("driver_version")
        return self._session_id

    def close(self) -> None:
        """Kill the session. Never raises: a failed close must not mask a verdict."""
        session_id = self._session_id
        self._session_id = None
        if session_id is None:
            return
        try:
            runtime_http.request(
                self._host,
                self._port,
                "DELETE",
                f"/v1/sessions/{session_id}",
                token=self._token or "",
                timeout=self._timeout or DEFAULT_TIMEOUT,
            )
        except Exception:  # noqa: BLE001 - see docstring
            pass

    def __enter__(self) -> RemoteDriver:
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- the eight verbs (interpreter.Driver) --------------------------------

    def _verb(self, verb: str, **kwargs: Any) -> dict[str, Any]:
        return self._post(verb, {"session_id": self.open(), **kwargs})

    def cost(self, **kwargs: Any) -> float:
        body = self._verb("cost", **kwargs)
        try:
            return float(body["usd"])
        except (KeyError, TypeError, ValueError) as exc:
            raise interpreter.InterpreterError(
                f"cost: the runtime answered without a number in 'usd' ({body!r})"
            ) from exc

    def resolve(self, **kwargs: Any) -> dict[str, Any]:
        return self._verb("resolve", **kwargs)

    def submit(self, **kwargs: Any) -> dict[str, Any]:
        return self._verb("submit", **kwargs)

    def poll(self, **kwargs: Any) -> dict[str, Any]:
        return self._verb("poll", **kwargs)

    def list_files(self, **kwargs: Any) -> list[dict[str, Any]]:
        body = self._verb("list_files", **kwargs)
        files = body.get("files")
        if not isinstance(files, list):
            raise interpreter.InterpreterError(
                f"list_files: the runtime answered without a 'files' list ({body!r})"
            )
        return [f for f in files if isinstance(f, dict)]

    def fetch(self, **kwargs: Any) -> list[str]:
        self._check_dest(kwargs.get("dest"))
        body = self._verb("fetch", **kwargs)
        written = body.get("written")
        if not isinstance(written, list):
            raise interpreter.InterpreterError(
                f"fetch: the runtime answered without a 'written' list ({body!r})"
            )
        return [str(name) for name in written]

    def convert(self, **kwargs: Any) -> list[str]:
        self._check_dest(kwargs.get("dest"))
        body = self._verb("convert", **kwargs)
        staged = body.get("staged")
        if not isinstance(staged, list):
            raise interpreter.InterpreterError(
                f"convert: the runtime answered without a 'staged' list ({body!r})"
            )
        return [str(name) for name in staged]

    def verify(self, **kwargs: Any) -> dict[str, Any]:
        """Evidence, or whatever the runtime said. Never validated here.

        A runtime that answers `{"manifest": "nonsense"}` must reach
        `classify()` intact, so the verdict can be `verifier_error`. Sanitising
        the evidence in the proxy would turn a broken verifier into a plausible
        shortfall, which is the one failure this protocol most needs to keep
        loud.
        """
        self._check_dest(kwargs.get("dest"))
        return self._verb("verify", **kwargs)

    # -- a refusal that must not travel --------------------------------------

    @staticmethod
    def _check_dest(dest: Any) -> None:
        """Refuse a `dest` that is absolute or climbs before it is sent.

        The runtime refuses these too (`400`), and that is the boundary that
        matters. This is the cheap half: a Job that stages outside its own
        request directory never gets the chance to be a server-side bug.
        """
        if dest is None:
            return
        text = str(dest)
        if text.startswith("/") or ".." in text.split("/"):
            raise interpreter.InterpreterError(
                f"dest {dest!r} is not a relative path inside the request directory"
            )
