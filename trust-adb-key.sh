#!/usr/bin/env bash
# Enters the adb key of this machine (~/.android/adbkey.pub, also used by the adb server on the remote machine) into
# /data/misc/adb/adb_keys of every AVD, so adb is trusted explicitly and not only through the key the emulator hands to
# the guest at boot. Changes are kept in the AVD. Needs adb root (google_apis / default images); images without it
# (the Google TV image of TLS_TV_34) are reported, their trust comes from "Always allow" or the emulator.
#   ./trust-adb-key.sh                          # every TLS_* AVD here
#   ./trust-adb-key.sh TLS_API_23 TLS_TV_30     # selected AVDs
#   REMOTE=user@remote-a ./trust-adb-key.sh  # the AVDs on the remote machine (adb through an ssh tunnel)
set -uo pipefail
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
ADB="$SDK/platform-tools/adb"
PUB="$HOME/.android/adbkey.pub"
PORT=5630; SERIAL="emulator-$PORT"
REMOTE="${REMOTE:-}"
SSH=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -o ConnectTimeout=5)
# Remote: the host GPU needs the display of the desktop session there (see run-matrix.sh)
REMOTE_ENV='export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 XAUTHORITY=$(ls /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1);'
log() { echo "[$(date +%H:%M:%S)] $*"; }
[ -f "$PUB" ] || { echo "no $PUB"; exit 1; }
key=$(cut -d' ' -f1 "$PUB")

on_host() { # [-tt] command…: here, or over ssh on REMOTE
	local tty=(); [ "$1" = -tt ] && { tty=(-tt); shift; }
	if [ -n "$REMOTE" ]; then "${SSH[@]}" "${tty[@]}" "$REMOTE" "$REMOTE_ENV exec $(printf '%q ' "$@")"; else "$@"; fi
}
if [ -n "$REMOTE" ]; then
	EMULATOR_ON_HOST=Android/Sdk/emulator/emulator; ADB_ON_HOST=Android/Sdk/platform-tools/adb
	"${SSH[@]}" "$REMOTE" Android/Sdk/platform-tools/adb start-server > /dev/null 2>&1
	export ANDROID_ADB_SERVER_PORT=15039
	"${SSH[@]}" -N -o ExitOnForwardFailure=yes -L "$ANDROID_ADB_SERVER_PORT:127.0.0.1:5037" "$REMOTE" &
	tunnel_pid=$!
	trap 'kill $tunnel_pid 2>/dev/null' EXIT
	for _ in $(seq 1 20); do (exec 3<>/dev/tcp/127.0.0.1/$ANDROID_ADB_SERVER_PORT) 2>/dev/null && break; sleep 1; done
else
	EMULATOR_ON_HOST="$SDK/emulator/emulator"; ADB_ON_HOST="$ADB"
fi

if [ $# -gt 0 ]; then AVDS=("$@"); else mapfile -t AVDS < <(on_host "$EMULATOR_ON_HOST" -list-avds | tr -d '\r' | grep '^TLS_'); fi
adb_shell() { "$ADB" -s "$SERIAL" shell "$@" 2>/dev/null | tr -d '\r'; }

power_off() { # clean power off: a hard kill corrupted Android 9's lock data before (see manual-test.py kill_emulator)
	adb_shell sync
	"$ADB" -s "$SERIAL" shell reboot -p > /dev/null 2>&1
	for _ in $(seq 1 60); do "$ADB" devices | grep -q "^$SERIAL" || break; sleep 1; done
	"$ADB" devices | grep -q "^$SERIAL" && on_host "$ADB_ON_HOST" -s "$SERIAL" emu kill > /dev/null 2>&1
	wait "$1" 2>/dev/null
}

for avd in "${AVDS[@]}"; do
	log "=== $avd${REMOTE:+ on $REMOTE}"
	on_host -tt "$EMULATOR_ON_HOST" -avd "$avd" -port "$PORT" -no-window -no-audio -no-boot-anim -no-snapshot \
		> /tmp/trust-adb-key-emulator.log 2>&1 < /dev/null &
	emulator_pid=$!
	timeout 180 "$ADB" -s "$SERIAL" wait-for-device
	for _ in $(seq 1 90); do [ "$(adb_shell getprop sys.boot_completed)" = 1 ] && break; sleep 2; done
	if [ "$(adb_shell getprop sys.boot_completed)" != 1 ]; then
		log "  no boot (adb state: $("$ADB" -s "$SERIAL" get-state 2>&1))"; power_off "$emulator_pid"; continue
	fi
	# Root by the result, not adb's message: while adbd restarts adb answers differently (or "closed"), the message
	# check reported half of the images as without root
	root=0
	for _ in 1 2 3; do
		reply=$(timeout 30 "$ADB" -s "$SERIAL" root 2>&1)
		[[ "$reply" == *"cannot run as root"* ]] && break  # production build
		sleep 3; timeout 60 "$ADB" -s "$SERIAL" wait-for-device
		for _ in $(seq 1 10); do [ "$(adb_shell id -u)" = 0 ] && { root=1; break; }; sleep 1; done
		[ "$root" = 1 ] && break
	done
	if [ "$root" = 1 ]; then
		"$ADB" -s "$SERIAL" push "$PUB" /data/local/tmp/adbkey.pub > /dev/null
		adb_shell "mkdir -p /data/misc/adb; touch /data/misc/adb/adb_keys
			grep -qF '$key' /data/misc/adb/adb_keys || cat /data/local/tmp/adbkey.pub >> /data/misc/adb/adb_keys
			rm /data/local/tmp/adbkey.pub; chown system:shell /data/misc/adb/adb_keys; chmod 640 /data/misc/adb/adb_keys
			restorecon /data/misc/adb/adb_keys 2>/dev/null; sync"
		count=$(adb_shell "grep -cF '$key' /data/misc/adb/adb_keys")
		log "  adb_keys: key present ($count), $(adb_shell ls -lZ /data/misc/adb/adb_keys | awk '{print $1, $3, $4, $5}')"
		"$ADB" -s "$SERIAL" unroot > /dev/null 2>&1; sleep 3; timeout 60 "$ADB" -s "$SERIAL" wait-for-device
	else
		log "  no adb root (${reply:-no answer}): trusted through \"Always allow\" or the emulator (adb state: $("$ADB" -s "$SERIAL" get-state 2>&1))"
	fi
	power_off "$emulator_pid"
done
