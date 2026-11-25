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
boot at `~/.openhands/agent-canvas/api-key.txt` *inside its own container*. Read
it per job rather than caching it in an environment file -- a recreated
container has a new key, and a stale one is a 401 that reads like a broken
server. Two ways to read it, in this order:

* **`OPENHANDS_KEY_FILE`** -- the file mounted into *this* process. This is what
  the control plane uses: the app container has no docker socket and no docker
  CLI, so `docker exec` is not available to it and should not be.
* **`docker exec`** -- for a run from a host that has the docker CLI. It needs no
  host root: the image sets `USER openhands` (uid 10001) and that uid owns the
  file.

`converse()` is the whole conversation as one call, so the control plane can
author in-process; `main()` is the same thing behind a command, for a
deployment that would rather run an agent as a child process.

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
from collections.abc import Callable
from pathlib import Path
from typing import Any

DEFAULT_URL = "http://127.0.0.1:3088"
DEFAULT_CONTAINER = "openhands"
#: Where the agent works. The container's own path: the control plane mounts the
#: same directory at the same path, so a prompt written here is a prompt about a
#: path that means the same thing on both sides.
DEFAULT_WORKDIR = "/projects/driver-loop"
DEFAULT_TIMEOUT = 3000.0
DEFAULT_POLL = 10.0
DEFAULT_BUDGET = 2.0
DEFAULT_ITERATIONS = 200

#: Where the server keeps the key it generated. Container path, not host.
KEY_PATH = "/home/openhands/.openhands/agent-canvas/api-key.txt"

#: The only status that means the work happened. Everything else is a refusal
#: -- `paused` included, because a paused conversation is waiting for a human.
FINISHED = "finished"

#: Statuses a conversation can end in. `idle` is deliberately absent: it is the
#: at-rest state, not an ending.
ENDED = frozenset({"finished", "error", "stopped"})


class AgentError(Exception):
    """The conversation could not be started, or did not finish.

    `conversation` is the transcript id when there was one, so a caller that
    reports a failure can still say where to read it: the id is what proves
    *which* run was refused, and it exists from the moment the POST answers.
    """

    conversation: str = ""


class AgentConfig(AgentError):
    """Nothing to talk to: no key, no server, no profile. Exit 2, not 1."""


def session_key(
    *, explicit: str = "", container: str = DEFAULT_CONTAINER, key_file: str = ""
) -> str:
    """The server's own key, from the file this process can see, or the container."""
    if explicit:
        return explicit
    where = key_file or (os.environ.get("OPENHANDS_KEY_FILE") or "").strip()
    if where:
        try:
            key = Path(where).read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise AgentConfig(f"cannot read OPENHANDS_KEY_FILE {where}: {exc}") from None
        if not key:
            raise AgentConfig(f"OPENHANDS_KEY_FILE {where} is empty")
        return key
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


#: `error` is one bucket for several different problems -- a starved run, a dead
#: credential, an exhausted vendor balance. The only place they are told apart
#: is this event, so a refusal has to go and read it.
ERROR_KIND = "ConversationErrorEvent"

#: Error codes that mean "this ticket did not fit the limits we set", as opposed
#: to "this deployment cannot author anything right now". The first is a job a
#: human decides about; the second is not worth spending another job on.
_STARVED = ("MaxIterationsReached", "MaxBudgetReached")


def _event_list(answer: Any) -> list[dict]:
    """The events from `/events/search`, whatever shape the server wraps them in."""
    if isinstance(answer, list):
        return [event for event in answer if isinstance(event, dict)]
    if not isinstance(answer, dict):
        return []
    for key in ("items", "events", "data", "results"):
        found = answer.get(key)
        if isinstance(found, list):
            return [event for event in found if isinstance(event, dict)]
    return []


def failure(conversation_id: str, *, url: str, key: str) -> dict[str, Any]:
    """Why the server stopped this conversation, in its own words.

    Returns the last `ConversationErrorEvent` as `{code, detail, user_action,
    retryable}`, or `{}` when there is none. Spending one extra call only on a
    conversation that already failed keeps the happy path at four calls.
    """
    answer = _call(
        "GET", f"/api/conversations/{conversation_id}/events/search?limit=100", url=url, key=key
    )
    for event in reversed(_event_list(answer)):
        if event.get("kind") != ERROR_KIND:
            continue
        classification = event.get("classification") or {}
        return {
            "code": str(event.get("code") or ""),
            "detail": str(event.get("detail") or ""),
            "user_action": str(classification.get("user_action") or ""),
            "retryable": bool(classification.get("retryable")),
        }
    return {}


def ends_badly(status: str, why: dict[str, Any]) -> tuple[str, str]:
    """Turn a failed conversation into the sentence a human reads, and a class.

    The distinction that matters operationally: a run starved of iterations or
    budget is a ticket that did not fit, and the loop should stop and say so. A
    dead API key or an empty vendor balance is a broken deployment, and every
    further job would fail the same way -- so it must not be reported as "this
    driver was refused".
    """
    code = str(why.get("code") or "")
    detail = str(why.get("detail") or "")
    said = f"{code}: {detail}" if code and detail else (code or detail)
    if code in _STARVED or not code:
        return ("refused", f"the conversation ended {status}" + (f" ({said})" if said else ""))
    return ("misconfigured", f"the conversation ended {status} ({said}); the agent server needs attention")


