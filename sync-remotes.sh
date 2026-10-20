#!/usr/bin/env bash
# Brings the remote machines in line with this directory: webview-apks/ (files that are not here are deleted there), the
# setup and WebView scripts, no leftover snapshots/ folders in the AVDs; then compares everything with xxh3 checksums.
#   ./sync-remotes.sh                                     # user@remote-c, remote-a, remote-b (or MANUAL_REMOTE_HOST)
#   ./sync-remotes.sh user@remote-a
# Stops when an emulator is running on the machine. Not touched: the AVDs themselves (./setup.sh creates them).
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
R=StudioProjects/tls-test-matrix
SCRIPTS=(setup.sh webview-resign.sh webview-api23.sh webview-fetch.sh webview-install.sh)
HOSTS=("$@")
[ ${#HOSTS[@]} -gt 0 ] || { IFS=', ' read -ra HOSTS <<< "${MANUAL_REMOTE_HOST:-user@remote-c,user@remote-a,user@remote-b}"; }

sync_one() {
	local h=$1
	if ssh -o BatchMode=yes -o ConnectTimeout=8 "$h" 'pgrep -f "[q]emu-system" > /dev/null'; then echo "$h: an emulator is running, skipped"; return 1; fi
	ssh -o BatchMode=yes "$h" "rm -rf ~/.android/avd/*/snapshots; mkdir -p ~/$R/webview-apks" || return 1
	rsync -a --delete "$HERE/webview-apks/" "$h:$R/webview-apks/" || { echo "$h: webview-apks failed"; return 1; }
	(cd "$HERE" && rsync -a "${SCRIPTS[@]}" "$h:$R/") || { echo "$h: scripts failed"; return 1; }
	# Compare by content: nothing printed = identical
	local diff
	diff=$(rsync -rcn --delete --checksum-choice=xxh3 --itemize-changes "$HERE/webview-apks/" "$h:$R/webview-apks/"; \
	       cd "$HERE" && rsync -rcn --checksum-choice=xxh3 --itemize-changes "${SCRIPTS[@]}" "$h:$R/")
	[ -z "$diff" ] && echo "$h: in sync (webview-apks, ${#SCRIPTS[@]} scripts, xxh3 checked)" || { echo "$h: DIFFERS"; echo "$diff"; return 1; }
}
status=0
for h in "${HOSTS[@]}"; do sync_one "$h" & done
wait
