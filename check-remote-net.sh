#!/usr/bin/env bash
# Measures the connection to the remote machines of manual-test.py (--remote): everything of a remote emulator (adb,
# Appium, the mTLS proxy, the streamed and downloaded media) goes through ssh tunnels, a slow link makes the remote
# AVDs slow (player and download steps: 126 - 240 ms ping, 0.5 MB/s).
#   1. ping: packet loss, round trip min / avg / max
#   2. ssh round trip of a trivial command on an open connection (what every Appium / adb call pays)
#   3. ssh throughput down (remote to here, the media) and up, one stream
#   4. cores, load and memory of the remote machine
# Run it when no test is running, the tests share the link and the numbers get worse (warning below).
#
# Usage:
#   ./check-remote-net.sh                              # MANUAL_REMOTE_HOST (comma separated) or both remote machines
#   ./check-remote-net.sh user@remote-b           # one or more machines
#   MB=50 PINGS=20 ./check-remote-net.sh               # size of the throughput test in MB (default 20), pings (10)
set -uo pipefail
MB="${MB:-20}"
PINGS="${PINGS:-10}"
ROUNDS=10
if [ $# -gt 0 ]; then
	HOSTS=("$@")
else
	IFS=, read -r -a HOSTS <<< "${MANUAL_REMOTE_HOST:-user@remote-b,user@remote-a}"
fi
SOCKET_DIR=$(mktemp -d)
trap 'rm -rf "$SOCKET_DIR"' EXIT
SSH_OPTIONS=(-o BatchMode=yes -o ConnectTimeout=5)
now() { date +%s.%N; }
# awk for the arithmetic: no bc needed
calc() { awk "BEGIN { printf \"%.1f\", $1 }"; }

if pgrep -f '[m]anual-test.py --auto' > /dev/null; then
	echo "WARNING: manual-test.py is running, it shares the link: the numbers are worse than the idle link"
fi

for target in "${HOSTS[@]}"; do
	host="${target#*@}"
	echo "=== $target"
	if ! timeout 10 ssh "${SSH_OPTIONS[@]}" "$target" true; then
		echo "  ssh login failed (machine off, ssh key, network)"
		continue
	fi

	# 1. ping
	ping_output=$(ping -c "$PINGS" -i 0.5 -q "$host" 2>&1)
	loss=$(grep -oE '[0-9.]+% packet loss' <<< "$ping_output" | head -1)
	read -r rtt_min rtt_avg rtt_max <<< "$(grep -oE '= [0-9./]+' <<< "$ping_output" | tr -d '= ' | awk -F/ '{print $1, $2, $3}')"
	echo "  ping:        ${loss:-no answer}, round trip min/avg/max ${rtt_min:-?}/${rtt_avg:-?}/${rtt_max:-?} ms"

	# 2. ssh round trip on an open connection
	socket="$SOCKET_DIR/$host"
	ssh "${SSH_OPTIONS[@]}" -M -S "$socket" -fN "$target"
	start=$(now)
	for _ in $(seq "$ROUNDS"); do ssh -S "$socket" "$target" true; done
	end=$(now)
	ssh -S "$socket" -O exit "$target" > /dev/null 2>&1
	ssh_ms=$(calc "($end - $start) * 1000 / $ROUNDS")
	echo "  ssh command: $ssh_ms ms per command (average of $ROUNDS)"

	# 3. throughput, one ssh stream, no compression
	start=$(now)
	bytes=$(ssh "${SSH_OPTIONS[@]}" "$target" "head -c ${MB}000000 /dev/zero" | wc -c)
	end=$(now)
	down=$(calc "$bytes / 1000000 / ($end - $start)")
	start=$(now)
	head -c "${MB}000000" /dev/zero | ssh "${SSH_OPTIONS[@]}" "$target" 'cat > /dev/null'
	end=$(now)
	up=$(calc "$MB / ($end - $start)")
	echo "  throughput:  $down MB/s down (remote to here), $up MB/s up ($MB MB each)"

	# 4. the remote machine
	ssh "${SSH_OPTIONS[@]}" "$target" 'echo "  machine:     $(nproc) cores, load $(cut -d" " -f1-3 /proc/loadavg), $(free -m | awk "/Mem/{print \$3 \"/\" \$2}") MB memory used, $(grep -m1 "model name" /proc/cpuinfo | cut -d: -f2 | sed "s/^ //")"'

	# Verdict: a wired LAN is under 2 ms, a 34 MB test clip should take seconds, not a minute
	verdict=()
	awk "BEGIN { exit !(${rtt_avg:-999} > 20) }" && verdict+=("round trip over 20 ms (Wi-Fi or a busy link?)")
	awk "BEGIN { exit !(${rtt_max:-999} > 100) }" && verdict+=("round trips above 100 ms")
	[ -n "${loss%%0%*}" ] && verdict+=("packet loss $loss")
	awk "BEGIN { exit !($down < 5) }" && verdict+=("under 5 MB/s down: a 34 MB clip takes $(calc "34 / ($down + 0.001)") s")
	if [ ${#verdict[@]} -eq 0 ]; then
		echo "  verdict:     link fine"
	else
		printf '  verdict:     slow link: %s\n' "$(IFS=';'; echo "${verdict[*]}")"
	fi
done
