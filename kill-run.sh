#!/usr/bin/env bash
# Stops a manual-test.py run, or one AVD of it, and cleans up what its workers leave behind: emulators (ports
# 5590 + 2n), Appium (4724 + n) of every worker slot n (one per mTLS port in tls-test-proxy/Caddyfile), ssh sessions
# and the tunnels to the remote machines, here and on every --remote of the run (MANUAL_REMOTE_HOST, comma separated,
# plus the machines of the running workers and of the --remote options of the run),
# and adb servers left on the ports of those tunnels.
# A worker has no SIGTERM handler, so killing it alone leaves its emulator holding the port.
#   ./kill-run.sh               # the whole run: Ctrl+C to manual-test.py (SUMMARY.md is written), then cleanup
#   ./kill-run.sh TLS_API_30    # one AVD: its worker, emulator and Appium; the run goes on with the next AVD
#                               # in that slot, the AVD counts as failed ("worker exited … before the report")
#   ./kill-run.sh --dry-run …   # only shows what would be stopped
# Patterns start with [x] so that pkill -f does not match the shell running it (here or over ssh).
set -uo pipefail
REMOTE_DEFAULT="${MANUAL_REMOTE_HOST:-user@remote-a}"
# ssh gives up connecting after 5 s and a command that does not finish in 10 s is cut (a machine that answers late
# must not hang the cleanup, user@remote-b took 30 s to log in)
SSH=(timeout 10 ssh -o BatchMode=yes -o ConnectTimeout=5)
DRY=""; [ "${1:-}" = "--dry-run" ] && { DRY=1; shift; }
HERE="$(cd "$(dirname "$0")" && pwd)"
# Worker slots: the mTLS ports on the Caddyfile line with 8920 (see mtls_ports in manual-test.py)
SLOTS=$(grep -m1 'https://:8920' "$HERE/tls-test-proxy/Caddyfile" | grep -o 'https://:[0-9]*' | wc -l)
EMULATORS=$(for n in $(seq 0 $((SLOTS - 1))); do echo $((5590 + 2 * n)); done | paste -sd'|')
APPIUMS=$(for n in $(seq 0 $((SLOTS - 1))); do echo $((4724 + n)); done | paste -sd'|')
log() { echo "[$(date +%H:%M:%S)] $*"; }

# pkill -f here (host "") or on REMOTE, pattern with a [x] first letter
stop() {
	local host=$1 pattern=$2 signal=${3:-TERM}
	if [ -n "$DRY" ]; then
		if [ -z "$host" ]; then pgrep -af "$pattern" | cut -c1-120; else "${SSH[@]}" "$host" "pgrep -af '$pattern' | cut -c1-120"; fi
		return
	fi
	if [ -z "$host" ]; then pkill "-$signal" -f "$pattern"; else "${SSH[@]}" "$host" "pkill -$signal -f '$pattern'"; fi
}

# Emulator and Appium of a port pair, on its machine; TERM, then KILL what is left after 10 s
stop_slot() {
	local host=$1 port=$2 appium=$3
	log "emulator port $port, Appium $appium${host:+ on $host}"
	stop "$host" "[q]emu-system.* -port $port( |\$)"
	stop "$host" "[a]ppium --port $appium( |\$)"
	[ -n "$DRY" ] && return
	sleep 10
	stop "$host" "[q]emu-system.* -port $port( |\$)" KILL
	stop "$host" "[a]ppium --port $appium( |\$)" KILL
}

workers() { pgrep -f "[m]anual-test.py --auto --skip-build --app .* ${1:-TLS_[A-Z0-9_]+}( |\$)"; }
env_of() { tr '\0' '\n' < "/proc/$1/environ" 2>/dev/null | sed -n "s/^$2=//p"; }

if [ -n "${1:-}" ]; then
	avd=$1
	pid=$(workers "$avd" | head -1)
	[ -z "$pid" ] && { echo "no worker for $avd"; exit 1; }
	port=$(env_of "$pid" MANUAL_PORT); appium=$(env_of "$pid" MANUAL_APPIUM_PORT); host=$(env_of "$pid" MANUAL_REMOTE)
	log "$avd: worker $pid${host:+ (remote $host)}"
	if [ -z "$DRY" ]; then
		pkill -TERM -P "$pid"  # its logcat and ssh sessions
		kill "$pid"; sleep 2; kill -0 "$pid" 2>/dev/null && kill -9 "$pid"
	fi
	stop_slot "$host" "${port:-5590}" "${appium:-4724}"
	exit 0
fi

# Remote machines: the default ones, those of the workers (MANUAL_REMOTE) and of --remote HOST[:JOBS] of the parents,
# read before anything is stopped
REMOTES=$({
	echo "$REMOTE_DEFAULT" | tr ', ' '\n\n'
	for pid in $(workers); do env_of "$pid" MANUAL_REMOTE; done
	for pid in $(pgrep -f '[m]anual-test.py'); do  # python only, not a shell that mentions the script
		[[ $(cat "/proc/$pid/comm" 2>/dev/null) == python* ]] || continue
		tr '\0' ' ' < "/proc/$pid/cmdline" | grep -oE -- '--remote[= ][^ ]+' | sed -E 's/^--remote[= ]//'
	done
} | sed -E 's/:.*$//' | grep . | sort -u)

# Whole run: parents are manual-test.py processes that are not --auto --skip-build workers of one AVD
# (python only: a shell whose command line mentions manual-test.py is no run)
parents=$(pgrep -f '[m]anual-test.py' | while read -r p; do
	[[ $(cat "/proc/$p/comm" 2>/dev/null) == python* ]] && [ -z "$(env_of "$p" MANUAL_RUN_DIR)" ] && echo "$p"
done)
if [ -n "$parents" ]; then
	log "stopping manual-test.py $(echo $parents) (Ctrl+C, summary written)"
	if [ -z "$DRY" ]; then
		kill -INT $parents
		for _ in $(seq 60); do kill -0 $parents 2>/dev/null || break; sleep 1; done
		kill -9 $parents 2>/dev/null
	fi
fi
for pid in $(workers); do
	log "worker $pid still running, killing it"
	[ -z "$DRY" ] && { pkill -TERM -P "$pid"; kill -9 "$pid"; }
done
for host in "" $REMOTES; do
	stop "$host" "[q]emu-system.* -port ($EMULATORS)( |\$)"
	stop "$host" "[a]ppium --port ($APPIUMS)( |\$)"
done
for host in $REMOTES; do
	log "ssh sessions and tunnel to $host"
	stop "" "[s]sh -o BatchMode=yes .*$host"
done
# An adb run while a tunnel was down starts its own server on the tunnel's port (REMOTE_ADB_PORT 15037, 15038, … of
# manual-test.py) and keeps it: the next tunnel gets only [::1]
log "adb servers on the tunnel ports"
stop "" "[a]db -L tcp:150[3-9][0-9] fork-server"
[ -n "$DRY" ] && exit 0
sleep 10
for host in "" $REMOTES; do
	stop "$host" "[q]emu-system.* -port ($EMULATORS)( |\$)" KILL
	stop "$host" "[a]ppium --port ($APPIUMS)( |\$)" KILL
done
log "left here:"; pgrep -af '[q]emu-system|[a]ppium --port' | cut -c1-120
for host in $REMOTES; do
	log "left on $host:"; "${SSH[@]}" "$host" "pgrep -af '[q]emu-system|[a]ppium --port' | cut -c1-120"
done
log "done"
