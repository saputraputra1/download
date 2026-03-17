import os
import re
import uuid
import zipfile
import threading
from pathlib import Path
from flask import Flask, request, jsonify, send_file, render_template_string

import yt_dlp
try:
    import requests as _req
    HAS_REQUESTS = True
except ImportError:
    HAS_REQUESTS = False

app = Flask(__name__)
# Vercel: filesystem is read-only except /tmp
DOWNLOAD_DIR = Path("/tmp/downloads")
DOWNLOAD_DIR.mkdir(parents=True, exist_ok=True)

progress_store = {}   # single-download progress
batch_store    = {}   # batch job store  {batch_id: {...}}

# ─────────────────────────────────────────────────────────────
#  PLATFORM REGISTRY
# ─────────────────────────────────────────────────────────────
PLATFORMS = {
    "youtube.com":      ("YouTube",     "▶",  "#ff0000"),
    "youtu.be":         ("YouTube",     "▶",  "#ff0000"),
    "tiktok.com":       ("TikTok",      "♪",  "#69c9d0"),
    "instagram.com":    ("Instagram",   "◈",  "#e1306c"),
    "facebook.com":     ("Facebook",    "f",  "#1877f2"),
    "fb.watch":         ("Facebook",    "f",  "#1877f2"),
    "twitter.com":      ("Twitter/X",   "𝕏",  "#1da1f2"),
    "x.com":            ("Twitter/X",   "𝕏",  "#1da1f2"),
    "reddit.com":       ("Reddit",      "⬆",  "#ff4500"),
    "vimeo.com":        ("Vimeo",       "V",  "#1ab7ea"),
    "dailymotion.com":  ("Dailymotion", "D",  "#0066dc"),
    "twitch.tv":        ("Twitch",      "⬛", "#9146ff"),
    "pinterest.com":    ("Pinterest",   "P",  "#e60023"),
    "linkedin.com":     ("LinkedIn",    "in", "#0a66c2"),
    "soundcloud.com":   ("SoundCloud",  "☁",  "#ff5500"),
    "rumble.com":       ("Rumble",      "R",  "#85c742"),
    "bilibili.com":     ("Bilibili",    "B",  "#00a1d6"),
    "ted.com":          ("TED",         "T",  "#e62b1e"),
    "loom.com":         ("Loom",        "L",  "#625df5"),
    "ucshare.com":      ("UCShare",     "U",  "#f5a623"),
    "uc.cn":            ("UCShare",     "U",  "#f5a623"),
}

def detect_platform(url: str):
    u = url.lower()
    for kw, (name, emoji, color) in PLATFORMS.items():
        if kw in u:
            return name, emoji, color
    return None

def get_format_selector(fmt: str) -> str:
    return {
        "360p":  "bestvideo[height<=360]+bestaudio/best[height<=360]/best",
        "720p":  "bestvideo[height<=720]+bestaudio/best[height<=720]/best",
        "1080p": "bestvideo[height<=1080]+bestaudio/best[height<=1080]/best",
        "mp3":   "bestaudio/best",
    }.get(fmt, "bestvideo[height<=720]+bestaudio/best")

def base_ydl_opts(hook=None) -> dict:
    o = {
        "quiet": True, "no_warnings": True, "socket_timeout": 60,
        "http_headers": {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            )
        },
    }
    if hook:
        o["progress_hooks"] = [hook]
    return o

def pick_thumbnail(info: dict) -> str:
    t = info.get("thumbnail") or ""
    if not t and info.get("thumbnails"):
        thumbs = [x for x in info["thumbnails"] if x.get("url")]
        if thumbs:
            thumbs.sort(key=lambda x: (x.get("width") or 0)*(x.get("height") or 0), reverse=True)
            t = thumbs[0]["url"]
    return t

# ─────────────────────────────────────────────────────────────
#  UCSHARE CUSTOM EXTRACTOR
#  yt-dlp belum support UCShare secara native, jadi kita scrape
#  langsung dari halaman HTML-nya.
# ─────────────────────────────────────────────────────────────
def is_ucshare(url: str) -> bool:
    u = url.lower()
    return "ucshare.com" in u or "uc.cn" in u or "share.uc.cn" in u

