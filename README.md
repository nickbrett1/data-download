# data-download

A data-download project generated with genproj

## Capabilities

This project includes the following capabilities:

- **Doppler Secrets Management**: Integrates Doppler for secure secrets management. Enables the various MCP servers that rely on privileged tokens to access their services (e.g. Buildkite, GitHub, SonarQube).
- **AI Coding Agents**: Sets up the AI coding agents in the devcontainer: goose (config, MCP servers and spec-first recipes) plus the Antigravity CLI.
- **Container Agent**: Every generated devcontainer brings up and registers its own a2a-goose agent (`<repo>-dev`), reached over the tailnet by the LiteLLM proxy; reuses the a2a-goose GitHub release channel, so the container has the same self-update path as a host.
- **Secret Scanning (gitleaks)**: Required on every project: gitleaks scans staged changes in a pre-commit hook and the full history in CI. Free, offline, no account and no quota.
- **Docker**: Adds Docker support for containerised builds and tooling.
- **Python DevContainer**: Sets up a VS Code DevContainer with Python environment.
- **Docker Container**: Containerize the project and publish to the GitHub Container Registry (GHCR) for deployment to a NAS or self-hosted host via Docker Compose. Mutually exclusive with other deployment systems. Set `workload: batch` for a scheduled job image (runs and exits; no port, no healthcheck, no Watchtower) instead of a long-running service.
- **Buildkite Integration**: Runs CI on a self-hosted Buildkite agent (Apple silicon) instead of a metered cloud fleet. The pipeline and its GitHub webhook are created during generation, so there is no manual "set up project" step. It can coexist with an existing CI provider, so a repository can migrate without a flag day.
- **Ruff (Python code quality)**: Adds fast, zero-configuration Python linting with Ruff (rules live in pyproject.toml [tool.ruff]). Lint locally with `ruff check`. Requires a Python devcontainer. The generated CI pipeline also runs `ruff check`.
- **Editor Configuration**: Shared VS Code extensions and workspace settings for consistent tooling across the team.
- **Shell & Terminal**: Zsh shell with the Powerlevel10k prompt and productivity plugins.

## Setup

1. Clone the repository
2. Create a virtualenv and install the package with dev extras:

   ```bash
   python3 -m venv .venv
   . .venv/bin/activate
   pip install -e ".[dev]"
   ```

3. Run the checks:

   ```bash
   ruff check src tests
   pytest -v
   ```

4. Build the frontend (the HTML routes serve it; without it they answer 503
   with the command to run):

   ```bash
   cd web && npm ci && npm run build && npx playwright test
   ```

## The frontend, and why FastAPI is still here

`src/data_download/` owns the model; `web/` owns the view. The split is
deliberate, because the two sides have different kinds of truth in them:

| stays in FastAPI | why it cannot move |
| --- | --- |
| the SQLite store | it is the only writer |
| `v_pending` / `v_done` / `v_stuck` / `v_blocked` | the views *are* the read-model; a page is a projection of them |
| the token, on `/v1/*` | the pages are a public read-only projection; the verbs are machine credentials |

| `/v1/*` | the CLI and the agents already call it; moving it would break them |
| `_with_facet` precedence | "`running` + `lease_expired` is broken, not running" is a domain rule, not a colour — and so is which of the three blocked facets a row lands in |
| the plan-type registry | loaded from `plan_types/*.yaml` at startup |

What moved into the browser is what only a browser needs: `$0.0412` instead of
a float, `1h 4m` instead of seconds, and which pill tone a state gets.

So there is no SvelteKit server. `web/` is Vite + Svelte 5, it builds to static
files, and FastAPI serves them: `/` and `/plan-types` return the built
`index.html`, `/assets/*` is mounted from the bundle, and the app picks its view
from `location.pathname`. `/ui/dashboard.json` and `/ui/plan-types.json` carry
exactly the context the Jinja templates used to be handed. The image builds the
frontend in a `node` stage and the runtime is still plain python — no node, no
second server, and `/v1/*` never moved.

### What is public, and what is not

The UI is **public**: `/`, `/plan-types`, `/requests/<id>` and the `/ui/*.json`
view-models they read all open with no credential. That is a decision, not an
omission. Everything they show is a GET of a projection — a request book, the
registry, an append-only trail — so "can you reach the serve URL" is the whole
read trust model. The port itself is published on loopback and fronted by
`tailscale serve`; exposing it on the LAN would make approval forgeable, which
is why the read view lost its LAN door (see `deploy/README.md` §6).

