#!/usr/bin/env bash
# Generates the two test movies of the Jellyfin library from ffmpeg's built-in test sources (no copyrighted footage):
#   TLS Test Clip H264.mp4   card 0: direct play, the seek step switches on its external English .srt
#   TLS Test Transcode.mkv   card 1: play-second picks the lowest quality (420 kbps), the server transcodes
# Both 2:00 (seek jumps 30 s past ExoPlayer's ~50 s buffer), 1080p 23.976 fps H.264 High, AAC stereo 48 kHz (eng), a
# moving picture with the name and the running time burnt in (the background step fails on a frozen picture).
# Constant 2 Mbps (filler): testsrc2 alone compresses below 420 kbps and the lowest quality would play directly.
# Writes into OUT (default: a new temporary folder) and prints it, replaces nothing.
#   ./make-test-media.sh                 # then copy the two files over ~/med/ and scan the library
#   OUT=/tmp/media ./make-test-media.sh
set -euo pipefail
OUT="${OUT:-$(mktemp -d /tmp/tls-test-media.XXXXXX)}"
mkdir -p "$OUT"
FONT="$(fc-match -f '%{file}' 'DejaVu Sans Mono')"

# make NAME FILE SOURCE: SOURCE is the lavfi video source with its options
make() {
	local name=$1 file=$2 source=$3
	local extra=(); [[ $file == *.mp4 ]] && extra=(-movflags +faststart)  # moov first: playback starts at once
	local text="drawtext=fontfile=$FONT:fontcolor=white:box=1:boxcolor=black@0.6:boxborderw=12"
	ffmpeg -hide_banner -loglevel error -y \
		-f lavfi -i "$source=size=1920x1080:rate=24000/1001:duration=120" \
		-f lavfi -i "sine=frequency=440:beep_factor=4:sample_rate=48000:duration=120" \
		-vf "$text:fontsize=64:x=(w-tw)/2:y=60:text='$name',$text:fontsize=96:x=(w-tw)/2:y=h-180:text='%{pts\:hms}'" \
		-map 0:v -map 1:a -map_metadata -1 \
		-c:v libx264 -profile:v high -pix_fmt yuv420p -preset medium \
		-b:v 2M -minrate 2M -maxrate 2M -bufsize 2M -x264-params nal-hrd=cbr:force-cfr=1 \
		-c:a aac -b:a 128k -ac 2 -metadata:s:a:0 language=eng \
		"${extra[@]}" "$OUT/$file"
	echo "$OUT/$file"
}

make "TLS Test Clip - direct play" "TLS Test Clip H264.mp4" testsrc2
make "TLS Test Transcode - low quality" "TLS Test Transcode.mkv" "testsrc"
echo "written to $OUT"