def ucshare_extract_info(url: str) -> dict:
    """
    Scrape info & video URL dari halaman UCShare.
    Return dict mirip yt-dlp info_dict, atau raise Exception jika gagal.
    """
    if not HAS_REQUESTS:
        raise Exception("Library 'requests' tidak terinstall. Jalankan: pip install requests")

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Linux; Android 10; SM-G975F) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.6099.144 Mobile Safari/537.36"
        ),
        "Accept-Language": "id-ID,id;q=0.9,en-US;q=0.8",
        "Referer": "https://www.ucshare.com/",
    }

    resp = _req.get(url, headers=headers, timeout=20, allow_redirects=True)
    resp.raise_for_status()
    html = resp.text

    # ── Cari video URL ──────────────────────────────
    video_url = None

    # Pola 1: JSON embed {"url":"https://...mp4"}
    for pat in [
        r'"url"\s*:\s*"(https?://[^"]+\.mp4[^"]*)"',
        r'"videoUrl"\s*:\s*"(https?://[^"]+)"',
        r'"video_url"\s*:\s*"(https?://[^"]+)"',
        r'<video[^>]+src=["\']([^"\']+)["\']',
        r'source\s+src=["\']([^"\']+mp4[^"\']*)["\']',
        r'"playUrl"\s*:\s*"(https?://[^"]+)"',
        r'"play_url"\s*:\s*"(https?://[^"]+)"',
        r'data-src=["\']([^"\']+mp4[^"\']*)["\']',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            candidate = m.group(1).replace('\\/', '/').replace('\\u0026', '&')
            if candidate.startswith("http"):
                video_url = candidate
                break

    if not video_url:
        raise Exception(
            "Tidak dapat menemukan URL video di halaman UCShare. "
            "Kemungkinan konten private, sudah dihapus, atau format halaman berubah."
        )

    # ── Cari judul ──────────────────────────────────
    title = "UCShare Video"
    for pat in [
        r'<title>([^<]+)</title>',
        r'"title"\s*:\s*"([^"]+)"',
        r'<meta[^>]+property="og:title"[^>]+content="([^"]+)"',
        r'<h1[^>]*>([^<]+)</h1>',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            t = m.group(1).strip()
            if t and t.lower() not in ("ucshare","share","video"):
                title = t
                break

    # ── Cari thumbnail ───────────────────────────────
    thumbnail = ""
    for pat in [
        r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"',
        r'"thumbnail"\s*:\s*"([^"]+)"',
        r'"cover"\s*:\s*"([^"]+)"',
        r'"poster"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            thumbnail = m.group(1).replace('\\/', '/').replace('\\u0026', '&')
            break

    # ── Cari uploader ────────────────────────────────
    uploader = "UCShare User"
    for pat in [
        r'"author"\s*:\s*"([^"]+)"',
        r'"nickname"\s*:\s*"([^"]+)"',
        r'"username"\s*:\s*"([^"]+)"',
        r'"name"\s*:\s*"([^"]+)"',
    ]:
        m = re.search(pat, html, re.IGNORECASE)
        if m:
            u = m.group(1).strip()
            if u:
                uploader = u
                break

    return {
        "title":     title,
        "uploader":  uploader,
        "thumbnail": thumbnail,
        "duration":  None,
        "video_url": video_url,
        "ext":       "mp4",
    }


def ucshare_download(url: str, out_path: str, hook_fn=None) -> dict:
    """
    Download video UCShare langsung via requests streaming.
    Kembalikan info dict.
    """
    info = ucshare_extract_info(url)
    video_url = info["video_url"]

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/120.0.0.0 Safari/537.36"
        ),
        "Referer": url,
    }

    out_file = Path(out_path)
    with _req.get(video_url, headers=headers, stream=True, timeout=60) as r:
        r.raise_for_status()
        total = int(r.headers.get("content-length", 0))
        downloaded = 0
        with open(out_file, "wb") as f:
            for chunk in r.iter_content(chunk_size=1024*64):
                if chunk:
                    f.write(chunk)
                    downloaded += len(chunk)
                    if hook_fn and total > 0:
                        pct = int(downloaded / total * 100)
                        hook_fn({"status":"downloading",
                                 "downloaded_bytes": downloaded,
                                 "total_bytes": total,
                                 "speed": None})

    if hook_fn:
        hook_fn({"status":"finished"})

    return info


def safe_fname(info: dict, fallback="video") -> str:
    t = re.sub(r'[^\w\s\-.]', '', info.get("title", fallback))
    return t.strip()[:60] or fallback

# ─────────────────────────────────────────────────────────────
#  HTML TEMPLATE
# ─────────────────────────────────────────────────────────────
HTML = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1.0"/>
<title>VortexDL — Universal Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Mono:wght@300;400;500&display=swap" rel="stylesheet"/>
<style>
:root{
  --bg:#080b10;--surface:#0e1319;--card:#141920;--border:#1e2730;
  --accent:#ff2d44;--accent2:#ff6b35;--accent-glow:#ff2d4420;
  --text:#e8edf3;--muted:#5a6a7a;--dim:#2a3540;
  --success:#00e5a0;--warn:#ffb830;--error:#ff6b7a;
  --radius:14px;--font:'Syne',sans-serif;--mono:'DM Mono',monospace;
}
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
html{scroll-behavior:smooth}
body{background:var(--bg);color:var(--text);font-family:var(--font);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;z-index:0;
  background-image:linear-gradient(rgba(255,45,68,.03)1px,transparent 1px),
  linear-gradient(90deg,rgba(255,45,68,.03)1px,transparent 1px);
  background-size:48px 48px;pointer-events:none}
body::after{content:'';position:fixed;top:-40%;left:50%;transform:translateX(-50%);
  width:800px;height:600px;
  background:radial-gradient(ellipse,rgba(255,45,68,.08)0%,transparent 70%);
  pointer-events:none;z-index:0}

.wrap{position:relative;z-index:1;max-width:800px;margin:0 auto;padding:36px 20px 80px}

/* ── Header ── */
header{text-align:center;margin-bottom:40px;animation:fadeDown .6s ease both}
.logo-row{display:inline-flex;align-items:center;gap:12px;margin-bottom:8px}
.logo-icon{width:46px;height:46px;background:linear-gradient(135deg,var(--accent),var(--accent2));
  border-radius:12px;display:grid;place-items:center;font-size:20px;
  box-shadow:0 0 24px var(--accent-glow),0 4px 12px rgba(0,0,0,.4)}
h1{font-size:clamp(1.8rem,4vw,2.8rem);font-weight:800;letter-spacing:-1.5px;
  background:linear-gradient(135deg,#fff 30%,var(--accent)100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text}
.tagline{color:var(--muted);font-family:var(--mono);font-size:.8rem;letter-spacing:.06em;margin-top:4px}

/* ── Tab switcher ── */
.tab-bar{display:flex;gap:8px;margin-bottom:24px;background:var(--surface);
  border:1px solid var(--border);border-radius:12px;padding:4px}
.tab-btn{flex:1;padding:10px;border:none;background:transparent;color:var(--muted);
  font-family:var(--font);font-weight:700;font-size:.9rem;border-radius:9px;
  cursor:pointer;transition:all .2s;display:flex;align-items:center;justify-content:center;gap:8px}
.tab-btn.active{background:var(--card);color:var(--text);
  box-shadow:0 2px 8px rgba(0,0,0,.3);border:1px solid var(--border)}
.tab-btn .tab-badge{background:var(--accent);color:#fff;font-size:.65rem;
  padding:2px 6px;border-radius:999px;font-family:var(--mono)}

/* ── Card ── */
.card{background:var(--card);border:1px solid var(--border);border-radius:20px;padding:28px;
  box-shadow:0 0 0 1px rgba(255,255,255,.04),0 24px 64px rgba(0,0,0,.5);
  animation:fadeUp .5s ease .1s both}

/* ── Shared input styles ── */
.input-row{display:flex;gap:10px;align-items:stretch;margin-bottom:6px}
.inp-wrap{flex:1;position:relative}
.inp-icon{position:absolute;left:14px;top:50%;transform:translateY(-50%);font-size:16px;pointer-events:none;z-index:1}
.url-input{width:100%;background:var(--surface);border:1.5px solid var(--border);
  border-radius:var(--radius);padding:14px 14px 14px 42px;
  font-size:.92rem;font-family:var(--mono);color:var(--text);outline:none;
  transition:border-color .2s,box-shadow .2s}
.url-input:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.url-input::placeholder{color:var(--muted)}
.hint{font-family:var(--mono);font-size:.7rem;color:var(--muted);margin-top:5px;padding-left:2px}

/* ── Buttons ── */
.btn-red{padding:13px 20px;background:linear-gradient(135deg,var(--accent),#c0001a);
  color:#fff;font-family:var(--font);font-weight:700;font-size:.88rem;
  border:none;border-radius:var(--radius);cursor:pointer;white-space:nowrap;
  transition:transform .15s,box-shadow .15s,opacity .2s;
  box-shadow:0 4px 16px rgba(255,45,68,.35);position:relative;overflow:hidden}
.btn-red::before{content:'';position:absolute;inset:0;
  background:linear-gradient(135deg,rgba(255,255,255,.15),transparent)}
.btn-red:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 8px 24px rgba(255,45,68,.45)}
.btn-red:disabled{opacity:.5;cursor:not-allowed}

.btn-full{width:100%;padding:15px;margin-top:18px;justify-content:center;
  display:flex;align-items:center;gap:9px;font-size:.95rem;letter-spacing:.04em;text-transform:uppercase}

/* ── Detected platform ── */
.det-bar{display:none;align-items:center;gap:9px;margin-top:10px;padding:9px 14px;
  background:var(--surface);border-radius:9px;border:1px solid var(--border);
  font-family:var(--mono);font-size:.8rem}
.det-bar.show{display:flex}
.det-dot{width:9px;height:9px;border-radius:50%;flex-shrink:0}

/* ── Error ── */
.err-box{display:none;background:rgba(255,45,68,.08);border:1px solid rgba(255,45,68,.3);
  border-radius:10px;padding:12px 16px;margin-top:14px;font-size:.85rem;
  color:var(--error);font-family:var(--mono);line-height:1.5}
.err-box.show{display:block}

/* ── Loader ── */
.loader{display:none;flex-direction:column;align-items:center;gap:14px;padding:28px 0}
.loader.show{display:flex}
.spinner{width:40px;height:40px;border:3px solid var(--border);
  border-top-color:var(--accent);border-radius:50%;animation:spin .8s linear infinite}
.loader-txt{font-family:var(--mono);font-size:.8rem;color:var(--muted);letter-spacing:.05em}

/* ── Single preview ── */
.preview{display:none;margin-top:20px;animation:fadeUp .35s ease both}
.preview.show{display:block}
.thumb-card{background:var(--surface);border:1px solid var(--border);
  border-radius:14px;overflow:hidden;margin-bottom:16px}
.thumb-wrap{position:relative;width:100%;padding-top:42%;background:var(--dim);overflow:hidden}
.thumb-wrap img{position:absolute;inset:0;width:100%;height:100%;object-fit:cover;transition:transform .4s}
.thumb-wrap:hover img{transform:scale(1.04)}
.thumb-overlay{position:absolute;inset:0;
  background:linear-gradient(to top,rgba(0,0,0,.65)0%,transparent 55%)}
.plat-tag{position:absolute;top:10px;left:10px;padding:4px 11px;border-radius:7px;
  font-family:var(--mono);font-size:.7rem;font-weight:600;letter-spacing:.06em;backdrop-filter:blur(6px)}
.dur-tag{position:absolute;bottom:10px;right:10px;padding:3px 9px;border-radius:6px;
  background:rgba(0,0,0,.75);color:#fff;font-family:var(--mono);font-size:.73rem}
.thumb-meta{padding:14px 18px}
.vid-title{font-size:1rem;font-weight:700;line-height:1.4;margin-bottom:8px;
  display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.meta-row{display:flex;gap:14px;flex-wrap:wrap}
.meta-it{display:flex;align-items:center;gap:4px;
  font-family:var(--mono);font-size:.73rem;color:var(--muted)}

/* ── Format selector ── */
.sec-label{font-size:.68rem;font-weight:700;letter-spacing:.12em;color:var(--muted);
  text-transform:uppercase;margin-bottom:9px;font-family:var(--mono)}
.fmt-grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(108px,1fr));gap:7px;margin-bottom:10px}
.fmt-btn{background:var(--surface);border:1.5px solid var(--border);border-radius:10px;
  padding:9px 6px;text-align:center;cursor:pointer;transition:all .2s;
  position:relative;overflow:hidden}
.fmt-btn:hover{border-color:var(--accent);background:var(--accent-glow)}
.fmt-btn.sel{border-color:var(--accent);background:rgba(255,45,68,.12);
  box-shadow:0 0 10px var(--accent-glow)}
.fmt-btn input[type=radio]{position:absolute;opacity:0;width:0;height:0}
.fi{font-size:1.2rem;display:block;margin-bottom:3px;pointer-events:none}
.fl{display:block;font-size:.8rem;font-weight:600;color:var(--text);pointer-events:none}
.fs{display:block;font-size:.66rem;font-family:var(--mono);color:var(--muted);
  margin-top:1px;pointer-events:none}
.divider{display:flex;align-items:center;gap:9px;margin:14px 0;
  color:var(--dim);font-size:.68rem;font-family:var(--mono);letter-spacing:.1em}
.divider::before,.divider::after{content:'';flex:1;height:1px;background:var(--border)}

/* ── Progress bar ── */
.prog-wrap{display:none;margin-top:14px}
.prog-wrap.show{display:block}
.prog-info{display:flex;justify-content:space-between;
  font-family:var(--mono);font-size:.73rem;color:var(--muted);margin-bottom:7px}
.prog-bg{height:5px;background:var(--dim);border-radius:999px;overflow:hidden}
.prog-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));
  border-radius:999px;width:0%;transition:width .4s ease}
