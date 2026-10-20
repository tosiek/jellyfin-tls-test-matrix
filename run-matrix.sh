#!/usr/bin/env bash
# Runs the TLS test suites of jellyfin-androidtv and jellyfin-android on every emulator, all selected suites on one
# boot, and builds the compatibility matrices (results/MATRIX.md, results-gui/MATRIX.md, results-e2e/MATRIX.md).
#
# The APKs are built once with Gradle, then installed and run with adb on every emulator (no Gradle per emulator).
# Timeouts are derived from the durations measured in previous runs (timings.txt).
#
# Usage:
#   ./run-matrix.sh                         # all AVDs starting with TLS_
#   ./run-matrix.sh TLS_API_23 TLS_API_36   # selected AVDs
#   APPS=tv ./run-matrix.sh TLS_API_23      # only one app (tv, mobile or "tv mobile")
#   SHOW=1 ./run-matrix.sh TLS_API_34       # emulator window to watch the tests
#   MATRIX_GPU=swiftshader_indirect ./run-matrix.sh   # emulator GPU mode (default: host GPU, always with KVM)
#   SKIP_BUILD=1 ./run-matrix.sh            # use the APKs of the previous build
#   READ_ONLY=1 ./run-matrix.sh             # do not keep any changes in the AVD user data
#   RETRY=0 ./run-matrix.sh                 # no second run of the AVDs whose run was cut short (see retry_aborted)
#
# WebView updates for old images (once, kept in the user data of the AVD) from webview-apks/, see update_webview:
#   webview-api<API>.apk   update of the preinstalled WebView (or the next lower API level)
#   provider-api<API>.apk  other WebView provider when the preinstalled one can not be updated (Chrome, WebView Beta)
#   JOBS=2 ./run-matrix.sh                  # two emulators at a time (about 2 - 3 GB RAM each)
#   JOBS=2 REMOTE_JOBS=2 ./run-matrix.sh    # two here, two on the remote machine (REMOTE, default user@remote-a)
#   JOBS=0 REMOTE_JOBS=1 ./run-matrix.sh    # only on the remote machine
#     Remote: ssh key login, SDK in ~/Android/Sdk, the TLS_* AVDs, a desktop session logged in (host GPU). Emulators
#     run there, adb talks to the adb server there through an ssh tunnel (ANDROID_ADB_SERVER_PORT), the e2e suite
#     reaches the test Caddy here through reverse tunnels (8920, 8930 on the remote loopback, the emulator's 10.0.2.2)
#   SUITES="device gui" ./run-matrix.sh     # several suites on one boot (default: device)
#   SUITES=all E2E_USER=… E2E_PASSWORD=… ./run-matrix.sh   # all suites, e2e: real Jellyfin behind tls-test-proxy
#   SUITES=webview ./run-matrix.sh          # update the WebView if older than 80 and show the version in use,
#                                           # with E2E_USER and E2E_PASSWORD also the WebView login test
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
log() { echo "[$(date +%H:%M:%S)] $*"; }
ROOT="$(dirname "$HERE")"
# Suites, all run on one boot of each emulator, results in their own directory:
#   device  TLS matrix (TlsDeviceTests)                       results/
#   gui     GUI tests (package tls.ui)                        results-gui/
#   e2e     real Jellyfin behind tls-test-proxy (opt-in)      results-e2e/
#   webview WebView version after the update, login test      results-webview/
SUITES="${SUITES:-${SUITE:-device}}"
[ "$SUITES" = all ] && SUITES="device gui e2e"
if [[ " $SUITES " == *" e2e "* ]] && { [ -z "${E2E_USER:-}" ] || [ -z "${E2E_PASSWORD:-}" ]; }; then
	echo "Skipping the e2e suite, set E2E_USER and E2E_PASSWORD to run it"
	SUITES=$(echo " $SUITES " | sed 's/ e2e / /' | xargs)
fi
[ -n "$SUITES" ] || { echo "No suite to run"; exit 1; }
if [[ " $SUITES " == *" webview "* ]] && { [ -z "${E2E_USER:-}" ] || [ -z "${E2E_PASSWORD:-}" ]; }; then
	echo "WebView suite without the login test, set E2E_USER and E2E_PASSWORD to run it"
fi

suite_out() { # suite
	case $1 in
		device) echo "$HERE/results" ;;
		*) echo "$HERE/results-$1" ;;
	esac
}

