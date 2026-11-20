#!/usr/bin/env python3
"""
Downloader untuk GitHub Actions — dipanggil oleh .github/workflows/download.yml

Input lewat environment variable (bukan argumen shell, agar aman dari injection):
  MODE                download (default) | probe
  CHAT_ID             ID chat Telegram tujuan
  YOUTUBE_URL         link YouTube
  FORMAT_CHOICE       vid_best | vid_1080 | vid_720 | vid_480 | vid_360 |
                      aud_mp3 | aud_m4a | v<tinggi> dinamis (mis. v2160, v1080)
                      | v<tinggi>p<fps> dinamis (mis. v1080p60, v2160p30)
  PROBE_MESSAGE_ID    (mode probe) ID pesan placeholder dari Worker untuk
                      dihapus / diedit setelah hasil probe siap
  TELEGRAM_BOT_TOKEN  (secret repo) token bot
  YT_COOKIES_FILE     (opsional) path file cookies YouTube dari secret
  PROXY               (opsional) proxy custom per-permintaan, dari Worker.
                      Prioritas tertinggi. Bentuk yang diterima (semua
                      tanpa user:pass juga bisa, mis. proxy gratis
                      ProxyScrape):
                      http://203.0.113.10:8080, socks5://203.0.113.10:1080,
                      socks5:203.0.113.10:1080, 203.0.113.10:8080 (= http)
  YT_PROXY            (opsional) proxy DEFAULT, bentuknya sama seperti
                      PROXY (dengan atau tanpa user:pass). Dipakai bila
                      PROXY kosong. Isi lewat wrangler.toml, atau lewat
                      GitHub secret YT_PROXY — keduanya default, yang
                      di wrangler.toml didahulukan bila keduanya terisi.
                      Isi lewat GitHub secret YT_PROXY (disarankan bila
                      ada username/password) atau lewat variable
                      YT_PROXY di wrangler.toml (hanya bila tanpa
                      kredensial). Skema yang diterima yt-dlp:
                      http://, https://, socks4://, socks4a://,
                      socks5://, socks5h://

Mode download: kirim pesan status -> yt-dlp download + convert (ffmpeg) ->
cek batas 50 MB -> kirim file ke Telegram -> hapus pesan status.

Mode probe: ekstrak info video (tanpa download) -> hitung resolusi yang
BENAR-BENAR tersedia (maks 4320p/8K) + fps-nya + perkiraan ukuran tiap
format -> kirim foto + tombol format dinamis ke Telegram. Hanya kombinasi
(height, fps) yang benar-benar ada di daftar format YouTube yang
ditampilkan; yang tidak tersedia tidak dibuat tombolnya. Tombolnya diklik
lewat webhook Worker seperti biasa, lalu mode download yang bekerja.
Proxy (bila ada) dipakai di probe DAN di download, supaya IP yang
dilihat YouTube sama di kedua langkah.
"""

from __future__ import annotations

import base64
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path
from urllib.parse import urlparse

import requests
import yt_dlp

MAX_SIZE_SAFE = 49 * 1024 * 1024
# Telegram membatasi callback_data maksimal 64 byte. Proxy custom yang
# panjang tidak bisa diselipkan ke tombol, jadi ada batas ini (lihat
# callback_with_proxy di bawah).
CALLBACK_DATA_MAX = 64

PROXY_SCHEMES = {"http", "https", "socks4", "socks4a", "socks5", "socks5h"}

# Proxy tanpa user:pass (kebanyakan proxy gratis, mis. ProxyScrape)
# ditulis langsung IP:PORT, dengan atau tanpa skema:
#   socks5://203.0.113.10:1080   (bentuk penuh, yt-dlp)
#   socks5:203.0.113.10:1080     (tanpa //, seperti di ProxyScrape)
#   203.0.113.10:8080            (tanpa skema = http://)
#   http:203.0.113.10:8080       (tanpa //)
# Dengan user:pass juga bisa: socks5://user:pass@host:1080 atau
# socks5:user:pass@host:1080. Semua bentuk di atas dinormalisasi ke
# bentuk penuh scheme://[user:pass@]host:port sebelum ke yt-dlp.
_HOST_RE = re.compile(r"^(?:[A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\])$")
_SCHEME_RE = re.compile(
    r"^(?:(http|https|socks4a?|socks5h?)(?:://|:))?"
    r"(?:([^:@/\s]+)(?::([^@/\s]*))?@)?"
    r"([A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):(\d{1,5})$",
    re.IGNORECASE,
)