.prog-fill.indet{width:30%!important;animation:slide 1.5s infinite}

/* ════════════════════════════════════════════
   BATCH MODE
   ════════════════════════════════════════════ */
#batchPane{display:none}
#batchPane.show{display:block}

.batch-textarea{
  width:100%;min-height:140px;resize:vertical;
  background:var(--surface);border:1.5px solid var(--border);
  border-radius:var(--radius);padding:14px 16px;
  font-size:.85rem;font-family:var(--mono);color:var(--text);outline:none;
  transition:border-color .2s,box-shadow .2s;line-height:1.7}
.batch-textarea:focus{border-color:var(--accent);box-shadow:0 0 0 3px var(--accent-glow)}
.batch-textarea::placeholder{color:var(--muted)}

/* format row for batch */
.batch-fmt-row{display:flex;gap:8px;flex-wrap:wrap;margin:14px 0 4px}
.bfmt{padding:7px 14px;background:var(--surface);border:1.5px solid var(--border);
  border-radius:8px;font-family:var(--mono);font-size:.78rem;font-weight:600;
  color:var(--muted);cursor:pointer;transition:all .2s}
.bfmt:hover{border-color:var(--accent);color:var(--text)}
.bfmt.sel{border-color:var(--accent);background:rgba(255,45,68,.12);
  color:var(--accent);box-shadow:0 0 8px var(--accent-glow)}

/* batch summary bar */
.batch-summary{display:none;align-items:center;gap:10px;margin-top:12px;
  padding:10px 16px;background:var(--surface);border-radius:10px;
  border:1px solid var(--border);font-family:var(--mono);font-size:.8rem}
.batch-summary.show{display:flex}
.bs-num{font-weight:700;color:var(--accent)}
.bs-sep{color:var(--dim)}

/* overall progress */
.overall-bar{margin-top:16px;display:none}
.overall-bar.show{display:block}
.overall-label{font-family:var(--mono);font-size:.72rem;color:var(--muted);
  display:flex;justify-content:space-between;margin-bottom:6px}
.overall-track{height:8px;background:var(--dim);border-radius:999px;overflow:hidden}
.overall-fill{height:100%;
  background:linear-gradient(90deg,var(--accent),var(--accent2),var(--success));
  border-radius:999px;width:0%;transition:width .5s ease}

/* batch queue list */
.queue-list{margin-top:20px;display:flex;flex-direction:column;gap:10px}

.q-item{
  display:grid;grid-template-columns:72px 1fr auto;gap:12px;align-items:center;
  background:var(--surface);border:1px solid var(--border);border-radius:12px;
  padding:12px;transition:border-color .3s;position:relative;overflow:hidden}
