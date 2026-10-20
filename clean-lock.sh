#!/usr/bin/env bash
# Removes a leftover screen lock (PIN 1234) and system credentials from AVDs after an interrupted system step:
# boot, unlock with the PIN, then the system-cleanup flow of appium_flows.py. Changes are kept in the AVD.
#   ./clean-lock.sh TLS_API_24 TLS_API_28
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"; ADB="$SDK/platform-tools/adb"
PORT=5640; S="emulator-$PORT"; APPIUM_PORT=4740  # above the --jobs slots (5590 + 2n, 4724 + n)
export ANDROID_HOME="$SDK"
log() { echo "[$(date +%H:%M:%S)] $*"; }

appium --port $APPIUM_PORT > "$HERE/appium-clean-lock.log" 2>&1 &
appium_pid=$!
trap 'kill $appium_pid 2>/dev/null' EXIT

for avd in "$@"; do
	log "=== $avd"
	"$SDK/emulator/emulator" -avd "$avd" -port $PORT -gpu host -accel on -no-audio -no-boot-anim -no-snapshot \
		> "$HERE/emulator-clean-lock.log" 2>&1 &
	timeout 180 "$ADB" -s $S wait-for-device
	until [ "$("$ADB" -s $S shell getprop sys.boot_completed | tr -d '\r')" = 1 ]; do sleep 2; done
	sleep 5
	# Unlock: wake, swipe up / menu, PIN
	"$ADB" -s $S shell input keyevent KEYCODE_WAKEUP; "$ADB" -s $S shell input keyevent 82; sleep 2
	"$ADB" -s $S shell input text 1234; "$ADB" -s $S shell input keyevent 66; sleep 3
	"$ADB" -s $S shell svc power stayon true
	log "  keyguard: $("$ADB" -s $S shell dumpsys window policy | grep -m1 -o 'isStatusBarKeyguard=[a-z]*\|mShowingLockscreen=[a-z]*')"
	APPIUM_URL="http://127.0.0.1:$APPIUM_PORT" python3 "$HERE/appium_flows.py" $S system-cleanup
	log "  user credentials left: $("$ADB" -s $S shell "su 0 ls /data/misc/keystore/user_0" | grep -E '^(1000|1010)_' | grep -vc synthetic_password_)"
	log "  PIN left: $("$ADB" -s $S shell "su 0 stat -c %s /data/system/gatekeeper.password.key" 2>/dev/null)"
	# Clean power off, a hard kill right after removing the PIN corrupted the lock data of Android 9
	"$ADB" -s $S shell sync; "$ADB" -s $S shell reboot -p
	for _ in $(seq 60); do "$ADB" devices | grep -q "^$S" || break; sleep 1; done
	"$ADB" devices | grep -q "^$S" && "$ADB" -s $S emu kill >/dev/null
	while "$ADB" devices | grep -q "^$S"; do sleep 2; done
	# adb drops the emulator before its process ends, the next AVD on the port would not start
	while pgrep -f "qemu-system.* -port $PORT( |$)" > /dev/null; do sleep 1; done
done
