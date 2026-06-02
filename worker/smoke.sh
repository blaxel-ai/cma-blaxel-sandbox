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

version_ge() {
    awk -v got="$1" -v want="$2" 'BEGIN {
        split(got, g, ".")
        split(want, w, ".")
        for (i = 1; i <= 3; i++) {
            gv = g[i] + 0
            wv = w[i] + 0
            if (gv > wv) exit 0
            if (gv < wv) exit 1
        }
        exit 0
    }'
}

require_min_version() {
    name="$1"
    current="$2"
    minimum="$3"
    version_ge "$current" "$minimum" || fail "$name $current is older than $minimum"
    ok "$name $current >= $minimum"
}

# Anthropic cloud sandbox reference compatibility: language runtimes, package
# managers, database clients, and utilities documented for managed cloud
# sandboxes, with version floors checked below.
for bin in \
    bash python3 pip uv node npm yarn pnpm go rustc cargo java mvn gradle \
    ruby bundle gem php composer gcc g++ make cmake sqlite3 psql redis-cli \
    git curl wget jq tar zip unzip ssh scp tmux screen docker rg tree htop \
    sed awk grep vim nano diff patch nc ant sandbox-api
do
    command -v "$bin" >/dev/null 2>&1 || fail "missing $bin"
    ok "$bin present"
done

require_min_version "Python" "$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')" "3.12"
require_min_version "Node.js" "$(node -p 'process.versions.node')" "20"
require_min_version "Go" "$(go env GOVERSION | sed 's/^go//')" "1.22"
require_min_version "Rust" "$(rustc --version | awk '{print $2}')" "1.77"
require_min_version "Java" "$(java -version 2>&1 | awk -F\" '/version/ {print $2; exit}')" "21"
require_min_version "Ruby" "$(ruby -e 'print RUBY_VERSION')" "3.3"
require_min_version "PHP" "$(php -r 'echo PHP_VERSION;')" "8.3"
require_min_version "GCC" "$(gcc -dumpfullversion -dumpversion)" "13"

ant --version >/dev/null 2>&1 || fail "ant --version failed"
ok "ant $(ant --version 2>/dev/null | head -1)"
ant beta:worker poll --help >/dev/null 2>&1 || fail "ant beta:worker poll subcommand missing"
ok "ant beta:worker poll available"
bundle --version >/dev/null 2>&1 || fail "bundle --version failed"
ok "$(bundle --version | head -1)"
php --version >/dev/null 2>&1 || fail "php --version failed"
ok "$(php --version | head -1)"
composer --version >/dev/null 2>&1 || fail "composer --version failed"
ok "$(composer --version | head -1)"

# Working directories the orchestrator and CMA expect.
[ -d /workspace ] || fail "/workspace missing"
[ -d /mnt/session/outputs ] || fail "/mnt/session/outputs missing"
ok "/workspace and /mnt/session/outputs present"

# File tools execute in /workspace, so it must be writable.
( echo smoke > /workspace/.smoke && rm -f /workspace/.smoke ) || fail "/workspace not writable"
ok "/workspace writable"

echo "WORKER IMAGE SMOKE: PASS"