.q-item::before{content:'';position:absolute;left:0;top:0;bottom:0;width:3px}
.q-item.waiting  {border-color:var(--border)}
.q-item.waiting::before{background:var(--dim)}
.q-item.fetching {border-color:#ffb83040}
.q-item.fetching::before{background:var(--warn)}
.q-item.queued   {border-color:#1877f240}
.q-item.queued::before{background:#1877f2}
.q-item.running  {border-color:rgba(255,45,68,.3)}
.q-item.running::before{background:var(--accent);animation:pulse 1s infinite}
.q-item.done     {border-color:rgba(0,229,160,.25)}
.q-item.done::before{background:var(--success)}
.q-item.error    {border-color:rgba(255,45,68,.3)}
.q-item.error::before{background:var(--error)}
.q-item.skipped  {border-color:var(--border);opacity:.5}

.q-thumb{width:72px;height:48px;border-radius:7px;object-fit:cover;
  background:var(--dim);flex-shrink:0;border:1px solid var(--border)}
.q-thumb-placeholder{width:72px;height:48px;border-radius:7px;
  background:var(--dim);border:1px solid var(--border);
  display:grid;place-items:center;font-size:1.2rem;flex-shrink:0}

.q-body{min-width:0}
.q-title{font-size:.85rem;font-weight:600;line-height:1.3;margin-bottom:4px;
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.q-meta{display:flex;gap:10px;font-family:var(--mono);font-size:.68rem;color:var(--muted)}
.q-url{font-family:var(--mono);font-size:.65rem;color:var(--muted);
  white-space:nowrap;overflow:hidden;text-overflow:ellipsis;margin-top:2px}
.q-prog-mini{margin-top:6px;height:3px;background:var(--dim);border-radius:999px;overflow:hidden}
.q-prog-fill{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent2));
  border-radius:999px;width:0%;transition:width .4s}
.q-prog-fill.indet{width:35%!important;animation:slide 1.2s infinite}

.q-status{flex-shrink:0;text-align:right}
.status-badge{display:inline-flex;align-items:center;gap:4px;padding:3px 9px;
  border-radius:999px;font-family:var(--mono);font-size:.65rem;font-weight:600;
  letter-spacing:.04em;white-space:nowrap}
.sb-waiting {background:var(--dim);color:var(--muted)}
.sb-fetching{background:rgba(255,184,48,.15);color:var(--warn)}
.sb-queued  {background:rgba(24,119,242,.15);color:#60a0ff}
.sb-running {background:rgba(255,45,68,.15);color:var(--accent)}
.sb-done    {background:rgba(0,229,160,.12);color:var(--success)}
.sb-error   {background:rgba(255,45,68,.12);color:var(--error)}
.sb-skipped {background:var(--dim);color:var(--muted)}

/* btn download single item */
.q-dl-btn{margin-top:6px;padding:3px 10px;font-family:var(--mono);font-size:.68rem;
  font-weight:600;background:rgba(0,229,160,.12);color:var(--success);
  border:1px solid rgba(0,229,160,.3);border-radius:6px;cursor:pointer;
  transition:all .2s;text-decoration:none;display:inline-block}
.q-dl-btn:hover{background:rgba(0,229,160,.2)}

/* batch action buttons */
.batch-actions{display:flex;gap:8px;margin-top:20px;flex-wrap:wrap}
.btn-outline{padding:10px 18px;background:transparent;
  border:1.5px solid var(--border);border-radius:10px;
  color:var(--muted);font-family:var(--font);font-weight:600;font-size:.82rem;
  cursor:pointer;transition:all .2s}
.btn-outline:hover{border-color:var(--accent);color:var(--text)}
.btn-outline:disabled{opacity:.4;cursor:not-allowed}

.btn-zip{padding:10px 18px;background:linear-gradient(135deg,#00c87a,#00a060);
  border:none;border-radius:10px;color:#fff;font-family:var(--font);
  font-weight:700;font-size:.82rem;cursor:pointer;transition:all .2s;
  box-shadow:0 4px 14px rgba(0,200,122,.3);display:none}
.btn-zip.show{display:inline-flex;align-items:center;gap:7px}
.btn-zip:hover:not(:disabled){transform:translateY(-2px);box-shadow:0 8px 20px rgba(0,200,122,.4)}
.btn-zip:disabled{opacity:.5;cursor:not-allowed}

/* stats strip */
.stats-strip{display:none;gap:16px;margin-top:12px;flex-wrap:wrap}
.stats-strip.show{display:flex}
.stat-pill{background:var(--surface);border:1px solid var(--border);
  border-radius:8px;padding:6px 14px;font-family:var(--mono);font-size:.72rem;
  display:flex;align-items:center;gap:6px}

footer{text-align:center;margin-top:52px;font-family:var(--mono);
  font-size:.7rem;color:var(--muted)}

/* ── Animations ── */
@keyframes fadeDown{from{opacity:0;transform:translateY(-18px)}to{opacity:1;transform:translateY(0)}}
@keyframes fadeUp{from{opacity:0;transform:translateY(18px)}to{opacity:1;transform:translateY(0)}}
@keyframes spin{to{transform:rotate(360deg)}}
@keyframes slide{0%{margin-left:-35%}100%{margin-left:100%}}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.4}}

@media(max-width:540px){
  .card{padding:18px}
  .input-row{flex-direction:column}
  .btn-red{width:100%}
  .q-item{grid-template-columns:56px 1fr}
  .q-status{grid-column:1/-1}
  h1{font-size:1.7rem}
}
</style>
</head>
<body>
<div class="wrap">

<!-- Header -->
<header>
  <div class="logo-row">
    <div class="logo-icon">⚡</div>
    <h1>VortexDL</h1>
  </div>
  <p class="tagline">// universal video downloader · single &amp; batch mode</p>
</header>

<!-- Tab Bar -->
<div class="tab-bar">
  <button class="tab-btn active" id="tabSingle" onclick="switchTab('single')">
    🎬 Single Download
  </button>
  <button class="tab-btn" id="tabBatch" onclick="switchTab('batch')">
    📦 Batch Download <span class="tab-badge">BARU</span>
  </button>
</div>

<!-- ═══════════════════════════════════════
     SINGLE PANE
═══════════════════════════════════════ -->
<div id="singlePane">
<div class="card">

  <div class="input-row">
    <div class="inp-wrap">
      <span class="inp-icon">🔗</span>
      <input type="text" class="url-input" id="sUrl"
        placeholder="Tempel link YouTube, TikTok, Instagram..."
        autocomplete="off" spellcheck="false"/>
    </div>
    <button class="btn-red" id="sBtnFetch" onclick="singleFetch()">
      <span id="sBtnTxt">Get Info</span>
    </button>
  </div>
  <p class="hint">YouTube · TikTok · Instagram · Facebook · Twitter · Vimeo · dan banyak lagi</p>

  <div class="det-bar" id="sDetBar">
    <div class="det-dot" id="sDetDot"></div>
    <span id="sDetLabel" style="font-weight:600"></span>
    <span style="margin-left:auto;color:var(--success);font-size:.7rem">✓ TERDETEKSI</span>
  </div>

  <div class="err-box" id="sErr"></div>

  <div class="loader" id="sLoader">
    <div class="spinner"></div>
    <p class="loader-txt">Mengambil informasi video...</p>
  </div>

  <!-- Preview -->
  <div class="preview" id="sPreview">

    <div class="thumb-card">
      <div class="thumb-wrap">
        <img id="sThumb" src="" alt="thumb"/>
        <div class="thumb-overlay"></div>
        <span class="plat-tag" id="sPlatTag"></span>
        <span class="dur-tag" id="sDurTag" style="display:none"></span>
      </div>
      <div class="thumb-meta">
        <p class="vid-title" id="sTitle"></p>
        <div class="meta-row">
          <div class="meta-it">👤 <span id="sUploader"></span></div>
          <div class="meta-it" id="sDurWrap">⏱ <span id="sDur"></span></div>
        </div>
      </div>
    </div>

    <p class="sec-label">🎬 Format Video</p>
    <div class="fmt-grid">
      <label class="fmt-btn" onclick="selFmt(this,'s')">
        <input type="radio" name="sFmt" value="360p"/>
        <span class="fi">📺</span><span class="fl">360p</span><span class="fs">Standard</span>
      </label>
      <label class="fmt-btn sel" onclick="selFmt(this,'s')">
        <input type="radio" name="sFmt" value="720p" checked/>
        <span class="fi">🎥</span><span class="fl">720p</span><span class="fs">HD</span>
      </label>
      <label class="fmt-btn" onclick="selFmt(this,'s')">
        <input type="radio" name="sFmt" value="1080p"/>
        <span class="fi">🎞</span><span class="fl">1080p</span><span class="fs">Full HD</span>
      </label>
    </div>
    <div class="divider">atau</div>
    <div class="fmt-grid">
      <label class="fmt-btn" onclick="selFmt(this,'s')">
        <input type="radio" name="sFmt" value="mp3"/>
        <span class="fi">🎵</span><span class="fl">MP3</span><span class="fs">Audio only</span>
      </label>
    </div>

    <button class="btn-red btn-full" id="sBtnDl" onclick="singleDl()">
      <span>⬇</span> DOWNLOAD SEKARANG
    </button>

    <div class="prog-wrap" id="sProgWrap">
      <div class="prog-info">
        <span id="sProgStatus">Mempersiapkan...</span>
        <span id="sProgPct">0%</span>
      </div>
      <div class="prog-bg"><div class="prog-fill" id="sProgFill"></div></div>
    </div>

  </div>
</div>
</div><!-- /singlePane -->


<!-- ═══════════════════════════════════════
     BATCH PANE
═══════════════════════════════════════ -->
<div id="batchPane">
<div class="card">

  <p class="sec-label" style="margin-bottom:10px">📋 Tempel Link (satu per baris)</p>
  <textarea class="batch-textarea" id="bTextarea"
    placeholder="https://youtube.com/watch?v=xxxxx&#10;https://vt.tiktok.com/xxxxx&#10;https://instagram.com/reel/xxxxx&#10;https://vimeo.com/xxxxx&#10;..."></textarea>
  <p class="hint">Maksimal 20 link sekaligus · Satu URL per baris</p>

  <!-- Format pilihan untuk batch -->
  <p class="sec-label" style="margin-top:16px;margin-bottom:8px">🎬 Format untuk semua link</p>
  <div class="batch-fmt-row">
    <button class="bfmt" onclick="selBFmt(this,'360p')">📺 360p</button>
    <button class="bfmt sel" onclick="selBFmt(this,'720p')">🎥 720p HD</button>
    <button class="bfmt" onclick="selBFmt(this,'1080p')">🎞 1080p</button>
    <button class="bfmt" onclick="selBFmt(this,'mp3')">🎵 MP3 Audio</button>
  </div>

  <!-- Summary bar -->
  <div class="batch-summary" id="bSummary">
    <span class="bs-num" id="bCount">0</span>
    <span class="bs-sep">link terdeteksi</span>
    <span id="bPlatList" style="color:var(--muted);font-size:.72rem;margin-left:4px"></span>
  </div>

  <div class="err-box" id="bErr"></div>

  <!-- Action buttons -->
  <div class="batch-actions">
    <button class="btn-red" id="bBtnStart" onclick="batchStart()">
      ⚡ Mulai Antrian Download
    </button>
    <button class="btn-outline" id="bBtnClear" onclick="batchClear()">
      🗑 Bersihkan
    </button>
    <button class="btn-zip" id="bBtnZip" onclick="batchZip()">
      🗜 Download Semua (ZIP)
    </button>
  </div>

  <!-- Stats -->
  <div class="stats-strip" id="bStats">
    <div class="stat-pill">✅ <span id="statDone">0</span> selesai</div>
    <div class="stat-pill">⏳ <span id="statPending">0</span> antrian</div>
    <div class="stat-pill">❌ <span id="statErr">0</span> gagal</div>
  </div>

  <!-- Overall progress -->
  <div class="overall-bar" id="overallBar">
    <div class="overall-label">
      <span id="overallStatus">Memproses antrian...</span>
      <span id="overallPct">0%</span>
    </div>
    <div class="overall-track">
      <div class="overall-fill" id="overallFill"></div>
    </div>
  </div>

  <!-- Queue list -->
  <div class="queue-list" id="queueList"></div>

</div>
</div><!-- /batchPane -->

<footer>⚡ VortexDL v4.0 · Single &amp; Batch Download · Powered by Flask + yt-dlp</footer>
</div><!-- /wrap -->

<script>
// ═══════════════════════════════════════════════
//  SHARED UTILS
// ═══════════════════════════════════════════════
const PLATS = [
  ["youtube.com",    "YouTube",    "▶", "#ff0000"],
  ["youtu.be",       "YouTube",    "▶", "#ff0000"],
  ["tiktok.com",     "TikTok",     "♪", "#69c9d0"],
  ["instagram.com",  "Instagram",  "◈", "#e1306c"],
  ["facebook.com",   "Facebook",   "f", "#1877f2"],
  ["fb.watch",       "Facebook",   "f", "#1877f2"],
  ["twitter.com",    "Twitter/X",  "𝕏", "#1da1f2"],
  ["x.com",          "Twitter/X",  "𝕏", "#1da1f2"],
  ["reddit.com",     "Reddit",     "⬆","#ff4500"],
  ["vimeo.com",      "Vimeo",      "V", "#1ab7ea"],
  ["dailymotion.com","Dailymotion","D", "#0066dc"],
  ["twitch.tv",      "Twitch",     "⬛","#9146ff"],
  ["pinterest.com",  "Pinterest",  "P", "#e60023"],
  ["linkedin.com",   "LinkedIn",  "in", "#0a66c2"],
  ["soundcloud.com", "SoundCloud", "☁","#ff5500"],
  ["rumble.com",     "Rumble",     "R", "#85c742"],
  ["bilibili.com",   "Bilibili",   "B", "#00a1d6"],
  ["ted.com",        "TED",        "T", "#e62b1e"],
  ["loom.com",       "Loom",       "L", "#625df5"],
  ["ucshare.com",    "UCShare",    "U", "#f5a623"],
  ["uc.cn",          "UCShare",    "U", "#f5a623"],
];

function detectPlat(url) {
  const u = url.toLowerCase();
  for (const [kw, name, emoji, color] of PLATS) {
    if (u.includes(kw)) return { name, emoji, color };
  }
  return null;
}

function fmtDur(sec) {
  if (!sec) return '';
  const h=Math.floor(sec/3600),m=Math.floor((sec%3600)/60),s=Math.floor(sec%60);
  return h>0?`${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`:`${m}:${String(s).padStart(2,'0')}`;
}

function triggerDownload(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  document.body.appendChild(a); a.click();
  URL.revokeObjectURL(a.href); a.remove();
}

function getFilename(resp, fallback) {
  const d = resp.headers.get('Content-Disposition') || '';
  const m = d.match(/filename\*?=(?:UTF-8'')?["']?([^"';\r\n]+)["']?/i);
  return m ? decodeURIComponent(m[1].trim()) : fallback;
}

// ═══════════════════════════════════════════════
//  TAB SWITCHER
// ═══════════════════════════════════════════════
function switchTab(tab) {
  document.getElementById('singlePane').style.display = tab==='single' ? '' : 'none';
  document.getElementById('batchPane').classList.toggle('show', tab==='batch');
  document.getElementById('tabSingle').classList.toggle('active', tab==='single');
  document.getElementById('tabBatch').classList.toggle('active',  tab==='batch');
}

// ═══════════════════════════════════════════════
//  SINGLE MODE
// ═══════════════════════════════════════════════
let sCurrentUrl = '', sPollInterval = null;

document.getElementById('sUrl').addEventListener('input', function() {
  const p = detectPlat(this.value.trim());
  const bar = document.getElementById('sDetBar');
  if (p) {
    bar.classList.add('show');
    document.getElementById('sDetDot').style.background  = p.color;
    document.getElementById('sDetLabel').textContent = `${p.emoji} ${p.name}`;
  } else bar.classList.remove('show');
});
document.getElementById('sUrl').addEventListener('keydown', e => { if(e.key==='Enter') singleFetch(); });

function selFmt(el, prefix) {
  document.querySelectorAll(`.fmt-btn`).forEach(b => b.classList.remove('sel'));
  el.classList.add('sel');
}

function showSErr(msg) { const e=document.getElementById('sErr'); e.innerHTML='⚠ '+msg; e.classList.add('show'); }
function hideSErr()    { document.getElementById('sErr').classList.remove('show'); }

async function singleFetch() {
  const url = document.getElementById('sUrl').value.trim();
  if (!url) { showSErr('Masukkan URL terlebih dahulu.'); return; }
  const p = detectPlat(url);
  if (!p) { showSErr('Platform tidak dikenali.'); return; }

  hideSErr(); sCurrentUrl = url;
  document.getElementById('sLoader').classList.add('show');
  document.getElementById('sPreview').classList.remove('show');
  document.getElementById('sBtnFetch').disabled = true;
  document.getElementById('sBtnTxt').textContent = 'Memuat...';

  try {
    const resp = await fetch('/api/info', {
      method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})
    });
    const data = await resp.json();
    document.getElementById('sLoader').classList.remove('show');

    if (!data.success) { showSErr(data.error||'Gagal.'); return; }
    const info = data.info;

    // thumbnail
    const img = document.getElementById('sThumb');
    img.src = info.thumbnail || `https://placehold.co/640x360/0e1319/333?text=${encodeURIComponent(p.name)}`;
    img.onerror = () => img.src = `https://placehold.co/640x360/0e1319/333?text=${encodeURIComponent(p.name)}`;

    const tag = document.getElementById('sPlatTag');
    tag.textContent = `${p.emoji} ${p.name}`;
    tag.style.cssText = `background:${p.color}22;color:${p.color};border:1px solid ${p.color}44`;

    const dur = fmtDur(info.duration);
    const durTag = document.getElementById('sDurTag');
    if (dur) { durTag.textContent=dur; durTag.style.display=''; } else durTag.style.display='none';

    document.getElementById('sTitle').textContent    = info.title    || 'Tanpa Judul';
    document.getElementById('sUploader').textContent = info.uploader || 'Unknown';
    if (dur) document.getElementById('sDur').textContent = dur;
    else document.getElementById('sDurWrap').style.display='none';

    document.getElementById('sPreview').classList.add('show');
    document.getElementById('sProgWrap').classList.remove('show');
    document.getElementById('sBtnDl').disabled = false;

  } catch(e) {
    document.getElementById('sLoader').classList.remove('show');
    showSErr('Kesalahan jaringan. Coba lagi.');
  } finally {
    document.getElementById('sBtnFetch').disabled = false;
    document.getElementById('sBtnTxt').textContent = 'Get Info';
  }
}

