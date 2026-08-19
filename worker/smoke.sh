#!/bin/sh
set -eu

fail() { echo "FAIL: $1"; exit 1; }
ok() { echo "ok: $1"; }

version_ge() {
    awk -v got="$1" -v want="$2" 'BEGIN {
        split(got, g, "[.]"); split(want, w, "[.]")
        for (i = 1; i <= 3; i++) {
            if ((g[i] + 0) > (w[i] + 0)) exit 0
            if ((g[i] + 0) < (w[i] + 0)) exit 1
        }
        exit 0
    }'
}

require_min_version() {
    version_ge "$2" "$3" || fail "$1 $2 is older than $3"
    ok "$1 $2 >= $3"
}

profile="${CMA_WORKER_PROFILE:-quickstart}"
case "$profile" in quickstart|full) ;; *) fail "unknown profile $profile" ;; esac
ok "profile $profile"

for bin in bash python3 pip uv node npm pnpm git curl wget jq tar zip unzip ssh rg tree sed awk grep diff patch nc ant sandbox-api; do
    command -v "$bin" >/dev/null 2>&1 || fail "missing $bin"
done
ok "core tools present"

require_min_version "Python" "$(python3 -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')" "3.12"
require_min_version "Node.js" "$(node -p 'process.versions.node')" "22"
require_min_version "ant CLI" "$(ant --version 2>/dev/null | awk 'NR == 1 {print $NF}' | sed 's/^v//')" "1.22.1"
ant beta:worker run --help >/dev/null 2>&1 || fail "ant beta:worker run subcommand missing"
ok "ant beta:worker run available"

if [ "$profile" = "full" ]; then
    for bin in go rustc cargo java mvn gradle ruby bundle gem php composer gcc g++ make cmake psql redis-cli docker tmux screen htop vim nano; do
        command -v "$bin" >/dev/null 2>&1 || fail "missing $bin"
    done
    require_min_version "Go" "$(go env GOVERSION | sed 's/^go//')" "1.22"
    require_min_version "Rust" "$(rustc --version | awk '{print $2}')" "1.77"
    require_min_version "Java" "$(java -version 2>&1 | awk -F\" '/version/ {print $2; exit}')" "21"
    require_min_version "Ruby" "$(ruby -e 'print RUBY_VERSION')" "3.3"
    require_min_version "PHP" "$(php -r 'echo PHP_VERSION;')" "8.3"
    require_min_version "GCC" "$(gcc -dumpfullversion -dumpversion)" "13"
    ok "full toolchain present"
fi

[ -d /workspace ] || fail "/workspace missing"
[ -d /mnt/session/outputs ] || fail "/mnt/session/outputs missing"
(echo smoke > /workspace/.smoke && rm -f /workspace/.smoke) || fail "/workspace not writable"
ok "workspace contract"
echo "WORKER IMAGE SMOKE: PASS"
