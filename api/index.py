from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp
import requests
import json
import re
import os
import tempfile
import threading
import time
from urllib.parse import urlparse

app = Flask(__name__)

# ─────────────────────────────────────────
#  PLATFORM DETECTION
# ─────────────────────────────────────────
def detect_platform(url: str) -> str:
    patterns = {
        "youtube":   r"(youtube\.com|youtu\.be)",
        "tiktok":    r"(tiktok\.com|vm\.tiktok\.com)",
        "instagram": r"instagram\.com",
        "facebook":  r"(facebook\.com|fb\.watch|fb\.com)",
        "twitter":   r"(twitter\.com|x\.com|t\.co)",
    }
    for platform, pattern in patterns.items():
        if re.search(pattern, url, re.IGNORECASE):
            return platform
    return "unknown"

# ─────────────────────────────────────────
#  YT-DLP HELPERS
# ─────────────────────────────────────────
def get_ydl_opts_info(platform: str) -> dict:
    base = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "skip_download": True,
        "socket_timeout": 30,
        "http_headers": {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) "
                          "Chrome/120.0.0.0 Safari/537.36"
        },
    }
    if platform == "tiktok":
        base["extractor_args"] = {"tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"}}
    return base


def format_duration(seconds) -> str:
    if not seconds:
        return "N/A"
    try:
        s = int(seconds)
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"
    except Exception:
        return "N/A"


def format_filesize(size) -> str:
    if not size:
        return "N/A"
    try:
        for unit in ["B", "KB", "MB", "GB"]:
            if size < 1024:
                return f"{size:.1f} {unit}"
            size /= 1024
        return f"{size:.1f} TB"
    except Exception:
        return "N/A"


def parse_formats(info: dict, platform: str) -> list:
    formats = info.get("formats", [])
    result = []
    seen = set()

    for f in formats:
        vcodec = f.get("vcodec", "none")
        acodec = f.get("acodec", "none")
        if vcodec in (None, "none"):
            continue

        height = f.get("height") or 0
        ext    = f.get("ext", "mp4")
        fid    = f.get("format_id", "")
        size   = f.get("filesize") or f.get("filesize_approx")
        tbr    = f.get("tbr") or 0

        label = f"{height}p" if height else fid
        if label in seen:
            continue
        seen.add(label)

        result.append({
            "format_id": fid,
            "label":     label,
            "ext":       ext,
            "height":    height,
            "filesize":  format_filesize(size),
            "tbr":       tbr,
            "has_audio": acodec not in (None, "none"),
        })

    result.sort(key=lambda x: x["height"], reverse=True)
    return result[:8] if result else [{"format_id": "best", "label": "Best", "ext": "mp4", "height": 0, "filesize": "N/A", "tbr": 0, "has_audio": True}]


# ─────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/info", methods=["POST"])
def api_info():
    data = request.get_json(silent=True) or {}
    url  = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "URL tidak boleh kosong"}), 400

    platform = detect_platform(url)
    if platform == "unknown":
        return jsonify({"error": "Platform tidak didukung. Gunakan YouTube, TikTok, Instagram, Facebook, atau Twitter/X."}), 400

    try:
        opts = get_ydl_opts_info(platform)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"error": "Tidak dapat mengambil informasi video"}), 400

        thumbnails = info.get("thumbnails", [])
        thumb = ""
        if thumbnails:
            best = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            thumb = best.get("url", "")
        if not thumb:
            thumb = info.get("thumbnail", "")

        formats = parse_formats(info, platform)

        return jsonify({
            "title":      info.get("title", "Video"),
            "duration":   format_duration(info.get("duration")),
            "thumbnail":  thumb,
            "uploader":   info.get("uploader") or info.get("channel") or "",
            "view_count": info.get("view_count"),
            "platform":   platform,
            "formats":    formats,
            "original_url": url,
        })

    except yt_dlp.utils.DownloadError as e:
        msg = str(e)
        if "Sign in" in msg or "login" in msg.lower():
            return jsonify({"error": "Video memerlukan login / bersifat privat"}), 400
        if "Unsupported URL" in msg:
            return jsonify({"error": "URL tidak didukung oleh yt-dlp"}), 400
        return jsonify({"error": f"Gagal mengambil info: {msg[:200]}"}), 400
    except Exception as e:
        return jsonify({"error": f"Error tidak terduga: {str(e)[:200]}"}), 500


