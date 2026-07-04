"""
AUDIO ENGINE — free, ffmpeg-only music intake system for HomeHustleNG MotivationBot.

You upload a full song (one you've already made sure is emotional) and pick
its mood from a dropdown. The engine:
  1. Scans the track with ffmpeg to find the loudest ~50s window (a free,
     zero-dependency proxy for "the emotional/energetic peak" — no AI API,
     no librosa, no numpy).
  2. Trims that window with a fade in/out.
  3. Commits the finished clip to a GitHub repo, into a folder named after
     the mood you picked, via GitHub's Contents API.

main.py's fetch_music() reads clips back via raw.githubusercontent.com
URLs from that same repo — so GitHub itself is the free storage + CDN,
no Supabase or any other paid service involved.

COST: $0. Dependencies: fastapi, uvicorn, python-multipart, httpx — all
free, no paid API calls anywhere in this file. GitHub API is free for
personal use at this volume (5000 requests/hour on an authenticated token).

COPYRIGHT NOTE: this tool automates trimming/organizing whatever you
upload. It does not change the copyright status of the source material.
Only upload songs you actually have the right to use (bought royalty-free,
CC-licensed, or your own compositions) — using an unlicensed commercial
song in a monetized YouTube Short still risks a Content ID claim, same as
before this tool existed.
"""

import os
import re
import json
import uuid
import base64
import subprocess
from pathlib import Path
from datetime import datetime
from collections import deque

from fastapi import FastAPI, UploadFile, File, Form, BackgroundTasks
from fastapi.responses import HTMLResponse, JSONResponse
import httpx

# ─────────────────────────────────────────────
# ENV VARS
# ─────────────────────────────────────────────
# GITHUB_TOKEN needs a fine-grained PAT scoped to this one repo with
# "Contents: Read and write" permission (or a classic PAT with the
# "repo" scope). The repo must be PUBLIC so main.py can read clips back
# via raw.githubusercontent.com without needing any token itself.
GITHUB_TOKEN      = os.getenv("GITHUB_TOKEN")
GITHUB_REPO       = os.getenv("GITHUB_REPO")        # e.g. "yourname/motivationbot-music"
GITHUB_BRANCH     = os.getenv("GITHUB_BRANCH", "main")
GITHUB_MUSIC_PATH = os.getenv("GITHUB_MUSIC_PATH", "music").strip("/")  # folder inside the repo

CLIP_SECONDS  = int(os.getenv("CLIP_SECONDS", "50"))
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", "30"))

# Same 8 moods main.py's fetch_music() expects — keep these in sync if you
# ever change one side.
MOOD_CATEGORIES = ["epic", "intense", "uplifting", "calm", "heartbreak", "redemption", "dark", "hopeful"]

TMP = Path("/tmp/audio_engine")
TMP.mkdir(parents=True, exist_ok=True)

process_log = deque(maxlen=100)
pipeline_status = {"running": False, "last_error": None}


def log_step(level: str, message: str):
    entry = {"time": datetime.utcnow().strftime("%H:%M:%S"), "level": level, "message": message}
    process_log.append(entry)
    print(f"[{entry['time']}] {level.upper()}: {message}")


# ─────────────────────────────────────────────
# FFMPEG HELPERS — no python audio libs, ffmpeg does everything
# ─────────────────────────────────────────────
def get_duration_seconds(path: str) -> float:
    result = subprocess.run(
        ["ffprobe", "-v", "quiet", "-show_entries", "format=duration", "-of", "csv=p=0", path],
        capture_output=True, text=True, timeout=30,
    )
    return float(result.stdout.strip())


def measure_loudness(path: str, start: float, duration: float) -> float:
    """Returns mean_volume in dB for the given window (less negative = louder).
    Uses ffmpeg's volumedetect filter — a standard, built-in ffmpeg feature."""
    result = subprocess.run(
        [
            "ffmpeg", "-ss", str(start), "-t", str(duration), "-i", path,
            "-af", "volumedetect", "-f", "null", "-",
        ],
        capture_output=True, text=True, timeout=60,
    )
    match = re.search(r"mean_volume:\s*(-?\d+(\.\d+)?)\s*dB", result.stderr)
    return float(match.group(1)) if match else -999.0


def find_best_window(path: str, clip_seconds: int) -> float:
    """Scans the track in 5s steps (skipping likely intro/outro) and picks
    the loudest clip_seconds-long window as a free proxy for the emotional
    peak — no AI needed, just signal loudness."""
    duration = get_duration_seconds(path)
    if duration <= clip_seconds:
        return 0.0

    # Skip the first 8% (intros) and last 12% (outros/fades) where possible
    skip_start = duration * 0.08
    skip_end = duration * 0.12
    search_start = skip_start
    search_end = max(skip_start + 1, duration - skip_end - clip_seconds)

    best_start, best_volume = search_start, -999.0
    t = search_start
    while t <= search_end:
        vol = measure_loudness(path, t, clip_seconds)
        if vol > best_volume:
            best_volume, best_start = vol, t
        t += 5.0

    return best_start


