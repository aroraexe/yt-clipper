import os
import subprocess
import threading
import uuid
import re
import shutil
import requests
import random
import queue
import time
import concurrent.futures
from pathlib import Path
from flask import Flask, request, jsonify, send_from_directory

import yt_dlp

app = Flask(__name__)

DOWNLOAD_DIR = Path(__file__).parent.parent / "downloads"
DOWNLOAD_DIR.mkdir(exist_ok=True)
downloads = {}

import json
with open(Path(__file__).parent / 'styles.json', encoding='utf-8') as f:
    SUBTITLE_STYLES = json.load(f)
with open(Path(__file__).parent / 'animations.json', encoding='utf-8') as f:
    SUBTITLE_ANIMATIONS = json.load(f)
# ── Subtitle helpers ──────────────────────────────────────────────────────────

from subtitle_engine import find_subtitle_file, burn_subtitles, _fmt_bytes, sanitize_filename

def get_cookie_file():
    render_secret = Path("/etc/secrets/cookies.txt")
    if render_secret.exists():
        tmp_cookie = Path("/tmp/youtube_cookies.txt")
        shutil.copy2(render_secret, tmp_cookie)
        return str(tmp_cookie)
    local_cookie = Path(__file__).parent / "cookies.txt"
    if local_cookie.exists():
        return str(local_cookie)
    return None

def get_free_proxies():
    try:
        res = requests.get('https://raw.githubusercontent.com/TheSpeedX/PROXY-List/master/http.txt', timeout=5)
        proxies = res.text.strip().split('\n')
        proxies = [f"http://{p.strip()}" for p in proxies if p.strip()]
        random.shuffle(proxies)
        return proxies
    except Exception as e:
        print(f"Failed to fetch proxies: {e}")
        return []

WORKING_PROXIES = queue.Queue(maxsize=10)

class DummyLogger:
    def debug(self, msg): pass
    def warning(self, msg): pass
    def error(self, msg): pass