async function singleDl() {
  if (!sCurrentUrl) return;
  const fmt = document.querySelector('input[name="sFmt"]:checked')?.value || '720p';
  const sid = Math.random().toString(36).substring(2);

  const btn = document.getElementById('sBtnDl');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner" style="width:16px;height:16px;border-width:2px;display:inline-block;margin-right:8px;vertical-align:middle"></span>Memproses...';

  const pw=document.getElementById('sProgWrap'), pf=document.getElementById('sProgFill');
  const ps=document.getElementById('sProgStatus'), pp=document.getElementById('sProgPct');
  pw.classList.add('show'); pf.classList.add('indet');
  ps.textContent='Memulai...'; pp.textContent='...';

  if (sPollInterval) clearInterval(sPollInterval);
  sPollInterval = setInterval(async()=>{
    try {
      const r=await fetch(`/api/progress/${sid}`);
      const d=await r.json();
      if(d.percent!==undefined){
        pf.classList.remove('indet'); pf.style.width=d.percent+'%';
        pp.textContent=d.percent+'%'; ps.textContent=d.status||'Downloading...';
      }
    }catch{}
  },800);

  try {
    const resp = await fetch('/api/download',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:sCurrentUrl,format:fmt,session_id:sid})
    });
    clearInterval(sPollInterval); pf.classList.remove('indet');

    if (!resp.ok) {
      const e=await resp.json().catch(()=>({}));
      showSErr(e.error||'Download gagal.'); pw.classList.remove('show');
    } else {
      pf.style.width='100%'; ps.textContent='✓ Download selesai!'; pp.textContent='100%';
      const blob=await resp.blob();
      triggerDownload(blob, getFilename(resp,'video.mp4'));
    }
  } catch(e) {
    clearInterval(sPollInterval);
    showSErr('Download gagal. Periksa koneksi.'); pw.classList.remove('show');
  } finally {
    btn.disabled=false; btn.innerHTML='<span>⬇</span> DOWNLOAD SEKARANG';
  }
}