A lock was tried here and removed. It cost a live read token in a Homepage
tile's Docker label (and four world-readable backups of it), a rotation, and a
bookmark that stopped working — all to stop a stranger on the LAN reading a
table of Databento requests. The mechanisms that remain are the ones that
matter:

| door | who holds the key |
| --- | --- |
| `GET /v1/*` | `read` or `submit` in an `Authorization` header |
| `POST /v1/requests` | `submit` |
| `POST /v1/requests/{id}/escalate` | `escalate` |
| `POST /v1/quotes` | `read` — a question, not a purchase: it prices a request's Job and writes nothing |
| `POST /v1/requests/{id}/approve` | a tailnet login, asserted by `tailscale serve` and named in `DATA_DOWNLOAD_APPROVERS` |
| `POST /v1/requests/{id}/retry` | either a `submit` token or that same tailnet login — it files a `draft`, which spends nothing until somebody approves it |
| `POST /v1/requests/{id}/cancel` | the same pair — it cancels a `draft` nobody has approved, which spends nothing and deletes nothing |
| `POST /v1/requests/{id}/archive` | the same pair — it hides a *finished* row from the book, and `{"archived": false}` brings it back |
| `GET/POST /mcp/` | the tools send `DATA_DOWNLOAD_MCP_TOKEN`, which is one of the tokens above — so a tool can do exactly what that token's scopes allow and no more |
| `python -m data_download.approve` | a shell on the host: no route, no credential, no identity |

Approval started as a shell command with no route at all, on the argument that a
button on a web page is the hand-edit problem with better typography. That
argument rested on a dashboard that opened the store `mode=ro` in a container
holding no credentials — a page that could not have approved anything. That
container was never built, and the read surface is now this app's own
view-models, so the choice became "a route with an authz check, or a `docker
exec` with none" — and the shell command is the weaker of the two: it
authenticates nothing, logs nothing, and records whatever `--actor` was typed.
It was overruled. What was kept: the button is two clicks with the ceiling
between them, and the page it sits on is still the only page with a control.

The check itself has been through two shapes. It was an `approve`-scoped token
(a human door on a machine key) pasted into the page; it is now **identity**.
`tailscale serve` terminates the tailnet connection and proxies to this process
on loopback, adding `Tailscale-User-Login` for the peer it just authenticated.
`auth.require_approver` trusts that header only from a loopback peer — anywhere
else, the header is a string the caller wrote — and then checks the login
against `DATA_DOWNLOAD_APPROVERS`. The allowlist is the part that replaces the
token: a tailnet is a network, not a person, and a device somebody shares a node
with arrives with a valid identity of its own.

What that buys over a token: the record names *who* spent, not *someone holding
the key*; and a page in the approver's own browser cannot spend, because a
cross-origin `POST` (or a DNS-rebinding one, which defeats `Origin` checks) has
no way to arrive from Serve's hop. What it costs: Serve must be the only thing
that reaches this process *on the trusted address*, which is a property of the
host's config, not of the code -- and inside a container that address is the
bridge gateway rather than loopback, because Docker's port forward re-originates
the connection (`DATA_DOWNLOAD_TRUSTED_PROXIES` names it; unset trusts loopback
only). A direct LAN or container-network request is refused because its peer is
not on that list, even though it reached the same process. Publishing the port
on `127.0.0.1` is still the right default, because it keeps the read surface off
the LAN as well. `DATA_DOWNLOAD_APPROVERS` unset
means nobody may approve — the door fails closed rather than treating "has a
tailnet" as "may spend".

No URL anywhere carries a token or an identity, and `auth.py` has no
query-string path: one way in, so one thing to audit. `DATA_DOWNLOAD_API_TOKENS`
is a list of `<token>:<scopes>` pairs, and `python -c` is not how you read it —
it comes from Doppler on the NAS, or the service's own `.env` beside its compose
file.

### The MCP surface, for agents with no browser

The book has a page. An agent asking the same question has none, and the
alternative — teaching it `curl` with a bearer token someone pasted into a
prompt — is a credential in a transcript. So `/mcp` serves the same doors as
tools, at `https://<node>.<tailnet>.ts.net/mcp/` (streamable HTTP).

