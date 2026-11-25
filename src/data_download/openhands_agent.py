"""The authoring agent, spoken to over the OpenHands Agent Server's REST API.

The loop's seam is unchanged -- `AUTHOR_AGENT_CMD` gets the ticket as its last
argument and says what happened via its exit status -- but the thing behind the
seam is now a *conversation* rather than one shell command. That buys three
things a `goose run --no-session -t` cannot give: a transcript a person can read
afterwards, a cost in dollars, and a terminal status that is the server's answer
rather than a process exit code standing in for one.

Four calls, no SDK (docs: `GET /openapi.json` on the agent server):

    POST /api/conversations                     -> {"id": ...}, immediately
    GET  /api/conversations/{id}                -> execution_status, stats
    GET  /api/conversations/{id}/agent_final_response
    GET  /api/conversations/{id}/events/search

Two behaviours learned from the running server rather than from its docs, both
of which cost time when you assume otherwise:

* **`idle` is not "finished".** A conversation is created `idle` even when the
  request sets `initial_message_run`, so a poll that stops at "not running" ends
  in the first second having done nothing at all. Only `finished` is success;
  `error` and `stopped` are refusals, and `paused` is a refusal too -- it is a
  conversation waiting for a person who is not there.
* **`observability_metadata` is accepted and then dropped.** It comes back
  `null`, so attribution (which vendor, which job) goes in `tags`, which does
  persist.

Auth is `X-Session-API-Key`, a 64-character file the server generates on first
boot at `~/.openhands/agent-canvas/api-key.txt` *inside its own container*. It
is read with `docker exec`, which needs no host root: the image sets
`USER openhands` (uid 10001) and that uid owns the file. Read it per job rather
than caching it in an environment file -- a recreated container has a new key,
and a stale one is a 401 that reads like a broken server.

Not used, and why: the `openhands` CLI does not exist in the image; the Python
SDK is installed *inside* the container and using it would mean running this
service's code there; A2A would be a second integration layer for the same four
calls. The WebSocket at `/sockets` streams events live, which a build does not
need -- polling `execution_status` and reading the log afterwards is enough.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Any

DEFAULT_URL = "http://127.0.0.1:3088"
DEFAULT_CONTAINER = "openhands"

#: Where the server keeps the key it generated. Container path, not host.
KEY_PATH = "/home/openhands/.openhands/agent-canvas/api-key.txt"

#: The only status that means the work happened. Everything else is a refusal
#: -- `paused` included, because a paused conversation is waiting for a human.
FINISHED = "finished"

#: Statuses a conversation can end in. `idle` is deliberately absent: it is the
#: at-rest state, not an ending.
ENDED = frozenset({"finished", "error", "stopped"})


class AgentError(Exception):
    """The conversation could not be started, or did not finish."""


class AgentConfig(AgentError):
    """Nothing to talk to: no key, no server, no profile. Exit 2, not 1."""


def session_key(*, explicit: str = "", container: str = DEFAULT_CONTAINER) -> str:
    """The server's own key, read from the container that generated it."""
    if explicit:
        return explicit
    try:
        done = subprocess.run(  # noqa: S603 - docker exec, an operator's own host
            ["docker", "exec", container, "cat", KEY_PATH],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AgentConfig(f"cannot run docker to read the session key: {exc}") from None
    key = done.stdout.strip()
    if done.returncode != 0 or not key:
        raise AgentConfig(
            "cannot read the session key; set OPENHANDS_API_KEY, or check that "
            f"`docker exec {container} cat {KEY_PATH}` works: "
            f"{(done.stderr or '').strip()[:200]}"
        )
    return key


def _call(
    method: str,
    path: str,
    *,
    url: str,
    key: str,
    body: dict | None = None,
    timeout: float = 60.0,
) -> Any:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    request = urllib.request.Request(
        f"{url.rstrip('/')}{path}",
        data=data,
        method=method,
        headers={"X-Session-API-Key": key, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as answer:  # noqa: S310 - agent server
            return json.loads(answer.read().decode("utf-8") or "null")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        if exc.code == 401:
            raise AgentConfig(
                "the agent server answered 401: the session key is wrong or was "
                "regenerated (a recreated container has a new one)"
            ) from None
        if exc.code == 429:
            raise AgentError(
                "the agent server is at its conversation capacity (429); this is "
                "a wait, not a broken job"
            ) from None
        raise AgentError(f"{method} {path} answered {exc.code}: {detail}") from None
    except urllib.error.URLError as exc:
        raise AgentConfig(f"{method} {path} could not reach {url}: {exc.reason}") from None


def active_profile(*, url: str, key: str) -> str:
    """Which agent to run as. The server has one active profile; we ask it."""
    answer = _call("GET", "/api/agent-profiles", url=url, key=key) or {}
    return str(answer.get("active_agent_profile_id") or "")


def start(
    prompt: str,
    *,
    url: str,
    key: str,
    workdir: str,
    profile: str,
    tags: dict[str, str] | None = None,
    budget: float | None = None,
    iterations: int = 200,
) -> dict:
    """Open a conversation that starts running immediately.

    `initial_message_run` is what saves the second call: without it the
    conversation sits at `idle` with a message nobody is acting on.
    """
    body: dict[str, Any] = {
        "workspace": {"working_dir": workdir},
        "initial_message": {"role": "user", "content": [{"type": "text", "text": prompt}]},
        "initial_message_run": True,
        "autotitle": True,
        "max_iterations": int(iterations),
    }
    if profile:
        body["agent_profile_id"] = profile
    if tags:
        body["tags"] = tags
    if budget:
        body["max_budget_per_run"] = float(budget)
    answer = _call("POST", "/api/conversations", url=url, key=key, body=body) or {}
    if not answer.get("id"):
        raise AgentError(f"the server answered without a conversation id: {answer}")
    return answer


def wait(
    conversation_id: str, *, url: str, key: str, timeout: float = 3000.0, poll: float = 10.0
) -> dict:
    """Poll until the conversation ends, and answer with its last state."""
    deadline = time.monotonic() + timeout
    while True:
        info = _call("GET", f"/api/conversations/{conversation_id}", url=url, key=key) or {}
        status = str(info.get("execution_status") or "")
        if status in ENDED:
            return info
        if time.monotonic() >= deadline:
            raise AgentError(
                f"the conversation was still {status or 'unknown'} after {int(timeout)}s"
            )
        time.sleep(poll)


def summarise(info: dict) -> str:
    """The spend line, out of the stats the server keeps per conversation."""
    usage = ((info.get("stats") or {}).get("usage_to_metrics") or {})
    cost = 0.0
    tokens = 0
    for entry in usage.values() if isinstance(usage, dict) else []:
        if not isinstance(entry, dict):
            continue
        cost += float(entry.get("cost") or 0.0)
        counts = entry.get("tokens") or {}
        if isinstance(counts, dict):
            tokens += sum(int(value or 0) for value in counts.values())
    if not usage:
        return "no cost recorded"
    return f"${cost:.4f}, {tokens} tokens"


def final_response(conversation_id: str, *, url: str, key: str) -> str:
    answer = _call(
        "GET", f"/api/conversations/{conversation_id}/agent_final_response", url=url, key=key
    )
    if isinstance(answer, dict):
        return str(answer.get("response") or "")
    if isinstance(answer, str):
        return answer
    return ""


def conversation_url(base: str, conversation_id: str) -> str:
    """Where a person reads this transcript in the canvas UI."""
    return f"{base.rstrip('/')}/canvas/conversations/{conversation_id}"


def _tags(pairs: list[str]) -> dict[str, str]:
    tags: dict[str, str] = {}
    for pair in pairs:
        name, _, value = pair.partition("=")
        name, value = name.strip().lower(), value.strip()
        # The server's rule: lowercase alphanumeric keys, values to 256 chars.
        if name and name.isalnum() and value:
            tags[name] = value[:256]
    return tags


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Run one driver ticket as an OpenHands conversation.")
    parser.add_argument("prompt", help="the whole instruction, as one argument")
    parser.add_argument("--url", default=(os.environ.get("OPENHANDS_URL") or DEFAULT_URL).strip())
    parser.add_argument(
        "--container",
        default=(os.environ.get("OPENHANDS_CONTAINER") or DEFAULT_CONTAINER).strip(),
    )
    parser.add_argument("--key", default=(os.environ.get("OPENHANDS_API_KEY") or "").strip())
    parser.add_argument(
        "--workdir", default=(os.environ.get("OPENHANDS_WORKDIR") or "/projects/driver-loop").strip()
    )
    parser.add_argument("--profile", default=(os.environ.get("OPENHANDS_PROFILE") or "").strip())
    parser.add_argument(
        "--budget", type=float, default=float(os.environ.get("OPENHANDS_MAX_BUDGET") or 2.0)
    )
    parser.add_argument(
        "--iterations", type=int, default=int(os.environ.get("OPENHANDS_MAX_ITERATIONS") or 200)
    )
    parser.add_argument(
        "--timeout", type=float, default=float(os.environ.get("OPENHANDS_TIMEOUT") or 3000.0)
    )
    parser.add_argument("--poll", type=float, default=float(os.environ.get("OPENHANDS_POLL") or 10.0))
    parser.add_argument(
        "--tag",
        action="append",
        default=[],
        help="k=v attribution; also read from OPENHANDS_TAGS",
    )
    args = parser.parse_args(argv)

    tags = _tags([*(os.environ.get("OPENHANDS_TAGS") or "").split(","), *args.tag])

    try:
        key = session_key(explicit=args.key, container=args.container)
        profile = args.profile or active_profile(url=args.url, key=key)
        if not profile:
            raise AgentConfig(
                "the agent server has no active agent profile; GET /api/agent-profiles is empty"
            )
        info = start(
            args.prompt,
            url=args.url,
            key=key,
            workdir=args.workdir,
            profile=profile,
            tags=tags,
            budget=args.budget,
            iterations=args.iterations,
        )
        conversation = str(info["id"])
        print(
            f"openhands: started {conversation_url(args.url, conversation)} "
            f"in {args.workdir} (tags {tags or '{}'})",
            file=sys.stderr,
        )
        ended = wait(
            conversation, url=args.url, key=key, timeout=args.timeout, poll=args.poll
        )
        status = str(ended.get("execution_status") or "")
        print(f"openhands: {status}; {summarise(ended)}", file=sys.stderr)
        said = final_response(conversation, url=args.url, key=key)
        if said:
            print(f"openhands: it said: {said[:2000]}", file=sys.stderr)
        if status != FINISHED:
            raise AgentError(f"the conversation ended {status or 'without a status'}")
    except AgentConfig as exc:
        print(f"openhands: MISCONFIGURED: {exc}", file=sys.stderr)
        return 2
    except AgentError as exc:
        print(f"openhands: REFUSED: {exc}", file=sys.stderr)
        return 1

    # One line, for the harness to carry upward. The transcript id is the only
    # thing a later reader needs to find this build's conversation again.
    print(f"author-ref: {conversation}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
