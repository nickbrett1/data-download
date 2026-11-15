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
| token auth and the `read` scope | approval has no endpoint **and** no scope these pages can reach |
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

### Where the token lives

The HTML shell is **public**: it is one `index.html` and a hashed bundle with no
data in it, so a Homepage tile, a home-screen icon or a link pasted into a chat
opens the app. Every figure comes from `/ui/*.json`, which require `read` in an
`Authorization` header — and only from a header. A token is never accepted in a
URL, because a URL is exactly what gets copied: into a Docker label, a compose
file, a proxy access log, a `Referer`, browser history.

The browser holds the token and hands it over once, in one of two shapes:

- `#token=…` — a fragment, which no server and no access log ever sees. The app
  consumes it and strips it from the address bar on load, so screenshots and
  re-shares are clean. This is the shape to paste.
- a paste into the unlock card, kept in `localStorage`, which is what a phone
  gets the first time.

A refused token clears itself and brings the card back saying so. To sign out of
a browser, clear `localStorage` (or rotate the token, which does it for everyone).

That is also how the tests divide: `pytest` owns the contract (which row gets
which facet, what a plan type says a parameter demands), and `web/e2e/` owns the
render, mocking `/ui/*.json` so it needs neither Python nor a database.

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