@app.route("/download", methods=["POST"])
def download():
    data      = request.get_json(silent=True) or {}
    url       = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "bestvideo+bestaudio/best").strip()
    platform  = detect_platform(url)

    if not url:
        return jsonify({"error": "URL tidak boleh kosong"}), 400

    # Build format string
    if format_id and format_id not in ("best", "bestvideo+bestaudio/best"):
        # Try to merge with best audio if format has no audio
        fmt_str = f"{format_id}+bestaudio/bestvideo+bestaudio/best"
    else:
        fmt_str = "bestvideo+bestaudio/best"

    tmp_dir = tempfile.mkdtemp()

    try:
        opts = {
            "format":        fmt_str,
            "outtmpl":       os.path.join(tmp_dir, "%(title).80s.%(ext)s"),
            "quiet":         True,
            "no_warnings":   True,
            "merge_output_format": "mp4",
            "socket_timeout": 60,
            "http_headers": {
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                              "AppleWebKit/537.36 (KHTML, like Gecko) "
                              "Chrome/120.0.0.0 Safari/537.36"
            },
            "postprocessors": [{
                "key": "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        }

        if platform == "tiktok":
            opts["extractor_args"] = {"tiktok": {"api_hostname": "api22-normal-c-useast2a.tiktokv.com"}}

        with yt_dlp.YoutubeDL(opts) as ydl:
            info      = ydl.extract_info(url, download=True)
            filename  = ydl.prepare_filename(info)

        # Find the actual file (might have different extension after merge)
        actual_file = None
        for f in os.listdir(tmp_dir):
            fpath = os.path.join(tmp_dir, f)
            if os.path.isfile(fpath):
                actual_file = fpath
                break

        if not actual_file or not os.path.exists(actual_file):
            # fallback: use prepared filename
            base = os.path.splitext(filename)[0]
            for ext in [".mp4", ".mkv", ".webm", ".mov"]:
                candidate = base + ext
                if os.path.exists(candidate):
                    actual_file = candidate
                    break

        if not actual_file or not os.path.exists(actual_file):
            return jsonify({"error": "File download gagal – tidak ditemukan di tmp"}), 500

        video_title = info.get("title", "video")
        safe_title  = re.sub(r'[^\w\s\-]', '', video_title)[:80].strip() or "video"
        dl_name     = f"{safe_title}.mp4"

        def generate():
            with open(actual_file, "rb") as fh:
                while chunk := fh.read(1024 * 512):  # 512 KB chunks
                    yield chunk
            # Cleanup after streaming
            try:
                os.remove(actual_file)
                os.rmdir(tmp_dir)
            except Exception:
                pass

        filesize = os.path.getsize(actual_file)
        resp = Response(
            stream_with_context(generate()),
            mimetype="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length":      str(filesize),
                "X-Video-Title":       safe_title,
            },
        )
        return resp

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": f"Download error: {str(e)[:300]}"}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:300]}"}), 500


