#!/usr/bin/env bash
# Which -gpu mode for an AVD that crashes on the host GPU: runs the same steps of the checklist on one AVD once per mode,
# alone on this machine, and prints a table: wall time of the run, steps passed / failed, whether the emulator died.
# The steps are where the emulator died on the host GPU (the certificate installer UI of system-ca-client) and one that
# renders video (play), the others do not render much. Short: about 3 min per mode.
#   ./bench-steps.sh                                   # TLS_API_34, lavapipe swiftshader software host
#   AVD=TLS_API_36 STEPS=system-ca,play ./bench-steps.sh lavapipe host
# Results: bench-steps.tsv (appended), logs bench-steps-<mode>.log.
set -uo pipefail
cd "$(dirname "$0")"
AVD="${AVD:-TLS_API_34}"
STEPS="${STEPS:-system-ca-client,play}"
MODES=("$@"); [ ${#MODES[@]} -gt 0 ] || MODES=(lavapipe swiftshader software host)  # swangle was the slowest of all in bench-gpu.sh
OUT=bench-steps.tsv
for mode in "${MODES[@]}"; do
	echo "[$mode] $AVD steps $STEPS"
	start=$(date +%s)
	E2E_USER="${E2E_USER:-jellyfin}" E2E_PASSWORD="${E2E_PASSWORD:-1234}" ./manual-test.py --auto --skip-build --jobs 1 \
		--steps "$STEPS" --gpu "$mode" --here "$AVD" "$AVD" > "bench-steps-$mode.log" 2>&1 < /dev/null
	wall=$(( $(date +%s) - start ))
	total=$(sed 's/\x1b\[[0-9;]*m//g' "bench-steps-$mode.log" | grep -E "^$AVD: [0-9]+ passed" | tail -n 1)
	died=$(sed 's/\x1b\[[0-9;]*m//g' "bench-steps-$mode.log" | grep -c "emulator is gone")
	# Do not leave the emulator of this run (the last AVD stays up) for the next mode
	pkill -f "[q]emu-system.* -avd $AVD " 2>/dev/null; sleep 5
	printf '%s\t%s\t%ss\t%s\temulator died: %s\n' "$(date +%F\ %H:%M)" "$mode" "$wall" "${total:-no result}" "$died" | tee -a "$OUT"
done
echo; column -t -s$'\t' "$OUT" | tail -n ${#MODES[@]}