def test_proxy(proxy):
    opts = {
        'proxy': proxy,
        'quiet': True,
        'no_warnings': True,
        'logger': DummyLogger(),
        'extractor_args': {'youtube': ['player_client=ios,android']}
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            # lightweight check without downloading
            ydl.extract_info('https://youtu.be/xeF7VUGTu6M', download=False)
        return proxy
    except Exception:
        return None

def maintain_proxy_pool():
    while True:
        if WORKING_PROXIES.qsize() < 5:
            proxies = get_free_proxies()[:30]
            if proxies:
                with concurrent.futures.ThreadPoolExecutor(max_workers=10) as executor:
                    futures = {executor.submit(test_proxy, p): p for p in proxies}
                    for future in concurrent.futures.as_completed(futures):
                        res = future.result()
                        if res:
                            try:
                                WORKING_PROXIES.put(res, block=False)
                            except queue.Full:
                                pass
        time.sleep(10)

# Start background worker
threading.Thread(target=maintain_proxy_pool, daemon=True).start()

def run_download(dl_id: str, url: str, quality: str,
                 burn_subs: bool, sub_lang: str, sub_style: str, sub_anim: str, sub_color: str = "", aspect_ratio: str = "original", word_by_word: bool = False):
    record = downloads[dl_id]

    def progress_hook(d):
        if d["status"] == "downloading":
            total = d.get("total_bytes") or d.get("total_bytes_estimate", 0)
            downloaded = d.get("downloaded_bytes", 0)
            speed = d.get("speed") or 0
            eta   = d.get("eta") or 0
            pct   = (downloaded / total * 100) if total else 0
            record.update({
                "status":       "downloading",
                "status_label": "Downloading video…",
                "progress":     round(min(pct, 99), 1),
                "speed":        _fmt_bytes(speed) + "/s",
                "eta":          f"{eta}s" if eta else "",
            })
        elif d["status"] == "finished":
            record.update({"status": "processing", "status_label": "Merging audio/video…", "progress": 95})

    fmt_map = {
        "1080": "bestvideo[height<=1080]+bestaudio/bestvideo+bestaudio/best",
        "720": "bestvideo[height<=720]+bestaudio/bestvideo+bestaudio/best",
        "480": "bestvideo[height<=480]+bestaudio/bestvideo+bestaudio/best",
        "best": "bestvideo+bestaudio/best"
    }

    ydl_opts = {
        "format": fmt_map.get(quality, fmt_map["best"]),
        "outtmpl": str(DOWNLOAD_DIR / "%(title)s.%(ext)s"),
        "merge_output_format": "mp4",
        "progress_hooks": [progress_hook],
        "quiet": True, "no_warnings": True,
        "overwrites": True,
        "postprocessors": [{"key": "FFmpegVideoConvertor", "preferedformat": "mp4"}],
        "extractor_args": {"youtube": ["player_client=ios,android"]},
    }
    # We explicitly DO NOT load cookies here. 
    # Burned cookies cause the bot detection error. 
    # By staying anonymous and using the ios/android player clients, we bypass the block.
    if burn_subs:
        ydl_opts.update({
            "writesubtitles": True,
            "writeautomaticsub": True,
            "subtitleslangs": [sub_lang, "en"],
            "subtitlesformat": "vtt/srt/best",
        })
    premium_proxy = os.environ.get("PROXY_URL")
    
    success = False
    last_error = None
    
    # Try up to 5 times
    for attempt in range(5):
        if premium_proxy:
            proxy = premium_proxy
        else:
            try:
                # wait up to 30s for a working proxy
                record.update({"status": "processing", "status_label": "Waiting for a clean proxy...", "progress": 5})
                proxy = WORKING_PROXIES.get(timeout=30)
            except queue.Empty:
                record.update({"status": "error", "error": "Proxy pool is empty. Please try again later."})
                return

        if proxy:
            ydl_opts['proxy'] = proxy
            proxy_type = "Premium" if proxy == premium_proxy else "Free"
            record.update({"status": "processing", "status_label": f"Using {proxy_type} proxy...", "progress": 10})
        else:
            if 'proxy' in ydl_opts:
                del ydl_opts['proxy']
            record.update({"status": "processing", "status_label": "Starting download...", "progress": 5})

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)
                title      = info.get("title", "video")
                ext        = info.get("ext", "mp4")
                safe_title = sanitize_filename(title)
                video_file = DOWNLOAD_DIR / f"{safe_title}.{ext}"

                if not video_file.exists():
                    mp4s = sorted(DOWNLOAD_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True)
                    video_file = mp4s[0] if mp4s else video_file

                record.update({
                    "title":     title,
                    "thumbnail": info.get("thumbnail", ""),
                    "duration":  info.get("duration", 0),
                })

            # ── Subtitle burn ─────────────────────────────────────────────────
            if burn_subs:
                record.update({"status": "finding_subs", "status_label": "Looking for subtitle file…"})
                safe_title = sanitize_filename(record["title"])
                sub_file = find_subtitle_file(safe_title, sub_lang)

                if sub_file and sub_file.exists():
                    final_file = burn_subtitles(video_file, sub_file, sub_style, sub_anim, sub_color, record, aspect_ratio, word_by_word)

                    # Swap original with burned version
                    video_file.unlink(missing_ok=True)
                    final_file.rename(video_file)

                    # Cleanup leftover subtitle files
                    for f in DOWNLOAD_DIR.glob(f"{safe_title}*.srt"):
                        f.unlink(missing_ok=True)
                    for f in DOWNLOAD_DIR.glob(f"{safe_title}*.vtt"):
                        f.unlink(missing_ok=True)
                    for f in DOWNLOAD_DIR.glob(f"{safe_title}*.ass"):
                        f.unlink(missing_ok=True)

                    record["subs_burned"] = True
                    record["sub_style_label"] = SUBTITLE_STYLES.get(sub_style, {}).get("label", sub_style)
                else:
                    record.update({
                        "subs_burned": False,
                        "sub_warn":    f"No '{sub_lang}' subtitles available for this video.",
                    })
                
            record.update({"status": "done", "progress": 100, "filename": video_file.name})
            
            success = True
            break
        except yt_dlp.utils.DownloadError as e:
            print(f"yt-dlp error with proxy {proxy}: {e}")
            last_error = e
            continue
            
    if not success:
        record.update({"status": "error", "error": f"Failed after trying multiple proxies: {last_error}"})


# ── Routes ────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    with open(Path(__file__).parent / "templates" / "index.html", encoding="utf-8") as f:
        return f.read()