def trim_with_fade(path: str, start: float, clip_seconds: int, output_path: str):
    fade_out_start = max(0, clip_seconds - 3)
    af = f"afade=t=in:st=0:d=2,afade=t=out:st={fade_out_start}:d=3"
    subprocess.run(
        [
            "ffmpeg", "-y", "-ss", str(start), "-t", str(clip_seconds), "-i", path,
            "-af", af, "-c:a", "libmp3lame", "-q:a", "4", output_path,
        ],
        capture_output=True, text=True, timeout=60, check=True,
    )


# ─────────────────────────────────────────────
# GITHUB — upload via Contents API (free storage + CDN)
# ─────────────────────────────────────────────
GITHUB_API = "https://api.github.com"

def _github_headers() -> dict:
    return {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }


async def upload_to_github(local_path: str, mood: str, filename: str) -> str:
    """Commits the finished clip to {GITHUB_MUSIC_PATH}/{mood}/{filename} in
    the repo, returns the raw.githubusercontent.com URL main.py will read
    it from. Repo must be public for that URL to work without a token."""
    if not GITHUB_TOKEN or not GITHUB_REPO:
        raise Exception("GITHUB_TOKEN / GITHUB_REPO not configured")

    repo_path = f"{GITHUB_MUSIC_PATH}/{mood}/{filename}"
    url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{repo_path}"

    with open(local_path, "rb") as f:
        content_b64 = base64.b64encode(f.read()).decode("ascii")

    payload = {
        "message": f"Add {mood} track: {filename}",
        "content": content_b64,
        "branch": GITHUB_BRANCH,
    }

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.put(url, headers=_github_headers(), json=payload)
        if resp.status_code not in (200, 201):
            raise Exception(f"GitHub upload failed: HTTP {resp.status_code} — {resp.text[:200]}")

    return f"https://raw.githubusercontent.com/{GITHUB_REPO}/{GITHUB_BRANCH}/{repo_path}"


async def list_library() -> dict:
    """Lists what's currently in each mood folder, for the UI."""
    library = {}
    if not GITHUB_TOKEN or not GITHUB_REPO:
        return library

    async with httpx.AsyncClient(timeout=20) as client:
        for mood in MOOD_CATEGORIES:
            try:
                url = f"{GITHUB_API}/repos/{GITHUB_REPO}/contents/{GITHUB_MUSIC_PATH}/{mood}"
                resp = await client.get(url, headers=_github_headers(), params={"ref": GITHUB_BRANCH})
                if resp.status_code == 200:
                    items = resp.json() or []
                    library[mood] = [item["name"] for item in items if item.get("type") == "file"]
                else:
                    # 404 just means this mood's folder doesn't exist yet — not an error
                    library[mood] = []
            except Exception:
                library[mood] = []

    return library


# ─────────────────────────────────────────────
# PIPELINE
# ─────────────────────────────────────────────
async def process_upload(input_path: str, original_name: str, mood: str):
    pipeline_status["running"] = True
    pipeline_status["last_error"] = None
    try:
        log_step("running", f"Analyzing \"{original_name}\" for the loudest {CLIP_SECONDS}s window...")
        best_start = find_best_window(input_path, CLIP_SECONDS)
        log_step("success", f"Best window starts at {best_start:.0f}s")

        clip_path = str(TMP / f"{uuid.uuid4()}_clip.mp3")
        log_step("running", "Trimming + applying fade in/out...")
        trim_with_fade(input_path, best_start, CLIP_SECONDS, clip_path)

        filename = f"{uuid.uuid4().hex[:8]}_{re.sub(r'[^a-zA-Z0-9]+', '_', original_name)[:40]}.mp3"
        log_step("running", f"Committing to GitHub ({mood}/{filename})...")
        public_url = await upload_to_github(clip_path, mood, filename)

        log_step("success", f"✅ Done — added to '{mood}' library")
        pipeline_status["last_result"] = {"mood": mood, "filename": filename, "url": public_url}

    except Exception as e:
        log_step("error", f"❌ Failed: {str(e)[:200]}")
        pipeline_status["last_error"] = str(e)
    finally:
        pipeline_status["running"] = False
        Path(input_path).unlink(missing_ok=True)
        try:
            Path(clip_path).unlink(missing_ok=True)
        except Exception:
            pass


# ─────────────────────────────────────────────
# FASTAPI APP
# ─────────────────────────────────────────────
app = FastAPI(title="Audio Engine")


@app.on_event("startup")
async def startup():
    missing = [v for v in ["GITHUB_TOKEN", "GITHUB_REPO"] if not os.getenv(v)]
    if missing:
        log_step("error", f"⚠️ Missing env vars: {', '.join(missing)} — uploads will fail until set")
    else:
        log_step("success", "Audio Engine ready ✅")