| tool | what it does | scope |
| --- | --- | --- |
| `list_requests` | the book, filtered by `state`, `plan_type`, `dataset`, `since`, `until` | `read` |
| `get_request` | one row plus its trail, in sentences | `read` |
| `whats_landed` | what is on disk: per vendor, dataset, request, file, byte | `read` |
| `list_plan_types` | the vocabulary a request can be written in | `read` |
| `quote` | price a filed row's Job; writes nothing | `read` |
| `submit_request` | file a plan or an intent, with an optional `Idempotency-Key` | `submit` |

Three properties are the design, not an implementation detail:

* **Every tool is an HTTP call to this same process's `/v1`**, over an
  in-process ASGI transport — no port, no second listener, no store handle. A
  tool therefore cannot become a second writer of pricing, the ceiling,
  idempotency or the event trail, and cannot drift from what `curl` sees. The
  gate that keeps egress in named files (`tests/test_store_and_egress.py`)
  allows this one client and checks that it is built over the app object.
* **The scopes are the enforcement.** The token in `DATA_DOWNLOAD_MCP_TOKEN`
  must already be listed in `DATA_DOWNLOAD_API_TOKENS`, and a deployment that
  gives it `read` only has a read-only surface, whatever the tools are willing
  to call. Unset means `/mcp` is not mounted at all (404) rather than a surface
  that answers `401` to everything and reads as broken tools.
* **There is no tool that approves, runs, cancels or archives.** Approval is a
  tailnet identity no header can produce, so a tool that could approve would
  make `DATA_DOWNLOAD_APPROVERS` decorative. Filing costs nothing: a filed
  request lands `draft` or `blocked` and waits for a human on the page.

The transport's DNS-rebinding check is off (`transport_security()` in
`mcp_surface.py`), because it refuses honest callers and stops no one here:
there is no cookie to ride, every call carries a token, and the Host `tailscale
serve` presents is whatever node name the caller used — including `testserver`
under `pytest`. That decision and its argument are written down beside the code.

### The price, and the one check

A request is priced at submit, from its own Job: the emitted `quote` step's
kwargs go to `databento.metadata.get_cost` and the answer is stored as
`estimate_usd`. Nothing prices from the params, because the params are what was
asked for and the Job is what will be run. If nothing can price it — no key, no
`quote` step, a vendor that answers nothing or something that is not a positive
finite number — the submit is refused (`quote_unavailable`) and no row is
written. A zero is refused rather than recorded, because "free" and "the vendor
said nothing" are the same number and only one of them is a fact.

The ceiling is the only budget check. `max_usd` is required on both body kinds —
a plan without one is a request nobody can refuse on cost, and it is the body
kind most likely to be expensive — and it is compared to that one quote, at
submit, once. Above it, a plan submit is refused `over_ceiling`;
a matched *intent* cannot be refused (it is already a row), so it lands
`blocked` with `blocked_reason='ceiling_exceeded'` and no Job, and the number
comes back in the response for whoever raises the ceiling. There is no
aggregate, no running total, and no `sum()` inside the claim transaction:
vendor-side caps and statements are the control for the aggregate, and this
service's job is to not authorise a single request above the number somebody
stated. `POST /v1/quotes` answers the same question without creating anything,
so a caller can ask "what would this cost?" first.

That is also how the tests divide: `pytest` owns the contract (which row gets
which facet, what a plan type says a parameter demands), and `web/e2e/` owns the
render, mocking `/ui/*.json` so it needs neither Python nor a database.

### The runner, and where the bytes go

Phase 5 turns an approved row into files. One invocation runs one request:

```
python -m data_download.runner <request_id>     # a named request
python -m data_download.runner --next           # the oldest approved one
```

It claims the row (`Store.claim`, the same CAS the `claim` CLI uses), interprets
the sealed Job, writes the result under

```
$DATA_DOWNLOAD_DATASETS_DIR/<vendor>/<dataset>/<request_id>/files/
```

— the vendor first, because a dataset code is vendor vocabulary (`XNAS.ITCH`
means nothing without knowing who sells it) and the datasets root already holds
more than one vendor's data; then a folder per dataset, then a folder per
request, because two requests can ask for the same dataset and must not
overwrite each other. The vendor comes from the plan type's declaration in
`plan_types/*.yaml`, not from the Job: a Job is a flow and knows no seller. A
row whose plan type is no longer in the registry fails with `vendor_unknown`
rather than being filed under a guessed name. The
root is a setting and there is no default: with `DATA_DOWNLOAD_DATASETS_DIR`
unset the runner refuses before it claims anything, rather than writing bytes to
a path nobody named and then reporting `succeeded`. A Job whose `dest` is
absolute or escapes its request directory is refused too.