def normalize_proxy(url: str) -> str | None:
    """Normalisasi proxy ke bentuk penuh yt-dlp, atau None bila invalid.

    Menerima bentuk penuh, shorthand `socks5:IP:PORT`, dan bare `IP:PORT`
    (dianggap http). User:pass opsional di semua bentuk.
    """
    if not url:
        return None
    raw = url.strip()
    # Bentuk penuh sudah benar — validasi lewat urlparse.
    if "://" in raw:
        try:
            p = urlparse(raw)
        except Exception:
            return None
        if p.scheme.lower() not in PROXY_SCHEMES or not p.hostname or not p.port:
            return None
        auth = ""
        if p.username:
            auth = f"{p.username}:{p.password}@" if p.password else f"{p.username}@"
        return f"{p.scheme.lower()}://{auth}{p.hostname}:{p.port}"
    m = _SCHEME_RE.match(raw)
    if not m:
        return None
    scheme, user, password, host, port_s = m.groups()
    scheme = (scheme or "http").lower()
    if scheme not in PROXY_SCHEMES:
        return None
    host = host[1:-1] if host.startswith("[") and host.endswith("]") else host
    # Validasi IP bila host terlihat seperti IPv4.
    if re.fullmatch(r"\d+\.\d+\.\d+\.\d+", host):
        if any(not 0 <= int(o) <= 255 for o in host.split(".")):
            return None
    try:
        port = int(port_s)
    except ValueError:
        return None
    if not 1 <= port <= 65535:
        return None
    auth = ""
    if user:
        auth = f"{user}:{password}@" if password else f"{user}@"
    bracket_host = f"[{host}]" if ":" in host and not host.startswith("[") else host
    return f"{scheme}://{auth}{bracket_host}:{port}"


def valid_proxy(url: str) -> bool:
    """True bila url berupa proxy yang diterima yt-dlp (--proxy),
    dalam bentuk penuh maupun shorthand."""
    return normalize_proxy(url) is not None


def effective_proxy() -> str:
    """Proxy yang dipakai run ini (sudah dinormalisasi):
    PROXY (custom) > YT_PROXY (default)."""
    raw = os.environ.get("PROXY", "").strip() or os.environ.get("YT_PROXY", "").strip()
    return normalize_proxy(raw) or ""


def custom_proxy() -> str:
    """Hanya proxy custom per-permintaan (dari Worker), bukan default."""
    return normalize_proxy(os.environ.get("PROXY", "").strip()) or ""


def mask_proxy(url: str) -> str:
    """Tampilkan proxy tanpa password, mis. http://user:***@host:8080.
    Menerima bentuk penuh maupun shorthand."""
    norm = normalize_proxy(url) if url else None
    if not norm:
        return "***" if url else ""
    try:
        p = urlparse(norm)
        auth = ""
        if p.username:
            auth = f"{p.username}:***@" if p.password else f"{p.username}@"
        return f"{p.scheme}://{auth}{p.hostname}:{p.port}"
    except Exception:
        return "***"


def _sanitize(text: str) -> str:
    """Ganti proxy asli (bila ada kredensial) dengan versi tersamar di teks
    yang akan dikirim ke Telegram / log, supaya tidak bocor."""
    if not text:
        return ""
    out = text
    for raw in {os.environ.get("PROXY", "").strip(),
                os.environ.get("YT_PROXY", "").strip()}:
        if raw and raw in out:
            out = out.replace(raw, mask_proxy(raw))
    proxy = effective_proxy()
    if proxy and proxy in out:
        out = out.replace(proxy, mask_proxy(proxy))
    return out


