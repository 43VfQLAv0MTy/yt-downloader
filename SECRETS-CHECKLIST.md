# Checklist Secrets & Variables — yt-downloader-bot

Repo: `43VfQLAv0MTy/yt-downloader-bot` — Worker: `youtube-telegram-bot`

## A. GitHub Repo — Secrets

Tempat: repo → **Settings → Secrets and variables → Actions → New repository secret**

| Nama | Wajib? | Isi | Dipakai oleh |
|---|---|---|---|
| `TELEGRAM_BOT_TOKEN` | ✅ Wajib | Token dari @BotFather | `download.yml` → `downloader/download.py` untuk mengirim file ke Telegram |
| `YT_PROXY` | Opsional — proxy default | Proxy yt-dlp, mis. `socks5://user:pass@203.0.113.10:1080`, `socks5:203.0.113.10:1080`, atau `203.0.113.10:8080` (tanpa user:pass, seperti ProxyScrape). Dipakai bila Worker tidak mengirim proxy (tidak ada proxy custom dan `YT_PROXY` di `wrangler.toml` kosong) | `download.yml` → `downloader/download.py` (`--proxy` yt-dlp, probe + download) |
| `YOUTUBE_COOKIES` | Opsional — cadangan terakhir | Isi file cookies.txt YouTube (format Netscape) dari akun Google **cadangan/burner**, diekspor pakai ekstensi "Get cookies.txt LOCALLY" | `download.yml` → ditulis ke file sementara → `download.py` memakainya bila rotasi client + PO-token masih kena bot-check YouTube. **Jangan pakai akun utama** (akun yang dipakai otomatisasi berisiko dibatasi YouTube) dan jangan pernah commit / kirim isi cookies ke chat |
| `CLOUDFLARE_API_TOKEN` | Hanya untuk deploy otomatis | API Token Cloudflare (izin Edit Workers) | `deploy-worker.yml` |
| `CLOUDFLARE_ACCOUNT_ID` | Hanya untuk deploy otomatis | Account ID Cloudflare | `deploy-worker.yml` |

> Kalau deploy Worker dilakukan manual pakai `npx wrangler deploy`, dua secret
> Cloudflare di atas **tidak perlu** diisi di GitHub.
> Di GitHub **tidak ada Variables** yang wajib diisi untuk project ini.

## B. Cloudflare Worker — Secrets (terenkripsi)

Tempat: Dashboard → **Workers & Pages → youtube-telegram-bot → Settings →
Variables and Secrets** — atau `npx wrangler secret put <NAMA>`

| Nama | Isi |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token yang **sama** dengan di GitHub (Worker memakainya untuk balas pesan / kirim tombol ke Telegram) |
| `TELEGRAM_WEBHOOK_SECRET` | String acak panjang buatanmu sendiri (contoh: hasil password generator, 32+ karakter). Dipakai juga saat membuka `/set-webhook?secret=...` |
| `GITHUB_TOKEN` | Fine-grained PAT: hanya repo `yt-downloader-bot`, permission **Actions: Read and write** |

## C. Cloudflare Worker — Variables (teks biasa, sudah diisi di `wrangler.toml`)

| Nama | Nilai |
|---|---|
| `GITHUB_OWNER` | `43VfQLAv0MTy` |
| `GITHUB_REPO` | `yt-downloader-bot` |
| `GITHUB_REF` | `main` |
| `YT_PROXY` | *(kosong = tanpa proxy)* — proxy default yt-dlp, mis. `socks5:203.0.113.10:1080` atau `203.0.113.10:8080`. **Hanya tanpa kredensial**; bila ada username/password, isi GitHub secret `YT_PROXY` (tabel A) saja, bukan di sini |
| `ALLOWED_USER_IDS` | *(kosong = semua boleh)* — isi ID Telegram pisah koma kalau mau dibatasi |

## E. Proxy custom per pesan (bukan secret, dikirim di chat)

Format: `proxy <proxy_url> <link YouTube>`, mis.
`proxy socks5://203.0.113.10:1080 https://youtu.be/xxxx`.
Dipakai untuk probe dan download itu saja, mengalahkan default.
Batas: proxy yang terlalu panjang tidak muat di tombol Telegram
(maks 64 byte) — bot akan memberi peringatan dan memakai default.

## D. BUKAN secret — jangan diisi manual

`CHAT_ID`, `YOUTUBE_URL`, `FORMAT_CHOICE`, `PROXY` di downloader
**bukan** pengaturan. Nilainya dikirim otomatis: Worker →
workflow_dispatch inputs → env di `download.yml` →
`downloader/download.py`. `PROXY` di sini adalah proxy custom dari
pesan user; proxy default-nya adalah `YT_PROXY` (tabel A / C).

## Ringkasan jumlah

- GitHub: **1 secret wajib** (+1 opsional `YT_PROXY`, +2 kalau mau deploy otomatis)
- Cloudflare Worker: **3 secret + 5 variable** (variable sudah tertulis di `wrangler.toml`)
- Nilai yang sama di dua tempat: hanya `TELEGRAM_BOT_TOKEN`
