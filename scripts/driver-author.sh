#!/usr/bin/env bash
#
# driver-author.sh - the driver loop's authoring harness.
#
# This is the `--author-cmd` / DRIVER_LOOP_AUTHOR_CMD the loop expects: a ticket
# arrives on **stdin** as plain text and one JSON line goes to **stdout**:
#
#     {"branch": "driver-loop/databento-20261125t041233z",
#      "commit_sha": "abc123…", "author_ref": "<openhands conversation id>"}
#
# Everything else is a refusal: a non-zero exit, or stdout that is not that
# line, stops the job for a human. The loop does not read our logs, so a failure
# has to be loud on stderr rather than a half-answer on stdout.
#
# The work moved into `data_download/driver_author.py` when the agent started
# pushing its own branch -- there is no host checkout to keep any more, so there
# was nothing left for a shell script to be good at. This file stays because the
# loop's configuration names a *path*, and a path is a stable thing to point at
# while the thing behind it changes.
#
# Environment (all read by the module; see its docstring for the rest):
#   PYTHON             interpreter that has this package -- on ryzen the venv,
#                      which is why it is a variable and not `python3`
#   DRIVERS_REPO       default nickbrett1/data-download-drivers
#   BASE_BRANCH        default main
#   AUTHOR_AGENT_CMD   default "<python> -m data_download.openhands_agent";
#                      receives the prompt as its last argument
#   AUTHOR_TIMEOUT     default 3600 (seconds; the loop's own ceiling)
#   GITHUB_TOKEN / GH_TOKEN  verifies the branch, opens the PR, and is handed
#                      to the agent as a 0600 file for the duration of the run
#
# Exit codes: 0 answered, 1 refused (the reason is on stderr), 2 misconfigured.

set -uo pipefail

PYTHON="${PYTHON:-python3}"

exec "${PYTHON}" -m data_download.driver_author "$@"