def callback_with_proxy(video_id: str, choice: str) -> tuple[str, bool]:
    """Buat callback_data dl:<video_id>:<choice>[:<proxy-b64url>].

    Proxy custom hanya diselipkan bila totalnya masih <= 64 byte.
    Mengembalikan (callback_data, proxy_ikut_diselipkan).
    Proxy default TIDAK diselipkan: Actions sudah pakai YT_PROXY
    sebagai fallback bila input proxy kosong.
    """
    base = f"dl:{video_id}:{choice}"
    proxy = custom_proxy()
    if not proxy:
        return base, True
    encoded = base64.urlsafe_b64encode(proxy.encode()).decode().rstrip("=")
    full = f"{base}:{encoded}"
    if len(full.encode()) <= CALLBACK_DATA_MAX:
        return full, True
    return base, False

FORMAT_CHOICES = {
    "vid_best": {
        "label": "🎬 Video - Terbaik", "kind": "video",
        "format": "bestvideo[ext=mp4]+bestaudio[ext=m4a]/best[ext=mp4]/best",
    },
    "vid_1080": {
        "label": "🎬 Video - 1080p", "kind": "video",
        "format": "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]/best[height<=1080][ext=mp4]/best[height<=1080]",
    },
    "vid_720": {
        "label": "🎬 Video - 720p", "kind": "video",
        "format": "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]/best[height<=720][ext=mp4]/best[height<=720]",
    },
    "vid_480": {
        "label": "🎬 Video - 480p", "kind": "video",
        "format": "bestvideo[height<=480][ext=mp4]+bestaudio[ext=m4a]/best[height<=480][ext=mp4]/best[height<=480]",
    },
    "vid_360": {
        "label": "🎬 Video - 360p", "kind": "video",
        "format": "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]/best[height<=360][ext=mp4]/best[height<=360]",
    },
    "aud_mp3": {
        "label": "🎵 Audio - MP3", "kind": "audio",
        "format": "bestaudio/best", "codec": "mp3", "quality": "192",
    },
    "aud_m4a": {
        "label": "🎵 Audio - M4A", "kind": "audio",
        "format": "bestaudio[ext=m4a]/bestaudio/best", "codec": "m4a", "quality": "192",
    },
}

_DYNAMIC_RE = re.compile(r"^v(\d{3,4})(?:p(\d+))?$")


def resolve_choice(choice_key: str) -> dict | None:
    """Terima pilihan statis FORMAT_CHOICES atau dinamis:
    v<tinggi> (mis. v1440) atau v<tinggi>p<fps> (mis. v1080p60, v2160p30).
    Bila fps disebut, format dipilih dengan height DAN fps yang persis
    itu, supaya 1080p30 tidak tertukar dengan 1080p60."""
    if choice_key in FORMAT_CHOICES:
        return FORMAT_CHOICES[choice_key]
    m = _DYNAMIC_RE.match(choice_key or "")
    if m:
        h = int(m.group(1))
        fps = int(m.group(2)) if m.group(2) else None
        if 144 <= h <= 4320 and (fps is None or 1 <= fps <= 120):
            if fps is not None:
                fmt = (f"bestvideo[height={h}][fps={fps}][ext=mp4]+bestaudio[ext=m4a]"
                       f"/best[height={h}][fps={fps}][ext=mp4]/best[height={h}][fps={fps}]")
                return {"label": f"🎬 Video - {h}p{fps}", "kind": "video", "format": fmt}
            return {
                "label": f"🎬 Video - {h}p", "kind": "video",
                "format": (f"bestvideo[height<={h}][ext=mp4]+bestaudio[ext=m4a]"
                           f"/best[height<={h}][ext=mp4]/best[height<={h}]"),
            }
    return None


def _fps_int(f: dict) -> int | None:
    """fps dari satu format yt-dlp, dibulatkan ke int terdekat (30, 60, ...).
    Mengembalikan None bila format tidak punya fps (mis. audio / unknown)."""
    raw = f.get("fps")
    if raw is None:
        return None
    try:
        fps = int(round(float(raw)))
    except (TypeError, ValueError):
        return None
    return fps if 1 <= fps <= 120 else None


