#!/usr/bin/env bash
# Downloads a LineageOS Chromium WebView prebuilt (package com.android.webview) of a given version. The prebuilts of
# LineageOS are one repo per ABI (android_external_chromium-webview_prebuilt_<abi>, branch main), one commit per version,
# the file webview.apk is in Git LFS (review.lineageos.org), not on GitHub. The newest that runs per Android version:
# 6: 88 (only in the old ..._backup-20210212 repo, ./webview-api23.sh), 7: 119, 8 - 9: 138, 10 and newer: the latest.
# Re-sign the result for the emulator images with ./webview-resign.sh.
#   ./webview-fetch.sh 119 /tmp/webview-119.apk          # the newest 119.x
#   ./webview-fetch.sh 138.0.7204.63 /tmp/w.apk          # an exact version
#   ./webview-fetch.sh latest /tmp/w.apk
#   ./webview-fetch.sh -a x86 119 /tmp/w.apk             # another ABI (x86_64, x86, arm, arm64)
set -euo pipefail

ABI=x86_64
while getopts "a:h" opt; do
	case $opt in a) ABI=$OPTARG ;; *) sed -n '2,/^set -e/p' "$0" | sed '$d;s/^# \?//'; exit 1 ;; esac
done
shift $((OPTIND - 1))
[ $# -eq 2 ] || { echo "usage: $0 [-a ABI] VERSION|latest OUT.apk (-h for details)"; exit 1; }
VERSION=$1; OUT=$2
REPO=LineageOS/android_external_chromium-webview_prebuilt_$ABI

# Newest commit whose message "Update Chromium Webview … to <version>" starts with VERSION
sha=""
for page in 1 2 3 4 5 6; do
	commits=$(curl -sf -m 30 "https://api.github.com/repos/$REPO/commits?per_page=100&page=$page") || break
	sha=$(python3 -c '
import sys, json
want = sys.argv[1]
for c in json.load(sys.stdin):
    version = c["commit"]["message"].split("\n")[0].split(" to ")[-1].strip()
    if want == "latest" or version == want or version.startswith(want + "."):
        print(c["sha"], version); break' "$VERSION" <<< "$commits")
	[ -n "$sha" ] && break
done
[ -n "$sha" ] || { echo "no version $VERSION in $REPO"; exit 1; }
read -r sha found <<< "$sha"
echo "$found ($sha)"

# The LFS pointer (oid, size) of that commit, then the signed download link (valid for 60 s) from the LFS batch API
pointer=$(curl -sf -m 30 "https://raw.githubusercontent.com/$REPO/$sha/webview.apk")
oid=$(sed -n 's/^oid sha256://p' <<< "$pointer"); size=$(sed -n 's/^size //p' <<< "$pointer")
[ -n "$oid" ] || { echo "no LFS pointer in $REPO@$sha"; exit 1; }
href=$(curl -sf -m 30 -X POST -H "Accept: application/vnd.git-lfs+json" -H "Content-Type: application/vnd.git-lfs+json" \
	-d "{\"operation\":\"download\",\"transfers\":[\"basic\"],\"objects\":[{\"oid\":\"$oid\",\"size\":$size}]}" \
	"https://review.lineageos.org/$REPO.git/info/lfs/objects/batch" \
	| python3 -c 'import sys, json; print(json.load(sys.stdin)["objects"][0]["actions"]["download"]["href"])')
mkdir -p "$(dirname "$OUT")"
curl -sf -m 1800 -o "$OUT.part" "$href"
echo "$oid  $OUT.part" | sha256sum -c --quiet - || { rm -f "$OUT.part"; echo "SHA-256 differs"; exit 1; }
mv "$OUT.part" "$OUT"
echo "$OUT: $found, $((size / 1000000)) MB, SHA-256 checked"
