"""Fit a recorded voice-over to the demo video, scene by scene.

    python3 fit_voice.py <voice.m4a> <ffmpeg> <work_dir> <scene times json>

The scene times JSON lists [start, end] seconds of each scene's speech in the
recording (found with a Whisper transcript), in the order of build_video.py's
SCENES. Each scene's audio is cleaned, long pauses are shortened, and the
whole voice is sped up just enough (pitch kept) that the video stays under
MAX_TOTAL. Writes scene_lengths.json (for PIN_LENGTHS) and voice_track.wav,
the voice placed at each scene's start, ready to lay under the video.
"""
import json, subprocess, sys, wave
from pathlib import Path

VOICE, FFMPEG, WORK, TIMES = sys.argv[1], sys.argv[2], Path(sys.argv[3]), json.loads(Path(sys.argv[4]).read_text())
WORK.mkdir(parents=True, exist_ok=True)
LEAD, TAIL, MAX_TOTAL = 0.5, 0.6, 179.0
# Shortest each scene can be and still show its scripted footage (from the build log).
MIN_LEN = [7.0, 8.2, 14.8, 21.6, 20.4, 23.3, 12.5, 4.0, 7.0]


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def dur(p):
    with wave.open(str(p)) as w:
        return w.getnframes() / w.getframerate()


clips = []
for i, (a, b) in enumerate(TIMES):
    out = WORK / f"voice_{i}.wav"
    run([FFMPEG, "-y", "-ss", f"{max(0, a - 0.15):.2f}", "-to", f"{b + 0.3:.2f}", "-i", VOICE, "-af",
         "highpass=f=80,afftdn=nf=-25,silenceremove=stop_periods=-1:stop_duration=0.45:"
         "stop_threshold=-38dB:stop_silence=0.4", "-ac", "1", "-ar", "48000", str(out)])
    clips.append(out)

speech = [dur(c) for c in clips]
pad = LEAD + TAIL
tempo = max(1.0, sum(s + pad for s in speech) / MAX_TOTAL)
# Raise the tempo until scene minimums plus speech fit under MAX_TOTAL.
while sum(max(m, s / tempo + pad) for s, m in zip(speech, MIN_LEN)) > MAX_TOTAL and tempo < 1.25:
    tempo += 0.005
lengths = [round(max(m, s / tempo + pad), 3) for s, m in zip(speech, MIN_LEN)]
print(f"tempo {tempo:.3f}, total {sum(lengths):.1f}s")
for i, (s, l) in enumerate(zip(speech, lengths)):
    print(f"  scene {i}: speech {s / tempo:5.1f}s  scene {l:5.1f}s")
(WORK / "scene_lengths.json").write_text(json.dumps(lengths))

# One voice track: each scene's audio placed LEAD seconds into its scene.
inputs, fc, starts, t = [], [], [], 0.0
for i, (c, l) in enumerate(zip(clips, lengths)):
    inputs += ["-i", str(c)]
    ms = int((t + LEAD) * 1000)
    fc.append(f"[{i}:a]atempo={tempo:.3f},adelay={ms}|{ms}[a{i}]")
    t += l
fc.append("".join(f"[a{i}]" for i in range(len(clips))) +
          f"amix=inputs={len(clips)}:normalize=0,loudnorm=I=-16:TP=-1.5:LRA=11,"
          f"apad,atrim=0:{t:.2f},aformat=sample_rates=48000:channel_layouts=stereo[out]")
run([FFMPEG, "-y", *inputs, "-filter_complex", ";".join(fc), "-map", "[out]", str(WORK / "voice_track.wav")])
print("wrote", WORK / "voice_track.wav")
