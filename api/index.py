from flask import Flask, request, jsonify, Response, stream_with_context
import yt_dlp
import requests
import re
import os
import tempfile

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
#  USER-AGENTS
# ─────────────────────────────────────────
UA_CHROME = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)
UA_ANDROID_TT = (
    "com.zhiliaoapp.musically/2022600030 "
    "(Linux; U; Android 13; en_US; Pixel 7; "
    "Build/TQ3A.230901.001; Cronet/58.0.2991.0)"
)
UA_IPHONE = (
    "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/17.0 Mobile/15E148 Safari/604.1"
)

# ─────────────────────────────────────────
#  PER-PLATFORM YT-DLP OPTIONS
# ─────────────────────────────────────────
def build_opts(platform: str, skip_download: bool = True) -> dict:
    opts = {
        "quiet":              True,
        "no_warnings":        True,
        "skip_download":      skip_download,
        "socket_timeout":     30,
        "nocheckcertificate": True,
        "geo_bypass":         True,
        "age_limit":          99,
        "extractor_args":     {},
        "http_headers": {
            "User-Agent":      UA_CHROME,
            "Accept-Language": "en-US,en;q=0.9",
            "Accept":          "text/html,application/xhtml+xml,*/*;q=0.8",
        },
    }

    if platform == "youtube":
        # Android client bypasses most sign-in prompts and age gates
        opts["extractor_args"] = {
            "youtube": {
                "player_client": ["android", "web"],
                "player_skip":   ["webpage", "config"],
            }
        }

    elif platform == "tiktok":
        opts["http_headers"]["User-Agent"] = UA_ANDROID_TT
        opts["extractor_args"] = {
            "tiktok": {
                "api_hostname": "api22-normal-c-useast2a.tiktokv.com",
                "app_version":  "26.1.3",
            }
        }

    elif platform == "instagram":
        opts["http_headers"] = {
            "User-Agent":      UA_IPHONE,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.instagram.com/",
        }
        opts["extractor_args"] = {
            "instagram": {"include_feed_data": ["0"]}
        }

    elif platform == "facebook":
        opts["http_headers"] = {
            "User-Agent":      UA_CHROME,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://www.facebook.com/",
            "sec-fetch-site":  "same-origin",
        }

    elif platform == "twitter":
        opts["http_headers"] = {
            "User-Agent":      UA_CHROME,
            "Accept-Language": "en-US,en;q=0.9",
            "Referer":         "https://twitter.com/",
        }

    return opts

# ─────────────────────────────────────────
#  HELPERS
# ─────────────────────────────────────────
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


def parse_formats(info: dict) -> list:
    formats = info.get("formats", [])
    result, seen = [], set()

    for f in formats:
        vcodec = f.get("vcodec", "none")
        if vcodec in (None, "none"):
            continue
        height = f.get("height") or 0
        fid    = f.get("format_id", "")
        size   = f.get("filesize") or f.get("filesize_approx")
        acodec = f.get("acodec", "none")
        label  = f"{height}p" if height else fid
        if label in seen:
            continue
        seen.add(label)
        result.append({
            "format_id": fid,
            "label":     label,
            "ext":       f.get("ext", "mp4"),
            "height":    height,
            "filesize":  format_filesize(size),
            "has_audio": acodec not in (None, "none"),
        })

    result.sort(key=lambda x: x["height"], reverse=True)
    if not result:
        return [{"format_id": "best", "label": "Best Quality",
                 "ext": "mp4", "height": 0, "filesize": "N/A", "has_audio": True}]
    return result[:8]


def classify_error(raw: str) -> str:
    """Convert raw yt-dlp error to friendly Bahasa Indonesia message."""
    m = raw.lower()

    if any(k in m for k in ["sign in", "login", "log in", "private",
                              "not available", "members only",
                              "this video is private", "who can watch"]):
        return (
            "Video bersifat privat atau memerlukan login. "
            "Pastikan video bisa dibuka tanpa akun, lalu coba lagi."
        )
    if any(k in m for k in ["not available in your country", "geo", "blocked in"]):
        return "Video dibatasi secara geografis dan tidak dapat diakses dari server ini."
    if any(k in m for k in ["age", "18+", "adult content", "age-restricted"]):
        return "Video memiliki batasan usia (18+) dan memerlukan akun terverifikasi."
    if any(k in m for k in ["copyright", "removed", "deleted", "terminated",
                              "no longer available", "unavailable"]):
        return "Video telah dihapus, dikenai copyright, atau tidak tersedia lagi."
    if "unsupported url" in m:
        return (
            "URL tidak dikenali. Pastikan link mengarah langsung ke video, "
            "bukan ke profil atau beranda platform."
        )
    if any(k in m for k in ["429", "too many requests", "rate limit"]):
        return "Terlalu banyak permintaan ke platform. Tunggu 1–2 menit lalu coba lagi."
    if "404" in m:
        return "Video tidak ditemukan (404). Mungkin sudah dihapus atau link salah."
    if "403" in m:
        return "Akses ditolak platform (403). Video mungkin memerlukan autentikasi."

    return f"Gagal mengambil video: {raw[:220].strip()}"


