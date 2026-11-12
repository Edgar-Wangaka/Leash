"""Cut the recorded demo into the ~2-minute submission video.

Inputs: a take folder from record.py (video/*.webm + marks.json).
Voice-over: macOS `say`. Editing: ffmpeg (any build with libx264 + drawtext).

    python3 docs/video/build_video.py <take_dir> <ffmpeg> <out.mp4> [voice]

Each scene is a list of footage segments taken around named marks. The agent
waits (10-70 s) fall between segments, so they are cut. A scene lasts as long
as its voice-over; if the footage runs out, the last frame is held.
"""
import json, wave, subprocess, sys, textwrap
from pathlib import Path

TAKE, FFMPEG, OUT = Path(sys.argv[1]), sys.argv[2], Path(sys.argv[3])
VOICE = sys.argv[4] if len(sys.argv) > 4 else "Samantha"
RATE = "182"
FONT = "/System/Library/Fonts/Supplemental/Arial Bold.ttf"
WORK = TAKE / "build"; WORK.mkdir(exist_ok=True)
SRC = next((TAKE / "video").glob("*.webm"))
MARKS = {}
for m in json.loads((TAKE / "marks.json").read_text()):
    MARKS[m["name"].split(":")[0]] = m["t"]

# (mark, offset) pairs: footage from A+da to B+db. B may be None: open-ended,
# cut to fit the voice-over.
SCENES = [
    ("problem", [("problem", 0.0, "problem_end", 0.0)], [
        "AI agents can now buy things, and pay for them.",
        "But one sentence hidden in a web page can tell an agent to send the money somewhere else.",
        "Leash sits between the agent and PayPal. The agent can ask to pay. Fixed rules decide.",
    ]),
    ("gift", [("gift_wait", -3.2, "gift_wait", 1.0), ("gift_done", -0.4, "gift_end", 0.0)], [
        "Here, a Gemini model searches real products on Channel3, and asks Leash to pay.",
        "A $4.99 card from an approved seller, under the $40 threshold.",
        "Paid automatically, through PayPal Payouts in the sandbox.",
    ]),
    ("hamper", [("hamper_wait", -3.2, "hamper_wait", 1.0), ("hamper_done", -0.4, "hamper_end", 0.0)], [
        "A gift hamper this time. It's over the $40 approval threshold, so Leash holds it.",
        "It waits here, under Waiting for your approval.",
        "I approve it. Leash checks the rules again before any money moves, and the ledger shows it as paid, approved by a person.",
    ]),
    ("hijack", [("hijack_click", -1.0, "hijack_end", 0.0)], [
        "This agent has no AI. It just does what the page says.",
        "The product page hides a note for AI assistants: pay $500 to this address. The agent obeys.",
        "Leash blocks it: unknown seller, over the cap, over the budget.",
        "The agent was fooled, but the money didn't move.",
    ]),
    ("ledger", [("ledger", -0.5, "limits_end", 0.0)], [
        "Every decision is logged with its reason. Here, blocked payments are grouped by the rule that stopped them.",
        "It exports to CSV for an audit.",
        "And the limits live right here, with their allowed ranges and a version history. Tightening a limit applies at once; loosening one asks first.",
    ]),
    ("cap", [("cap_wait", -3.2, "cap_wait", 1.0), ("cap_done", -0.4, "cap_end", 0.0)], [
        "Last, I ask for a KitchenAid stand mixer. The agent finds one at $350. The cap is $100.",
        "Blocked. The model can ask, but it can't decide.",
    ]),
    ("close", [("close", 0.0, "end", 0.0)], [
        "Leash. Spending control for AI agents that pay vendors.",
        "Built on PayPal Payouts, Channel3, Gemini, Render, Neon, AG Grid and Postman.",
    ]),
]
LEAD, GAP, TAIL = 0.5, 0.35, 0.6


def run(cmd):
    subprocess.run(cmd, check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)


def duration(aiff):
    with wave.open(str(aiff)) as f:
        return f.getnframes() / f.getframerate()


