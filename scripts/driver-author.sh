#!/usr/bin/env bash
#
# driver-author.sh - the driver loop's authoring harness.
#
# This is the `--author-cmd` / DRIVER_LOOP_AUTHOR_CMD the loop expects: a ticket
# arrives on **stdin** as plain text and one JSON line goes to **stdout**:
#
#     {"branch": "driver-loop/20261124t153400z", "commit_sha": "abc123..."}
#
# Everything else is a refusal: a non-zero exit, or stdout that is not that line,
# stops the job for a human. The loop does not read our logs, so a failure has to
# be loud here rather than a half-answer on stdout.
#
# WHY A SCRIPT AND NOT A MODULE: the harness is a process contract on purpose
# (see driver_build.py). The loop must not acquire an opinion about *who* writes
# the driver, so the authoring agent is one variable -- AUTHOR_AGENT_CMD -- and
# swapping goose for OpenHands, an A2A agent, or a person is an edit to this
# file rather than a change to the loop.
#
# The harness owns the git work, not the agent:
#
#   * it clones (or refreshes) the drivers repo into a scratch checkout, because
#     the loop container has no checkout and must not need one;
#   * it gives the agent a *branch name it chose*, so the branch a ticket lands
#     on is the harness's fact and not something read back out of prose;
#   * it pushes and opens the pull request itself, so "the agent said it pushed"
#     is never the evidence that a branch exists;
#   * it answers with the sha it just pushed, which is what the loop gates on.
#
# An agent that changes nothing is a refusal, not an empty success: an unchanged
# tree has no commit to gate and no driver to install.
#
# Environment:
#   DRIVERS_REPO       default nickbrett1/data-download-drivers
#   DRIVERS_CLONE      default /tmp/driver-author/drivers (reused between runs)
#   AUTHOR_AGENT_CMD   default "goose run --no-session -t"
#                      receives the prompt as its last argument; add flags here,
#                      e.g. AUTHOR_AGENT_CMD="goose run -r" for a recipe.
#   AUTHOR_TIMEOUT     default 3000 (seconds; the loop's own ceiling is 3600)
#   GITHUB_TOKEN / GH_TOKEN  a token that can push a branch and open a PR
#   BASE_BRANCH        default main
#
# Exit codes: 0 answered, 1 refused (the reason is on stderr), 2 misconfigured.

set -uo pipefail

REPO="${DRIVERS_REPO:-nickbrett1/data-download-drivers}"
CLONE="${DRIVERS_CLONE:-/tmp/driver-author/drivers}"
AGENT_CMD="${AUTHOR_AGENT_CMD:-goose run --no-session -t}"
TIMEOUT="${AUTHOR_TIMEOUT:-3000}"
BASE_BRANCH="${BASE_BRANCH:-main}"
TOKEN="${GITHUB_TOKEN:-${GH_TOKEN:-}}"

say() { printf 'driver-author: %s\n' "$*" >&2; }

refuse() {
  say "REFUSED: $*"
  exit 1
}

# The ticket is the whole input. Read it once: the loop writes it and closes.
TICKET="$(cat)"
[ -n "${TICKET}" ] || refuse "no ticket on stdin"
[ -n "${TOKEN}" ] || refuse "no GITHUB_TOKEN/GH_TOKEN; a driver cannot be pushed without one"

# The vendor, for the branch name and the prompt. The ticket's first line is
# `Write a driver for the vendor 'Databento'.` -- if that line ever changes shape
# the fallback is a timestamped branch, which is still a valid answer.
VENDOR="$(printf '%s\n' "${TICKET}" | sed -n "1s/^Write a driver for the vendor '\(.*\)'\..*$/\1/p")"
[ -n "${VENDOR}" ] || VENDOR="unknown"
SLUG="$(printf '%s' "${VENDOR}" | tr '[:upper:]' '[:lower:]' | tr -cs 'a-z0-9' '-')"
SLUG="${SLUG#-}"
SLUG="${SLUG%-}"
BRANCH="driver-loop/${SLUG:-unknown}-$(date -u '+%Y%m%dt%H%M%Sz')"

# -- the checkout ------------------------------------------------------------
#
# Reused rather than re-cloned: the drivers repo is small but its history is the
# context an author benefits from. A checkout that cannot be refreshed is
# replaced, because a stale tree would make the agent write against yesterday's
# protocol.
# `owner/name` becomes an authenticated HTTPS URL. Anything that already looks
# like a URL or a path is used as given, which is what makes this script
# testable against a local bare repository instead of the real one.
case "${REPO}" in
  *://* | /* | ./*) clone_url="${REPO}" ;;
  *) clone_url="https://x-access-token:${TOKEN}@github.com/${REPO}.git" ;;
esac
mkdir -p "$(dirname "${CLONE}")" || refuse "cannot create $(dirname "${CLONE}")"