The verdict decides the state, and nothing else does: `pass` -> `succeeded`,
`commit_marked` -> `landed_with_errors`, `quarantine` or an interpreter error
-> `failed`. `succeeded` and `landed_with_errors` record `landed_sha256` (the
hash of the sorted `sha256  name` lines of what actually landed) and write a
`landed` event; `failed` records its errors and writes `failed`. Every settle
carries `AND state = 'running' AND owner = ?`, so a runner whose lease expired
and was reclaimed cannot write over the new claimant's attempt — losing that
CAS is the reclaim working, and is reported, not raised. `landing.json` sits
beside `files/` rather than inside it, because a file inside the staging
directory would be staged-but-not-in-the-vendor-manifest and the shared verifier
would rightly call a good landing a contradiction.

**The driver is in another container.** `driver_client.RemoteDriver` sends the
interpreter's eight verbs over HTTP to `data-download-drivers`, one session (and
one child process there) per run, closed in a `finally`. The stub artefact
behind it makes no network call from *this* process, holds no credential here,
and writes files that literally begin `STUB:`; what it proves is the runner —
the claim, the path, the digest, the settlement, the events — and the transport.
Which driver runs is discovered, not compiled in: `GET /v1/drivers` is asked who
serves this row's vendor, and a vendor with no installed artefact settles
`failed` with `driver_not_ready` naming what *is* installed. With no
`DRIVER_RUNTIME_TOKEN` the runner refuses **before** the claim, so an un-runnable
row stays `approved` instead of becoming a `failed` row that reads like a vendor
outcome. A new driver is an install in the other container, not a deploy of this
one — which is why the artefact directory is not mounted here.

### The worker, and why nothing waits for a command

Phase 6 starts the loop that phase 5 only had a command for. An approval is now
enough: `worker.py` runs a pass every `DATA_DOWNLOAD_RUNNER_INTERVAL` seconds
(default 60) over the approved queue, oldest first, and the approve route
nudges it, so a request approved on the page is picked up in milliseconds rather
than in up to a minute. A pass that ran something goes straight back for the
next row, so a backlog drains at the speed of the downloads.

It runs **in this process**, on the app's own event loop, sharing the store and
the registry that are already open. The queue is a SQL query, not a broker; a
second container would want the same sqlite file (two writers, one more mount to
get wrong) to learn what one `SELECT` answers. The claim is still the
permission: this loop and a human running `--next` can both want one row, and
`Store.claim`'s CAS decides, exactly as before. Nothing about the worker is
privileged.

Two things it does not do. It does not run the download on the loop — the
interpreter is blocking (a vendor job is 120 polls of 30 s), so it goes to a
thread and the page stays answerable, including to a cancel. And it does not
pretend to be running when it cannot finish: with no `DATA_DOWNLOAD_DATASETS_DIR`
or no `DRIVER_RUNTIME_TOKEN` the loop is not started at all, because a pass that
can only log a refusal would leave the row `approved` while filling the log with
something that reads like a failure. `DATA_DOWNLOAD_RUNNER=off` turns it off
deliberately (tests, or a deployment that runs the CLI instead).

**The lease is renewed while the work outlives it.** The 900 s default was
doing two jobs — "how long may this be worked on" and "how long will it take" —
and a healthy run outlives it, which means the row is reclaimable while the
first attempt is still spending and the vendor is asked twice. A longer lease is
the wrong fix (a crashed claimant would freeze its rows for the worst case), so
`Store.renew` pushes the lease forward on a heartbeat at a third of its length,
carrying the same `AND state = 'running' AND owner = ?`. A false answer means
the row is no longer ours: the run stops and records `lease_lost` rather than
settling, and `settle`'s CAS refuses that write anyway.

## Retrying a failed request

A failed row is history and stays history. `POST /v1/requests/{id}/retry` does
not rewind it: the route rebuilds the original body (from `intent_json`, or
from `plan_type`/`params_json`/`max_usd`, carrying the Job's `verify.on_failure`
policy across) and sends it through the **same** `_accept_submission` the submit
route uses — the same binding, the same pricing, the same ceiling check, the
same duplicate rule. What comes back is a new Request: a new id, a fresh quote
against today's prices, its own output directory, its own events, and
`retried_from` naming the row it came from. It lands in `draft`, because an
approval authorises one attempt; the retry has not authorised anything and still
spends nothing.

