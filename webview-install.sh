#!/usr/bin/env bash
# Installs the WebView of webview-apks/ into the old AVDs (kept in the user data of the AVD, once per AVD): boots every
# given AVD alone (port 5610, cold boot), installs its APK with --no-streaming (a streamed install of a 260 MB APK hung on
# Android 7), prints the version that is in use. Stock WebViews of API 30 and newer are 80+, nothing to install there.
# API -> file: 23 webview-api23.apk (88), 24 webview-api24.apk (119), 26 provider-api26.apk (Chrome 126, the AOSP Android 8
# image accepts no com.android.webview), 28 webview-api28.apk (135, not 138: see TESTING.md), 29 webview-api29.apk (155).
#   ./webview-install.sh                                  # here, API 23 24 26 28 29
#   ./webview-install.sh 28 29                            # here, only these
#   ./webview-install.sh -r user@remote-a            # on a remote machine (needs its webview-apks/, ./sync-remotes.sh)
#   ./webview-install.sh -c [-r HOST] [API...]            # only check: boots the AVDs and prints the WebView in use, installs nothing
# Needs no other emulator running on the machine, and its desktop session for the window.
set -uo pipefail

REMOTE=""; CHECK=""
while getopts "r:ch" opt; do
	case $opt in r) REMOTE=$OPTARG ;; c) CHECK=1 ;; *) sed -n '2,/^set -u/p' "$0" | sed '$d;s/^# \?//'; exit 1 ;; esac
done
shift $((OPTIND - 1))
AVDS=("${@:-23 24 26 28 29}"); read -ra AVDS <<< "${AVDS[*]}"

if [ -n "$REMOTE" ]; then
	# The script runs there from a copy (stdin free for adb), the output comes back here
	scp -q "$0" "$REMOTE:.webview-install.sh" || exit 1
	exec ssh -o BatchMode=yes "$REMOTE" "bash ~/.webview-install.sh ${CHECK:+-c} ${AVDS[*]}"
fi

# Window of the desktop session over ssh and the machine's own emulator settings (the same as manual-test.py REMOTE_ENV)
if [ -z "${DISPLAY:-}" ]; then
	export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0
	export XAUTHORITY=$(ls /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1)
fi
[ -f ~/.tls-emulator-env ] && . ~/.tls-emulator-env
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
ADB="$SDK/platform-tools/adb"; EMU="$SDK/emulator/emulator"; S=emulator-5610
APKS="$(cd "$(dirname "$0")" 2>/dev/null && pwd)/webview-apks"
[ -d "$APKS" ] || APKS="$HOME/StudioProjects/tls-test-matrix/webview-apks"
declare -A APK=([23]=webview-api23.apk [24]=webview-api24.apk [26]=provider-api26.apk [28]=webview-api28.apk [29]=webview-api29.apk)
a() { timeout "${T:-30}" "$ADB" -s $S "$@" < /dev/null 2>&1 | tr -d '\r'; }

if pgrep -f "[q]emu-system" > /dev/null; then echo "an emulator is running on this machine, stop it first"; exit 1; fi
for n in "${AVDS[@]}"; do
	file=${APK[$n]:-}
	[ -n "$file" ] && [ -f "$APKS/$file" ] || { echo "### API $n: no APK ($file) in $APKS, skipped"; continue; }
	"$EMU" -avd "TLS_API_$n" -port 5610 -no-snapshot -no-boot-anim -no-audio > "/tmp/webview-install-$n.log" 2>&1 < /dev/null &
	pid=$!; t0=$(date +%s); ok=0
	while [ $(( $(date +%s) - t0 )) -lt 400 ]; do
		kill -0 $pid 2>/dev/null || break
		# Android 6: pm list packages android (pm path android prints nothing there)
		[ "$(a shell getprop sys.boot_completed)" = 1 ] && a shell pm list packages android | grep -q package:android && { ok=1; break; }
		sleep 3
	done
	echo "### API $n boot ok=$ok ($(( $(date +%s) - t0 ))s) $file"
	if [ $ok = 1 ]; then
		sleep 5
		if [ -z "$CHECK" ]; then
			echo "install: $(T=900 a install --no-streaming -r -d "$APKS/$file" | tail -n 1)"
			# The package manager writes its package list about 10 s after a change: killed sooner, the update was gone
			# after the next boot (API 28 and 29 on every machine)
			sleep 15
			a shell sync
		fi
		if [ "$n" -le 24 ]; then echo "version: $(a shell dumpsys package com.android.webview | grep -m1 versionName | tr -d ' ')"  # no webviewupdate before Android 8
		else
			for _ in $(seq 1 15); do a shell dumpsys webviewupdate | grep -q "Current WebView package (name" && break; sleep 2; done
			a shell dumpsys webviewupdate | grep -E "Current WebView package|Valid package" | head -n 2
		fi
	fi
	# Power off the guest (files written), only then end the emulator
	a shell reboot -p > /dev/null
	for _ in $(seq 1 30); do kill -0 $pid 2>/dev/null || break; sleep 2; done
	a emu kill > /dev/null; sleep 2; kill $pid 2>/dev/null; sleep 1; kill -9 $pid 2>/dev/null; wait $pid 2>/dev/null
done
echo "WEBVIEW INSTALL DONE"