// ═══════════════════════════════════════════════
//  BATCH MODE
// ═══════════════════════════════════════════════
let bFmt      = '720p';
let bItems    = [];    // [{id, url, status, info, file_id}]
let bRunning  = false;
let bDoneFiles = [];   // [{file_id, filename}] for ZIP

function selBFmt(el, fmt) {
  document.querySelectorAll('.bfmt').forEach(b=>b.classList.remove('sel'));
  el.classList.add('sel');
  bFmt = fmt;
}

// Live parse textarea
document.getElementById('bTextarea').addEventListener('input', parseTextarea);

function parseTextarea() {
  const lines = document.getElementById('bTextarea').value
    .split('\n').map(l=>l.trim()).filter(l=>l.length>0);

  const valid = lines.filter(l => detectPlat(l));
  const summary = document.getElementById('bSummary');

  if (valid.length > 0) {
    summary.classList.add('show');
    document.getElementById('bCount').textContent = valid.length;
    // unique platforms
    const platSet = new Set(valid.map(u => detectPlat(u)?.name).filter(Boolean));
    document.getElementById('bPlatList').textContent = '· ' + [...platSet].join(', ');
  } else {
    summary.classList.remove('show');
  }
}

function showBErr(msg) { const e=document.getElementById('bErr'); e.innerHTML='⚠ '+msg; e.classList.add('show'); }
function hideBErr()    { document.getElementById('bErr').classList.remove('show'); }

function batchClear() {
  document.getElementById('bTextarea').value = '';
  document.getElementById('queueList').innerHTML = '';
  document.getElementById('bSummary').classList.remove('show');
  document.getElementById('bStats').classList.remove('show');
  document.getElementById('overallBar').classList.remove('show');
  document.getElementById('bBtnZip').classList.remove('show');
  hideBErr();
  bItems = []; bDoneFiles = []; bRunning = false;
  updateStats();
}

async function batchStart() {
  if (bRunning) return;

  const lines = document.getElementById('bTextarea').value
    .split('\n').map(l=>l.trim()).filter(l=>l.length>0);

  const valid = lines.filter(l=>detectPlat(l));
  if (valid.length === 0) { showBErr('Tidak ada URL yang valid. Periksa kembali link-nya.'); return; }
  if (valid.length > 20)  { showBErr('Maksimal 20 link sekaligus.'); return; }

  hideBErr();
  bItems    = [];
  bDoneFiles= [];
  bRunning  = true;

  document.getElementById('bBtnStart').disabled = true;
  document.getElementById('bBtnZip').classList.remove('show');
  document.getElementById('overallBar').classList.add('show');
  document.getElementById('bStats').classList.add('show');
  document.getElementById('queueList').innerHTML = '';

  // Build item list & render cards
  for (const url of valid) {
    const id   = 'b_' + Math.random().toString(36).substring(2,8);
    const plat = detectPlat(url);
    bItems.push({ id, url, status:'waiting', info:null, plat, file_id:null });
    renderQItem(bItems[bItems.length-1]);
  }

  updateStats();
  updateOverall();

  // Step 1: fetch info for all items
  for (const item of bItems) {
    setItemStatus(item, 'fetching', 'Mengambil info...');
    try {
      const resp = await fetch('/api/info',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:item.url})
      });
      const data = await resp.json();
      if (data.success) {
        item.info = data.info;
        updateQItemInfo(item);
        setItemStatus(item,'queued','Menunggu giliran');
      } else {
        setItemStatus(item,'error', data.error||'Gagal fetch info');
      }
    } catch(e) {
      setItemStatus(item,'error','Koneksi gagal');
    }
    updateStats(); updateOverall();
  }

  // Step 2: download one by one
  for (const item of bItems) {
    if (item.status === 'error') continue;
    setItemStatus(item,'running','Downloading...');
    updateStats(); updateOverall();

    const sid = Math.random().toString(36).substring(2);
    item.file_id = sid;

    // mini progress polling
    const poll = setInterval(async()=>{
      try {
        const r=await fetch(`/api/progress/${sid}`);
        const d=await r.json();
        setItemProgress(item.id, d.percent||0, d.status||'');
      }catch{}
    },800);

    try {
      const resp = await fetch('/api/download',{
        method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({url:item.url, format:bFmt, session_id:sid, save_for_zip:true})
      });
      clearInterval(poll);

      if (!resp.ok) {
        const e=await resp.json().catch(()=>({}));
        setItemStatus(item,'error', e.error||'Download gagal');
      } else {
        setItemProgress(item.id,100,'✓ Selesai');
        setItemStatus(item,'done','✓ Selesai');
        // offer individual download
        const blob = await resp.blob();
        const fname = getFilename(resp, `video_${item.id}.mp4`);
        bDoneFiles.push({id:item.id, blob, fname});
        addItemDlBtn(item.id, blob, fname);
      }
    } catch(e) {
      clearInterval(poll);
      setItemStatus(item,'error','Download error');
    }

    updateStats(); updateOverall();
    // small delay between downloads
    await new Promise(r=>setTimeout(r,600));
  }

  bRunning = false;
  document.getElementById('bBtnStart').disabled = false;
  if (bDoneFiles.length > 1) document.getElementById('bBtnZip').classList.add('show');
  updateOverall();
}