Rewinding was rejected on purpose. `approve()` is `draft -> approved` only, and
an approval is a statement about one attempt; making `failed -> approved` legal
would rewrite the events, the recorded errors and the output directory of a row
that already ran, and would let a green badge replace a red one without anyone
asking a second time.

Retryability is keyed off the failure, not the state alone. `failed` and
`landed_with_errors` can be retried, except when the failure is
`verify_shortfall` or `verify_contradiction`: those compare the vendor's bytes
against an immutable manifest, so the same bytes would fail the same way and
"retry" would only spend again to learn that. The page mirrors
`UNRETRYABLE_FAILURES` in `format.js` so it never offers a verb the server would
refuse, and shows the server's own refusal text when a stale page tries anyway.
The check is `409 not_retryable`, with `reason: not_settled` for a row still in
flight and `reason: same_bytes` for the immutable-verdict cases.

## Cancelling a draft nobody wants

Filing twice is easy — the retry card will do it for you — and until there was a
verb for it, the only ways to get rid of the spare were approving a request
nobody wanted (a spend) or editing the store by hand (a lie about the record).
`POST /v1/requests/{id}/cancel` is a third way: `draft` (and only `draft`)
becomes `cancelled`, and the row, its quote, its `why` and its events all stay.

It is not a DELETE, and that is the design rather than an omission. A missing
row reads as a request that was never filed; `cancelled` reads as a request
somebody looked at and dropped, which is the truth and is also the thing worth
keeping. The badge is deliberately colourless: nothing failed and nothing
landed.

It is kept, and it is not **listed**. The book's job is "what needs me", and a
cancelled draft needs nothing, so `cancelled` has its own facet that the
dashboard leaves out of `rows` — and out of `total`, because a count that
disagrees with what is drawn is its own bug. The filter bar offers it by name
("Cancelled, 1") whenever there is one, so nothing is unreachable; the page and
its events are one click away.

Only `draft` is accepted. `approved` is refused because an approval authorises
one attempt, and once the runner may hold the lease, stopping the work is the
runner's business — writing `cancelled` under a running claim would be this
process claiming a cancellation it cannot enforce. Everything past that is
history and answers to `retry`, not to `cancel`. `blocked` is refused for a
duller reason: two CHECKs encode "a blocked row has no Job and no estimate", so
a cancelled row that never had one would need both relaxed, and that is a
decision about what a never-bound row *is*.

A widening of the `state` CHECK is a table rebuild in SQLite, so `db.py` grows
one (`_widen_state_check`, idempotent by inspecting `sqlite_master.sql` for the
word). It is not only about the CHECK: `v_done` is `CREATE VIEW IF NOT EXISTS`,
so a deployed store would otherwise keep a definition that cannot show a
cancelled row. One rebuild fixes both, and it copies columns by intersection
with the live table, so a store a phase behind is rebuilt into what it has.

## Archiving a finished request

Cancel and archive are the two ways a row leaves the book, and they are not
interchangeable — which is the whole reason there are two.

**Cancel** writes `state`. It is for a `draft` nobody has approved: a decision
that has not been made yet, taken back. **Archive** writes a date
(`archived_at`), and it is for a row that is finished. A `failed` row is the case
that forced it: the state is the outcome of something that really happened — the
vendor was asked, the driver refused, the errors are on the row — so writing
`cancelled` over it would make the record claim the request was cancelled before
it ran. Archiving leaves `state = 'failed'` exactly where it was, keeps the badge,
the `errors_json` and the events, and takes the row out of the book's default
list. That list is "what needs me"; a finished row nobody is looking at is noise
next to one that does.

`cancelled` and `archived` are both hidden buckets with their own filter chips,
so nothing becomes unreachable. `{"archived": false}` on the same route restores
a row — hiding is never a one-way door — and both directions are idempotent with
no second event. Only settled states are accepted (`succeeded`,
`landed_with_errors`, `failed`, `cancelled`): a draft is cancelled, and an
approved or running row is still happening, where hiding something that may
still spend is the one thing a list of "what is happening" must not do.

`archived_at` is a nullable column added in place by `ADDED_COLUMNS`, so this
needed no rebuild — the opposite of `cancelled`, which is a value in a CHECK and
cost one.

## When the registry moves