# Post-quantum key exchange (X25519MLKEM768) with the test Caddy on its mTLS port, run by TlsDeviceTests on Android 17+
# (skipped before). Only when the proxy is up: the device suite does not need it otherwise.
if curl -sf -o /dev/null --max-time 5 http://127.0.0.1:8930/ca.pem; then
	PQC_ARGS="-e pqcHost 10.0.2.2 -e pqcPort 8920 -e pqcGroups X25519MLKEM768"
	PQC_ARGS+=" -e pqcCaUrl http://10.0.2.2:8930/ca.pem -e pqcClientUrl http://10.0.2.2:8930/client-ec.p12,http://10.0.2.2:8930/client-rsa.p12"
else
	[[ " $SUITES " == *" device "* ]] && echo "tls-test-proxy is not running, the device suite skips the post-quantum handshake"
	PQC_ARGS=""
fi

# Instrumentation arguments of a suite for an app
suite_filter() { # suite, app
	local pkg; [ "$2" = tv ] && pkg=org.jellyfin.androidtv || pkg=org.jellyfin.mobile
	case $1 in
		device) echo "-e class $pkg.tls.TlsDeviceTests $PQC_ARGS" ;;
		gui) echo "-e package $pkg.tls.ui" ;;
		webview)
			# The WebView update runs on every boot with the mobile app, its login test only with credentials
			[ "$2" = mobile ] && [ -n "${E2E_USER:-}" ] && [ -n "${E2E_PASSWORD:-}" ] \
				&& echo "-e class $pkg.tls.ui.WebViewLoginE2ETests -e e2eUser $E2E_USER -e e2ePassword $E2E_PASSWORD"
			;;
		e2e)
			# Credentials from the environment, never stored
			local classes="$pkg.tls.JellyfinProxyE2ETests"
			if [ "$2" = tv ]; then classes+=",$pkg.tls.ui.ServerAddE2ETests"
			else classes+=",$pkg.tls.ui.ConnectE2ETests,$pkg.tls.ui.WebViewLoginE2ETests"; fi
			echo "-e class $classes -e e2eUser $E2E_USER -e e2ePassword $E2E_PASSWORD"
			;;
	esac
}