# Rencana percobaan download: (player_client, pakai_cookies, fetch_pot).
# Pelajaran dari log Actions 20 Nov 2026:
# - Tanpa cookies, semua client kena bot-check dari IP Actions — kecuali
#   bila IP runner sedang bersih, `visionos` memberi format paling lengkap.
# - Dengan cookies, ekstraksi lolos, tapi format dari chain default authed
#   (web_embedded, tv_samsung, web) saat diunduh kena HTTP 403: format
#   client web butuh PO-token (GVS) yang mengikat URL-nya, dan tv_samsung
#   BUKAN client WebPO jadi formatnya tidak bisa diotorisasi bgutil.
# - Client yang PO-tokennya bisa dibuat bgutil (WEBPO_CLIENTS di yt-dlp):
#   web, mweb, tv, web_embedded, web_creator, web_music, tv_simply.
#   Untuk mereka fetch_pot dipaksa "always" (default "auto" sering tidak
#   meminta token sama sekali).
ATTEMPT_PLAN: list[tuple[list[str] | None, bool, bool]] = [
    (["visionos"], True, False),
    (["visionos"], False, False),
    (["web"], True, True),
    (["mweb"], True, True),
    (["tv"], True, True),
    (["web_embedded"], True, True),
    (None, True, True),            # default yt-dlp + cookies + POT
    (["android_vr"], False, False),
    (None, False, False),          # default yt-dlp polos (IP bersih)
]

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
API = f"https://api.telegram.org/bot{TOKEN}"


def tg_post(method: str, **kwargs) -> dict:
    r = requests.post(f"{API}/{method}", timeout=120, **kwargs)
    data = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not data.get("ok"):
        print(f"Telegram {method} gagal: {data}", file=sys.stderr)
    return data


def send_text(chat_id: str, text: str) -> int | None:
    data = tg_post("sendMessage", json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})
    return (data.get("result") or {}).get("message_id")


def edit_text(chat_id: str, message_id: int | None, text: str) -> None:
    if message_id:
        tg_post("editMessageText", json={
            "chat_id": chat_id, "message_id": message_id, "text": text, "parse_mode": "HTML",
        })


def fmt_size(n: int) -> str:
    return f"{n / (1024 * 1024):.1f} MB"


def fmt_duration(sec: int | None) -> str:
    if not sec:
        return "-"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def escape_html(t: str) -> str:
    return (t or "").replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _cookies_file() -> str | None:
    """Path file cookies YouTube dari env YT_COOKIES_FILE, bila ada & terisi."""
    path = os.environ.get("YT_COOKIES_FILE", "").strip()
    if path and Path(path).is_file() and Path(path).stat().st_size > 0:
        return path
    return None


def _attempt_extras() -> list[tuple[dict, str]]:
    """Daftar (extra_opts, label) sesuai ATTEMPT_PLAN & ketersediaan cookies."""
    cookies = _cookies_file()
    out: list[tuple[dict, str]] = []
    for clients, use_cookies, fetch_pot in ATTEMPT_PLAN:
        if use_cookies and not cookies:
            continue
        yt_args: dict = {}
        if clients:
            yt_args["player_client"] = clients
        if fetch_pot:
            yt_args["fetch_pot"] = ["always"]
        extra: dict = {}
        if yt_args:
            extra["extractor_args"] = {"youtube": yt_args}
        if use_cookies:
            extra["cookiefile"] = cookies
        label_parts = list(clients or ["default"])
        if use_cookies:
            label_parts.append("+cookies")
        if fetch_pot:
            label_parts.append("+pot")
        out.append((extra, ",".join(label_parts)))
    return out


