import asyncio
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path

import yt_dlp
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("ytbot")

TOKEN = os.environ["BOT_TOKEN"]
MAX_MB = int(os.getenv("MAX_MB", "49"))            # batas upload Bot API = 50 MB
MAX_DURATION = int(os.getenv("MAX_DURATION", "1800"))  # detik
ALLOWED = {int(x) for x in os.getenv("ALLOWED_USER_IDS", "").split(",") if x.strip()}

URL_RE = re.compile(
    r"https?://(?:www\.|m\.)?(?:youtube\.com/(?:watch\?v=|shorts/)|youtu\.be/)[\w\-]+\S*"
)


def allowed(update: Update) -> bool:
    return not ALLOWED or (update.effective_user and update.effective_user.id in ALLOWED)


def download(url: str, mode: str, outdir: str):
    limit = MAX_MB * 1024 * 1024
    opts = {
        "outtmpl": f"{outdir}/%(id)s.%(ext)s",
        "noplaylist": True,
        "quiet": True,
        "no_warnings": True,
        "max_filesize": limit,
        "restrictfilenames": True,
        "match_filter": yt_dlp.utils.match_filter_func(f"duration <= {MAX_DURATION}"),
    }
    if mode == "audio":
        opts["format"] = "bestaudio/best"
        opts["postprocessors"] = [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "128"}
        ]
    else:
        opts["format"] = (
            "bv*[height<=720][ext=mp4]+ba[ext=m4a]/b[height<=720][ext=mp4]/b[height<=720]"
        )
        opts["merge_output_format"] = "mp4"

    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
    files = list(Path(outdir).glob(f"{info['id']}.*"))
    if not files:
        return info.get("title", "file"), None
    f = files[0]
    if f.stat().st_size > limit:
        return info.get("title", "file"), None
    return info.get("title", "file"), f


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    await update.message.reply_text("Kirim link YouTube, lalu pilih Video atau MP3.")


async def on_link(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not allowed(update):
        return
    m = URL_RE.search(update.message.text or "")
    if not m:
        await update.message.reply_text("Link YouTube tidak ditemukan.")
        return
    context.user_data["url"] = m.group(0)
    kb = InlineKeyboardMarkup(
        [[
            InlineKeyboardButton("🎬 Video (≤720p)", callback_data="video"),
            InlineKeyboardButton("🎵 MP3", callback_data="audio"),
        ]]
    )
    await update.message.reply_text("Pilih format:", reply_markup=kb)


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if not allowed(update):
        return
    url = context.user_data.get("url")
    if not url:
        await q.edit_message_text("Sesi kedaluwarsa, kirim linknya lagi.")
        return
    mode = q.data
    await q.edit_message_text("⏳ Mengunduh...")
    tmp = tempfile.mkdtemp(prefix="yt_")
    try:
        title, path = await asyncio.to_thread(download, url, mode, tmp)
        if path is None:
            await q.edit_message_text(
                f"Gagal: file melebihi {MAX_MB} MB atau durasi > {MAX_DURATION // 60} menit."
            )
            return
        await q.edit_message_text("📤 Mengunggah...")
        with open(path, "rb") as fh:
            if mode == "audio":
                await q.message.reply_audio(fh, title=title[:60])
            else:
                await q.message.reply_video(fh, caption=title[:200], supports_streaming=True)
        await q.edit_message_text("✅ Selesai.")
    except Exception as e:
        log.exception("download failed")
        await q.edit_message_text(f"Gagal: {str(e)[:200]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    app = (
        Application.builder()
        .token(TOKEN)
        .concurrent_updates(True)
        .read_timeout(120)
        .write_timeout(300)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(on_button))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_link))

    public_url = os.getenv("WEBHOOK_URL")  # URL Worker, mis. https://ytbot.nama.workers.dev
    if public_url:
        app.run_webhook(
            listen="0.0.0.0",
            port=int(os.getenv("PORT", "8080")),
            url_path="telegram",
            webhook_url=f"{public_url.rstrip('/')}/telegram",
            secret_token=os.environ["WEBHOOK_SECRET"],
        )
    else:
        app.run_polling()


if __name__ == "__main__":
    main()