# ─────────────────────────────────────────
#  ROUTES
# ─────────────────────────────────────────
@app.route("/")
def index():
    return HTML_PAGE


@app.route("/api/info", methods=["POST"])
def api_info():
    data     = request.get_json(silent=True) or {}
    url      = (data.get("url") or "").strip()

    if not url:
        return jsonify({"error": "URL tidak boleh kosong"}), 400

    platform = detect_platform(url)
    if platform == "unknown":
        return jsonify({
            "error": (
                "Platform tidak didukung. "
                "Gunakan link dari YouTube, TikTok, Instagram, Facebook, atau Twitter/X."
            )
        }), 400

    try:
        opts = build_opts(platform, skip_download=True)
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)

        if not info:
            return jsonify({"error": "Tidak dapat mengambil informasi video"}), 400

        thumbnails = info.get("thumbnails") or []
        thumb = ""
        if thumbnails:
            best  = max(thumbnails, key=lambda t: (t.get("width") or 0) * (t.get("height") or 0))
            thumb = best.get("url", "")
        if not thumb:
            thumb = info.get("thumbnail", "")

        return jsonify({
            "title":        info.get("title", "Video"),
            "duration":     format_duration(info.get("duration")),
            "thumbnail":    thumb,
            "uploader":     info.get("uploader") or info.get("channel") or "",
            "view_count":   info.get("view_count"),
            "platform":     platform,
            "formats":      parse_formats(info),
            "original_url": url,
        })

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": classify_error(str(e))}), 400
    except Exception as e:
        return jsonify({"error": f"Error tidak terduga: {str(e)[:200]}"}), 500


@app.route("/download", methods=["POST"])
def download():
    data      = request.get_json(silent=True) or {}
    url       = (data.get("url") or "").strip()
    format_id = (data.get("format_id") or "best").strip()
    platform  = detect_platform(url)

    if not url:
        return jsonify({"error": "URL tidak boleh kosong"}), 400

    if format_id and format_id != "best":
        fmt_str = f"{format_id}+bestaudio[ext=m4a]/bestvideo+bestaudio/best"
    else:
        fmt_str = "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio/best[ext=mp4]/best"

    tmp_dir = tempfile.mkdtemp()

    try:
        opts = build_opts(platform, skip_download=False)
        opts.update({
            "format":              fmt_str,
            "outtmpl":             os.path.join(tmp_dir, "%(title).80s.%(ext)s"),
            "socket_timeout":      60,
            "merge_output_format": "mp4",
            "postprocessors": [{
                "key":            "FFmpegVideoConvertor",
                "preferedformat": "mp4",
            }],
        })

        with yt_dlp.YoutubeDL(opts) as ydl:
            info     = ydl.extract_info(url, download=True)
            prepared = ydl.prepare_filename(info)

        # Find output file
        actual_file = None
        for fname in os.listdir(tmp_dir):
            fpath = os.path.join(tmp_dir, fname)
            if os.path.isfile(fpath):
                actual_file = fpath
                break

        if not actual_file:
            base = os.path.splitext(prepared)[0]
            for ext in (".mp4", ".mkv", ".webm", ".mov"):
                if os.path.exists(base + ext):
                    actual_file = base + ext
                    break

        if not actual_file or not os.path.exists(actual_file):
            return jsonify({"error": "File hasil download tidak ditemukan di server"}), 500

        safe_title = re.sub(r'[^\w\s\-]', '', info.get("title", "video"))[:80].strip() or "video"
        dl_name    = f"{safe_title}.mp4"
        filesize   = os.path.getsize(actual_file)

        def generate():
            with open(actual_file, "rb") as fh:
                while chunk := fh.read(512 * 1024):
                    yield chunk
            try:
                os.remove(actual_file)
                os.rmdir(tmp_dir)
            except Exception:
                pass

        return Response(
            stream_with_context(generate()),
            mimetype="video/mp4",
            headers={
                "Content-Disposition": f'attachment; filename="{dl_name}"',
                "Content-Length":      str(filesize),
                "X-Video-Title":       safe_title,
            },
        )

    except yt_dlp.utils.DownloadError as e:
        return jsonify({"error": classify_error(str(e))}), 400
    except Exception as e:
        return jsonify({"error": f"Server error: {str(e)[:300]}"}), 500