# WebView in use and login test result per AVD
webview_matrix() { # results dir
	local dir avd login
	echo "| AVD | API | WebView in use | Login test |"
	echo "| --- | --- | --- | --- |"
	for dir in "$1"/*/; do
		avd=$(basename "$dir")
		login=$(cat "$dir/mobile/summary.txt" 2>/dev/null || echo "–")
		echo "| $avd | $(cat "$dir/api-level" 2>/dev/null || echo "?") | $(cat "$dir/webview-version" 2>/dev/null || echo "?") | $login |"
	done
}

write_matrices() {
	local suite out
	for suite in $SUITES; do
		out=$(suite_out "$suite")
		[ -d "$out" ] || continue
		if [ "$suite" = webview ]; then
			webview_matrix "$out" > "$out/MATRIX.md"
			cat "$out/MATRIX.md"
		else
			python3 "$HERE/matrix.py" "$out" > "$out/MATRIX.md"
		fi
		log "Matrix written to $out/MATRIX.md"
	done
}

SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
EMULATOR="$SDK/emulator/emulator"
# Host GPU like the GUI tests (manual-test.py), explicit: not every AVD has hw.gpu.mode=host
GPU="${MATRIX_GPU:-host}"
# KVM is required, "-accel on" makes the emulator fail instead of falling back to slow software emulation
"$EMULATOR" -accel-check > /dev/null 2>&1 || { echo "KVM not usable, see: $EMULATOR -accel-check"; exit 1; }
ADB="$SDK/platform-tools/adb"
# Not $PORT: other tools set it (seen as PORT=0, the emulator then refused "-port 0")
PORT="${MATRIX_PORT:-5580}"
SERIAL="emulator-$PORT"
JOBS="${JOBS:-1}"
REMOTE_JOBS="${REMOTE_JOBS:-0}"
REMOTE="${REMOTE:-user@remote-a}"
REMOTE_ADB_PORT=15037  # local end of the tunnel to the remote adb server
SSH=(ssh -o BatchMode=yes -o ServerAliveInterval=30 -o ConnectTimeout=5)
# Set for a remote worker: its emulator runs there. The host GPU needs the display of the desktop session logged in
# there (over plain ssh: "Failed to get EGL display"), Ubuntu's GNOME: Wayland with Xwayland on :0
MATRIX_REMOTE="${MATRIX_REMOTE:-}"
REMOTE_ENV='export DISPLAY=:0 XDG_RUNTIME_DIR=/run/user/$(id -u) WAYLAND_DISPLAY=wayland-0 XAUTHORITY=$(ls /run/user/$(id -u)/.mutter-Xwaylandauth.* 2>/dev/null | head -1);'
# A command on the emulator's machine: here, or over ssh (-tt for the emulator: it ends with the connection)
on_host() { # [-tt] command…
	local tty=(); [ "$1" = -tt ] && { tty=(-tt); shift; }
	if [ -n "$MATRIX_REMOTE" ]; then "${SSH[@]}" "${tty[@]}" "$MATRIX_REMOTE" "$REMOTE_ENV exec $(printf '%q ' "$@")"; else "$@"; fi
}
if [ -n "$MATRIX_REMOTE" ]; then EMULATOR_ON_HOST=Android/Sdk/emulator/emulator; ADB_ON_HOST=Android/Sdk/platform-tools/adb
else EMULATOR_ON_HOST="$EMULATOR"; ADB_ON_HOST="$ADB"; fi
APPS="${APPS:-tv mobile}"
# The TV app has no WebView
[ "$SUITES" = webview ] && APPS=mobile
TIMINGS="$HERE/timings.txt"
WEBVIEW_MINIMUM=80  # Constants.MINIMUM_WEB_VIEW_VERSION of jellyfin-android
RUNNER="androidx.test.runner.AndroidJUnitRunner"

export JAVA_HOME="${JAVA_HOME:-/usr/lib/jvm/java-21-openjdk-amd64}"
export ANDROID_SERIAL="$SERIAL"

if [ $# -gt 0 ]; then AVDS=("$@"); else mapfile -t AVDS < <("$EMULATOR" -list-avds | grep '^TLS_'); fi
[ ${#AVDS[@]} -gt 0 ] || { echo "No AVDs found (is the disk with ~/.android/avd mounted?)"; exit 1; }
for avd in "${AVDS[@]}"; do
	"$EMULATOR" -list-avds | grep -qx "$avd" || { echo "AVD $avd not found (is the disk with ~/.android/avd mounted?)"; exit 1; }
done


# --- Timings of previous runs ----------------------------------------------------------------------------------------

# Last measured value of a timing key, or the default
last_timing() { # key, default
	local value
	value=$(grep "^$1 " "$TIMINGS" 2>/dev/null | tail -1 | awk '{print $2}')
	echo "${value:-$2}"
}
record_timing() { # key, seconds; locked, parallel workers share the file
	(
		flock 9
		touch "$TIMINGS"
		grep -v "^$1 " "$TIMINGS" > "$TIMINGS.$$"; echo "$1 $2" >> "$TIMINGS.$$"; mv "$TIMINGS.$$" "$TIMINGS"
	) 9> "$TIMINGS.lock"
}
# Timeout from a previous duration: factor times the last value, at least the minimum
timeout_for() { # key, factor, minimum, default
	local last; last=$(last_timing "$1" "$4")
	local value=$(( last * $2 )); [ "$value" -lt "$3" ] && value=$3
	echo "$value"
}

# --- Build once ------------------------------------------------------------------------------------------------------

declare -A APP_APK TEST_APK APP_ID TEST_ID
apk_info() { # name, project, gradle tasks, apk dir, test apk dir
	local name=$1 project=$2 tasks=$3 apk_dir=$4 test_dir=$5
	if [ "${SKIP_BUILD:-0}" != 1 ]; then
		log "build $name"
		# shellcheck disable=SC2086
		(cd "$ROOT/$project" && ./gradlew $tasks -q > "$HERE/build-$name.log" 2>&1) || { log "build $name failed, see build-$name.log"; exit 1; }
	fi
	APP_APK[$name]=$(ls "$ROOT/$project/app/build/outputs/apk/$apk_dir"/*.apk | head -1)
	TEST_APK[$name]=$(ls "$ROOT/$project/app/build/outputs/apk/$test_dir"/*.apk | head -1)
	APP_ID[$name]=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['applicationId'])" \
		"$ROOT/$project/app/build/outputs/apk/$apk_dir/output-metadata.json")
	TEST_ID[$name]=$(python3 -c "import json,sys; print(json.load(open(sys.argv[1]))['applicationId'])" \
		"$ROOT/$project/app/build/outputs/apk/$test_dir/output-metadata.json")
}
[[ " $APPS " == *" tv "* ]] && apk_info tv jellyfin-androidtv ":app:assembleDebug :app:assembleDebugAndroidTest" debug androidTest/debug
[[ " $APPS " == *" mobile "* ]] && apk_info mobile jellyfin-android ":app:assembleLibreDebug :app:assembleLibreDebugAndroidTest" \
	libre/debug androidTest/libre/debug

# --- Parallel workers ------------------------------------------------------------------------------------------------

# Tunnel for the remote workers: the remote adb server here on REMOTE_ADB_PORT, the test Caddy's 8920 and file server
# 8930 of this machine on the remote loopback. Returns when the remote ends listen.
start_tunnel() {
	"${SSH[@]}" "$REMOTE" Android/Sdk/platform-tools/adb start-server > /dev/null 2>&1
	log "tunnel to $REMOTE (adb, 8920, 8930)"
	"${SSH[@]}" -N -o ExitOnForwardFailure=yes -L "$REMOTE_ADB_PORT:127.0.0.1:5037" -R 8920:127.0.0.1:8920 \
		-R 8930:127.0.0.1:8930 "$REMOTE" &
	tunnel_pid=$!
	for _ in $(seq 1 30); do
		kill -0 "$tunnel_pid" 2>/dev/null || { echo "the ssh tunnel to $REMOTE failed (ports in use there?)"; exit 1; }
		listening=$("${SSH[@]}" "$REMOTE" ss -ltnH 2>/dev/null)
		if [[ "$listening" == *"127.0.0.1:8920 "* && "$listening" == *"127.0.0.1:8930 "* ]] \
			&& (exec 3<>/dev/tcp/127.0.0.1/$REMOTE_ADB_PORT) 2>/dev/null; then return 0; fi
		sleep 1
	done
	echo "the ssh tunnel to $REMOTE did not come up"; kill "$tunnel_pid"; exit 1
}

# AVDs whose run was cut short: an emulator crash (host GPU, gfxstream "null ctx"), a boot or install failure, or an
# instrumentation that did not finish (timeout, abort). Failed tests are results, not a reason to run again.
aborted_avds() {
	local avd suite dir code
	for avd in "${AVDS[@]}"; do
		for suite in $SUITES; do
			for dir in "$(suite_out "$suite")/$avd" "$(suite_out "$suite")/$avd"/*/; do
				code=$(cat "$dir/exit-code" 2>/dev/null)
				if [ -f "$dir/error" ] || { [ -n "$code" ] && [ "$code" != 0 ]; }; then echo "$avd"; continue 3; fi
			done
		done
	done
}