def _base_opts() -> dict:
    opts: dict = {
        "quiet": True, "no_warnings": True, "noplaylist": True,
        "retries": 3, "fragment_retries": 3,
        # GitHub runner punya node terpasang; yt-dlp default cuma mengaktifkan
        # deno, jadi aktifkan node agar tantangan JS YouTube bisa diselesaikan.
        "js_runtimes": {"node": {}},
        # Solver JS eksternal resmi yt-dlp: membuka daftar format client web
        # (web_safari, tv_samsung, dll) yang kosong tanpa komponen ini.
        "remote_components": ["ejs:github"],
    }
    # Proxy yt-dlp (--proxy): dipakai di probe DAN download, supaya IP
    # yang dilihat YouTube sama di kedua langkah. Tanpa ini, probe bisa
    # lolos dari satu IP lalu download kena bot-check dari IP lain.
    proxy = effective_proxy()
    if proxy:
        opts["proxy"] = proxy
    return opts


def download_media(url: str, choice: dict, out_dir: Path) -> tuple[Path, str]:
    base_opts = _base_opts()
    base_opts.update({
        "format": choice["format"],
        "outtmpl": str(out_dir / "%(title).80s.%(ext)s"),
    })
    if choice["kind"] == "audio":
        base_opts["postprocessors"] = [{
            "key": "FFmpegExtractAudio",
            "preferredcodec": choice.get("codec", "mp3"),
            "preferredquality": choice.get("quality", "192"),
        }]
    else:
        base_opts["merge_output_format"] = "mp4"

    attempts = _attempt_extras()
    last_error: Exception | None = None
    for i, (extra, label) in enumerate(attempts, 1):
        opts = {**base_opts, **extra}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=True)
        except Exception as e:  # noqa: BLE001 - coba client berikutnya
            last_error = e
            print(f"Percobaan {i}/{len(attempts)} (client={label}) "
                  f"gagal: {e}", file=sys.stderr)
            for p in out_dir.glob("*"):
                if p.is_file():
                    p.unlink()
            continue

        if info and "entries" in info:
            info = info["entries"][0]
        title = (info or {}).get("title") or "Video YouTube"
        candidates = [
            p for p in out_dir.glob("*")
            if p.is_file() and p.suffix.lower() in {".mp4", ".mkv", ".webm", ".mp3", ".m4a"}
            and ".part" not in p.name
        ]
        if not candidates:
            raise FileNotFoundError("File hasil download tidak ditemukan.")
        print(f"Berhasil dengan percobaan {i}/{len(attempts)} (client={label})")
        return max(candidates, key=lambda p: p.stat().st_size), title

    raise last_error or RuntimeError("Semua percobaan download gagal.")


def extract_info_probe(url: str) -> dict:
    """Ekstrak info lengkap (tanpa download) memakai tangga client yang sama."""
    attempts = _attempt_extras()
    last_error: Exception | None = None
    for i, (extra, label) in enumerate(attempts, 1):
        opts = {**_base_opts(), **extra}
        try:
            with yt_dlp.YoutubeDL(opts) as ydl:
                info = ydl.extract_info(url, download=False)
        except Exception as e:  # noqa: BLE001 - coba client berikutnya
            last_error = e
            print(f"Probe percobaan {i}/{len(attempts)} (client={label}) "
                  f"gagal: {e}", file=sys.stderr)
            continue
        if info and "entries" in info:
            info = info["entries"][0]
        print(f"Probe berhasil dengan percobaan {i}/{len(attempts)} (client={label})")
        return info
    raise last_error or RuntimeError("Semua percobaan probe gagal.")


def _fmt_size_est(f: dict, duration: int) -> int:
    if f.get("filesize"):
        return int(f["filesize"])
    if f.get("filesize_approx"):
        return int(f["filesize_approx"])
    if f.get("tbr") and duration:
        return int(f["tbr"] * 1000 / 8 * duration)
    return 0