// ── Batch ZIP ──
async function batchZip() {
  if (bDoneFiles.length === 0) return;
  const btn = document.getElementById('bBtnZip');
  btn.disabled = true; btn.innerHTML = '⏳ Membuat ZIP...';

  try {
    // Use server-side zip
    const ids = bDoneFiles.map(f=>f.id);
    const resp = await fetch('/api/batch_zip',{
      method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({file_ids: ids})
    });
    if (resp.ok) {
      const blob = await resp.blob();
      triggerDownload(blob, getFilename(resp,'vortexdl_batch.zip'));
    } else {
      // fallback: individual
      alert('ZIP server tidak tersedia. Silakan download satu per satu.');
    }
  } catch(e) {
    alert('Gagal membuat ZIP. Download satu per satu ya.');
  } finally {
    btn.disabled = false; btn.innerHTML = '🗜 Download Semua (ZIP)';
  }
}

// ── Render helpers ──
function renderQItem(item) {
  const qList = document.getElementById('queueList');
  const el = document.createElement('div');
  el.className = 'q-item waiting';
  el.id = 'qi_' + item.id;
  const plat = item.plat || {};
  const shortUrl = item.url.length > 55 ? item.url.substring(0,55)+'…' : item.url;
  el.innerHTML = `
    <div class="q-thumb-placeholder" id="qtp_${item.id}">${plat.emoji||'🎬'}</div>
    <div class="q-body">
      <div class="q-title" id="qt_${item.id}">${shortUrl}</div>
      <div class="q-meta">
        <span style="color:${plat.color||'var(--muted)'}">${plat.name||'Unknown'}</span>
        <span id="qm_${item.id}"></span>
      </div>
      <div class="q-prog-mini"><div class="q-prog-fill" id="qp_${item.id}"></div></div>
      <div id="qdl_${item.id}"></div>
    </div>
    <div class="q-status">
      <span class="status-badge sb-waiting" id="qb_${item.id}">⏸ Menunggu</span>
    </div>`;
  qList.appendChild(el);
}

function updateQItemInfo(item) {
  const info = item.info;
  if (!info) return;
  const title = (info.title||'').substring(0,60) || item.url.substring(0,40);
  const el_t = document.getElementById('qt_'+item.id);
  if (el_t) el_t.textContent = title;

  const dur = fmtDur(info.duration);
  const el_m = document.getElementById('qm_'+item.id);
  if (el_m && dur) el_m.textContent = '· ' + dur;

  // thumbnail
  if (info.thumbnail) {
    const tp = document.getElementById('qtp_'+item.id);
    if (tp) {
      const img = document.createElement('img');
      img.className = 'q-thumb';
      img.src = info.thumbnail;
      img.onerror = () => {};
      tp.replaceWith(img);
    }
  }
}

const STATUS_MAP = {
  waiting:  ['waiting',  'sb-waiting',  '⏸ Menunggu'],
  fetching: ['fetching', 'sb-fetching', '🔍 Mengambil info...'],
  queued:   ['queued',   'sb-queued',   '🕐 Antrian'],
  running:  ['running',  'sb-running',  '⬇ Downloading'],
  done:     ['done',     'sb-done',     '✓ Selesai'],
  error:    ['error',    'sb-error',    '✗ Gagal'],
  skipped:  ['skipped',  'sb-skipped',  '— Dilewati'],
};

function setItemStatus(item, status, msg) {
  item.status = status;
  const el  = document.getElementById('qi_'+item.id);
  const badge = document.getElementById('qb_'+item.id);
  if (!el || !badge) return;
  const [cls, bcls, label] = STATUS_MAP[status] || STATUS_MAP.waiting;
  el.className = 'q-item ' + cls;
  badge.className = 'status-badge ' + bcls;
  badge.textContent = label;
}

function setItemProgress(id, pct, statusTxt) {
  const fill = document.getElementById('qp_'+id);
  if (!fill) return;
  if (pct >= 100) { fill.classList.remove('indet'); fill.style.width='100%'; }
  else if (pct > 0) { fill.classList.remove('indet'); fill.style.width=pct+'%'; }
  else { fill.classList.add('indet'); }
}

function addItemDlBtn(id, blob, fname) {
  const cont = document.getElementById('qdl_'+id);
  if (!cont) return;
  const btn = document.createElement('a');
  btn.className = 'q-dl-btn';
  btn.textContent = '⬇ Simpan file';
  btn.href = URL.createObjectURL(blob);
  btn.download = fname;
  cont.appendChild(btn);
}

function updateStats() {
  const done    = bItems.filter(i=>i.status==='done').length;
  const pending = bItems.filter(i=>['waiting','queued','running','fetching'].includes(i.status)).length;
  const err     = bItems.filter(i=>i.status==='error').length;
  document.getElementById('statDone').textContent    = done;
  document.getElementById('statPending').textContent = pending;
  document.getElementById('statErr').textContent     = err;
}