@app.route("/api/styles")
def get_styles():
    return jsonify([
        {"key": k, "label": v["label"], "emoji": v["emoji"], "desc": v["desc"]}
        for k, v in SUBTITLE_STYLES.items()
    ])

@app.route("/api/animations")
def get_animations():
    return jsonify([
        {"key": k, "label": v["label"], "emoji": v["emoji"], "desc": v["desc"]}
        for k, v in SUBTITLE_ANIMATIONS.items()
    ])


@app.route("/api/info", methods=["POST"])
def get_info():
    data = request.get_json()
    url  = (data or {}).get("url", "").strip()
    if not url:
        return jsonify({"error": "No URL provided"}), 400
    try:
        ydl_opts = {"quiet": True, "no_warnings": True}
        cookie_file = get_cookie_file()
        if cookie_file:
            ydl_opts["cookiefile"] = cookie_file
            
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info     = ydl.extract_info(url, download=False)
            subs     = info.get("subtitles", {})
            auto_sub = info.get("automatic_captions", {})
            return jsonify({
                "title":           info.get("title", ""),
                "thumbnail":       info.get("thumbnail", ""),
                "duration":        info.get("duration", 0),
                "uploader":        info.get("uploader", ""),
                "view_count":      info.get("view_count", 0),
                "sub_langs":       sorted(set(list(subs) + list(auto_sub))),
                "has_manual_subs": bool(subs),
                "has_auto_subs":   bool(auto_sub),
            })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/api/download", methods=["POST"])
def start_download():
    data      = request.get_json() or {}
    url       = data.get("url", "").strip()
    quality   = data.get("quality", "720")
    burn_subs = bool(data.get("burn_subs", False))
    sub_lang  = data.get("sub_lang", "en").strip() or "en"
    sub_style = data.get("sub_style", "classic")
    sub_anim  = data.get("sub_anim", "normal")
    sub_color = data.get("sub_color", "").strip()
    aspect_ratio = data.get("aspect_ratio", "original")
    word_by_word = bool(data.get("word_by_word", False))

    if not url:
        return jsonify({"error": "No URL provided"}), 400

    dl_id = str(uuid.uuid4())[:8]
    downloads[dl_id] = {
        "id": dl_id, "url": url,
        "status": "starting", "status_label": "Starting…",
        "progress": 0, "title": "", "filename": "", "thumbnail": "",
        "speed": "", "eta": "", "error": "",
        "burn_subs": burn_subs, "sub_lang": sub_lang, "sub_style": sub_style, "sub_anim": sub_anim, "sub_color": sub_color,
        "subs_burned": False, "sub_warn": "", "sub_style_label": "",
    }

    threading.Thread(
        target=run_download,
        args=(dl_id, url, quality, burn_subs, sub_lang, sub_style, sub_anim, sub_color, aspect_ratio, word_by_word),
        daemon=True,
    ).start()

    return jsonify({"id": dl_id})


@app.route("/api/status/<dl_id>")
def get_status(dl_id):
    r = downloads.get(dl_id)
    return jsonify(r) if r else (jsonify({"error": "Not found"}), 404)


@app.route("/api/files")
def list_files():
    files = [
        {"name": f.name, "size": _fmt_bytes(f.stat().st_size)}
        for f in sorted(DOWNLOAD_DIR.glob("*.mp4"), key=os.path.getmtime, reverse=True)
    ]
    return jsonify(files)


@app.route("/downloads/<path:filename>")
def serve_file(filename):
    return send_from_directory(DOWNLOAD_DIR, filename, as_attachment=True)


@app.route("/api/open-folder", methods=["POST"])
def open_folder():
    if hasattr(os, 'startfile'):
        os.startfile(str(DOWNLOAD_DIR))
        return jsonify({"ok": True})
    else:
        return jsonify({"error": "Opening folders is only supported on local Windows machines."}), 400


if __name__ == "__main__":
    import sys
    sys.stdout.reconfigure(encoding="utf-8")
    print(f"\n  Shorts Downloader  →  http://localhost:5000")
    print(f"  Downloads folder   →  {DOWNLOAD_DIR}\n")
    print(f"  Subtitle styles loaded: {len(SUBTITLE_STYLES)}\n")
    app.run(debug=False, port=5000, threaded=True)