def _selected_size(formats: list[dict], fmt_string: str, duration: int) -> int:
    """Ukuran format yang AKAN dipilih yt-dlp untuk string format tertentu,
    dihitung dengan pemilih format yt-dlp sendiri atas daftar format probe."""
    try:
        # Pemilih format yt-dlp butuh field 'url' di tiap format; format
        # tanpa url (jarang, tapi ada) diisi placeholder supaya estimasi
        # ukuran tidak gagal dengan KeyError 'url'.
        safe_formats = [dict(f, url=f.get("url") or "https://example.invalid/")
                        for f in formats]
        with yt_dlp.YoutubeDL({"quiet": True, "no_warnings": True}) as ydl:
            selector = ydl.build_format_selector(fmt_string)
            picked = list(selector({"formats": safe_formats, "incomplete_formats": False}))
        total = 0
        for p in picked:
            for r in (p.get("requested_formats") or [p]):
                total += _fmt_size_est(r, duration)
        return total
    except Exception as e:  # noqa: BLE001 - jatuh ke perkiraan manual
        print(f"build_format_selector gagal ({fmt_string}): {e}", file=sys.stderr)
        return 0


def compute_options(info: dict) -> dict:
    """Hitung kombinasi (resolusi, fps) yang tersedia (maks 4320p/8K)
    + perkiraan ukuran tiap opsi.

    Hanya kombinasi yang benar-benar ada di daftar format YouTube yang
    dikembalikan — bila sebuah resolusi/fps tidak tersedia, tidak ada
    tombolnya. Urutan: resolusi besar dulu, lalu fps besar dulu.
    buckets: list[(height, fps_atau_None, ukuran_bytes)].
    """
    formats = info.get("formats") or []
    duration = int(info.get("duration") or 0)

    combos = sorted(
        {
            (int(f["height"]), _fps_int(f))
            for f in formats
            if f.get("height") and f.get("vcodec") not in (None, "none")
            and 144 <= int(f["height"]) <= 4320
        },
        key=lambda hf: (hf[0], hf[1] or 0),
        reverse=True,
    )

    buckets: list[tuple[int, int | None, int]] = []
    for h, fps in combos:
        choice_key = f"v{h}p{fps}" if fps else f"v{h}"
        choice = resolve_choice(choice_key)
        size = _selected_size(formats, choice["format"], duration) if choice else 0
        if size <= 0:
            # Cadangan manual: video mp4 tertinggi bitrate-nya pada
            # (tinggi, fps) ini + audio m4a terbaik.
            vids = [f for f in formats
                    if f.get("height") == h and _fps_int(f) == fps
                    and f.get("vcodec") not in (None, "none")
                    and f.get("acodec") in (None, "none")]
            auds = [f for f in formats
                    if f.get("vcodec") in (None, "none") and f.get("ext") == "m4a"]
            if vids:
                best_v = max(vids, key=lambda f: (f.get("ext") != "mp4", -(f.get("tbr") or 0)))
                size = _fmt_size_est(best_v, duration)
                if auds:
                    size += _fmt_size_est(max(auds, key=lambda f: f.get("tbr") or 0), duration)
        if size > 0:
            buckets.append((h, fps, size))

    m4a_choice = FORMAT_CHOICES["aud_m4a"]
    m4a_size = _selected_size(formats, m4a_choice["format"], duration)
    if m4a_size <= 0 and duration:
        m4a_size = int(duration * 128_000 / 8)
    mp3_size = int(duration * 192_000 / 8) if duration else 0

    return {
        "title": info.get("title") or "Video YouTube",
        "uploader": info.get("uploader") or info.get("channel") or "-",
        "duration": duration,
        "video_id": info.get("id") or "",
        "buckets": buckets,
        "mp3_size": mp3_size,
        "m4a_size": m4a_size,
    }