if [ -d "${CLONE}/.git" ]; then
  say "refreshing ${CLONE}"
  if ! git -C "${CLONE}" fetch --prune origin >&2; then
    say "fetch failed; replacing the checkout"
    rm -rf "${CLONE}"
  fi
fi

if [ ! -d "${CLONE}/.git" ]; then
  say "cloning ${REPO}"
  git clone --quiet "${clone_url}" "${CLONE}" >&2 || refuse "clone of ${REPO} failed"
fi

git -C "${CLONE}" remote set-url origin "${clone_url}" || refuse "cannot set the remote"
git -C "${CLONE}" checkout --quiet "${BASE_BRANCH}" >&2 || refuse "no ${BASE_BRANCH} in ${REPO}"
git -C "${CLONE}" reset --hard --quiet "origin/${BASE_BRANCH}" >&2 || refuse "cannot reset to origin/${BASE_BRANCH}"
# A branch this harness made is never reused: the loop gates a sha, and a branch
# that already exists could answer with a commit the loop has already judged.
git -C "${CLONE}" checkout --quiet -b "${BRANCH}" >&2 || refuse "cannot create ${BRANCH}"
BASE_SHA="$(git -C "${CLONE}" rev-parse HEAD)"

# -- the agent ---------------------------------------------------------------
#
# Cwd is the checkout: agents read the working directory as their task root, and
# the loop's allowed-prefix check means anything else they touch is refused
# after the fact -- so say where to work rather than discovering it later.
PROMPT="${TICKET}

Work in this checkout: ${CLONE} (branch ${BRANCH} is already checked out).
Read drivers/stub/1/ and drivers/databento/1/ for the shape, and the protocol
document in the parent project's notes if present. Commit your work as you go;
you do not need to push -- the harness pushes and opens the pull request. Do not
commit anything outside drivers/ or tests/fixtures/."

say "authoring with: ${AGENT_CMD} (vendor ${VENDOR}, branch ${BRANCH})"
# The identity is supplied, not assumed. A container or a fresh host routinely
# has no `user.email`, and `git commit` then fails with "Author identity
# unknown" -- which would reach the loop as "the harness exited non-zero" and
# read like an agent that refused to work. An explicit identity also makes it
# obvious in `git log` which commits the loop's harness made.
if ! ( cd "${CLONE}" \
       && GIT_AUTHOR_NAME="${GIT_AUTHOR_NAME:-driver-loop}" \
          GIT_AUTHOR_EMAIL="${GIT_AUTHOR_EMAIL:-driver-loop@users.noreply.github.com}" \
          GIT_COMMITTER_NAME="${GIT_COMMITTER_NAME:-driver-loop}" \
          GIT_COMMITTER_EMAIL="${GIT_COMMITTER_EMAIL:-driver-loop@users.noreply.github.com}" \
          timeout "${TIMEOUT}" ${AGENT_CMD} "${PROMPT}" ) >&2; then
  refuse "the authoring agent exited non-zero or timed out"
fi

HEAD_SHA="$(git -C "${CLONE}" rev-parse HEAD)"
if [ "${HEAD_SHA}" = "${BASE_SHA}" ]; then
  refuse "the agent committed nothing (HEAD is still ${BASE_SHA})"
fi

# -- publish -----------------------------------------------------------------
#
# The push is the harness's, not the agent's: a branch the loop is told about
# must be a branch that exists on the remote right now.
say "pushing ${BRANCH} (${HEAD_SHA})"
if ! git -C "${CLONE}" push --quiet --set-upstream origin "${BRANCH}" >&2; then
  refuse "push of ${BRANCH} failed"
fi

# A branch with no pull request cannot be merged, and github_api.merge finds the
# PR by head branch -- so opening it here is what makes the merge step possible.
if command -v gh >/dev/null 2>&1; then
  if ! GH_TOKEN="${TOKEN}" gh pr view "${BRANCH}" --repo "${REPO}" >/dev/null 2>&1; then
    say "opening a pull request"
    GH_TOKEN="${TOKEN}" gh pr create --repo "${REPO}" \
      --base "${BASE_BRANCH}" --head "${BRANCH}" \
      --title "driver: ${VENDOR}" \
      --body "Authored by the driver loop for the vendor ${VENDOR}.

This branch was produced by \`scripts/driver-author.sh\`. The loop gates its
check runs, probes it against the vendor, and merges it only if every changed
path is under \`drivers/\` or \`tests/fixtures/\`." >&2 ||
      refuse "could not open a pull request for ${BRANCH}"
  fi
else
  say "gh is not installed: open the pull request by hand or the merge step will refuse"
fi

# One JSON line, stdout only. Everything above went to stderr on purpose.
printf '{"branch": "%s", "commit_sha": "%s"}\n' "${BRANCH}" "${HEAD_SHA}"
