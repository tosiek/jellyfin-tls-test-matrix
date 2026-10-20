#!/usr/bin/env bash
# Benchmarks the emulator's -gpu modes without the app: boot time, screencap speed, frame times while scrolling Settings and
# playing a video, and a soak (the same load for SOAK seconds) that notes when the emulator vanishes. Runs on the machine
# it is started on (display of the desktop session needed); one emulator at a time, port 5610. Results: table at the end,
# a line per mode in bench-gpu-results.tsv.
#   ./bench-gpu.sh swiftshader_indirect host swangle lavapipe software swiftshader
#   AVD=TLS_API_34 SOAK=300 VIDEO=/tmp/bench-video.mp4 ./bench-gpu.sh host
#   ./bench-gpu.sh --remote user@remote-a host swangle      # the whole script runs there over ssh (video copied)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
if [ "${1:-}" = "--remote" ]; then
	host=$2; shift 2
	video="${VIDEO:-/tmp/bench-video.mp4}"
	[ -f "$video" ] || ffmpeg -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=30 -t 20 -c:v libx264 -pix_fmt yuv420p "$video"
	scp -q "$video" "$host:/tmp/bench-video.mp4"
	# Same environment as manual-test.py's REMOTE_ENV (display of the desktop session, machine settings)
	exec ssh -o BatchMode=yes "$host" "export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/\$(id -u) WAYLAND_DISPLAY=wayland-0 \
XAUTHORITY=\$(ls /run/user/\$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1); [ -f ~/.tls-emulator-env ] && . ~/.tls-emulator-env; \
AVD=${AVD:-TLS_API_34} SOAK=${SOAK:-180} VIDEO=/tmp/bench-video.mp4 bash -s -- $*" < "$0"
fi
AVD="${AVD:-TLS_API_34}"; SOAK="${SOAK:-180}"; PORT=5610; SERIAL="emulator-$PORT"
VIDEO="${VIDEO:-/tmp/bench-video.mp4}"
ADB="$HOME/Android/Sdk/platform-tools/adb"; EMU="$HOME/Android/Sdk/emulator/emulator"
OUT="${OUT:-bench-gpu-results.tsv}"
a() { timeout "${T:-30}" "$ADB" -s "$SERIAL" "$@"; }
now() { date +%s.%N; }
elapsed() { awk -v a="$1" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}'; }
alive() { kill -0 "$EMU_PID" 2>/dev/null && [ "$(a get-state 2>/dev/null)" = "device" ]; }
gfx() {  # frames, janky, p50, p90, p99 of a package since the last reset
	a shell dumpsys gfxinfo "$1" | awk '/Total frames rendered/{f=$NF} /Janky frames:/{j=$3} /50th percentile/{p50=$NF} /90th percentile/{p90=$NF} /99th percentile/{p99=$NF} END{printf "%s/%s %s %s %s", j, f, p50, p90, p99}'
}
swipes() {  # n swipes up and down on the screen
	read -r w h < <(a shell wm size | grep -oE '[0-9]+x[0-9]+' | tail -1 | tr x ' ')
	for ((i = 0; i < $1; i++)); do
		a shell input swipe $((w / 2)) $((h * 3 / 4)) $((w / 2)) $((h / 4)) 120
		a shell input swipe $((w / 2)) $((h / 4)) $((w / 2)) $((h * 3 / 4)) 120
	done
}
cpu_ticks() {  # utime + stime (clock ticks) of the emulator's qemu process
	local pid; pid=$(pgrep -f "[q]emu-system.* -port $PORT( |\$)" | head -1)
	[ -n "$pid" ] && awk '{print $14 + $15}' "/proc/$pid/stat" 2>/dev/null || echo 0
}
play() { a shell am start -a android.intent.action.VIEW -d file:///sdcard/Download/bench.mp4 -t video/mp4 >/dev/null 2>&1; }

bench() {
	local mode=$1 log=/tmp/bench-$1.log boot="-" cap="-" scroll="-" video="-" soak="-" note="" cpu="-"
	"$ADB" start-server >/dev/null 2>&1
	"$EMU" -avd "$AVD" -port $PORT -gpu "$mode" -no-snapshot -no-boot-anim -no-audio >"$log" 2>&1 &
	EMU_PID=$!
	local t0; t0=$(now)
	echo "[$mode] booting"
	while true; do
		kill -0 $EMU_PID 2>/dev/null || { note="emulator exited while booting: $(grep -v '^$' "$log" | tail -1 | cut -c1-80)"; break; }
		[ "$(elapsed "$t0" | cut -d. -f1)" -gt 240 ] && { note="no boot in 240 s"; break; }
		if [ "$(T=5 a shell getprop sys.boot_completed 2>/dev/null | tr -d '\r')" = "1" ] && T=5 a shell pm path android 2>/dev/null | grep -q package:; then
			boot=$(elapsed "$t0"); break
		fi
		sleep 2
	done
	if [ "$boot" != "-" ]; then
		sleep 5
		a shell "input keyevent KEYCODE_WAKEUP; wm dismiss-keyguard; svc power stayon true; settings put global hide_error_dialogs 1" >/dev/null 2>&1
		a push "$VIDEO" /sdcard/Download/bench.mp4 >/dev/null 2>&1
		local c0 w0; c0=$(cpu_ticks); w0=$(now)
		# screencap: 20 calls
		local t; t=$(now)
		for _ in $(seq 20); do a exec-out screencap -p >/dev/null 2>&1; done
		cap=$(awk -v s="$(elapsed "$t")" 'BEGIN{printf "%.0f", s / 20 * 1000}')
		# Settings scrolling
		a shell am start -W -a android.settings.SETTINGS >/dev/null 2>&1; sleep 2
		a shell dumpsys gfxinfo com.android.settings reset >/dev/null 2>&1
		swipes 15
		scroll=$(gfx com.android.settings)
		# video
		play; sleep 3
		pkg=$(a shell dumpsys activity activities | grep -m1 topResumedActivity | grep -oE '[a-zA-Z0-9_.]+/' | head -1 | tr -d /)
		if [ -n "$pkg" ] && [ "$pkg" != "com.google.android.apps.nexuslauncher" ]; then
			a shell dumpsys gfxinfo "$pkg" reset >/dev/null 2>&1; sleep 10; video="$pkg $(gfx "$pkg")"
		else video="no player"; fi
		# soak: scrolling and video again until SOAK seconds are over
		local s0; s0=$(now); soak="ok ${SOAK}s"
		while [ "$(elapsed "$s0" | cut -d. -f1)" -lt "$SOAK" ]; do
			alive || { soak="DIED at $(elapsed "$s0") s: $(grep -v '^$' "$log" | tail -1 | cut -c1-60)"; break; }
			a shell am start -W -a android.settings.SETTINGS >/dev/null 2>&1
			swipes 6
			alive || continue
			play; sleep 8
		done
	fi
	[ "$boot" != "-" ] && cpu=$(awk -v t="$(( $(cpu_ticks) - c0 ))" -v w="$(elapsed "$w0")" -v hz="$(getconf CLK_TCK)" 'BEGIN{printf "%.0f%%", t / hz / w * 100}')
	printf '%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\t%s\n' "$mode" "$boot" "$cap" "$scroll" "$video" "$soak" "$note" "$cpu" "$(hostname)" | tee -a "$OUT" >/dev/null
	a emu kill >/dev/null 2>&1; sleep 5; kill $EMU_PID 2>/dev/null; sleep 2; kill -9 $EMU_PID 2>/dev/null
	wait $EMU_PID 2>/dev/null
	cat >> /tmp/bench-rows.txt <<EOF
$mode|boot ${boot}s|cpu ${cpu} (100% = one core)|screencap ${cap}ms|Settings janky/frames p50 p90 p99: $scroll|video: $video|soak: $soak $note
EOF
}

: > /tmp/bench-rows.txt
[ -f "$VIDEO" ] || ffmpeg -loglevel error -f lavfi -i testsrc2=size=1280x720:rate=30 -t 20 -c:v libx264 -pix_fmt yuv420p "$VIDEO"
for mode in "$@"; do bench "$mode"; done
echo; echo "== $(hostname), $AVD, soak ${SOAK}s"; column -t -s'|' /tmp/bench-rows.txt