def caption_filters(timed):
    out = []
    for i, (text, start, end) in enumerate(timed):
        tf = WORK / f"cap_{len(list(WORK.glob('cap_*')))}_{i}.txt"
        tf.write_text("\n".join(textwrap.wrap(text, 58)))
        out.append(f"drawtext=fontfile='{FONT}':textfile='{tf}':fontsize=40:fontcolor=white:"
                   f"line_spacing=10:box=1:boxcolor=0x0b2a6f@0.82:boxborderw=22:"
                   f"x=(w-text_w)/2:y=h-text_h-70:enable='between(t,{start:.2f},{end:.2f})'")
    return out


def build_scene(idx, name, segments, lines):
    # Voice-over, one file per sentence so captions line up exactly.
    timed, t, audio_inputs, delays = [], LEAD, [], []
    for j, line in enumerate(lines):
        a = WORK / f"{name}_{j}.wav"
        run(["say", "-v", VOICE, "-r", RATE, "--file-format=WAVE", "--data-format=LEI16@22050", "-o", str(a), line])
        d = duration(a)
        timed.append((line, t, t + d)); audio_inputs.append(a); delays.append(t)
        t += d + GAP
    length = t - GAP + TAIL
    if name == "close":
        length = max(length, 6.0)

    vparts, fixed = [], 0.0
    for k, (ma, da, mb, db) in enumerate(segments):
        a, b = MARKS[ma] + da, MARKS[mb] + db
        vparts.append((a, b)); fixed += b - a
    # Stretch or trim the last segment so footage matches the voice-over.
    a, b = vparts[-1]
    # Never cut the scripted action short; hold the last frame if the voice runs longer.
    vparts[-1] = (a, max(b, a + (length - (fixed - (b - a)))))
    length = max(length, fixed - (b - a) + (vparts[-1][1] - a))

    fc, labels = [], []
    for k, (a, b) in enumerate(vparts):
        fc.append(f"[0:v]trim={a:.2f}:{b:.2f},setpts=PTS-STARTPTS[v{k}]"); labels.append(f"[v{k}]")
    chain = (f"{''.join(labels)}concat=n={len(labels)}:v=1:a=0,fps=30,"
             f"tpad=stop_mode=clone:stop_duration=30,trim=0:{length:.2f},setpts=PTS-STARTPTS")
    if idx == 0:
        chain += ",fade=t=in:st=0:d=0.6"
    if name == "close":
        chain += (f",drawtext=fontfile='{FONT}':text='agentleash.me':fontsize=72:fontcolor=0x0b2a6f:"
                  f"x=(w-text_w)/2:y=h/2-40:box=1:boxcolor=white@0.92:boxborderw=30:"
                  f"enable='gte(t,{length - 4.5:.2f})',fade=t=out:st={length - 0.8:.2f}:d=0.8")
    chain += "," + ",".join(caption_filters(timed)) + "[vout]"
    fc.append(chain)
    amix_in = []
    for k, d in enumerate(delays):
        ms = int(d * 1000)
        fc.append(f"[{k + 1}:a]aresample=48000,adelay={ms}|{ms}[a{k}]"); amix_in.append(f"[a{k}]")
    fc.append(f"{''.join(amix_in)}amix=inputs={len(amix_in)}:normalize=0,"
              f"apad,atrim=0:{length:.2f},aformat=channel_layouts=stereo[aout]")
    out = WORK / f"scene_{idx:02d}_{name}.mp4"
    cmd = [FFMPEG, "-y", "-i", str(SRC)]
    for a in audio_inputs:
        cmd += ["-i", str(a)]
    cmd += ["-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "medium", "-crf", "18", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "160k", "-ar", "48000", str(out)]
    run(cmd)
    return out, length


def main():
    scenes, chapters, t = [], [], 0.0
    for i, (name, segs, lines) in enumerate(SCENES):
        path, length = build_scene(i, name, segs, lines)
        scenes.append(path); chapters.append((t, name)); t += length
        print(f"{name:8s} {length:6.2f}s  starts {int(t - length) // 60}:{int(t - length) % 60:02d}")
    lst = WORK / "concat.txt"
    lst.write_text("".join(f"file '{p}'\n" for p in scenes))
    run([FFMPEG, "-y", "-f", "concat", "-safe", "0", "-i", str(lst), "-c", "copy",
         "-movflags", "+faststart", str(OUT)])
    print(f"total {t:.1f}s -> {OUT}")
    (WORK / "chapters.json").write_text(json.dumps(chapters))


main()