function updateOverall() {
  const total   = bItems.length;
  if (total === 0) return;
  const done    = bItems.filter(i=>['done','error','skipped'].includes(i.status)).length;
  const pct     = Math.round(done/total*100);
  document.getElementById('overallFill').style.width = pct+'%';
  document.getElementById('overallPct').textContent  = pct+'%';
  const running = bItems.find(i=>i.status==='running');
  if (running?.info?.title) {
    document.getElementById('overallStatus').textContent = `⬇ ${running.info.title.substring(0,40)}`;
  } else if (pct===100) {
    document.getElementById('overallStatus').textContent = `✓ Semua selesai (${total} item)`;
  } else {
    document.getElementById('overallStatus').textContent = `Memproses ${done}/${total} item...`;
  }
}
</script>
</body>
</html>
"""

# ─────────────────────────────────────────────────────────────
#  ROUTES — SINGLE
# ─────────────────────────────────────────────────────────────
@app.route("/")
def index():
    return render_template_string(HTML)


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json() or {}
    url  = data.get("url","").strip()
    if not url:
        return jsonify({"success":False,"error":"URL tidak boleh kosong."})
    if not detect_platform(url):
        return jsonify({"success":False,"error":"Platform tidak dikenali."})

    # ── UCShare: custom extractor ──
    if is_ucshare(url):
        try:
            info = ucshare_extract_info(url)
            return jsonify({"success":True,"info":{
                "title":    info["title"],
                "uploader": info["uploader"],
                "duration": info["duration"],
                "thumbnail":info["thumbnail"],
                "platform": "UCShare",
            }})
        except Exception as e:
            return jsonify({"success":False,"error":f"UCShare: {str(e)[:180]}"})

    opts = base_ydl_opts()
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        entries = info.get("entries") or []
        if entries:
            first    = next((e for e in entries if e), info)
            thumb    = pick_thumbnail(first) or pick_thumbnail(info)
            title    = info.get("title") or first.get("title") or "Tanpa Judul"
            uploader = (info.get("uploader") or first.get("uploader")
                        or info.get("channel") or "Unknown")
            duration = first.get("duration")
        else:
            thumb    = pick_thumbnail(info)
            title    = info.get("title") or "Tanpa Judul"
            uploader = info.get("uploader") or info.get("channel") or "Unknown"
            duration = info.get("duration")

        plat = detect_platform(url)
        return jsonify({"success":True,"info":{
            "title":title,"uploader":uploader,"duration":duration,
            "thumbnail":thumb,"platform":plat[0] if plat else "Unknown",
        }})

    except yt_dlp.utils.DownloadError as e:
        msg = str(e).lower()
        if "private"   in msg: err="Konten ini bersifat private."
        elif "removed" in msg or "deleted" in msg: err="Konten telah dihapus."
        elif "age"     in msg: err="Konten memerlukan verifikasi usia."
        elif "unavailable" in msg: err="Konten tidak tersedia di wilayah ini."
        else: err="Gagal memuat konten. Periksa URL-nya."
        return jsonify({"success":False,"error":err})
    except Exception as e:
        return jsonify({"success":False,"error":f"Error: {str(e)[:120]}"})


@app.route("/api/download", methods=["POST"])
def api_download():
    data       = request.get_json() or {}
    url        = data.get("url","").strip()
    fmt        = data.get("format","720p")
    session_id = data.get("session_id", str(uuid.uuid4()))
    save_zip   = data.get("save_for_zip", False)  # keep file for zip later

    if not url or not detect_platform(url):
        return jsonify({"error":"URL tidak valid atau platform tidak didukung."}), 400

    progress_store[session_id] = {"percent":0,"status":"Mempersiapkan..."}

    def hook(d):
        if d["status"]=="downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
            dl    = d.get("downloaded_bytes",0)
            pct   = int(dl/total*100) if total>0 else 0
            spd   = d.get("speed")
            ss    = f"{spd/1024/1024:.1f} MB/s" if spd else ""
            progress_store[session_id]={"percent":min(pct,95),"status":f"Downloading... {ss}".strip()}
        elif d["status"]=="finished":
            progress_store[session_id]={"percent":98,"status":"Memproses file..."}

    is_audio = fmt=="mp3"
    safe_sid = re.sub(r'[^a-zA-Z0-9_-]','',session_id)

    # ── UCShare: gunakan custom downloader ──
    if is_ucshare(url):
        try:
            out_file = DOWNLOAD_DIR / f"{safe_sid}_ucshare.mp4"
            progress_store[session_id] = {"percent":0,"status":"Menghubungi UCShare..."}
            info = ucshare_download(str(url), str(out_file), hook_fn=hook)

            if not out_file.exists():
                return jsonify({"error":"File UCShare tidak ditemukan setelah download."}), 500

            progress_store[session_id] = {"percent":100,"status":"✓ Selesai!"}
            title   = re.sub(r'[^\w\s\-.]','', info.get("title","ucshare_video")).strip()[:60] or "ucshare_video"
            dl_name = f"{title}.mp4"

            if save_zip:
                batch_store[session_id] = {"path":str(out_file),"name":dl_name}

            response = send_file(str(out_file), as_attachment=True,
                                 download_name=dl_name, mimetype="video/mp4")
            @response.call_on_close
            def _uc_cleanup():
                try:
                    if session_id not in batch_store:
                        out_file.unlink(missing_ok=True)
                    progress_store.pop(session_id,None)
                except: pass
            return response

        except Exception as e:
            progress_store.pop(session_id,None)
            return jsonify({"error":f"UCShare download gagal: {str(e)[:180]}"}), 400

    out_tmpl = str(DOWNLOAD_DIR / f"{safe_sid}_%(title)s.%(ext)s")

    opts = base_ydl_opts(hook)
    opts["outtmpl"] = out_tmpl
    opts["format"]  = get_format_selector(fmt)

    if is_audio:
        opts["postprocessors"] = [{"key":"FFmpegExtractAudio","preferredcodec":"mp3","preferredquality":"192"}]
    else:
        opts["merge_output_format"] = "mp4"
        opts["postprocessors"]      = [{"key":"FFmpegVideoConvertor","preferedformat":"mp4"}]

    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            prepared = ydl.prepare_filename(info)

        ext       = "mp3" if is_audio else "mp4"
        candidate = Path(prepared).with_suffix(f".{ext}")
        if not candidate.exists():
            matches = list(DOWNLOAD_DIR.glob(f"{safe_sid}_*.{ext}"))
            if not matches: matches = list(DOWNLOAD_DIR.glob(f"{safe_sid}_*"))
            if matches: candidate = matches[0]
            else: return jsonify({"error":"File tidak ditemukan setelah download."}), 500

        progress_store[session_id] = {"percent":100,"status":"✓ Selesai!"}
        title    = safe_fname(info)
        dl_name  = f"{title}.{ext}"
        mime     = "audio/mpeg" if is_audio else "video/mp4"

        # Register file for batch zip usage
        if save_zip:
            batch_store[session_id] = {"path": str(candidate), "name": dl_name}

        response = send_file(str(candidate), as_attachment=True,
                             download_name=dl_name, mimetype=mime)

        @response.call_on_close
        def _cleanup():
            try:
                # Only delete if not reserved for zip
                if session_id not in batch_store:
                    candidate.unlink(missing_ok=True)
                progress_store.pop(session_id, None)
            except: pass

        return response

    except yt_dlp.utils.DownloadError as e:
        progress_store.pop(session_id, None)
        msg = str(e).lower()
        if "private"   in msg: err="Konten bersifat private."
        elif "unavailable" in msg: err="Konten tidak tersedia."
        else: err="Download gagal. Coba lagi."
        return jsonify({"error":err}), 400
    except Exception as e:
        progress_store.pop(session_id, None)
        return jsonify({"error":f"Error: {str(e)[:120]}"}), 500


@app.route("/api/progress/<session_id>")
def api_progress(session_id):
    return jsonify(progress_store.get(session_id,{"percent":0,"status":"Mempersiapkan..."}))


# ─────────────────────────────────────────────────────────────
#  BATCH ZIP ENDPOINT
# ─────────────────────────────────────────────────────────────
@app.route("/api/batch_zip", methods=["POST"])
def api_batch_zip():
    data     = request.get_json() or {}
    file_ids = data.get("file_ids", [])

    files = []
    for fid in file_ids:
        entry = batch_store.get(fid)
        if entry and Path(entry["path"]).exists():
            files.append(entry)

    if not files:
        return jsonify({"error":"Tidak ada file yang tersedia untuk di-ZIP."}), 400

    zip_id   = str(uuid.uuid4())[:8]
    zip_path = DOWNLOAD_DIR / f"vortexdl_batch_{zip_id}.zip"

    try:
        with zipfile.ZipFile(zip_path,"w",zipfile.ZIP_DEFLATED) as zf:
            for entry in files:
                zf.write(entry["path"], entry["name"])

        response = send_file(str(zip_path), as_attachment=True,
                             download_name=f"VortexDL_Batch_{zip_id}.zip",
                             mimetype="application/zip")

        @response.call_on_close
        def _cleanup_zip():
            try:
                zip_path.unlink(missing_ok=True)
                for entry in files:
                    try: Path(entry["path"]).unlink(missing_ok=True)
                    except: pass
                    batch_store.pop([k for k,v in batch_store.items() if v==entry][0], None)
            except: pass

        return response

    except Exception as e:
        return jsonify({"error":f"Gagal membuat ZIP: {str(e)[:100]}"}), 500


if __name__ == "__main__":
    print("\n" + "="*56)
    print("  ⚡  VortexDL v4.0 — Single & Batch Downloader")
    print("="*56)
    print("  ✔  Mode      : Single + Batch (antrian otomatis)")
    print("  ✔  Platform  : YouTube, TikTok, Instagram, UCShare, +16 lagi")
    print("  ✔  Batch     : Maks 20 link · ZIP semua hasil")
    print("  ✔  Format    : 360p · 720p · 1080p · MP3")
    print(f"  ✔  Folder    : {DOWNLOAD_DIR.resolve()}")
    print("\n  ▶  Buka browser:  http://localhost:8080")
    print("="*56 + "\n")
    port = int(os.environ.get("PORT", 8080))
    app.run(debug=False, host="0.0.0.0", port=port)