A Request filed while nothing registered could take it sits `blocked` with a
match report that describes the registry **at that moment**. Plans are code
(`plan_types/*.yaml` plus its binder, baked into the image), so "we wrote a plan"
and "a new process started" are the same event: the deploy is the trigger. On
startup, `rematch.reconcile` walks the blocked rows, and any whose stored
registry digest differs from the one in force is re-matched through the same
`bind.bind_intent` the API uses. A row that now matches binds and becomes an
ordinary `draft` to approve (`plan_origin: rematched`); a row that is closer but
still short gets a fresh verdict and the page says which fields are missing.

The digest covers more than `name:version` — it is
`name:version:fingerprint(params_schema, datasets)` — so editing a YAML without
bumping its version still invalidates stored verdicts. The pass is idempotent
(equal digests write nothing) and never touches a row that already has a Job.

The same pass, by hand, when you want to see what a deploy would do:

```
python3 -m data_download.rematch [request_id ...] [--dry-run] [--force] \
    [--plan-types-dir plan_types] [--db /data/data-download.db]
```

`--dry-run` prints what a deploy would sweep; `--force` re-matches even when the
digest is unchanged. Note that a plan type added to a *running* container is
invisible until it restarts — the image is the registry.

## Secret scanning

Every commit is scanned for credentials by `gitleaks`, and CI scans the
**full history**. The local hook is convenience; CI is the gate you cannot skip
with `git commit --no-verify`.

```bash
scripts/setup-git-hooks.sh   # installs gitleaks and sets core.hooksPath
```

If a commit is blocked, **rotate the credential first** — a finding means it may
already be exposed. Then remove it from the staged changes and commit again.

Only for a verified false positive, add its fingerprint to `.gitleaksignore`,
which is a committed and reviewable diff. Never bypass the hook with
`--no-verify`: it leaves no record and the next commit is unscanned too.

This is detection, not storage (Doppler holds secrets) and not rotation (that is
a human step). Allowlist edits in `.gitleaks.toml` are reviewable changes on
purpose.

## Doppler

This project uses Doppler for secrets from the shared `common` project
(config `dev`) — no per-repo Doppler project is created. First use (links
the shared project and `dev` config):

```bash
doppler setup --project common --config dev
```

If your repo needs app-specific secrets that shouldn't live in the shared
`common` project, regenerate it with the doppler capability set to
`projectStrategy: "new"` to get a dedicated project.

The Doppler CLI is installed in the devcontainer — it must be on PATH for the
VS Code extension and `doppler run` to work. Auth is persisted via the host
`~/.doppler` bind-mount.

### Env-var precedence (read this if `doppler run` hits the wrong project)

Doppler resolves its target as **environment variables > `doppler.yaml` >
`~/.doppler` scoped config**. If your shell — or the session that launched
the devcontainer (e.g. an agent runtime) — exports `DOPPLER_PROJECT` /
`DOPPLER_CONFIG` / `DOPPLER_ENVIRONMENT`, those silently override this
repo's `doppler.yaml` and every `doppler` command targets the wrong
project. The devcontainer's post-create setup pins this repo's context
(`common`/`dev`) in `~/.bashrc` and `~/.zshrc` and warns at
setup if resolution still mismatches. To force the correct context manually:

```bash
unset DOPPLER_PROJECT DOPPLER_CONFIG DOPPLER_ENVIRONMENT
doppler setup --no-interactive --project common --config dev
```

## Deployment

See `deploy/README.md` for the deployment runbook (Buildkite -> GHCR ->
Watchtower -> Docker host). Deploy with:

```bash
docker compose up -d
```

## The container's agent

This devcontainer brings up its own `a2a-goose` agent, registered in the hub as
`data-download-dev` - one agent per repo, so a restart reclaims the same entry
instead of adding a second one. Turns are billed through the LiteLLM proxy
configured in Doppler (`LITELLM_BASE_URL`).

```bash
scripts/agent-dev.sh start    # write secrets + config, fetch the launcher, run it
scripts/agent-dev.sh status   # running or not, the card URL, the log tail
scripts/agent-dev.sh stop     # SIGTERM, wait for a clean deregister, confirm gone
```

`start` runs from the devcontainer's post-start hook, so the agent is normally
already up when you arrive. It fails open: with no network on a first start it
prints why it did not start and leaves the project usable. Secrets come from
Doppler into `~/.config/a2a-goose/env` (mode 0600) and never into the image or
`containerEnv`.

## Generated by genproj

This project was generated using the genproj tool.