# Runs the aborted AVDs once more at the end, alone and on this machine: the crashes and hangs come with load
# (several emulators, remote machines) and do not repeat on their own
retry_aborted() {
	[ "${RETRY:-1}" = 1 ] && [ -z "${WORKER:-}" ] && [ -z "${RETRYING:-}" ] || return 0
	local avds
	mapfile -t avds < <(aborted_avds)
	[ ${#avds[@]} -gt 0 ] || return 0
	log "run cut short on ${avds[*]}, once more, alone"
	env -u MATRIX_REMOTE RETRYING=1 JOBS=1 REMOTE_JOBS=0 SKIP_BUILD=1 bash "$HERE/$(basename "$0")" "${avds[@]}"
}

if [ $((JOBS + REMOTE_JOBS)) -gt 1 ] || [ "$REMOTE_JOBS" -gt 0 ] && [ -z "${WORKER:-}" ]; then
	# Distribute the AVDs round-robin over the workers, each with its own emulator port; workers JOBS… run remotely
	workers=$((JOBS + REMOTE_JOBS))
	tunnel_pid=""
	[ "$REMOTE_JOBS" -gt 0 ] && start_tunnel
	pids=()
	for ((job = 0; job < workers; job++)); do
		list=()
		for ((i = job; i < ${#AVDS[@]}; i += workers)); do list+=("${AVDS[i]}"); done
		[ ${#list[@]} -gt 0 ] || continue
		remote=(); name="worker $job"
		[ "$job" -ge "$JOBS" ] && { remote=(MATRIX_REMOTE="$REMOTE" ANDROID_ADB_SERVER_PORT="$REMOTE_ADB_PORT"); name+=" remote"; }
		env "${remote[@]}" WORKER=$job JOBS=1 REMOTE_JOBS=0 SKIP_BUILD=1 MATRIX_PORT=$((5580 + job * 2)) \
			bash "$HERE/$(basename "$0")" "${list[@]}" 2>&1 | sed -u "s/^/[$name] /" &
		pids+=($!)
	done
	for pid in "${pids[@]}"; do wait "$pid"; done
	[ -n "$tunnel_pid" ] && kill "$tunnel_pid"
	retry_aborted
	write_matrices
	exit 0
fi

# --- Emulator --------------------------------------------------------------------------------------------------------

# An emulator of a previous run can still be shutting down (listed as offline), give it time to disappear
for _ in $(seq 1 40); do "$ADB" devices | grep -q "^$SERIAL" || break; sleep 3; done
if "$ADB" devices | grep -q "^$SERIAL"; then
	echo "An emulator is still running on port $PORT, stop it first:"
	"$ADB" devices
	exit 1
fi

# Always shut down the emulator, also when the script fails or is interrupted
emulator_pid=""
cleanup() {
	[ -n "$emulator_pid" ] || return 0
	on_host "$ADB_ON_HOST" -s "$SERIAL" emu kill >/dev/null 2>&1 || kill "$emulator_pid" 2>/dev/null
	wait "$emulator_pid" 2>/dev/null
	emulator_pid=""
	# Wait until adb no longer lists the emulator, the next one uses the same port
	for _ in $(seq 1 40); do "$ADB" devices | grep -q "^$SERIAL" || break; sleep 3; done
}
trap cleanup EXIT
trap 'exit 130' INT TERM

adb_shell() { "$ADB" -s "$SERIAL" shell "$@" 2>/dev/null | tr -d '\r'; }

wait_for_boot() { # timeout
	local deadline=$((SECONDS + $1))
	timeout "$1" "$ADB" -s "$SERIAL" wait-for-device || return 1
	# Booted, package manager answers (installs hung on older images before) and boot animation finished. Twice, 10 s
	# apart: API 28 restarts its system services right after boot_completed (surfaceflinger abort), the settings
	# below then failed
	local check
	for check in 1 2; do
		until [ "$(adb_shell getprop sys.boot_completed)" = 1 ] && adb_shell pm path android | grep -q "package:" \
			&& [ "$(adb_shell getprop init.svc.bootanim)" != running ]; do
			[ $SECONDS -lt $deadline ] || return 1
			sleep 2
		done
		[ $check = 2 ] || sleep 10
	done
	for setting in window_animation_scale transition_animation_scale animator_duration_scale; do
		adb_shell settings put global "$setting" 0
	done
	# Keep the screen on while the emulator runs, GUI tests need window focus
	adb_shell svc power stayon true
	adb_shell settings put system screen_off_timeout 1800000
	wake_device
}

# WebView the app actually loads, reported by WebViewProbe of the installed mobile test APK: "package version"
webview_in_use() {
	local status
	status=$(timeout 120 "$ADB" -s "$SERIAL" shell am instrument -r -w -e class org.jellyfin.mobile.tls.WebViewProbe \
		"${TEST_ID[mobile]}/$RUNNER" 2>/dev/null | tr -d '\r')
	local pkg version
	pkg=$(echo "$status" | sed -n 's/^INSTRUMENTATION_STATUS: webviewPackage=//p' | head -1)
	version=$(echo "$status" | sed -n 's/^INSTRUMENTATION_STATUS: webviewVersion=//p' | head -1)
	[ -n "$pkg" ] && [ -n "$version" ] && echo "$pkg $version"
}

# Major version of the WebView the app actually loads (an installed WebView may be unused):
# 1. WebViewProbe in the app (exact on every API level, needs the installed mobile test APK)
# Without the probe:
# 2. "dumpsys webviewupdate" (API 26+, prints nothing on API 24 - 25)
# 3. library of the last WebView preparation in the log (RelroFileCreator, at boot and on provider changes)
# 4. selected provider, else the installed package (Android 6 always uses com.android.webview on these images)
webview_major() {
	local version="" provider path apk pkg="" probe
	probe=$(webview_in_use)
	if [ -n "$probe" ]; then version=${probe#* }; echo "${version%%.*}"; return; fi
	version=$(adb_shell dumpsys webviewupdate | grep -m1 "Current WebView package" | sed -n 's/.*, \([0-9]*\)\..*/\1/p')
	[ -n "$version" ] && { echo "$version"; return; }

	# e.g. /system/app/webview/webview.apk!/lib/x86_64/…, /data/app/com.android.chrome-1/base.apk!/lib/…,
	# /data/app/com.android.webview-1/lib/x86_64/… (extracted libraries)
	local relro; relro=$(adb_shell logcat -d -s WebViewFactory:V | grep "RelroFileCreator" | tail -1)
	path=$(echo "$relro" | sed -n 's/.*64-bit lib: \([^ ,]*\).*/\1/p')
	# 32-bit only images
	[[ "$path" == /* ]] || path=$(echo "$relro" | sed -n 's/.*32-bit lib: \([^ ,]*\).*/\1/p')
	if [ -n "$path" ]; then
		if [[ "$path" == *.apk!* ]]; then apk="${path%%.apk!*}.apk=" ; else apk="${path%%/lib/*}/"; fi
		pkg=$(adb_shell pm list packages -f | grep -m1 -F "package:$apk" | sed 's/.*=//')
	fi
	if [ -z "$pkg" ]; then
		provider=$(adb_shell settings get global webview_provider)
		if [ -n "$provider" ] && [ "$provider" != null ]; then pkg=$provider
		elif adb_shell pm path com.android.webview | grep -q "package:"; then pkg=com.android.webview
		else pkg=com.google.android.webview; fi
	fi
	version=$(adb_shell dumpsys package "$pkg" | grep -m1 versionName | sed -n 's/.*versionName=\([0-9]*\).*/\1/p')
	echo "$version"
}

# Version after an install or provider change, the system prepares the new WebView in the background
webview_after_change() {
	local version deadline=$((SECONDS + 20))
	while version=$(webview_major); [ "${version:-0}" -lt "$WEBVIEW_MINIMUM" ] 2>/dev/null && [ $SECONDS -lt $deadline ]; do
		sleep 2
	done
	echo "$version"
}

# APK for an API level from webview-apks/: <prefix><API>.apk or the next lower API level
pick_apk() { # prefix, api level
	local api
	for ((api = $2; api >= 21; api--)); do
		[ -f "$HERE/webview-apks/$1$api.apk" ] && { echo "$HERE/webview-apks/$1$api.apk"; return 0; }
	done
	return 1
}

apk_package() { # apk
	local tools; tools=$(ls -d "$SDK"/build-tools/* 2>/dev/null | sort -V | tail -1)
	"$tools/aapt2" dump badging "$1" 2>/dev/null | sed -n "s/^package: name='\([^']*\)'.*/\1/p"
}

# Make the WebView at least the minimum version of the mobile app, kept in the user data of the AVD:
# 1. update the preinstalled WebView (webview-api<N>.apk), only possible when the image uses Google's signing key
# 2. otherwise install another allowed WebView provider (provider-api<N>.apk: Chrome on Android 7 - 9,
#    WebView Beta/Dev/Canary on Android 10+) and select it with "cmd webviewupdate set-webview-implementation"
update_webview() { # api level
	local current apk pkg; current=$(webview_major)
	log "  WebView in use: $(webview_in_use || echo "${current:-?} (probe failed)")"
	# Unknown version: do not install anything blindly
	[ -n "$current" ] || { log "  WebView version unknown, not updated"; return 0; }
	[ "$current" -ge "$WEBVIEW_MINIMUM" ] 2>/dev/null && return 0
	local incompatible="$HERE/webview-apks/.incompatible-$avd"

	# Only an update of an installed WebView package (the APK of a lower API level may be another package)
	if [ ! -f "$incompatible" ] && apk=$(pick_apk webview-api "$1") \
		&& ! adb_shell pm path "$(apk_package "$apk")" | grep -q "package:"; then
		log "  $(basename "$apk") is $(apk_package "$apk"), not installed on this image, no update possible"
	elif [ ! -f "$incompatible" ] && [ -n "${apk:-}" ]; then
		if timeout 900 "$ADB" -s "$SERIAL" install --no-streaming -r "$apk" > "$OUT/$avd/webview-install.log" 2>&1; then
			local updated; updated=$(webview_after_change)
			log "  WebView updated from ${current:-?} to ${updated:-?} ($(basename "$apk"))"
			[ "${updated:-0}" -ge "$WEBVIEW_MINIMUM" ] 2>/dev/null && return 0
			# Installed but not used, e.g. Android 6 always uses the WebView of the system image
			echo "installed WebView is not used by the system" > "$incompatible"
		fi
		log "  WebView update with $(basename "$apk") failed, see webview-install.log"
		grep -q "INSTALL_FAILED_UPDATE_INCOMPATIBLE" "$OUT/$avd/webview-install.log" \
			&& echo "signature of the preinstalled WebView differs" > "$incompatible"
	fi

	if [ "$1" -ge 24 ] && apk=$(pick_apk provider-api "$1"); then
		pkg=$(apk_package "$apk")
		# Only providers of the image's list, e.g. Chrome is no WebView provider on Android 10+ (API 24 prints no list)
		if [ "$1" -ge 26 ] && ! adb_shell dumpsys webviewupdate | grep -q " $pkg "; then
			log "  WebView ${current} older than $WEBVIEW_MINIMUM, $pkg ($(basename "$apk")) is no WebView provider on API $1"
		elif timeout 900 "$ADB" -s "$SERIAL" install --no-streaming -r "$apk" > "$OUT/$avd/webview-provider.log" 2>&1; then
			# The image may have the preinstalled Chrome uninstalled for user 0, the provider must be installed for all users
			adb_shell pm install-existing "$pkg" >/dev/null
			adb_shell pm enable "$pkg" >/dev/null
			adb_shell cmd webviewupdate set-webview-implementation "$pkg" >> "$OUT/$avd/webview-provider.log"
			# Validity of every provider, explains why a provider is not used
			adb_shell dumpsys webviewupdate >> "$OUT/$avd/webview-provider.log"
			log "  WebView provider $pkg ($(basename "$apk")): WebView ${current:-?} -> $(webview_after_change)"
		else
			log "  WebView provider $(basename "$apk") failed, see webview-provider.log"
		fi
	elif [ -z "${apk:-}" ]; then
		log "  WebView ${current:-?} older than $WEBVIEW_MINIMUM, no APK in webview-apks/ for API $1"
	fi
}

wake_device() {
	adb_shell input keyevent KEYCODE_WAKEUP >/dev/null
	adb_shell wm dismiss-keyguard >/dev/null
	adb_shell input keyevent 82 >/dev/null
	adb_shell am broadcast -a android.intent.action.CLOSE_SYSTEM_DIALOGS >/dev/null
}

install_apk() { # apk, timeout; retried with a restarted adb server, installs can hang on emulators
	local attempt
	for attempt in 1 2 3; do
		# Streamed installs hang on old images (seen on API 24), copy the APK first there
		local mode=(); [ "$(cat "$OUT/$avd/api-level" 2>/dev/null || echo 99)" -le 25 ] && mode=(--no-streaming)
		if timeout "$2" "$ADB" -s "$SERIAL" install "${mode[@]}" -r -t "$1" > /dev/null 2>&1; then return 0; fi
		log "  install of $(basename "$1") failed or timed out (attempt $attempt)"
		# Reconnect only this emulator, restarting the adb server would disturb parallel workers
		"$ADB" -s "$SERIAL" reconnect >/dev/null 2>&1; sleep 3
		timeout 120 "$ADB" -s "$SERIAL" wait-for-device
	done
	return 1
}

install_app() { # name; app and test APK
	local name=$1 start=$SECONDS
	local install_timeout; install_timeout=$(timeout_for "install-$avd-$name" 4 60 60)
	install_apk "${APP_APK[$name]}" "$install_timeout" && install_apk "${TEST_APK[$name]}" "$install_timeout" || return 1
	record_timing "install-$avd-$name" $((SECONDS - start))
}

# The emulator can crash during a test (seen with the host GPU: gfxstream "null ctx"), adb then waits forever
emulator_alive() {
	[ "$(timeout 10 "$ADB" -s "$SERIAL" get-state 2>/dev/null)" = device ]
}

mark_failed() { # name, reason; every suite of the app on this AVD that did not pass already
	local suite dir
	for suite in $SUITES; do
		[ -n "$(suite_filter "$suite" "$1")" ] || continue
		dir="$(suite_out "$suite")/$avd/$1"
		[ "$(cat "$dir/exit-code" 2>/dev/null)" = 0 ] && continue
		mkdir -p "$dir"
		[ -f "$dir/exit-code" ] || echo 1 > "$dir/exit-code"
		echo "$2" > "$dir/error"
	done
}

run_app() { # name, installed (1: already installed); runs every suite
	local name=$1 suite dest start
	if [ "$emulator_dead" = 1 ]; then
		log "  $name: skipped, the emulator crashed"
		mark_failed "$name" "emulator crashed"
		return
	fi
	if [ "${2:-0}" != 1 ] && ! install_app "$name"; then
		log "  $name: install failed"
		mark_failed "$name" "install failed"
		return
	fi

	for suite in $SUITES; do
		[ -n "$(suite_filter "$suite" "$name")" ] || continue
		dest="$(suite_out "$suite")/$avd/$name"
		mkdir -p "$dest"
		local test_timeout; test_timeout=$(timeout_for "tests-$suite-$avd-$name" 3 300 300)
		start=$SECONDS
		timeout 30 "$ADB" -s "$SERIAL" logcat -c
		wake_device
		# shellcheck disable=SC2046
		timeout "$test_timeout" "$ADB" -s "$SERIAL" shell am instrument -r -w $(suite_filter "$suite" "$name") \
			"${TEST_ID[$name]}/$RUNNER" > "$dest/instrument.txt" 2>&1
		local code=$?
		echo $code > "$dest/exit-code"
		record_timing "tests-$suite-$avd-$name" $((SECONDS - start))

		local summary; summary=$(python3 "$HERE/instrument2junit.py" "$dest/instrument.txt" "$dest/TEST-results.xml" "$avd-$name")
		# The summary only counts reported tests, an aborted run (timeout, crash) can look clean
		[ "$code" = 0 ] || summary+=", instrumentation exit $code"
		echo "$summary" > "$dest/summary.txt"
		if ! emulator_alive; then
			emulator_dead=1
			log "  $name $suite: $summary, the emulator crashed, see $(basename "$OUT")/$avd/emulator.log"
			mark_failed "$name" "emulator crashed"
			return
		fi
		timeout 60 "$ADB" -s "$SERIAL" logcat -d -v threadtime > "$dest/logcat.txt" 2>/dev/null
		grep "TlsSupport" "$dest/logcat.txt" | sed 's/.*TlsSupport: //' > "$dest/tls-support.txt"
		grep "TlsE2E" "$dest/logcat.txt" | sed 's/.*TlsE2E *: //' > "$dest/e2e.txt"
		timeout 60 "$ADB" -s "$SERIAL" logcat -d -b crash > "$dest/crash.txt" 2>/dev/null || true
		log "  $name $suite: $summary, $(grep -c '|' "$dest/tls-support.txt") matrix lines ($((SECONDS - start))s)"
	done

	timeout 60 "$ADB" -s "$SERIAL" uninstall "${TEST_ID[$name]}" > /dev/null 2>&1
	timeout 60 "$ADB" -s "$SERIAL" uninstall "${APP_ID[$name]}" > /dev/null 2>&1
}

for avd in "${AVDS[@]}"; do
	log "=== $avd"
	emulator_dead=0
	for suite in $SUITES; do out=$(suite_out "$suite"); rm -rf "${out:?}/$avd" && mkdir -p "$out/$avd"; done
	OUT=$(suite_out "${SUITES%% *}")  # emulator log and API level of the first suite, copied to the others

	# SHOW=1 opens an emulator window, default is headless; both with the host GPU and KVM
	if [ "${SHOW:-0}" = 1 ]; then display=(); else display=(-no-window); fi
	display+=(-gpu "$GPU" -accel on)
	# User data is kept (e.g. WebView updates), READ_ONLY=1 discards all changes on exit
	if [ "${READ_ONLY:-0}" = 1 ]; then read_only=(-read-only); else read_only=(); fi
	[ -n "$MATRIX_REMOTE" ] && display=(-no-window -gpu "$GPU" -accel on)  # no window there
	on_host -tt "$EMULATOR_ON_HOST" -avd "$avd" -port "$PORT" "${display[@]}" "${read_only[@]}" -no-audio -no-boot-anim \
		-no-snapshot > "$OUT/$avd/emulator.log" 2>&1 < /dev/null &
	emulator_pid=$!

	boot_start=$SECONDS
	if wait_for_boot "$(timeout_for "boot-$avd" 3 120 200)"; then
		record_timing "boot-$avd" $((SECONDS - boot_start))
		adb_shell getprop ro.build.version.sdk > "$OUT/$avd/api-level"
		log "  booted in $((SECONDS - boot_start))s, API $(cat "$OUT/$avd/api-level")"
		ANDROID_SERIAL="$SERIAL" "$HERE/push-certs.sh" > /dev/null 2>&1 || log "  copying certificates to Downloads failed"
		# The mobile app needs a current WebView, WebViewProbe of its test APK reports the WebView in use
		mobile_installed=0
		if [[ " $APPS " == *" mobile "* ]] && install_app mobile; then
			mobile_installed=1
			update_webview "$(cat "$OUT/$avd/api-level")"
			if [[ " $SUITES " == *" webview "* ]]; then
				version=$(webview_in_use || echo "$(webview_major) (probe failed)")
				echo "$version" > "$(suite_out webview)/$avd/webview-version"
				log "  WebView now: $version"
			fi
		fi
		for suite in $SUITES; do cp "$OUT/$avd/api-level" "$(suite_out "$suite")/$avd/" 2>/dev/null; done
		[[ " $APPS " == *" tv "* ]] && run_app tv
		[[ " $APPS " == *" mobile "* ]] && run_app mobile "$mobile_installed"
	else
		log "  boot timeout"
		echo "boot timeout" > "$OUT/$avd/error"
	fi

	cleanup
done

if [ -z "${WORKER:-}" ]; then
	retry_aborted
	write_matrices
fi
