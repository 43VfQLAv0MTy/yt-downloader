# YouTube → Telegram Bot (Cloudflare Workers + Containers)

Kirim link YouTube ke bot, pilih Video (≤720p) atau MP3. Batas file 50 MB (limit Bot API).

Telegram → Worker (`src/index.js`, verifikasi secret) → Container (`bot.py` + yt-dlp + ffmpeg).

## Syarat
- Workers Paid plan ($5/bln), Node.js 20+, Docker berjalan di komputermu (untuk build image)
- Token bot dari @BotFather

## Deploy
    npm install
    npx wrangler login
    npx wrangler secret put BOT_TOKEN
    npx wrangler secret put WEBHOOK_SECRET     # string acak panjang
    npx wrangler deploy

1. Salin URL Worker dari output deploy, isi `WEBHOOK_URL` di `wrangler.jsonc`, lalu `npx wrangler deploy` lagi.
2. Buka URL Worker itu di browser sekali (membangunkan container; bot mendaftarkan webhook).
3. Kirim `/start` ke botmu.

Opsional: isi `ALLOWED_USER_IDS` (ID Telegram, pisahkan koma) agar hanya kamu yang bisa memakai bot.

## Lokal tanpa Cloudflare
    pip install -r requirements.txt   # butuh ffmpeg
    BOT_TOKEN=xxx python bot.py       # mode polling

## Catatan
- Container tidur setelah 20 menit tidak aktif; pesan pertama setelahnya butuh beberapa detik untuk bangun.
- YouTube bisa memblokir IP datacenter ("Sign in to confirm you're not a bot"). Itu keterbatasan yt-dlp, bukan bug bot.
- Perbarui yt-dlp berkala (deploy ulang akan menarik versi terbaru).
- Gunakan hanya untuk konten yang diizinkan; mengunduh dari YouTube melanggar ToS-nya.
