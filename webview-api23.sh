#!/usr/bin/env bash
# Builds webview-apks/webview-api23.apk: a com.android.webview APK re-signed with the AOSP test key (Android 6 and every
# other emulator image: only com.android.webview, signed with that key, is accepted). The signing itself is
# webview-resign.sh (key inside, any APK, any output); this one only fetches the default APK.
#
# Usage:
#   ./webview-api23.sh                  # LineageOS chromium-webview 88 (x86_64), from GitHub: the newest that runs on Android 6
#                                       # (every newer prebuilt of LineageOS has minSdk 24)
#   ./webview-api23.sh some-webview.apk # another com.android.webview APK (x86_64, 80+), e.g. downloaded by hand
# Then on the AVD: SUITES=webview ./run-matrix.sh TLS_API_23
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="${OUT:-$HERE/webview-apks/webview-api23.apk}"
LINEAGE_URL="https://github.com/LineageOS/android_external_chromium-webview_backup-20210212/raw/master/prebuilt/x86_64/webview.apk"

if [ $# -ge 1 ]; then
	input=$1
else
	work=$(mktemp -d /tmp/webview-api23.XXXX)
	trap 'rm -r "$work"' EXIT
	echo "downloading the LineageOS WebView 88 (x86_64, ~85 MB)"
	curl -sfL -o "$work/input.apk" "$LINEAGE_URL"
	input="$work/input.apk"
fi
"$HERE/webview-resign.sh" -o "$OUT" "$input"
# A failed attempt with another APK marked the AVD, the new APK should be tried again
rm -f "$HERE/webview-apks/.incompatible-TLS_API_23"
echo "install on the AVD: SUITES=webview ./run-matrix.sh TLS_API_23"