@app.post("/upload")
async def upload(background_tasks: BackgroundTasks, file: UploadFile = File(...), mood: str = Form(...)):
    if mood not in MOOD_CATEGORIES:
        return JSONResponse({"error": f"mood must be one of {MOOD_CATEGORIES}"}, status_code=400)

    contents = await file.read()
    size_mb = len(contents) / 1024 / 1024
    if size_mb > MAX_UPLOAD_MB:
        return JSONResponse({"error": f"File too large ({size_mb:.1f}MB). Max is {MAX_UPLOAD_MB}MB."}, status_code=400)

    ext = Path(file.filename).suffix or ".mp3"
    input_path = str(TMP / f"{uuid.uuid4()}{ext}")
    with open(input_path, "wb") as f:
        f.write(contents)

    log_step("info", f"Received \"{file.filename}\" ({size_mb:.1f}MB) — mood: {mood}")
    background_tasks.add_task(process_upload, input_path, file.filename, mood)
    return {"status": "processing"}


@app.get("/api/state")
async def state():
    return {"log": list(process_log), "running": pipeline_status["running"], "last_error": pipeline_status["last_error"]}


@app.get("/api/library")
async def library():
    return await list_library()


@app.get("/", response_class=HTMLResponse)
async def home():
    mood_options = "".join(f'<option value="{m}">{m.title()}</option>' for m in MOOD_CATEGORIES)
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Audio Engine</title>
<style>
  :root {{ --bg:#0a0a0a; --surface:#151515; --border:#262626; --text:#f0f0f0; --muted:#888; --accent:#f5a623; }}
  * {{ box-sizing:border-box; }}
  body {{ background:var(--bg); color:var(--text); font-family:-apple-system,sans-serif; margin:0; padding:20px; }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  .sub {{ color:var(--muted); font-size:13px; margin-bottom:20px; }}
  .card {{ background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:16px; margin-bottom:16px; }}
  select, input[type=file] {{ width:100%; padding:10px; border-radius:8px; border:1px solid var(--border); background:#000; color:var(--text); margin-bottom:12px; }}
  button {{ width:100%; padding:14px; border-radius:10px; border:none; background:var(--accent); color:#000; font-weight:700; font-size:15px; }}
  .log-item {{ font-size:12px; padding:8px 0; border-bottom:1px solid var(--border); }}
  .log-empty {{ color:var(--muted); text-align:center; padding:10px; font-size:12px; }}
  .lib-mood {{ font-size:13px; font-weight:700; margin:10px 0 4px; color:var(--accent); }}
  .lib-file {{ font-size:12px; color:var(--muted); padding:2px 0; }}
</style></head>
<body>
  <h1>🎵 Audio Engine</h1>
  <div class="sub">Upload a song → auto-trims the loudest section → uploads to your MotivationBot music library</div>

  <div class="card">
    <form id="uploadForm">
      <input type="file" id="fileInput" accept="audio/*" required>
      <select id="moodSelect" required>
        <option value="" disabled selected>Pick the mood...</option>
        {mood_options}
      </select>
      <button type="submit">Upload & Process</button>
    </form>
  </div>

  <div class="card">
    <div style="font-weight:700; margin-bottom:8px;">Live Log</div>
    <div id="logList"><div class="log-empty">No activity yet</div></div>
  </div>

  <div class="card">
    <div style="font-weight:700; margin-bottom:8px;">Library</div>
    <div id="libList"><div class="log-empty">Loading...</div></div>
  </div>

<script>
document.getElementById('uploadForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const file = document.getElementById('fileInput').files[0];
  const mood = document.getElementById('moodSelect').value;
  if (!file || !mood) return;
  const fd = new FormData();
  fd.append('file', file);
  fd.append('mood', mood);
  await fetch('/upload', {{ method: 'POST', body: fd }});
  document.getElementById('uploadForm').reset();
}});

async function refresh() {{
  try {{
    const state = await (await fetch('/api/state')).json();
    const logEl = document.getElementById('logList');
    logEl.innerHTML = state.log.length
      ? state.log.slice().reverse().map(l => `<div class="log-item">${{l.time}} — ${{l.message}}</div>`).join('')
      : '<div class="log-empty">No activity yet</div>';

    const lib = await (await fetch('/api/library')).json();
    const libEl = document.getElementById('libList');
    let html = '';
    for (const [mood, files] of Object.entries(lib)) {{
      html += `<div class="lib-mood">${{mood}} (${{files.length}})</div>`;
      html += files.length ? files.map(f => `<div class="lib-file">${{f}}</div>`).join('') : '<div class="lib-file">empty</div>';
    }}
    libEl.innerHTML = html || '<div class="log-empty">No tracks yet</div>';
  }} catch (e) {{}}
}}
refresh();
setInterval(refresh, 3000);
</script>
</body></html>"""