def run_probe(chat_id: str, url: str) -> int:
    placeholder_raw = os.environ.get("PROBE_MESSAGE_ID", "").strip()
    placeholder_id = int(placeholder_raw) if placeholder_raw.isdigit() else None
    try:
        info = extract_info_probe(url)
        opts = compute_options(info)
        if not opts["buckets"] and not opts["m4a_size"]:
            raise RuntimeError("Tidak ada format yang bisa diunduh dari video ini.")

        vid = opts["video_id"]
        proxy = effective_proxy()
        proxy_too_long = False

        def kb(choice: str) -> str:
            nonlocal proxy_too_long
            if choice == "cancel":
                return f"dl:{vid}:cancel"
            data, included = callback_with_proxy(vid, choice)
            if not included:
                proxy_too_long = True
            return data

        rows: list[list[dict]] = []
        row: list[dict] = []
        for h, fps, size in opts["buckets"]:
            label = f"{h}p{fps}" if fps else f"{h}p"
            row.append({"text": f"🎬 {label} • ±{fmt_size(size)}",
                        "callback_data": kb(f"v{label}")})
            if len(row) == 2:
                rows.append(row)
                row = []
        if row:
            rows.append(row)
        audio_row = []
        if opts["mp3_size"]:
            audio_row.append({"text": f"🎵 MP3 • ±{fmt_size(opts['mp3_size'])}",
                              "callback_data": kb("aud_mp3")})
        if opts["m4a_size"]:
            audio_row.append({"text": f"🎵 M4A • ±{fmt_size(opts['m4a_size'])}",
                              "callback_data": kb("aud_m4a")})
        if audio_row:
            rows.append(audio_row)
        rows.append([{"text": "✖️ Batal", "callback_data": kb("cancel")}])

        caption = (
            f"<b>{escape_html(opts['title'])}</b>\n\n"
            f"👤 {escape_html(opts['uploader'])}\n"
            f"⏱ {fmt_duration(opts['duration'])}\n"
            f"🔒 Proxy: {escape_html(mask_proxy(proxy)) if proxy else 'tidak dipakai'}\n\n"
            "Pilih format yang mau didownload (ukuran perkiraan).\n"
            "Hanya resolusi + fps yang tersedia yang ditampilkan:"
        )
        if any(size > MAX_SIZE_SAFE for _, _, size in opts["buckets"]):
            caption += "\n⚠️ Opsi di atas 50 MB tidak bisa dikirim lewat Telegram."
        if proxy_too_long:
            caption += (
                "\n⚠️ Proxy custom terlalu panjang untuk tombol Telegram "
                "(maks 64 byte), jadi download akan pakai proxy default. "
                "Kirim ulang dengan proxy yang lebih pendek "
                "(mis. tanpa username/password) bila perlu."
            )

        if placeholder_id:
            tg_post("deleteMessage", json={"chat_id": chat_id, "message_id": placeholder_id})
        photo = tg_post("sendPhoto", json={
            "chat_id": chat_id,
            "photo": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
            "caption": caption, "parse_mode": "HTML",
            "reply_markup": {"inline_keyboard": rows},
        })
        if not photo.get("ok"):
            tg_post("sendMessage", json={
                "chat_id": chat_id, "text": caption, "parse_mode": "HTML",
                "reply_markup": {"inline_keyboard": rows},
            })
        print(f"Probe terkirim: {len(opts['buckets'])} resolusi untuk chat {chat_id}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"PROBE ERROR: {e}", file=sys.stderr)
        text = _user_error_text(e)
        if placeholder_id:
            edit_text(chat_id, placeholder_id, text)
        else:
            send_text(chat_id, text)
        return 1


def _user_error_text(e: Exception) -> str:
    msg = _sanitize(str(e))
    if "Sign in to confirm" in msg:
        return (
            "❌ YouTube memblokir IP yang dipakai "
            "(bot-check: \"Sign in to confirm you're not a bot\") "
            "setelah semua client dicoba.\n\n"
            "Yang bisa dicoba:\n"
            "• Kirim ulang linknya dengan proxy lain:\n"
            "<code>proxy http://IP:PORT https://www.youtube.com/watch?v=...</code>\n"
            "  (juga bisa socks5://, socks4://, https://)\n"
            "• Atau pemilik bot isi proxy default di variable "
            "<code>YT_PROXY</code> (wrangler.toml) / secret "
            "<code>YT_PROXY</code> di GitHub.\n"
            "• Bila terus gagal, pasang cookies YouTube "
            "(secret YOUTUBE_COOKIES) — lihat README."
        )
    return (
        f"❌ Gagal memproses video ini.\n<code>{escape_html(msg[:250])}</code>\n\n"
        "Coba kualitas lain, atau cek apakah video privat / dibatasi usia."
    )


def main() -> int:
    chat_id = os.environ.get("CHAT_ID", "").strip()
    url = os.environ.get("YOUTUBE_URL", "").strip()
    mode = os.environ.get("MODE", "download").strip().lower()

    if not TOKEN:
        print("ERROR: secret TELEGRAM_BOT_TOKEN belum diset di repo.", file=sys.stderr)
        return 1
    if not chat_id or not url:
        print(f"ERROR: input tidak valid. chat_id={chat_id!r} url={url!r}", file=sys.stderr)
        return 1

    # Validasi proxy sebelum probe/download, supaya pesannya jelas
    # dan proxy yang salah ketik tidak dibuang diam-diam oleh yt-dlp.
    proxy = effective_proxy()
    if proxy and not valid_proxy(proxy):
        print(f"ERROR: proxy tidak valid: {mask_proxy(proxy)}", file=sys.stderr)
        send_text(chat_id,
                  "❌ Proxy tidak valid.\n\n"
                  "Format yang diterima yt-dlp:\n"
                  "<code>http://IP:PORT</code>, <code>https://IP:PORT</code>,\n"
                  "<code>socks4://IP:PORT</code>, <code>socks5://IP:PORT</code>\n"
                  "Boleh pakai user:pass, mis. "
                  "<code>socks5://user:pass@IP:PORT</code>")
        return 1
    if proxy:
        print(f"Proxy dipakai: {mask_proxy(proxy)}")

    if mode == "probe":
        return run_probe(chat_id, url)

    choice_key = os.environ.get("FORMAT_CHOICE", "vid_720").strip()
    choice = resolve_choice(choice_key)
    if not choice:
        print(f"ERROR: FORMAT_CHOICE tidak dikenal: {choice_key!r}", file=sys.stderr)
        return 1

    status_id = send_text(chat_id, f"⬇️ Mengunduh {choice['label']}...\nProses di GitHub Actions, mohon tunggu.")
    work = Path(tempfile.mkdtemp(prefix="yt-"))
    try:
        file_path, title = download_media(url, choice, work)
        size = file_path.stat().st_size
        if size > MAX_SIZE_SAFE:
            edit_text(chat_id, status_id,
                      f"⚠️ Hasilnya terlalu besar untuk bot Telegram.\n\n"
                      f"📦 Ukuran: <b>{fmt_size(size)}</b> (maks 50 MB)\n{escape_html(title)}\n\n"
                      "Coba kualitas lebih rendah (480p/360p) atau audio MP3/M4A.")
            return 0

        edit_text(chat_id, status_id, f"⬆️ Mengunggah ke Telegram... ({fmt_size(size)})")
        caption = f"<b>{escape_html(title)}</b>\n{choice['label']} • {fmt_size(size)}"
        method = "sendAudio" if choice["kind"] == "audio" else "sendVideo"
        field = "audio" if choice["kind"] == "audio" else "video"
        with open(file_path, "rb") as fh:
            data = {"chat_id": chat_id, "caption": caption, "parse_mode": "HTML"}
            if method == "sendVideo":
                data["supports_streaming"] = "true"
            r = requests.post(f"{API}/{method}", data=data,
                              files={field: (file_path.name, fh)}, timeout=600)
        result = r.json()
        if not result.get("ok"):
            edit_text(chat_id, status_id, f"❌ Upload ke Telegram gagal: {escape_html(str(result)[:250])}")
            return 1

        if status_id:
            tg_post("deleteMessage", json={"chat_id": chat_id, "message_id": status_id})
        print(f"OK: terkirim {file_path.name} ({fmt_size(size)}) ke chat {chat_id}")
        return 0
    except Exception as e:  # noqa: BLE001
        print(f"ERROR: {e}", file=sys.stderr)
        edit_text(chat_id, status_id, _user_error_text(e))
        return 1
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
