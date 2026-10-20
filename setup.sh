#!/usr/bin/env bash
# Sets up the test environment from scratch, every step is skipped when its result already exists:
#   1. checks: KVM, SDK command line tools, openssl, caddy; Appium for manual-test.py (UiAutomator2 driver 5.x, the
#      last one that runs on Android 6 - 7) and its Python client
#   2. SDK packages: emulator, platform-tools, build-tools and the system images of the AVDs
#   3. the TLS_* AVDs (existing ones are not changed)
#   4. test PKI of the proxy (tls-test-proxy/generate.sh)
#   5. WebView APKs: only checked, they are downloaded by hand (sources in WEBVIEW-APKS.md); the file manager of
#      checklists/tv-picker.md (Material Files from F-Droid) is downloaded to tv-picker/ and checked
#   6. WebView update on the AVDs (SUITES=webview ./run-matrix.sh) when the APKs are there
#   7. the test Caddy: only checked, start it in its own terminal
# Jellyfin is not part of this script.
#
# Usage:
#   ./setup.sh                      # everything
#   ./setup.sh TLS_API_23 TLS_TV_30 # only these AVDs
#   SKIP_WEBVIEW=1 ./setup.sh       # no WebView update (it builds the mobile app and boots every AVD)
#   ANDROID_AVD_HOME=/tmp/avds ./setup.sh TLS_API_36   # AVDs somewhere else (avdmanager and emulator use it)
set -uo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="${ANDROID_HOME:-$HOME/Android/Sdk}"
export ANDROID_HOME="$SDK" ANDROID_SDK_ROOT="$SDK"
TOOLS="$SDK/cmdline-tools/latest/bin"
EMULATOR="$SDK/emulator/emulator"
AVD_HOME="${ANDROID_AVD_HOME:-$HOME/.android/avd}"
log() { echo "[$(date +%H:%M:%S)] $*"; }
problems=()

# name, system image, device profile
AVD_TABLE="
TLS_API_23 system-images;android-23;default;x86_64 pixel_5
TLS_API_24 system-images;android-24;default;x86_64 pixel_5
TLS_API_26 system-images;android-26;default;x86_64 pixel_5
TLS_API_28 system-images;android-28;default;x86_64 pixel_5
TLS_API_29 system-images;android-29;default;x86_64 pixel_5
TLS_API_30 system-images;android-30;default;x86_64 pixel_5
TLS_API_33 system-images;android-33;default;x86_64 pixel_5
TLS_API_34 system-images;android-34;default;x86_64 pixel_5
TLS_API_36 system-images;android-36;default;x86_64 pixel_5
TLS_API_37 system-images;android-37.0;google_apis;x86_64 pixel_5
TLS_TV_30 system-images;android-30;android-tv;x86 tv_1080p
TLS_TV_34 system-images;android-34;google-tv;x86 tv_1080p
"
# API 30: the AOSP image, the Google APIs image crashed system_server in the tests
# API 37 (Android 17): there is no AOSP (default) image, Google APIs without Play Store is the closest (adb root works)

# Virtual CPUs of an AVD: 4 on every machine (the remotes too, they had 2).
# ./check-avds.py knows a value that differs per machine (PER_MACHINE).
AVD_CORES="${AVD_CORES:-4}"

# Settings of every AVD: host GPU, KVM-friendly x86 images, cold boot (the tests need a fresh boot), no Play Store
AVD_SETTINGS="
hw.cpu.ncore=$AVD_CORES
hw.ramSize=2048M
vm.heapSize=256M
hw.gpu.enabled=yes
hw.gpu.mode=host
fastboot.forceColdBoot=yes
fastboot.forceFastBoot=no
disk.dataPartition.size=6G
hw.audioInput=no
PlayStore.enabled=no
"