# ─────────────────────────────────────────
#  HTML FRONTEND (single-file embedded)
# ─────────────────────────────────────────
HTML_PAGE = """<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>VortexDL — Video Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:ital,opsz,wght@0,9..40,300;0,9..40,400;0,9..40,500;1,9..40,300&display=swap" rel="stylesheet"/>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}

:root{
  --bg0:#05060a;
  --bg1:#0c0d14;
  --bg2:#121420;
  --bg3:#1a1d2e;
  --glass:rgba(255,255,255,0.04);
  --glass-border:rgba(255,255,255,0.08);
  --accent:#7c6aff;
  --accent2:#ff6ac1;
  --accent3:#6affdb;
  --text:#eeeef4;
  --text-dim:#888ca8;
  --text-muted:#444660;
  --success:#22d87a;
  --error:#ff4f6a;
  --warn:#ffb347;
  --radius:16px;
  --radius-sm:10px;
  --transition:.25s cubic-bezier(.4,0,.2,1);
}

html{scroll-behavior:smooth}
body{
  font-family:'DM Sans',sans-serif;
  background:var(--bg0);
  color:var(--text);
  min-height:100vh;
  overflow-x:hidden;
}

/* ── NOISE TEXTURE OVERLAY ── */
body::before{
  content:'';
  position:fixed;inset:0;
  background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 256 256' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='4' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");
  pointer-events:none;z-index:0;opacity:.5;
}

/* ── ANIMATED BG BLOBS ── */
.blob{
  position:fixed;border-radius:50%;filter:blur(120px);
  pointer-events:none;z-index:0;animation:drift 18s ease-in-out infinite;
}
.blob-1{width:600px;height:600px;background:radial-gradient(circle,rgba(124,106,255,.18),transparent 70%);top:-200px;left:-200px;animation-delay:0s}
.blob-2{width:500px;height:500px;background:radial-gradient(circle,rgba(255,106,193,.14),transparent 70%);top:40%;right:-150px;animation-delay:-6s}
.blob-3{width:400px;height:400px;background:radial-gradient(circle,rgba(106,255,219,.1),transparent 70%);bottom:-100px;left:30%;animation-delay:-12s}
@keyframes drift{0%,100%{transform:translate(0,0) scale(1)}33%{transform:translate(40px,-30px) scale(1.05)}66%{transform:translate(-20px,50px) scale(.97)}}

/* ── LAYOUT ── */
.wrap{
  position:relative;z-index:1;
  max-width:760px;margin:0 auto;
  padding:40px 20px 80px;
}

/* ── HEADER ── */
header{text-align:center;margin-bottom:56px;padding-top:16px}
.logo{
  font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(2.4rem,6vw,3.6rem);
  background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 50%,var(--accent3) 100%);
  -webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;
  letter-spacing:-.03em;line-height:1;
}
.logo span{-webkit-text-fill-color:var(--text-dim);font-weight:400;font-size:.45em;letter-spacing:.05em}
.tagline{margin-top:12px;color:var(--text-dim);font-size:.95rem;font-weight:300;letter-spacing:.02em}

/* ── PLATFORM BADGES ── */
.platforms{
  display:flex;justify-content:center;flex-wrap:wrap;gap:10px;margin-top:20px;
}
.badge{
  display:flex;align-items:center;gap:6px;
  padding:5px 12px;border-radius:100px;
  background:var(--glass);border:1px solid var(--glass-border);
  font-size:.78rem;font-weight:500;color:var(--text-dim);
  transition:var(--transition);
}
.badge:hover{border-color:var(--accent);color:var(--accent)}
.badge .dot{width:7px;height:7px;border-radius:50%}

/* ── CARD ── */
.card{
  background:var(--glass);
  border:1px solid var(--glass-border);
  border-radius:var(--radius);
  padding:32px;
  backdrop-filter:blur(24px);
  -webkit-backdrop-filter:blur(24px);
  transition:border-color var(--transition);
}
.card:focus-within{border-color:rgba(124,106,255,.35)}

/* ── INPUT GROUP ── */
.input-group{display:flex;gap:12px;flex-wrap:wrap}
.url-input{
  flex:1;min-width:220px;
  background:rgba(255,255,255,.05);
  border:1.5px solid var(--glass-border);
  border-radius:var(--radius-sm);
  color:var(--text);
  font-family:'DM Sans',sans-serif;
  font-size:.95rem;
  padding:14px 18px;
  outline:none;
  transition:border-color var(--transition),box-shadow var(--transition);
}
.url-input::placeholder{color:var(--text-muted)}
.url-input:focus{
  border-color:var(--accent);
  box-shadow:0 0 0 3px rgba(124,106,255,.15);
}

.btn{
  display:inline-flex;align-items:center;justify-content:center;gap:8px;
  font-family:'DM Sans',sans-serif;font-weight:500;font-size:.92rem;
  border:none;cursor:pointer;border-radius:var(--radius-sm);
  padding:14px 22px;transition:var(--transition);white-space:nowrap;
}
.btn-primary{
  background:linear-gradient(135deg,var(--accent),#9e6aff);
  color:#fff;
  box-shadow:0 4px 24px rgba(124,106,255,.35);
}
.btn-primary:hover{transform:translateY(-1px);box-shadow:0 6px 32px rgba(124,106,255,.5)}
.btn-primary:active{transform:translateY(0)}
.btn-primary:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-success{
  background:linear-gradient(135deg,#22d87a,#1aad62);
  color:#fff;width:100%;
  box-shadow:0 4px 24px rgba(34,216,122,.25);
  font-size:1rem;font-weight:600;padding:16px;
}
.btn-success:hover{transform:translateY(-1px);box-shadow:0 6px 32px rgba(34,216,122,.4)}
.btn-success:active{transform:translateY(0)}
.btn-success:disabled{opacity:.5;cursor:not-allowed;transform:none}
.btn-ghost{
  background:var(--glass);border:1px solid var(--glass-border);
  color:var(--text-dim);font-size:.82rem;padding:8px 14px;
}
.btn-ghost:hover{border-color:var(--accent3);color:var(--accent3)}

/* ── SPINNER ── */
@keyframes spin{to{transform:rotate(360deg)}}
.spinner{
  width:18px;height:18px;border-radius:50%;
  border:2.5px solid rgba(255,255,255,.2);
  border-top-color:#fff;
  animation:spin .7s linear infinite;display:inline-block;flex-shrink:0;
}

/* ── PLATFORM INDICATOR ── */
.platform-indicator{
  display:none;align-items:center;gap:8px;
  margin-top:14px;font-size:.82rem;
  color:var(--accent3);font-weight:500;
}
.platform-indicator.show{display:flex}
.pi-icon{font-size:1.1em}

/* ── DIVIDER ── */
.divider{height:1px;background:var(--glass-border);margin:28px 0}

/* ── VIDEO INFO ── */
#info-section{display:none;margin-top:28px}
#info-section.show{display:block;animation:slideUp .4s cubic-bezier(.4,0,.2,1) both}
@keyframes slideUp{from{opacity:0;transform:translateY(20px)}to{opacity:1;transform:translateY(0)}}

.video-meta{
  display:grid;grid-template-columns:auto 1fr;gap:18px;
  background:rgba(255,255,255,.03);
  border:1px solid var(--glass-border);
  border-radius:var(--radius-sm);
  overflow:hidden;
}
.thumb-wrap{
  position:relative;width:160px;
  cursor:pointer;overflow:hidden;flex-shrink:0;
}
.thumb-wrap img{
  width:100%;height:100%;object-fit:cover;
  display:block;transition:transform .4s ease;
}
.thumb-wrap:hover img{transform:scale(1.05)}
.play-overlay{
  position:absolute;inset:0;display:flex;align-items:center;justify-content:center;
  background:rgba(0,0,0,.4);opacity:0;transition:opacity var(--transition);
}
.thumb-wrap:hover .play-overlay{opacity:1}
.play-icon{
  width:44px;height:44px;border-radius:50%;background:rgba(255,255,255,.9);
  display:flex;align-items:center;justify-content:center;
}
.play-icon svg{fill:var(--bg0);margin-left:3px}

.meta-body{padding:18px 18px 18px 0;display:flex;flex-direction:column;gap:8px;min-width:0}
.meta-title{
  font-family:'Syne',sans-serif;font-weight:600;font-size:1rem;
  line-height:1.4;overflow:hidden;display:-webkit-box;
  -webkit-line-clamp:2;-webkit-box-orient:vertical;
}
.meta-row{display:flex;flex-wrap:wrap;gap:12px;margin-top:auto}
.meta-chip{
  display:flex;align-items:center;gap:5px;
  font-size:.78rem;color:var(--text-dim);
  background:rgba(255,255,255,.05);
  padding:4px 10px;border-radius:100px;
}
.meta-chip svg{opacity:.7}

/* ── FORMAT SELECT ── */
.format-section{margin-top:20px}
.format-label{font-size:.82rem;color:var(--text-dim);font-weight:500;margin-bottom:10px}
.format-grid{display:flex;flex-wrap:wrap;gap:8px}
.fmt-btn{
  background:rgba(255,255,255,.05);
  border:1.5px solid var(--glass-border);
  border-radius:8px;color:var(--text-dim);
  font-family:'DM Sans',sans-serif;font-size:.8rem;
  padding:7px 14px;cursor:pointer;
  transition:var(--transition);
}
.fmt-btn:hover{border-color:var(--accent);color:var(--accent)}
.fmt-btn.active{
  background:rgba(124,106,255,.15);
  border-color:var(--accent);color:var(--accent);
  font-weight:600;
}

/* ── DOWNLOAD BTN AREA ── */
.dl-area{margin-top:22px}
.progress-bar-wrap{
  height:4px;background:rgba(255,255,255,.08);
  border-radius:100px;overflow:hidden;margin-bottom:16px;display:none;
}
.progress-bar-fill{
  height:100%;background:linear-gradient(90deg,var(--accent),var(--accent3));
  border-radius:100px;width:0%;
  transition:width .3s ease;
  animation:shimmer 1.5s infinite;
}
@keyframes shimmer{0%{filter:brightness(1)}50%{filter:brightness(1.3)}100%{filter:brightness(1)}}

/* ── PREVIEW MODAL ── */
.modal-overlay{
  display:none;position:fixed;inset:0;z-index:100;
  background:rgba(0,0,0,.85);backdrop-filter:blur(12px);
  align-items:center;justify-content:center;padding:20px;
}
.modal-overlay.show{display:flex;animation:fadeIn .2s ease}
@keyframes fadeIn{from{opacity:0}to{opacity:1}}
.modal-inner{
  max-width:720px;width:100%;
  background:var(--bg2);border:1px solid var(--glass-border);
  border-radius:var(--radius);overflow:hidden;
  animation:popIn .3s cubic-bezier(.34,1.56,.64,1) both;
}
@keyframes popIn{from{opacity:0;transform:scale(.9)}to{opacity:1;transform:scale(1)}}
.modal-header{
  display:flex;align-items:center;justify-content:space-between;
  padding:16px 20px;border-bottom:1px solid var(--glass-border);
}
.modal-title{font-family:'Syne',sans-serif;font-weight:600;font-size:.95rem}
.modal-close{
  background:none;border:none;color:var(--text-dim);cursor:pointer;
  font-size:1.4rem;line-height:1;transition:color var(--transition);
}
.modal-close:hover{color:var(--text)}
#preview-video{width:100%;max-height:70vh;background:#000}

/* ── HISTORY ── */
#history-section{margin-top:48px;display:none}
#history-section.show{display:block}
.section-head{
  display:flex;align-items:center;justify-content:space-between;
  margin-bottom:16px;
}
.section-title{font-family:'Syne',sans-serif;font-weight:700;font-size:1.05rem}
.history-list{display:flex;flex-direction:column;gap:10px}
.history-item{
  display:flex;align-items:center;gap:14px;
  background:var(--glass);border:1px solid var(--glass-border);
  border-radius:var(--radius-sm);padding:12px 14px;
  transition:border-color var(--transition);
}
.history-item:hover{border-color:rgba(124,106,255,.25)}
.h-thumb{
  width:56px;height:38px;border-radius:6px;
  object-fit:cover;flex-shrink:0;background:var(--bg3);
}
.h-title{flex:1;font-size:.85rem;overflow:hidden;white-space:nowrap;text-overflow:ellipsis;color:var(--text)}
.h-platform{font-size:.72rem;color:var(--text-dim);background:rgba(255,255,255,.06);padding:2px 8px;border-radius:100px;flex-shrink:0}
.h-time{font-size:.72rem;color:var(--text-muted);flex-shrink:0}

/* ── TOAST ── */
.toast-wrap{position:fixed;bottom:24px;right:24px;z-index:999;display:flex;flex-direction:column;gap:10px}
.toast{
  padding:13px 18px;border-radius:var(--radius-sm);
  font-size:.87rem;font-weight:500;max-width:320px;
  display:flex;align-items:center;gap:10px;
  box-shadow:0 8px 32px rgba(0,0,0,.5);
  animation:toastIn .35s cubic-bezier(.34,1.56,.64,1) both;
}
@keyframes toastIn{from{opacity:0;transform:translateX(40px)}to{opacity:1;transform:translateX(0)}}
.toast-success{background:#0d2b1e;border:1px solid #22d87a44;color:#22d87a}
.toast-error{background:#2b0d14;border:1px solid #ff4f6a44;color:#ff4f6a}
.toast-info{background:#151128;border:1px solid #7c6aff44;color:#a89aff}

/* ── FOOTER ── */
footer{text-align:center;color:var(--text-muted);font-size:.78rem;padding-top:48px}
footer a{color:var(--text-dim);text-decoration:none}

/* ── RESPONSIVE ── */
@media(max-width:520px){
  .wrap{padding:24px 16px 60px}
  .card{padding:20px}
  .video-meta{grid-template-columns:1fr}
  .thumb-wrap{width:100%;height:180px}
  .meta-body{padding:14px}
}
</style>
</head>
<body>

<!-- BLOBS -->
<div class="blob blob-1"></div>
<div class="blob blob-2"></div>
<div class="blob blob-3"></div>

<div class="wrap">

  <!-- HEADER -->
  <header>
    <div class="logo">Vortex<span>DL</span></div>
    <p class="tagline">Download video dari semua platform, cepat & tanpa batas</p>
    <div class="platforms">
      <div class="badge"><span class="dot" style="background:#ff0000"></span>YouTube</div>
      <div class="badge"><span class="dot" style="background:#ff0050"></span>TikTok</div>
      <div class="badge"><span class="dot" style="background:#e1306c"></span>Instagram</div>
      <div class="badge"><span class="dot" style="background:#1877f2"></span>Facebook</div>
      <div class="badge"><span class="dot" style="background:#1da1f2"></span>Twitter/X</div>
    </div>
  </header>

  <!-- MAIN CARD -->
  <div class="card">
    <div class="input-group">
      <input
        id="url-input"
        class="url-input"
        type="url"
        placeholder="Paste URL video di sini... (YouTube, TikTok, IG, FB, X)"
        autocomplete="off"
        spellcheck="false"
      />
      <button class="btn btn-primary" id="fetch-btn" onclick="fetchInfo()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
        Cari
      </button>
    </div>

    <!-- Platform detected -->
    <div class="platform-indicator" id="platform-tag">
      <span class="pi-icon">⚡</span>
      <span id="platform-name">—</span>
    </div>

    <!-- Video info section -->
    <div id="info-section">
      <div class="divider"></div>

      <div class="video-meta" id="video-meta">
        <div class="thumb-wrap" id="thumb-wrap" onclick="openPreview()">
          <img id="thumb-img" src="" alt="Thumbnail"/>
          <div class="play-overlay">
            <div class="play-icon">
              <svg width="16" height="16" viewBox="0 0 24 24"><polygon points="5 3 19 12 5 21 5 3"/></svg>
            </div>
          </div>
        </div>
        <div class="meta-body">
          <div class="meta-title" id="meta-title">—</div>
          <div class="meta-row">
            <div class="meta-chip" id="meta-dur">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg>
              <span>—</span>
            </div>
            <div class="meta-chip" id="meta-uploader">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg>
              <span>—</span>
            </div>
            <div class="meta-chip" id="meta-platform">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg>
              <span>—</span>
            </div>
          </div>
        </div>
      </div>

      <!-- Format chooser -->
      <div class="format-section">
        <div class="format-label">Pilih kualitas:</div>
        <div class="format-grid" id="format-grid"></div>
      </div>

      <!-- Download -->
      <div class="dl-area">
        <div class="progress-bar-wrap" id="progress-wrap">
          <div class="progress-bar-fill" id="progress-bar"></div>
        </div>
        <button class="btn btn-success" id="dl-btn" onclick="startDownload()">
          <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
          Download Video (MP4)
        </button>
      </div>
    </div>
  </div>

  <!-- HISTORY -->
  <div id="history-section">
    <div class="divider" style="margin-top:40px"></div>
    <div class="section-head">
      <div class="section-title">📋 Riwayat Download</div>
      <button class="btn btn-ghost" onclick="clearHistory()">Hapus semua</button>
    </div>
    <div class="history-list" id="history-list"></div>
  </div>

  <footer>
    <p>VortexDL &mdash; Untuk penggunaan pribadi. Hormati hak cipta konten. &nbsp;|&nbsp; Powered by <a href="https://github.com/yt-dlp/yt-dlp" target="_blank">yt-dlp</a></p>
  </footer>
</div>

<!-- PREVIEW MODAL -->
<div class="modal-overlay" id="modal" onclick="closePreview(event)">
  <div class="modal-inner">
    <div class="modal-header">
      <span class="modal-title" id="modal-title">Preview Video</span>
      <button class="modal-close" onclick="closePreviewDirect()">✕</button>
    </div>
    <video id="preview-video" controls playsinline></video>
  </div>
</div>

<!-- TOAST CONTAINER -->
<div class="toast-wrap" id="toasts"></div>

<script>
// ── STATE ──
let currentInfo = null;
let selectedFormat = null;
let downloadHistory = JSON.parse(sessionStorage.getItem('vdl_history') || '[]');

// ── PLATFORM ICONS ──
const P_ICON = {
  youtube:   '▶️ YouTube',
  tiktok:    '🎵 TikTok',
  instagram: '📸 Instagram',
  facebook:  '🔵 Facebook',
  twitter:   '🐦 Twitter/X',
  unknown:   '❓ Unknown',
};

// ── TOAST ──
function toast(msg, type='info', dur=4000){
  const wrap = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast toast-${type}`;
  el.innerHTML = (type==='success'?'✅ ':type==='error'?'❌ ':'💬 ')+msg;
  wrap.appendChild(el);
  setTimeout(()=>{el.style.opacity='0';el.style.transform='translateX(40px)';el.style.transition='.4s ease';setTimeout(()=>el.remove(),400)},dur);
}

// ── DETECT PLATFORM ON INPUT ──
document.getElementById('url-input').addEventListener('input', function(){
  const v = this.value.trim();
  const tag = document.getElementById('platform-tag');
  if(!v){tag.classList.remove('show');return}
  const p = detectPlatform(v);
  if(p!=='unknown'){
    document.getElementById('platform-name').textContent = P_ICON[p] || p;
    tag.classList.add('show');
  } else {
    tag.classList.remove('show');
  }
});

document.getElementById('url-input').addEventListener('keydown',function(e){
  if(e.key==='Enter') fetchInfo();
});

function detectPlatform(url){
  if(/youtube\.com|youtu\.be/i.test(url)) return 'youtube';
  if(/tiktok\.com|vm\.tiktok\.com/i.test(url)) return 'tiktok';
  if(/instagram\.com/i.test(url)) return 'instagram';
  if(/facebook\.com|fb\.watch/i.test(url)) return 'facebook';
  if(/twitter\.com|x\.com/i.test(url)) return 'twitter';
  return 'unknown';
}

// ── FETCH INFO ──
async function fetchInfo(){
  const url = document.getElementById('url-input').value.trim();
  if(!url){toast('Masukkan URL video terlebih dahulu','error');return;}

  const btn = document.getElementById('fetch-btn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Mengambil...';
  document.getElementById('info-section').classList.remove('show');

  try{
    const res = await fetch('/api/info',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url})
    });
    const data = await res.json();

    if(!res.ok || data.error){
      toast(data.error||'Gagal mengambil info video','error',6000);
      return;
    }

    currentInfo = data;
    selectedFormat = data.formats?.[0]?.format_id || 'best';
    renderInfo(data);
    document.getElementById('info-section').classList.add('show');

  }catch(e){
    toast('Koneksi gagal. Cek internet kamu.','error');
  }finally{
    btn.disabled = false;
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Cari';
  }
}

function renderInfo(d){
  // Thumbnail
  const img = document.getElementById('thumb-img');
  img.src = d.thumbnail || '';
  img.onerror = () => { img.src=''; img.style.background='var(--bg3)'; };

  // Meta
  document.getElementById('meta-title').textContent = d.title || 'Tanpa Judul';
  document.getElementById('meta-dur').querySelector('span').textContent = d.duration || 'N/A';
  document.getElementById('meta-uploader').querySelector('span').textContent = d.uploader || 'N/A';
  document.getElementById('meta-platform').querySelector('span').textContent = P_ICON[d.platform] || d.platform;
  document.getElementById('modal-title').textContent = d.title || 'Preview';

  // Formats
  const grid = document.getElementById('format-grid');
  grid.innerHTML = '';
  (d.formats||[]).forEach((f,i)=>{
    const b = document.createElement('button');
    b.className = 'fmt-btn' + (i===0?' active':'');
    b.dataset.fid = f.format_id;
    const audio = f.has_audio ? '🔊' : '🔇';
    b.textContent = `${f.label} ${audio} ${f.filesize!=='N/A'?'· '+f.filesize:''}`.trim();
    b.onclick = ()=>{
      document.querySelectorAll('.fmt-btn').forEach(x=>x.classList.remove('active'));
      b.classList.add('active');
      selectedFormat = f.format_id;
    };
    grid.appendChild(b);
  });
}

// ── DOWNLOAD ──
async function startDownload(){
  if(!currentInfo){toast('Cari video dulu','error');return;}

  const btn = document.getElementById('dl-btn');
  const progressWrap = document.getElementById('progress-wrap');
  const progressBar = document.getElementById('progress-bar');

  btn.disabled = true;
  btn.innerHTML = '<span class="spinner"></span> Memproses...';
  progressWrap.style.display='block';
  progressBar.style.width='5%';

  // Animate progress bar
  let pct = 5;
  const pInterval = setInterval(()=>{
    if(pct<85){pct+=Math.random()*8;progressBar.style.width=Math.min(pct,85)+'%'}
  },400);

  try{
    const res = await fetch('/download',{
      method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:currentInfo.original_url, format_id:selectedFormat||'best'})
    });

    if(!res.ok){
      const err = await res.json().catch(()=>({}));
      toast(err.error||'Download gagal','error',7000);
      return;
    }

    clearInterval(pInterval);
    progressBar.style.width='100%';

    const blob = await res.blob();
    const dlName = res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1] || 'video.mp4';
    const objUrl = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = objUrl; a.download = dlName;
    document.body.appendChild(a); a.click();
    document.body.removeChild(a);
    setTimeout(()=>URL.revokeObjectURL(objUrl),30000);

    toast('Download berhasil! 🎉','success');
    addHistory(currentInfo);

  }catch(e){
    toast('Gagal download: '+e.message,'error',6000);
    clearInterval(pInterval);
  }finally{
    btn.disabled = false;
    btn.innerHTML = '<svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Download Video (MP4)';
    setTimeout(()=>{progressWrap.style.display='none';progressBar.style.width='0%'},1500);
  }
}

// ── PREVIEW MODAL ──
function openPreview(){
  if(!currentInfo?.original_url) return;
  const v = document.getElementById('preview-video');
  // Only works if direct stream is available; otherwise show thumbnail
  v.src = currentInfo.original_url;
  document.getElementById('modal').classList.add('show');
}
function closePreviewDirect(){
  const v = document.getElementById('preview-video');
  v.pause(); v.src='';
  document.getElementById('modal').classList.remove('show');
}
function closePreview(e){if(e.target.id==='modal') closePreviewDirect();}
document.addEventListener('keydown',e=>{if(e.key==='Escape') closePreviewDirect()});

// ── HISTORY ──
function addHistory(info){
  const item = {
    title: info.title, thumbnail: info.thumbnail,
    platform: info.platform, url: info.original_url,
    time: new Date().toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit'})
  };
  downloadHistory.unshift(item);
  if(downloadHistory.length>20) downloadHistory.pop();
  sessionStorage.setItem('vdl_history', JSON.stringify(downloadHistory));
  renderHistory();
}

function renderHistory(){
  const sec = document.getElementById('history-section');
  const list = document.getElementById('history-list');
  if(!downloadHistory.length){sec.classList.remove('show');return;}
  sec.classList.add('show');
  list.innerHTML = downloadHistory.map(h=>`
    <div class="history-item">
      <img class="h-thumb" src="${h.thumbnail||''}" alt="" onerror="this.style.background='var(--bg3)';this.src=''">
      <div class="h-title" title="${escHtml(h.title)}">${escHtml(h.title)}</div>
      <div class="h-platform">${P_ICON[h.platform]||h.platform}</div>
      <div class="h-time">${h.time}</div>
    </div>
  `).join('');
}

function clearHistory(){
  downloadHistory=[];
  sessionStorage.removeItem('vdl_history');
  renderHistory();
  toast('Riwayat dihapus','info',2000);
}

function escHtml(s){
  return (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
}

// Init
renderHistory();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