# ─────────────────────────────────────────
#  FRONTEND
# ─────────────────────────────────────────
HTML_PAGE = r"""<!DOCTYPE html>
<html lang="id">
<head>
<meta charset="UTF-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1.0"/>
<title>VortexDL — Video Downloader</title>
<link rel="preconnect" href="https://fonts.googleapis.com"/>
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400;600;700;800&family=DM+Sans:opsz,wght@9..40,300;9..40,400;9..40,500&display=swap" rel="stylesheet"/>
<style>
*,*::before,*::after{box-sizing:border-box;margin:0;padding:0}
:root{
  --bg0:#05060a;--bg1:#0c0d14;--bg2:#121420;--bg3:#1a1d2e;
  --glass:rgba(255,255,255,0.04);--glass-b:rgba(255,255,255,0.08);
  --accent:#7c6aff;--accent2:#ff6ac1;--accent3:#6affdb;
  --text:#eeeef4;--dim:#888ca8;--muted:#444660;
  --ok:#22d87a;--err:#ff4f6a;
  --r:16px;--rs:10px;--tr:.25s cubic-bezier(.4,0,.2,1);
}
html{scroll-behavior:smooth}
body{font-family:'DM Sans',sans-serif;background:var(--bg0);color:var(--text);min-height:100vh;overflow-x:hidden}
body::before{content:'';position:fixed;inset:0;background-image:url("data:image/svg+xml,%3Csvg viewBox='0 0 200 200' xmlns='http://www.w3.org/2000/svg'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.85' numOctaves='4'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)' opacity='0.03'/%3E%3C/svg%3E");pointer-events:none;z-index:0}
.blob{position:fixed;border-radius:50%;filter:blur(110px);pointer-events:none;z-index:0;animation:drift 18s ease-in-out infinite}
.b1{width:550px;height:550px;background:radial-gradient(circle,rgba(124,106,255,.2),transparent 70%);top:-180px;left:-180px}
.b2{width:450px;height:450px;background:radial-gradient(circle,rgba(255,106,193,.15),transparent 70%);top:40%;right:-130px;animation-delay:-6s}
.b3{width:380px;height:380px;background:radial-gradient(circle,rgba(106,255,219,.11),transparent 70%);bottom:-80px;left:28%;animation-delay:-12s}
@keyframes drift{0%,100%{transform:translate(0,0)}33%{transform:translate(35px,-25px)}66%{transform:translate(-18px,45px)}}
.wrap{position:relative;z-index:1;max-width:740px;margin:0 auto;padding:36px 20px 80px}
/* Header */
header{text-align:center;margin-bottom:44px;padding-top:12px}
.logo{font-family:'Syne',sans-serif;font-weight:800;font-size:clamp(2rem,6vw,3.2rem);background:linear-gradient(135deg,var(--accent) 0%,var(--accent2) 50%,var(--accent3) 100%);-webkit-background-clip:text;-webkit-text-fill-color:transparent;background-clip:text;letter-spacing:-.03em}
.logo em{-webkit-text-fill-color:var(--dim);font-style:normal;font-weight:400;font-size:.44em;letter-spacing:.05em}
.tagline{margin-top:9px;color:var(--dim);font-size:.91rem;font-weight:300}
.platforms{display:flex;justify-content:center;flex-wrap:wrap;gap:8px;margin-top:16px}
.badge{display:flex;align-items:center;gap:5px;padding:4px 11px;border-radius:100px;background:var(--glass);border:1px solid var(--glass-b);font-size:.74rem;font-weight:500;color:var(--dim);transition:var(--tr)}
.badge:hover{border-color:var(--accent);color:var(--accent)}
.dot{width:6px;height:6px;border-radius:50%}
/* Card */
.card{background:var(--glass);border:1px solid var(--glass-b);border-radius:var(--r);padding:26px;backdrop-filter:blur(24px);-webkit-backdrop-filter:blur(24px);transition:border-color var(--tr)}
.card:focus-within{border-color:rgba(124,106,255,.3)}
/* Input */
.input-row{display:flex;gap:10px;flex-wrap:wrap}
.url-in{flex:1;min-width:190px;background:rgba(255,255,255,.05);border:1.5px solid var(--glass-b);border-radius:var(--rs);color:var(--text);font-family:'DM Sans',sans-serif;font-size:.93rem;padding:12px 15px;outline:none;transition:border-color var(--tr),box-shadow var(--tr)}
.url-in::placeholder{color:var(--muted)}
.url-in:focus{border-color:var(--accent);box-shadow:0 0 0 3px rgba(124,106,255,.13)}
/* Buttons */
.btn{display:inline-flex;align-items:center;justify-content:center;gap:7px;font-family:'DM Sans',sans-serif;font-weight:500;font-size:.88rem;border:none;cursor:pointer;border-radius:var(--rs);padding:12px 18px;transition:var(--tr);white-space:nowrap}
.primary{background:linear-gradient(135deg,var(--accent),#9e6aff);color:#fff;box-shadow:0 4px 18px rgba(124,106,255,.33)}
.primary:hover{transform:translateY(-1px);box-shadow:0 6px 26px rgba(124,106,255,.48)}
.primary:active,.primary:disabled{transform:none;opacity:.55;cursor:not-allowed}
.success{background:linear-gradient(135deg,var(--ok),#1aad62);color:#fff;width:100%;box-shadow:0 4px 18px rgba(34,216,122,.22);font-size:.95rem;font-weight:600;padding:14px}
.success:hover{transform:translateY(-1px);box-shadow:0 6px 26px rgba(34,216,122,.38)}
.success:active,.success:disabled{transform:none;opacity:.55;cursor:not-allowed}
.ghost{background:var(--glass);border:1px solid var(--glass-b);color:var(--dim);font-size:.78rem;padding:6px 11px}
.ghost:hover{border-color:var(--accent3);color:var(--accent3)}
@keyframes spin{to{transform:rotate(360deg)}}
.spin{width:16px;height:16px;border-radius:50%;border:2.5px solid rgba(255,255,255,.2);border-top-color:#fff;animation:spin .7s linear infinite;display:inline-block;flex-shrink:0}
/* Platform tag */
.ptag{display:none;align-items:center;gap:7px;margin-top:11px;font-size:.8rem;color:var(--accent3);font-weight:500}
.ptag.on{display:flex}
/* Error box */
.ebox{background:rgba(255,79,106,.07);border:1px solid rgba(255,79,106,.22);border-radius:var(--rs);padding:14px 16px;margin-top:15px;display:none;animation:su .3s ease both}
.ebox.on{display:block}
.etitle{font-size:.79rem;font-weight:700;color:var(--err);margin-bottom:6px}
.emsg{font-size:.83rem;color:#ffb3bf;line-height:1.55}
.etips{margin-top:10px;padding-top:10px;border-top:1px solid rgba(255,79,106,.15)}
.etips b{font-size:.77rem;color:var(--dim)}
.etips ul{padding-left:16px;font-size:.77rem;color:var(--dim);line-height:1.75;margin-top:4px}
/* Divider */
hr{border:none;border-top:1px solid var(--glass-b);margin:22px 0}
/* Info section */
#info{display:none;margin-top:20px}
#info.on{display:block;animation:su .4s cubic-bezier(.4,0,.2,1) both}
@keyframes su{from{opacity:0;transform:translateY(16px)}to{opacity:1;transform:translateY(0)}}
/* Video meta */
.vmeta{display:grid;grid-template-columns:148px 1fr;background:rgba(255,255,255,.03);border:1px solid var(--glass-b);border-radius:var(--rs);overflow:hidden}
.tbox{position:relative;width:148px;min-height:108px;background:var(--bg3);cursor:pointer;overflow:hidden}
.tbox img{width:100%;height:100%;object-fit:cover;display:block;transition:transform .35s}
.tbox:hover img{transform:scale(1.06)}
.povr{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;background:rgba(0,0,0,.42);opacity:0;transition:opacity var(--tr)}
.tbox:hover .povr{opacity:1}
.pico{width:40px;height:40px;border-radius:50%;background:rgba(255,255,255,.92);display:flex;align-items:center;justify-content:center}
.mbody{padding:15px;display:flex;flex-direction:column;gap:7px;min-width:0}
.mtitle{font-family:'Syne',sans-serif;font-weight:600;font-size:.95rem;line-height:1.4;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical;overflow:hidden}
.mrow{display:flex;flex-wrap:wrap;gap:7px;margin-top:auto}
.chip{display:flex;align-items:center;gap:4px;font-size:.73rem;color:var(--dim);background:rgba(255,255,255,.05);padding:3px 8px;border-radius:100px}
/* Formats */
.fsec{margin-top:16px}
.flabel{font-size:.79rem;color:var(--dim);font-weight:500;margin-bottom:7px}
.fgrid{display:flex;flex-wrap:wrap;gap:6px}
.fbtn{background:rgba(255,255,255,.05);border:1.5px solid var(--glass-b);border-radius:7px;color:var(--dim);font-family:'DM Sans',sans-serif;font-size:.77rem;padding:5px 12px;cursor:pointer;transition:var(--tr)}
.fbtn:hover{border-color:var(--accent);color:var(--accent)}
.fbtn.sel{background:rgba(124,106,255,.14);border-color:var(--accent);color:var(--accent);font-weight:600}
/* Download area */
.dla{margin-top:18px}
.pbar-wrap{height:3px;background:rgba(255,255,255,.07);border-radius:100px;overflow:hidden;margin-bottom:12px;display:none}
.pbar{height:100%;background:linear-gradient(90deg,var(--accent),var(--accent3));border-radius:100px;width:0;transition:width .3s}
/* Modal */
.movr{display:none;position:fixed;inset:0;z-index:100;background:rgba(0,0,0,.86);backdrop-filter:blur(12px);align-items:center;justify-content:center;padding:20px}
.movr.on{display:flex;animation:fi .2s ease}
@keyframes fi{from{opacity:0}to{opacity:1}}
.min{max-width:700px;width:100%;background:var(--bg2);border:1px solid var(--glass-b);border-radius:var(--r);overflow:hidden;animation:pi .28s cubic-bezier(.34,1.56,.64,1) both}
@keyframes pi{from{opacity:0;transform:scale(.88)}to{opacity:1;transform:scale(1)}}
.mhd{display:flex;align-items:center;justify-content:space-between;padding:13px 17px;border-bottom:1px solid var(--glass-b)}
.mhd-t{font-family:'Syne',sans-serif;font-weight:600;font-size:.9rem}
.mclose{background:none;border:none;color:var(--dim);cursor:pointer;font-size:1.25rem;line-height:1;transition:color var(--tr)}
.mclose:hover{color:var(--text)}
#pvideo{width:100%;max-height:68vh;background:#000}
/* History */
#hist{margin-top:42px;display:none}
#hist.on{display:block}
.shed{display:flex;align-items:center;justify-content:space-between;margin-bottom:13px}
.shtitle{font-family:'Syne',sans-serif;font-weight:700;font-size:.98rem}
.hlist{display:flex;flex-direction:column;gap:7px}
.hi{display:flex;align-items:center;gap:11px;background:var(--glass);border:1px solid var(--glass-b);border-radius:var(--rs);padding:9px 11px;transition:border-color var(--tr)}
.hi:hover{border-color:rgba(124,106,255,.22)}
.hthumb{width:50px;height:34px;border-radius:5px;object-fit:cover;flex-shrink:0;background:var(--bg3)}
.htitle{flex:1;font-size:.81rem;overflow:hidden;white-space:nowrap;text-overflow:ellipsis}
.hplat{font-size:.69rem;color:var(--dim);background:rgba(255,255,255,.06);padding:2px 7px;border-radius:100px;flex-shrink:0}
.htime{font-size:.69rem;color:var(--muted);flex-shrink:0}
/* Toast */
.toasts{position:fixed;bottom:22px;right:22px;z-index:999;display:flex;flex-direction:column;gap:8px}
.toast{padding:11px 15px;border-radius:var(--rs);font-size:.83rem;font-weight:500;max-width:320px;display:flex;align-items:flex-start;gap:8px;box-shadow:0 8px 26px rgba(0,0,0,.5);animation:tin .3s cubic-bezier(.34,1.56,.64,1) both}
@keyframes tin{from{opacity:0;transform:translateX(38px)}to{opacity:1;transform:translateX(0)}}
.t-ok{background:#0d2b1e;border:1px solid #22d87a44;color:#22d87a}
.t-err{background:#2b0d14;border:1px solid #ff4f6a44;color:#ff4f6a}
.t-inf{background:#151128;border:1px solid #7c6aff44;color:#a89aff}
footer{text-align:center;color:var(--muted);font-size:.74rem;padding-top:42px}
footer a{color:var(--dim);text-decoration:none}
@media(max-width:500px){
  .wrap{padding:18px 13px 56px}
  .card{padding:15px}
  .vmeta{grid-template-columns:1fr}
  .tbox{width:100%;height:165px}
  .mbody{padding:12px}
}
</style>
</head>
<body>
<div class="blob b1"></div>
<div class="blob b2"></div>
<div class="blob b3"></div>
<div class="wrap">

<header>
  <div class="logo">Vortex<em>DL</em></div>
  <p class="tagline">Download video dari semua platform, cepat &amp; tanpa batas</p>
  <div class="platforms">
    <div class="badge"><span class="dot" style="background:#ff0000"></span>YouTube</div>
    <div class="badge"><span class="dot" style="background:#ff0050"></span>TikTok</div>
    <div class="badge"><span class="dot" style="background:#e1306c"></span>Instagram</div>
    <div class="badge"><span class="dot" style="background:#1877f2"></span>Facebook</div>
    <div class="badge"><span class="dot" style="background:#1da1f2"></span>Twitter/X</div>
  </div>
</header>

<div class="card">
  <div class="input-row">
    <input id="url-in" class="url-in" type="url"
      placeholder="Paste URL video di sini… (YouTube, TikTok, IG, FB, X)"
      autocomplete="off" spellcheck="false"/>
    <button class="btn primary" id="fbtn" onclick="fetchInfo()">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg>
      Cari
    </button>
  </div>

  <div class="ptag" id="ptag"><span>⚡</span><span id="pname">—</span></div>

  <!-- Error box -->
  <div class="ebox" id="ebox">
    <div class="etitle">⚠️ Gagal Mengambil Video</div>
    <div class="emsg" id="emsg"></div>
    <div class="etips">
      <b>💡 Tips untuk mengatasi:</b>
      <ul id="etips-list"></ul>
    </div>
  </div>

  <!-- Info section -->
  <div id="info">
    <hr/>
    <div class="vmeta">
      <div class="tbox" onclick="openPreview()">
        <img id="thumb" src="" alt=""/>
        <div class="povr"><div class="pico">
          <svg width="13" height="13" viewBox="0 0 24 24" fill="#05060a"><polygon points="5 3 19 12 5 21 5 3"/></svg>
        </div></div>
      </div>
      <div class="mbody">
        <div class="mtitle" id="mtitle">—</div>
        <div class="mrow">
          <div class="chip" id="mdur"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="10"/><polyline points="12 6 12 12 16 14"/></svg><span>—</span></div>
          <div class="chip" id="mup"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><path d="M20 21v-2a4 4 0 0 0-4-4H8a4 4 0 0 0-4 4v2"/><circle cx="12" cy="7" r="4"/></svg><span>—</span></div>
          <div class="chip" id="mplat"><svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2"/></svg><span>—</span></div>
        </div>
      </div>
    </div>

    <div class="fsec">
      <div class="flabel">Pilih kualitas:</div>
      <div class="fgrid" id="fgrid"></div>
    </div>

    <div class="dla">
      <div class="pbar-wrap" id="pwrap"><div class="pbar" id="pbar"></div></div>
      <button class="btn success" id="dlbtn" onclick="startDownload()">
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg>
        Download Video (MP4)
      </button>
    </div>
  </div>
</div>

<!-- History -->
<div id="hist">
  <hr style="margin-top:36px"/>
  <div class="shed">
    <div class="shtitle">📋 Riwayat Download</div>
    <button class="btn ghost" onclick="clearHist()">Hapus semua</button>
  </div>
  <div class="hlist" id="hlist"></div>
</div>

<footer>
  VortexDL — Untuk penggunaan pribadi. Hormati hak cipta konten. |
  Powered by <a href="https://github.com/yt-dlp/yt-dlp" target="_blank">yt-dlp</a>
</footer>
</div>

<!-- Preview Modal -->
<div class="movr" id="modal" onclick="closeModal(event)">
  <div class="min">
    <div class="mhd">
      <span class="mhd-t" id="mtitle2">Preview</span>
      <button class="mclose" onclick="closeModalDirect()">✕</button>
    </div>
    <video id="pvideo" controls playsinline></video>
  </div>
</div>

<div class="toasts" id="toasts"></div>

<script>
let info = null, selFmt = null;
let hist = JSON.parse(sessionStorage.getItem('vdl_h') || '[]');

const PL = {
  youtube:'▶ YouTube', tiktok:'♪ TikTok',
  instagram:'◈ Instagram', facebook:'ƒ Facebook',
  twitter:'✦ Twitter/X'
};

const TIPS = {
  login:   ['Pastikan video bersifat publik (bukan Privat atau "Hanya Teman")',
            'Buka link di browser tanpa login — jika tidak bisa terbuka, video memang privat',
            'Untuk Instagram Reels: salin link dari ikon Share → Salin Tautan',
            'Untuk TikTok: hanya video publik yang bisa diunduh'],
  geo:     ['Video dibatasi geografis — server tidak bisa mengaksesnya',
            'Coba platform lain atau video dari kreator yang tidak membatasi wilayah'],
  age:     ['Video memerlukan verifikasi usia akun',
            'Tidak dapat diakses tanpa akun yang sudah diverifikasi umur'],
  removed: ['Video kemungkinan sudah dihapus pemilik atau dikenai copyright',
            'Coba buka link di browser — jika "Video Unavailable", video memang sudah hilang'],
  url:     ['Pastikan link mengarah langsung ke video, bukan ke profil atau beranda',
            'YouTube: gunakan youtube.com/watch?v=... atau youtu.be/...',
            'TikTok: Share video → Copy Link (bukan salin alamat bar)',
            'Instagram: link harus ke postingan/Reels, bukan ke profil (@username)',
            'Facebook: link video publik (bukan video dari grup privat)'],
  rate:    ['Server terlalu banyak permintaan ke platform ini', 'Tunggu 1–2 menit lalu coba lagi'],
  generic: ['Pastikan URL bisa dibuka di browser terlebih dahulu',
            'Coba update yt-dlp: pip install -U yt-dlp',
            'Beberapa video memang tidak dapat didownload karena kebijakan platform']
};

function getCategory(msg) {
  const m = msg.toLowerCase();
  if (/privat|private|login|sign in|members only|who can watch/.test(m)) return 'login';
  if (/geo|negara|country|blocked in/.test(m)) return 'geo';
  if (/usia|age|18\+|adult/.test(m)) return 'age';
  if (/dihapus|removed|deleted|unavailable|no longer/.test(m)) return 'removed';
  if (/url|dikenali|unsupported/.test(m)) return 'url';
  if (/rate limit|429|terlalu banyak/.test(m)) return 'rate';
  return 'generic';
}

function toast(msg, type='info', ms=4200) {
  const w = document.getElementById('toasts');
  const el = document.createElement('div');
  el.className = `toast t-${type}`;
  el.textContent = (type==='ok'?'✅ ':type==='err'?'❌ ':'💬 ') + msg;
  w.appendChild(el);
  setTimeout(()=>{el.style.cssText='opacity:0;transform:translateX(38px);transition:.35s';
    setTimeout(()=>el.remove(),370)},ms);
}

function showErr(msg) {
  document.getElementById('emsg').textContent = msg;
  const cat  = getCategory(msg);
  const tips = TIPS[cat] || TIPS.generic;
  document.getElementById('etips-list').innerHTML = tips.map(t=>`<li>${t}</li>`).join('');
  document.getElementById('ebox').classList.add('on');
  document.getElementById('info').classList.remove('on');
}
function hideErr() { document.getElementById('ebox').classList.remove('on'); }

function detectPlatform(u) {
  if (/youtube\.com|youtu\.be/i.test(u))       return 'youtube';
  if (/tiktok\.com|vm\.tiktok\.com/i.test(u))  return 'tiktok';
  if (/instagram\.com/i.test(u))               return 'instagram';
  if (/facebook\.com|fb\.watch/i.test(u))      return 'facebook';
  if (/twitter\.com|x\.com/i.test(u))          return 'twitter';
  return 'unknown';
}

document.getElementById('url-in').addEventListener('input', function() {
  hideErr();
  const p = detectPlatform(this.value.trim());
  const t = document.getElementById('ptag');
  if (p !== 'unknown') {
    document.getElementById('pname').textContent = PL[p];
    t.classList.add('on');
  } else t.classList.remove('on');
});
document.getElementById('url-in').addEventListener('keydown', e => {
  if (e.key === 'Enter') fetchInfo();
});

async function fetchInfo() {
  const url = document.getElementById('url-in').value.trim();
  if (!url) { toast('Masukkan URL video dulu', 'err'); return; }

  const btn = document.getElementById('fbtn');
  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Mengambil…';
  hideErr();
  document.getElementById('info').classList.remove('on');

  try {
    const res  = await fetch('/api/info', {method:'POST',
      headers:{'Content-Type':'application/json'}, body:JSON.stringify({url})});
    const data = await res.json();
    if (!res.ok || data.error) { showErr(data.error || 'Gagal mengambil info video'); return; }

    info    = data;
    selFmt  = data.formats?.[0]?.format_id || 'best';
    renderInfo(data);
    document.getElementById('info').classList.add('on');
  } catch(e) {
    showErr('Koneksi gagal. Periksa koneksi internet kamu.');
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><circle cx="11" cy="11" r="8"/><line x1="21" y1="21" x2="16.65" y2="16.65"/></svg> Cari';
  }
}

function renderInfo(d) {
  const img = document.getElementById('thumb');
  img.src = d.thumbnail || '';
  img.onerror = () => { img.src = ''; img.style.background = 'var(--bg3)'; };
  document.getElementById('mtitle').textContent  = d.title || 'Tanpa Judul';
  document.getElementById('mtitle2').textContent = d.title || 'Preview';
  document.getElementById('mdur').querySelector('span').textContent   = d.duration || 'N/A';
  document.getElementById('mup').querySelector('span').textContent    = d.uploader || 'N/A';
  document.getElementById('mplat').querySelector('span').textContent  = PL[d.platform] || d.platform;

  const grid = document.getElementById('fgrid');
  grid.innerHTML = '';
  (d.formats || []).forEach((f, i) => {
    const b = document.createElement('button');
    b.className = 'fbtn' + (i === 0 ? ' sel' : '');
    b.dataset.fid = f.format_id;
    const au = f.has_audio ? '🔊' : '🔇';
    b.textContent = `${f.label} ${au}${f.filesize !== 'N/A' ? ' · ' + f.filesize : ''}`.trim();
    b.onclick = () => {
      document.querySelectorAll('.fbtn').forEach(x => x.classList.remove('sel'));
      b.classList.add('sel');
      selFmt = f.format_id;
    };
    grid.appendChild(b);
  });
}

async function startDownload() {
  if (!info) { toast('Cari video dulu', 'err'); return; }

  const btn  = document.getElementById('dlbtn');
  const pw   = document.getElementById('pwrap');
  const pb   = document.getElementById('pbar');

  btn.disabled = true;
  btn.innerHTML = '<span class="spin"></span> Memproses & Mengunduh…';
  pw.style.display = 'block';
  pb.style.width = '5%';

  let pct = 5;
  const pi = setInterval(() => {
    if (pct < 80) { pct += Math.random() * 5.5; pb.style.width = Math.min(pct, 80) + '%'; }
  }, 550);

  try {
    const res = await fetch('/download', {method:'POST',
      headers:{'Content-Type':'application/json'},
      body:JSON.stringify({url:info.original_url, format_id:selFmt||'best'})});

    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      showErr(err.error || 'Download gagal');
      return;
    }

    clearInterval(pi);
    pb.style.width = '100%';

    const blob = await res.blob();
    const name = res.headers.get('Content-Disposition')?.match(/filename="(.+)"/)?.[1] || 'video.mp4';
    const a = document.createElement('a');
    a.href = URL.createObjectURL(blob); a.download = name;
    document.body.appendChild(a); a.click(); document.body.removeChild(a);
    setTimeout(() => URL.revokeObjectURL(a.href), 30000);

    toast('Download berhasil! 🎉', 'ok');
    addHist(info);
  } catch(e) {
    showErr('Download gagal: ' + e.message);
    clearInterval(pi);
  } finally {
    btn.disabled = false;
    btn.innerHTML = '<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2.5"><path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/></svg> Download Video (MP4)';
    setTimeout(() => { pw.style.display = 'none'; pb.style.width = '0'; }, 1400);
  }
}

function openPreview() {
  if (!info?.original_url) return;
  document.getElementById('pvideo').src = info.original_url;
  document.getElementById('modal').classList.add('on');
}
function closeModalDirect() {
  const v = document.getElementById('pvideo');
  v.pause(); v.src = '';
  document.getElementById('modal').classList.remove('on');
}
function closeModal(e) { if (e.target.id === 'modal') closeModalDirect(); }
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModalDirect(); });

function addHist(d) {
  hist.unshift({title:d.title, thumbnail:d.thumbnail, platform:d.platform,
    url:d.original_url, time:new Date().toLocaleTimeString('id-ID',{hour:'2-digit',minute:'2-digit'})});
  if (hist.length > 20) hist.pop();
  sessionStorage.setItem('vdl_h', JSON.stringify(hist));
  renderHist();
}
function renderHist() {
  const sec = document.getElementById('hist');
  const ul  = document.getElementById('hlist');
  if (!hist.length) { sec.classList.remove('on'); return; }
  sec.classList.add('on');
  ul.innerHTML = hist.map(h => `
    <div class="hi">
      <img class="hthumb" src="${e(h.thumbnail)}" alt="" onerror="this.src='';this.style.background='var(--bg3)'">
      <div class="htitle" title="${e(h.title)}">${e(h.title)}</div>
      <div class="hplat">${PL[h.platform]||h.platform}</div>
      <div class="htime">${h.time}</div>
    </div>`).join('');
}
function clearHist() {
  hist = []; sessionStorage.removeItem('vdl_h'); renderHist();
  toast('Riwayat dihapus', 'inf', 2000);
}
const e = s => (s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');
renderHist();
</script>
</body>
</html>"""


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