def summarise(info: dict) -> str:
    """The spend line, out of the stats the server keeps per conversation.

    The shape is the server's, not ours, and it is easy to guess wrong: usage
    is keyed by *label* (`default`, `condenser`), the money is a flat
    `accumulated_cost` on each entry, and the counts live one level down in
    `accumulated_token_usage`. The counts in there are not all disjoint --
    `cache_read_tokens` is a subset of `prompt_tokens`, and `reasoning_tokens`
    a subset of `completion_tokens` -- so adding every `*_tokens` key would
    count the same tokens twice. Prompt plus completion is the honest total;
    `context_window` and `per_turn_token` are sizes, not spend.
    """
    usage = ((info.get("stats") or {}).get("usage_to_metrics") or {})
    if not isinstance(usage, dict) or not usage:
        return "no cost recorded"
    cost = 0.0
    tokens = 0
    for entry in usage.values():
        if not isinstance(entry, dict):
            continue
        cost += float(entry.get("accumulated_cost") or 0.0)
        counts = entry.get("accumulated_token_usage") or {}
        if isinstance(counts, dict):
            for name in ("prompt_tokens", "completion_tokens"):
                value = counts.get(name)
                if isinstance(value, (int, float)):
                    tokens += int(value)
    if not cost and not tokens:
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


def converse(
    prompt: str,
    *,
    url: str = DEFAULT_URL,
    key: str = "",
    container: str = DEFAULT_CONTAINER,
    key_file: str = "",
    workdir: str = DEFAULT_WORKDIR,
    profile: str = "",
    tags: dict[str, str] | None = None,
    budget: float | None = DEFAULT_BUDGET,
    iterations: int = DEFAULT_ITERATIONS,
    timeout: float = DEFAULT_TIMEOUT,
    poll: float = DEFAULT_POLL,
    say: Callable[[str], None] | None = None,
) -> dict[str, Any]:
    """One conversation, run to its end. The whole authoring turn as one call.

    Raises `AgentConfig` when there is nothing to talk to and `AgentError` when
    the conversation ended without doing the work; on the latter the exception
    carries the transcript id, because the run is worth reading even when it
    failed. Nothing here retries: a second conversation is a second spend.

    `say` is a progress sink, not a log: the control plane passes the logger,
    the CLI passes stderr. It is called *before* the wait, so an operator can
    see the transcript link while the work is still going.
    """
    say = say or (lambda line: None)
    resolved = session_key(explicit=key, container=container, key_file=key_file)
    chosen = profile or active_profile(url=url, key=resolved)
    if not chosen:
        raise AgentConfig(
            "the agent server has no active agent profile; GET /api/agent-profiles is empty"
        )
    info = start(
        prompt,
        url=url,
        key=resolved,
        workdir=workdir,
        profile=chosen,
        tags=tags or {},
        budget=budget,
        iterations=iterations,
    )
    conversation = str(info["id"])
    say(
        f"started {conversation_url(url, conversation)} in {workdir} "
        f"(tags {tags or '{}'})"
    )
    try:
        ended = wait(conversation, url=url, key=resolved, timeout=timeout, poll=poll)
        status = str(ended.get("execution_status") or "")
        say(f"{status}; {summarise(ended)}")
        said = final_response(conversation, url=url, key=resolved)
        if said:
            say(f"it said: {said[:2000]}")
        if status != FINISHED:
            why = failure(conversation, url=url, key=resolved) if status == "error" else {}
            kind, sentence = ends_badly(status or "without a status", why)
            raise (AgentConfig if kind == "misconfigured" else AgentError)(sentence)
    except AgentError as exc:
        exc.conversation = conversation
        raise
    return {
        "conversation": conversation,
        "status": status,
        "info": ended,
        "said": said,
    }


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

    def say(line: str) -> None:
        print(f"openhands: {line}", file=sys.stderr)

    try:
        answer = converse(
            args.prompt,
            url=args.url,
            key=args.key,
            container=args.container,
            workdir=args.workdir,
            profile=args.profile,
            tags=tags,
            budget=args.budget,
            iterations=args.iterations,
            timeout=args.timeout,
            poll=args.poll,
            say=say,
        )
    except AgentConfig as exc:
        say(f"MISCONFIGURED: {exc}")
        return 2
    except AgentError as exc:
        say(f"REFUSED: {exc}")
        return 1

    # One line, for the harness to carry upward. The transcript id is the only
    # thing a later reader needs to find this build's conversation again.
    print(f"author-ref: {answer['conversation']}")
    return 0


if __name__ == "__main__":  # pragma: no cover - the process entry point
    raise SystemExit(main())