selected=("$@")
wanted() { [ ${#selected[@]} -eq 0 ] || [[ " ${selected[*]} " == *" $1 "* ]]; }

# --- 1. Checks ---------------------------------------------------------------------------------------------------

[ -x "$TOOLS/sdkmanager" ] || { echo "SDK command line tools missing: Android Studio > SDK Manager > SDK Tools > \
Android SDK Command-line Tools (latest), or unpack them to $SDK/cmdline-tools/latest"; exit 1; }
command -v openssl > /dev/null || { echo "openssl missing"; exit 1; }
command -v caddy > /dev/null || problems+=("caddy is not installed (https://caddyserver.com/docs/install)")
if [ ! -r /dev/kvm ] || [ ! -w /dev/kvm ]; then
	echo "KVM not usable: /dev/kvm missing or not accessible (sudo usermod -aG kvm \$USER, log in again)"; exit 1
fi

# Appium of manual-test.py / appium_flows.py. The UiAutomator2 driver 6.x+ installs a server that needs API 26, 5.0.7 is
# the last one for API 23 - 25. chromedriver for the WebView is downloaded by manual-test.py per AVD.
APPIUM_VERSION=3.8.0
UIAUTOMATOR2_VERSION=5.0.7
PYTHON_CLIENT_VERSION=6.0.7
node_major=$(node --version 2>/dev/null | sed 's/^v\([0-9]*\).*/\1/')
if [ "${node_major:-0}" -lt 20 ]; then
	problems+=("Node.js 20+ missing (Appium $APPIUM_VERSION): sudo apt install nodejs npm")
elif ! command -v appium > /dev/null; then
	# Global npm packages are in /usr/local, owned by root: not installed by this script
	problems+=("Appium missing: sudo npm install -g appium@$APPIUM_VERSION, then ./setup.sh again")
else
	driver=$(appium driver list --installed --json 2>/dev/null | python3 -c \
		"import json, sys; print(json.load(sys.stdin).get('uiautomator2', {}).get('version', ''))" 2>/dev/null)
	if [ "${driver%%.*}" != 5 ]; then
		log "installing the UiAutomator2 driver $UIAUTOMATOR2_VERSION (installed: ${driver:-none})"
		[ -n "$driver" ] && appium driver uninstall uiautomator2 > /dev/null 2>&1
		appium driver install "uiautomator2@$UIAUTOMATOR2_VERSION" > /dev/null 2>&1 \
			|| problems+=("appium driver install uiautomator2@$UIAUTOMATOR2_VERSION failed")
	else
		log "Appium $(appium --version), UiAutomator2 driver $driver"
	fi
fi
if ! python3 -c "import appium" 2> /dev/null; then
	log "installing the Appium Python client $PYTHON_CLIENT_VERSION (user site-packages)"
	# The system Python is externally managed (PEP 668), a --user install does not touch the system packages
	pip install --user --break-system-packages "Appium-Python-Client==$PYTHON_CLIENT_VERSION" > /dev/null 2>&1 \
		|| problems+=("pip install --user Appium-Python-Client==$PYTHON_CLIENT_VERSION failed")
fi

# --- 2. SDK packages ---------------------------------------------------------------------------------------------

packages=("emulator" "platform-tools")
# Any build-tools version (aapt2 for the package names of the WebView APKs)
ls -d "$SDK"/build-tools/*/ > /dev/null 2>&1 || packages+=("build-tools;37.0.0")
while read -r name image _; do
	[ -n "$name" ] && wanted "$name" && packages+=("$image")
done <<< "$AVD_TABLE"
missing=()
for package in "${packages[@]}"; do
	[ -d "$SDK/${package//;//}" ] || missing+=("$package")
done
if [ ${#missing[@]} -gt 0 ]; then
	log "installing ${missing[*]}"
	yes | "$TOOLS/sdkmanager" --licenses > /dev/null 2>&1
	"$TOOLS/sdkmanager" --install "${missing[@]}" | grep -v "^\[=*" || { echo "sdkmanager failed"; exit 1; }
else
	log "SDK packages present"
fi
"$EMULATOR" -accel-check > /dev/null 2>&1 || { echo "KVM not usable, see: $EMULATOR -accel-check"; exit 1; }

# --- 3. AVDs -----------------------------------------------------------------------------------------------------

set_key() { # config, key, value: replaces the key or appends it
	if grep -q "^$2 *=" "$1"; then sed -i "s|^$2 *=.*|$2=$3|" "$1"; else echo "$2=$3" >> "$1"; fi
}

created=()
while read -r name image device; do
	[ -n "$name" ] && wanted "$name" || continue
	if [ -f "$AVD_HOME/$name.ini" ]; then
		log "$name exists, not changed"
		continue
	fi
	log "creating $name ($image, $device)"
	echo no | "$TOOLS/avdmanager" create avd -n "$name" -k "$image" -d "$device" > /dev/null \
		|| { problems+=("$name: avdmanager failed"); continue; }
	config="$AVD_HOME/$name.avd/config.ini"
	while IFS== read -r key value; do
		[ -n "$key" ] && set_key "$config" "$key" "$value"
	done <<< "$AVD_SETTINGS"
	# Android 6 encrypts /data on the first boot and fails with a 6 GB partition: it stays in the minimal framework
	# ("Not a coreApp", 256 MB tmpfs /data, every install "Aborted"), 2 GB works
	[ "$name" = TLS_API_23 ] && set_key "$config" disk.dataPartition.size 2048M
	# TV: D-pad and keyboard for the remote control
	if [[ "$name" == TLS_TV_* ]]; then
		set_key "$config" hw.dPad yes
		set_key "$config" hw.keyboard yes
		# Landscape with the TV skin (1920 x 1080) and no SD card, as the AVDs made by Android Studio had: avdmanager
		# alone gives portrait without a skin and an SD card file
		set_key "$config" hw.initialOrientation landscape
		set_key "$config" skin.name tv_1080p
		set_key "$config" skin.path "$SDK/skins/tv_1080p"
		set_key "$config" skin.dynamic yes
		set_key "$config" hw.sdCard no
		set_key "$config" sdcard.size 0
	fi
	created+=("$name")
done <<< "$AVD_TABLE"

# --- 4. Test PKI -------------------------------------------------------------------------------------------------

if [ -f "$HERE/tls-test-proxy/certs/ca.pem" ] && [ -f "$HERE/tls-test-proxy/public/client-ec.p12" ]; then
	log "test PKI present (tls-test-proxy/certs, public)"
else
	log "generating the test PKI"
	"$HERE/tls-test-proxy/generate.sh"
fi

# --- 5. WebView APKs (downloaded by hand) ------------------------------------------------------------------------

# File, Android versions, what it has to be (WEBVIEW-APKS.md)
WEBVIEW_TABLE="
webview-api23.apk|API 23|com.android.webview 88 (the newest that runs on Android 6) re-signed with the AOSP test key, build it with ./webview-api23.sh
webview-api24.apk|API 24 - 25|com.android.webview 119 (the newest for Android 7), LineageOS prebuilt re-signed with ./webview-resign.sh
provider-api26.apk|API 26 - 27|Chrome (com.android.chrome) 126, x86_64, single APK, REQUIRED for TLS_API_26 and not downloaded by any script (may not be redistributed): download it by hand, e.g. from APKMirror (WEBVIEW-APKS.md); the AOSP Android 8 image accepts no com.android.webview
webview-api28.apk|API 28|com.android.webview 135 (the newest chromedriver that works on Android 9, not 138), LineageOS prebuilt re-signed with ./webview-resign.sh
webview-api29.apk|API 29|com.android.webview 155 (minSdk 29), LineageOS prebuilt re-signed with ./webview-resign.sh
"
apks_missing=0
mkdir -p "$HERE/webview-apks"
while IFS='|' read -r file versions what; do
	[ -n "$file" ] || continue
	if [ -f "$HERE/webview-apks/$file" ]; then
		log "webview-apks/$file present"
	else
		log "webview-apks/$file missing: $versions, $what"
		apks_missing=1
	fi
done <<< "$WEBVIEW_TABLE"

# File manager as file picker for the TV AVDs (checklists/tv-picker.md, install-picker of manual-test.py): they have
# none. Material Files handles OPEN_DOCUMENT and GET_CONTENT. Pinned version, the hash is checked.
PICKER_APK="$HERE/tv-picker/me.zhanghai.android.files_40.apk"
PICKER_URL="https://f-droid.org/repo/me.zhanghai.android.files_40.apk"
PICKER_SHA256="2fe900bf43d725b655008d438f5ca46d0da0f301e3784403b5b96c3fc2df6e8a"
if [ -f "$PICKER_APK" ] && echo "$PICKER_SHA256  $PICKER_APK" | sha256sum -c --quiet 2>/dev/null; then
	log "tv-picker/$(basename "$PICKER_APK") present"
else
	log "downloading Material Files 1.7.5 (F-Droid) to tv-picker/"
	mkdir -p "$HERE/tv-picker"
	if curl -fsSL -o "$PICKER_APK.part" "$PICKER_URL" && echo "$PICKER_SHA256  $PICKER_APK.part" | sha256sum -c --quiet; then
		mv "$PICKER_APK.part" "$PICKER_APK"
	else
		rm -f "$PICKER_APK.part"
		problems+=("tv-picker/: Material Files download failed or its SHA-256 differs ($PICKER_URL)")
	fi
fi

# --- 6. WebView update on the AVDs -------------------------------------------------------------------------------

avds=()
while read -r name _; do
	[ -n "$name" ] && wanted "$name" && [ -f "$AVD_HOME/$name.ini" ] && avds+=("$name")
done <<< "$AVD_TABLE"
if [ "${SKIP_WEBVIEW:-0}" = 1 ]; then
	log "WebView update skipped (SKIP_WEBVIEW=1)"
elif [ $apks_missing = 1 ]; then
	problems+=("WebView APKs missing (above), then: SUITES=webview ./run-matrix.sh ${avds[*]}")
elif [ ${#avds[@]} -gt 0 ]; then
	log "WebView update on ${avds[*]} (builds the mobile app, boots every AVD)"
	SUITES=webview APPS=mobile "$HERE/run-matrix.sh" "${avds[@]}" | grep -E "===|WebView|timeout|failed"
	for name in "${avds[@]}"; do
		version=$(cat "$HERE/results-webview/$name/webview-version" 2>/dev/null)
		major=$(sed -n 's/^[^ ]* \([0-9]*\)\..*/\1/p' <<< "$version")
		[ "${major:-0}" -ge 80 ] || problems+=("$name: WebView ${version:-unknown} after the update, see results-webview/$name")
	done
fi

# --- 7. Test Caddy -----------------------------------------------------------------------------------------------

# Caddy 2.10 or newer for post-quantum key exchange (X25519MLKEM768), the distribution package is older (Ubuntu 26.04:
# 2.6.2). Official release in tls-test-proxy/bin/, the system Caddy (/usr/bin, /etc/caddy) stays as it is.
CADDY_VERSION=2.11.7
CADDY_BIN="$HERE/tls-test-proxy/bin/caddy"
if [ -x "$CADDY_BIN" ] && "$CADDY_BIN" version | grep -q "^v$CADDY_VERSION "; then
	log "tls-test-proxy/bin/caddy $CADDY_VERSION present"
else
	log "downloading Caddy $CADDY_VERSION to tls-test-proxy/bin/"
	caddy_tmp=$(mktemp -d)
	caddy_url="https://github.com/caddyserver/caddy/releases/download/v$CADDY_VERSION"
	caddy_tar="caddy_${CADDY_VERSION}_linux_amd64.tar.gz"
	if curl -fsSL -o "$caddy_tmp/$caddy_tar" "$caddy_url/$caddy_tar" \
		&& curl -fsSL -o "$caddy_tmp/checksums.txt" "$caddy_url/caddy_${CADDY_VERSION}_checksums.txt" \
		&& (cd "$caddy_tmp" && grep " $caddy_tar\$" checksums.txt | sha512sum -c --quiet) \
		&& tar -xzf "$caddy_tmp/$caddy_tar" -C "$caddy_tmp" caddy; then
		mkdir -p "$(dirname "$CADDY_BIN")" && install -m 755 "$caddy_tmp/caddy" "$CADDY_BIN"
	else
		problems+=("tls-test-proxy/bin/caddy: Caddy $CADDY_VERSION download failed or its checksum differs ($caddy_url)")
	fi
	rm -rf "$caddy_tmp"
fi

if ss -ltn 2>/dev/null | grep -q ":8920 "; then
	log "test Caddy runs (port 8920)"
else
	problems+=("test Caddy not running, in its own terminal: cd $HERE/tls-test-proxy && ./bin/caddy run --config Caddyfile --adapter caddyfile")
fi

echo
[ ${#created[@]} -gt 0 ] && log "created: ${created[*]}"
if [ ${#problems[@]} -gt 0 ]; then
	log "to do:"
	printf '  - %s\n' "${problems[@]}"
	exit 1
fi
log "setup complete"
