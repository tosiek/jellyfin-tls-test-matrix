#!/usr/bin/env bash
# Copies the TLS test certificates to Download/jellyfin-tls/ of a running emulator or device, so they can be picked
# manually in the system file picker of the certificates screen.
#   ./push-certs.sh                     # the only connected device
#   ANDROID_SERIAL=emulator-5554 ./push-certs.sh
#
# Download/jellyfin-tls/
#   proxy/     certificates of tls-test-proxy (Caddy in front of Jellyfin, https://10.0.2.2:8920), .p12 password: jellyfin
#   fixtures/  test fixtures (all CA formats, PKCS#12 matrix), .p12 password: jellyfin
#   downloads/ download location picked in the app (download step of manual-test.py)
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$HERE")"
ADB="${ANDROID_HOME:-$HOME/Android/Sdk}/platform-tools/adb"
TARGET=/sdcard/Download/jellyfin-tls

# Fresh copies: files left by an earlier install can be invisible in the file picker
"$ADB" shell rm -rf "$TARGET"
"$ADB" shell mkdir -p "$TARGET/proxy" "$TARGET/fixtures" "$TARGET/downloads"
# A file in it: an empty folder is not in the MediaStore, the folder picker of Android 10+ would not list it
"$ADB" shell "echo 'Download location of the app, see tls-test-matrix/manual-test.py' > $TARGET/downloads/README.txt"
[ -d "$HERE/tls-test-proxy/public" ] && "$ADB" push "$HERE/tls-test-proxy/public/." "$TARGET/proxy/" > /dev/null
"$ADB" push "$ROOT/jellyfin-androidtv/app/src/test/resources/tls/ca" "$TARGET/fixtures/" > /dev/null
"$ADB" push "$ROOT/jellyfin-androidtv/app/src/test/resources/tls/client" "$TARGET/fixtures/" > /dev/null
# Make the files visible in the document picker: the scan broadcast up to Android 10, it is ignored on Android 11+,
# there the MediaStore volume is rescanned (otherwise the picker can show an empty folder)
"$ADB" shell "find $TARGET -type f -exec am broadcast -a android.intent.action.MEDIA_SCANNER_SCAN_FILE -d file://{} \;" > /dev/null 2>&1 || true
SDK=$("$ADB" shell getprop ro.build.version.sdk | tr -d '\r')
scan_volume() {
	[ "$SDK" -ge 30 ] && "$ADB" shell content call --uri content://media --method scan_volume --arg external_primary > /dev/null 2>&1
	return 0
}
scan_volume
# The scan runs in the background: wait until the MediaStore lists every file of proxy/ (the picker showed proxy/ empty
# when setup started right after the push, TLS_API_30 on remote-a), scan again every 10 s
if [ -d "$HERE/tls-test-proxy/public" ]; then
	expected=$(find "$HERE/tls-test-proxy/public" -type f | wc -l)
	for i in $(seq 60); do
		listed=$("$ADB" shell "content query --uri content://media/external/file --projection _id --where \"_data LIKE '%/Download/jellyfin-tls/proxy/%'\"" | grep -c '^Row:' || true)
		[ "$listed" -ge "$expected" ] && break
		[ "$i" -eq 60 ] && { echo "only $listed of $expected files of $TARGET/proxy in the MediaStore after 60 s, the file picker would not show them" >&2; exit 1; }
		[ $((i % 10)) -eq 0 ] && scan_volume
		sleep 1
	done
fi
echo "Copied to $TARGET:"
"$ADB" shell ls -R "$TARGET" | head -40
