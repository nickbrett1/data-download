#!/bin/sh
#
# driver-loop.sh - the supervisor the driver loop deliberately does not have.
#
# `data_download.driver_build` is run-to-exit: it takes one job, walks it as far
# as it can, and returns. Nothing in it idles, which keeps the state machine
# testable and means a crash is a returned exit code rather than a hung process.
# Something therefore has to *call* it again, and that is this script:
#
#     while :; do driver_build --once; sleep $DRIVER_LOOP_INTERVAL; done
#
# The sleep is what makes this a poll and not a spin: an empty queue exits in
# milliseconds, so without it a quiet host would burn a core.
#
# Exit codes from --once are not all the same thing, and the difference decides
# how long to wait:
#
#   0  installed, or nothing to do        -> poll again shortly
#   1  the job stopped (needs_human)      -> poll again shortly: the *job* was
#                                            the problem, not the loop
#   2  misconfigured (no harness, no token) -> there is nothing to retry, so back
#                                            off hard and say so in the log once
#                                            per wait rather than once a minute
#
# A misconfiguration must not look like an idle host: that is why 2 backs off.
set -u

INTERVAL="${DRIVER_LOOP_INTERVAL:-60}"
BACKOFF="${DRIVER_LOOP_MISCONFIG_INTERVAL:-600}"

log() { printf '%s driver-loop: %s\n' "$(date -u '+%Y-%m-%dT%H:%M:%SZ')" "$*"; }

log "starting; interval ${INTERVAL}s, misconfigured backoff ${BACKOFF}s"
log "repo ${DRIVER_LOOP_REPO:-nickbrett1/data-download-drivers}; drivers dir ${DATA_DOWNLOAD_DRIVERS_DIR:-/drivers}"

while :; do
  python3 -m data_download.driver_build --once
  status=$?
  case "${status}" in
    0) log "nothing more to do right now"
       sleep "${INTERVAL}" ;;
    2) log "not configured; waiting ${BACKOFF}s (see the message above)"
       sleep "${BACKOFF}" ;;
    *) log "the job stopped (exit ${status}); polling again in ${INTERVAL}s"
       sleep "${INTERVAL}" ;;
  esac
done
