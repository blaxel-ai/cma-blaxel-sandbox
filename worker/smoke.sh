#!/bin/sh
# Smoke test for the CMA worker image: proves the runtime the agent's tool calls
# depend on is present and that the poll command exists. The worker image IS the
# agent's execution environment, so this is the contract the orchestrator relies
# on before spawning it.
#
#   docker build -t cma-worker:smoke worker
#   docker run --rm --entrypoint /worker/smoke.sh cma-worker:smoke
#
# It can also be run in-sandbox via the process API against a spawned worker.
set -e

fail() { echo "FAIL: $1"; exit 1; }
ok()   { echo "ok:   $1"; }

# Runtimes/tools the agent uses, plus the worker's own machinery.
#   bash         file/shell tools (Alpine would lack it; Debian base ships it)
#   python3/node common agent runtimes
#   git/curl/tar/unzip  skill download + general use
#   nc           backs the port wait in entrypoint.sh
#   ant          the worker poll binary
#   sandbox-api  the in-sandbox API the orchestrator drives
for bin in bash python3 node git curl tar unzip nc ant sandbox-api; do
    command -v "$bin" >/dev/null 2>&1 || fail "missing $bin"
    ok "$bin present"
done

ant --version >/dev/null 2>&1 || fail "ant --version failed"
ok "ant $(ant --version 2>/dev/null | head -1)"
ant beta:worker poll --help >/dev/null 2>&1 || fail "ant beta:worker poll subcommand missing"
ok "ant beta:worker poll available"

# Working directories the orchestrator and CMA expect.
[ -d /workspace ] || fail "/workspace missing"
[ -d /mnt/session/outputs ] || fail "/mnt/session/outputs missing"
ok "/workspace and /mnt/session/outputs present"

# File tools execute in /workspace, so it must be writable.
( echo smoke > /workspace/.smoke && rm -f /workspace/.smoke ) || fail "/workspace not writable"
ok "/workspace writable"

echo "WORKER IMAGE SMOKE: PASS"
