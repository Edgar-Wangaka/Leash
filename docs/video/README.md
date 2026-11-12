# How the demo video was made

No screen recording or editing app needed. Both scripts run on a Mac.

1. Start a local dashboard against the PayPal sandbox with its own SQLite files
   (never the production database): the `leash-dashboard-local-only` entry in
   `.claude/launch.json`, but with `DRY_RUN=false`.
2. `record.py <out_dir> <dashboard_url> <token> <chromium_path>` drives the
   dashboard in headless Chromium (Playwright) at 1920 × 1080 and writes a
   `.webm` plus `marks.json`, the time of each step.
3. `build_video.py <out_dir> <ffmpeg> <out.mp4> [voice]` cuts the agent waits,
   adds a macOS `say` voice-over (Samantha), burns in captions and joins the
   scenes. Any ffmpeg with libx264 and drawtext works; `pip install
   imageio-ffmpeg` ships one.

The voice-over lines are in `build_video.py`. If a re-take gets different
results (price, outcome), edit the lines to match before building.
