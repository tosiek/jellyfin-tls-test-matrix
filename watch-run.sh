#!/usr/bin/env bash
# Watches a manual-test.py run: state, machine ("here" or the remote host) and duration of every AVD (from the start of
# its worker to its last result, or until now while it runs), a warning when one writes nothing for HANG_MINUTES (default 10),
# a desktop notification (notify-send) on a hang and at the end. Per AVD the steps passed and failed so far with the pass
# rate, then the failures (AVD, step, reason) and the totals of the run, all from the REPORT.md every step updates.
# An emulator that died and was booted again by the harness (RECOVERY lines of the log) shows as ", recovered Nx" and with its
# cause and step below the failures. Read-only, stops nothing.
#   ./watch-run.sh                                   # the newest run in manual-results/
#   ./watch-run.sh manual-results/<time>    # a given run
#   HANG_MINUTES=15 ./watch-run.sh
#   INTERVAL=10 ./watch-run.sh                       # redraw every 10 s (default 5 s in a terminal, 30 s into a file)
set -uo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
newest() { local run; run=$(ls -td "$HERE"/manual-results/*/ 2>/dev/null | head -1); echo "${run%/}"; }
RUN="${1:-$(newest)}"
RUN="${RUN%/}"
HANG=$(( ${HANG_MINUTES:-10} * 60 ))
if [ -t 1 ]; then INTERVAL=${INTERVAL:-5}; else INTERVAL=${INTERVAL:-30}; fi
[ -d "$RUN" ] || { echo "no run directory (${RUN:-none})"; exit 1; }
notify() { notify-send -u "${2:-normal}" "TLS tests" "$1" 2>/dev/null; echo "$1"; }
declare -A warned

# Start of an AVD's worker: creation time of its log, or its first "[HH:MM:SS]" on the day of the run (file systems
# without a creation time)
started() {
	local log=$1 birth first
	birth=$(stat -c %W "$log" 2>/dev/null)
	if [ "${birth:-0}" -le 0 ]; then
		first=$(sed 's/\x1b\[[0-9;]*m//g' "$log" | grep -m1 -oE "^\[[0-9]{2}:[0-9]{2}:[0-9]{2}\]" | tr -d '[]')
		birth=$(date -d "$(date -r "$log" +%F) ${first:-00:00:00}" +%s 2>/dev/null)
	fi
	echo "${birth:-0}"
}
# Machine of an AVD's worker: "here", or the host of "… on user@host" in its log (Appium and the emulator there)
machine() {
	local host
	host=$(grep -m1 -oE " on [^ ,@]+@[^ ,)]+" "$1" | sed 's/.*@//')
	echo "${host:-here}"
}
duration() { printf '%dm %02ds' $(( $1 / 60 )) $(( $1 % 60 )); }
# Steps passed and failed so far in a report (a table row per finished step: "| step: title | ✅ | note |")
tally() {
	local report=$1
	[ -f "$report" ] || { echo "0 0"; return; }
	echo "$(grep -cE '^\| .+ \| ✅ \|' "$report") $(grep -cE '^\| .+ \| ❌ \|' "$report")"
}
rate() { [ $(( $1 + $2 )) -eq 0 ] && echo "-" || echo "$(( 100 * $1 / ($1 + $2) ))%"; }

running() { pgrep -f '[m]anual-test.py' > /dev/null; }

while true; do
	# Without an argument: follows the newest run (a run started after this script)
	if [ -z "${1:-}" ] && [ "$(newest)" != "$RUN" ]; then RUN=$(newest); warned=(); fi
	now=$(date +%s); lines=(); failures=(); recoveries=(); open=0; done_avds=0; total_pass=0; total_fail=0
	for log in "$RUN"/*.log; do
		[ -e "$log" ] || continue
		name=$(basename "$log" .log); avd=$name
		start=$(started "$log")
		name="$name @ $(machine "$log")"
		read -r pass fail <<< "$(tally "$RUN/$avd/REPORT.md")"
		# Emulators that died and were booted again by the harness ("RECOVERY 1/2: the emulator of … died in step …")
		recov=0; suffix=""
		while IFS= read -r row; do
			recov=$((recov + 1))
			recoveries+=("  RECOVER  ${avd%-*} @ ${name#* @ }: $row")
		done < <(sed 's/\x1b\[[0-9;]*m//g' "$log" | grep -E "RECOVERY [0-9]+/[0-9]+:" | sed -E 's/^.*(RECOVERY [0-9]+\/[0-9]+: )/\1/' | cut -c1-170)
		[ "$recov" -gt 0 ] && suffix=", recovered ${recov}x"
		total_pass=$((total_pass + pass)); total_fail=$((total_fail + fail))
		# The failed steps with their note (the third column of the row), the first 110 characters
		while IFS= read -r row; do
			step_id=$(sed -E 's/^\| ([a-z0-9-]+):.*/\1/' <<< "$row")
			note=$(awk -F' [|] ' '{print $NF}' <<< "$row" | sed -E 's/ *\|$//' | cut -c1-110)
			failures+=("  FAIL     ${avd%-*} @ ${name#* @ } $step_id: ${note:-no note}")
		done < <(grep -E '^\| .+ \| ❌ \|' "$RUN/$avd/REPORT.md" 2>/dev/null)
		if [ -f "$RUN/$avd/results.json" ]; then
			result=$(sed 's/\x1b\[[0-9;]*m//g' "$log" | grep -E "passed, .* failed" | tail -1 | sed 's/.*: //')
			took=$(duration $(( $(stat -c %Y "$RUN/$avd/results.json") - start )))
			done_avds=$((done_avds + 1))
			lines+=("  done     $name: ${result:-?} ($(rate "$pass" "$fail") passed), $took$suffix")
			continue
		fi
		# Ended without a result (worker stopped or killed): no worker process for the AVD any more
		if ! pgrep -af '[m]anual-test.py' | grep -qE " ${avd%-*}( --|$)"; then
			took=$(duration $(( $(stat -c %Y "$log") - start )))
			lines+=("  stopped  $name: ended without a result, $pass passed, $fail failed, $took$suffix")
			continue
		fi
		open=$((open + 1))
		# Newest of the log and the AVD's result folder (report and screenshot after every step; the log of a
		# run is written only at the end)
		newest=$(stat -c %Y "$log")
		if [ -d "$RUN/$avd" ]; then
			folder=$(find "$RUN/$avd" -type f -printf '%T@\n' | sort -n | tail -1 | cut -d. -f1)
			[ "${folder:-0}" -gt "$newest" ] && newest=$folder
		fi
		age=$(( now - newest ))
		[ "$age" -lt 0 ] && age=0  # written after "now" was read
		# The step in progress from the log ("[TLS_API_24 14/30] reimport: …" -> "step 14/30 reimport"), else the last
		# finished step of the report
		step=$(sed 's/\x1b\[[0-9;]*m//g' "$log" | grep -E "\[[A-Z_0-9]+ [0-9]+/[0-9]+\] [a-z0-9-]+:" | tail -1 \
			| sed -E 's/.*\[[A-Z_0-9]+ ([0-9]+\/[0-9]+)\] ([a-z0-9-]+):.*/step \1 \2/')
		[ -z "$step" ] && step=$(grep -E "^\| [a-z0-9-]+: " "$RUN/$avd/REPORT.md" 2>/dev/null | tail -1 | sed -E 's/^\| ([a-z0-9-]+):.*/after \1/')
		took=$(duration $(( now - start )))
		if [ "$age" -gt "$HANG" ]; then
			lines+=("  HANG?    $name: ${step:-booting}, $pass passed, $fail failed, no new result for $((age / 60)) min, $took$suffix")
			[ -z "${warned[$name]:-}" ] && { warned[$name]=1; notify "$name: no new result for $((age / 60)) min (${step:-booting})" critical > /dev/null; }
		else
			lines+=("  running  $name: ${step:-booting}, $pass passed, $fail failed ($(rate "$pass" "$fail")), last result $((age))s ago, $took$suffix")
			unset "warned[$name]"
		fi
	done
	# Detached (output to a file): no clear, a separator between the snapshots
	if [ -t 1 ]; then clear; else echo; fi
	echo "$(date +%H:%M:%S)  $RUN  ($open running)"
	printf '%s\n' "${lines[@]}"
	echo "  total    $total_pass passed, $total_fail failed of $((total_pass + total_fail)) steps so far, $(rate "$total_pass" "$total_fail") pass rate; $done_avds AVDs done, $open running"
	[ ${#failures[@]} -gt 0 ] && printf '%s\n' "${failures[@]}"
	[ ${#recoveries[@]} -gt 0 ] && printf '%s\n' "${recoveries[@]}"
	if ! running; then
		echo; notify "Run finished: $RUN/SUMMARY.md"
		exit 0
	fi
	sleep "$INTERVAL"
done
