#!/bin/bash
# Record a voice-over for the silent demo video, then join them into one MP4.
# Plays the video full-screen in QuickTime while recording the microphone.
#
#   bash docs/video/record_voice.sh [video] [mic name]
#
# Env: FFMPEG (path to ffmpeg), OFFSET (seconds to trim from the recording's
# start, default 1.3, to line the voice up with the picture).
set -euo pipefail
MIX_ONLY=0; [ "${1:-}" = "--mix-only" ] && { MIX_ONLY=1; shift; }
VIDEO="${1:-$HOME/Desktop/leash-demo-3min.mp4}"
MIC="${2:-MacBook Pro Microphone}"
FFMPEG="${FFMPEG:-ffmpeg}"
OFFSET="${OFFSET:-1.3}"
RAW="$HOME/Desktop/leash-voice.wav"
OUT="$HOME/Desktop/leash-demo-final.mp4"

DUR=$({ "$FFMPEG" -hide_banner -i "$VIDEO" 2>&1 || true; } | sed -n 's/.*Duration: \([0-9:.]*\).*/\1/p' \
      | awk -F: '{print $1*3600 + $2*60 + $3}')
if [ "$MIX_ONLY" = 0 ]; then
echo "Video: $VIDEO (${DUR}s). Mic: $MIC"
echo "Read each caption as it appears. Recording starts in:"
for i in 3 2 1; do echo "  $i"; sleep 1; done

"$FFMPEG" -hide_banner -loglevel error -y -f avfoundation -i ":$MIC" -ac 1 -ar 48000 "$RAW" &
REC=$!
sleep 1
osascript <<EOF
tell application "QuickTime Player"
  activate
  set d to open POSIX file "$VIDEO"
  set presenting of d to true
  play d
end tell
EOF
echo "Recording... (stops by itself after the video)"
sleep "$(awk -v d="$DUR" 'BEGIN{print d + 1.5}')"
kill -INT "$REC"; wait "$REC" 2>/dev/null || true
osascript -e 'tell application "QuickTime Player" to close every document saving no' || true
fi

echo "Mixing..."
"$FFMPEG" -hide_banner -loglevel error -y -i "$VIDEO" -ss "$OFFSET" -i "$RAW" \
  -filter_complex "[1:a]highpass=f=80,afftdn=nf=-25,loudnorm=I=-16:TP=-1.5:LRA=11,aformat=channel_layouts=stereo,apad[a]" \
  -map 0:v -map "[a]" -c:v copy -c:a aac -b:a 192k -shortest -movflags +faststart "$OUT"
echo "Done: $OUT"
echo "Voice raw take kept at: $RAW"
echo "If the voice is early/late, re-mix without re-recording:"
echo "  OFFSET=1.0 bash $0 --mix-only   (lower = voice later, higher = voice earlier)"
