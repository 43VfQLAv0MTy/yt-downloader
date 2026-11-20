# Bot Telegram YouTube Downloader — GitHub Repo + Cloudflare Workers

Arsitektur **tanpa server / VPS**:

```
User ──link──▶ Telegram ──webhook──▶ Cloudflare Worker ──trigger──▶ GitHub Actions
                                          │                              │
                                          │◀── tombol format ────────────┤ yt-dlp + ffmpeg
                                          │                              │ download + convert
User ◀──────────── file video / audio ────┴──────── Telegram Bot API ◀───┘
```

## Kenapa dibagi dua?

Cloudflare Workers **tidak bisa** menjalankan yt-dlp / ffmpeg: tidak ada binary
native, tidak ada filesystem persisten, memori 128 MB, CPU plan gratis 10 ms.
Jadi Worker hanya jadi *penerima pesan* (ringan, selalu online, gratis),
sedangkan download beratnya dijalankan GitHub Actions (ada Python, ffmpeg,
disk, dan gratis untuk repo publik).

Konsekuensinya: tidak instan. Setiap download butuh **±1–3 menit**
(start runner GitHub + download + upload). Untuk bot pribadi / komunitas
kecil ini wajar; untuk trafik besar, VPS tetap lebih cocok.

## Struktur repo

```
.
├── wrangler.toml          # konfigurasi Worker — WAJIB di root (dibaca Cloudflare Builds)
├── package.json
├── worker/
│   └── src/index.js       # webhook Telegram + trigger Actions (stateless)
├── downloader/
│   ├── download.py        # yt-dlp + kirim file ke Telegram (dijalankan Actions)
│   └── requirements.txt
└── .github/workflows/
    ├── download.yml       # workflow_dispatch: chat_id, youtube_url, format_choice
    └── deploy-worker.yml  # deploy Worker otomatis saat push ke main
```

## Setup — urutannya penting

### 1. Buat repo & push project ini

Buat dulu repo **43VfQLAv0MTy/yt-downloader** di GitHub (kosong, tanpa README /
.gitignore / license bawaan) kalau belum ada — per 19 Nov 2026 repo ini belum
terlihat publik. Project lokal di ZIP ini sudah `git init` + commit + remote,
jadi dari folder project tinggal:

```bash
git push -u origin main
```

> Workflow `download.yml` harus sudah ada di branch `main` (default branch)
> sebelum bisa dipicu lewat API.

### 2. Isi secrets di GitHub repo

Di repo: **Settings → Secrets and variables → Actions → New repository secret**

| Secret | Isi |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Token dari @BotFather (dipakai downloader untuk kirim file) |
| `YT_PROXY` | Opsional — proxy default yt-dlp, mis. `socks5://user:pass@203.0.113.10:1080` (dipakai bila Worker tidak mengirim proxy; lihat bagian Proxy di bawah) |
| `CLOUDFLARE_API_TOKEN` | Token Cloudflare dengan izin *Edit Cloudflare Workers* (hanya untuk deploy otomatis) |
| `CLOUDFLARE_ACCOUNT_ID` | Account ID di dashboard Cloudflare (hanya untuk deploy otomatis) |

### 3. Siapkan token GitHub untuk Worker

Worker butuh token untuk memicu workflow. Buat **fine-grained PAT**:
GitHub → Settings → Developer settings → Personal access tokens → Fine-grained

- Repository access: hanya repo ini
- Permissions: **Actions → Read and write**
- Salin tokennya (dipakai di langkah 4, jangan commit ke repo!)

### 4. Deploy Worker ke Cloudflare

`wrangler.toml` di root repo sudah diisi untuk repo ini: `GITHUB_OWNER = "43VfQLAv0MTy"`, `GITHUB_REPO = "yt-downloader"`, `main = "worker/src/index.js"`.

Cara A — otomatis: push ke `main`, workflow `deploy-worker.yml` akan deploy.

Cara B — manual dari komputer kamu:

```bash
# dari root repo (bukan dari folder worker/)
npx wrangler login
npx wrangler secret put TELEGRAM_BOT_TOKEN
npx wrangler secret put TELEGRAM_WEBHOOK_SECRET   # isi string acak panjang, catat!
npx wrangler secret put GITHUB_TOKEN              # fine-grained PAT dari langkah 3
npx wrangler deploy
```

Catat URL Worker yang keluar, contoh: `https://youtube-telegram-bot.<akun>.workers.dev`

> Kalau deploy lewat cara A, set ketiga secret di atas lewat
> **Cloudflare Dashboard → Workers & Pages → youtube-telegram-bot → Settings → Variables and Secrets**.

### 5. Daftarkan webhook Telegram

Setelah Worker online dan secret terisi, buka di browser:

```
https://youtube-telegram-bot.<akun>.workers.dev/set-webhook?secret=<TELEGRAM_WEBHOOK_SECRET kamu>
```

Balasan JSON dengan `"ok": true` artinya webhook berhasil dipasang.
Setelah ini bot tidak lagi pakai polling — versi `bot.py` lama jangan dijalankan bersamaan.

### 6. Tes

1. Chat `/start` ke bot → harus dibalas Worker (cepat).
2. Kirim link YouTube → bot membalas "sedang memeriksa kualitas & ukuran".
3. ±1 menit kemudian muncul foto + tombol format dinamis dari run **probe**:
   hanya resolusi yang benar-benar tersedia (maks 4320p/8K), tiap tombol
   ada perkiraan ukurannya.
4. Klik format → Worker membalas "sedang diproses di GitHub Actions".
5. Cek tab **Actions** di repo: run `YouTube Download` harus muncul.
6. File terkirim ke chat setelah run selesai.

## Pilihan format

🎬 Video dinamis per video (144p s/d 4320p / 8K, hanya yang tersedia)
lengkap dengan **fps dan perkiraan ukuran** — mis. `1080p60`, `2160p30`,
`4320p60`. Bila sebuah resolusi/fps tidak tersedia di video itu,
tombolnya **tidak ditampilkan**. 🎵 Audio MP3 / M4A 192kbps.
Batas kirim bot Telegram tetap **50 MB**; file lebih besar akan ditolak
dengan pesan saran turun kualitas / pilih audio.

## Proxy — anti "Sign in to confirm you're not a bot"

YouTube memblokir IP GitHub Actions yang sudah dikenal, jadi probe /
download bisa kena bot-check. yt-dlp punya opsi `--proxy`, dan bot ini
sudah mendukungnya di **probe dan download** (IP yang sama di kedua
langkah, supaya tidak lolos probe lalu gagal download).

**1. Proxy default — kirim link saja, proxy otomatis dipakai**

`YT_PROXY` itu **default**. Kalau terisi, dipakai. Mau pakai
username/password atau tidak, tetap default.

Cek proxy default yang sedang aktif di bot: kirim `/proxy` ke bot.
Bot membalas proxy-nya dalam bentuk tersamar, mis.
`socks5://user:***@203.0.113.10:1080`, atau "Tidak diset" kalau
`YT_PROXY` kosong. Yang ditampilkan adalah `YT_PROXY` dari Variable
Worker (Cloudflare); GitHub secret `YT_PROXY` (cadangan di Actions)
tidak terlihat dari Worker.

Isi di salah satu tempat ini (kalau keduanya terisi, yang di
`wrangler.toml` yang dipakai):

- `wrangler.toml` → `[vars]` → misal
  `YT_PROXY = "socks5:203.0.113.10:1080"` atau
  `YT_PROXY = "socks5://user:pass@203.0.113.10:1080"`.
  Setelah diubah, deploy ulang Worker.
- GitHub repo → Settings → Secrets → `YT_PROXY` — tempat yang sama,
  bedanya ini tersembunyi sebagai secret.

⚠️ **Penting:** repo `yt-downloader` ini **publik**, jadi kalau kamu
tulis `user:pass` di `wrangler.toml`, password proxy itu akan terlihat
siapa pun yang buka repo. Kalau proxy kamu pakai user:pass dan kamu
tidak mau bocor, kosongkan `YT_PROXY` di `wrangler.toml` dan isi
secret `YT_PROXY` di GitHub saja — fungsinya sama persis, tetap
default.

**Format proxy yang diterima (user:pass opsional di semuanya):**

```
socks5://203.0.113.10:1080   # bentuk penuh yt-dlp
socks5:203.0.113.10:1080     # tanpa //, seperti di ProxyScrape
203.0.113.10:8080            # tanpa skema = http://
http:203.0.113.10:8080       # tanpa //
socks5://user:pass@203.0.113.10:1080
```

Bot menormalisasi semuanya ke bentuk penuh `scheme://[user:pass@]host:port`
sebelum dikirim ke yt-dlp. Proxy ditampilkan di chat dalam bentuk
tersamar (`http://user:***@203.0.113.10:8080`), password tidak pernah
dikirim ke Telegram.

**Proxy gratis dari ProxyScrape:** di
https://proxyscrape.com/free-proxy-list tabelnya memisah Protocol,
IP, dan Port — jadi satu baris `SOCKS5 | 203.0.113.10 | 1080` ditulis
`proxy socks5:203.0.113.10:1080 <link>`. Lewat API-nya
(`proxy_format=protocolipport`) formatnya sudah `socks5://IP:PORT`,
itu juga diterima. Proxy gratis tidak pakai user:pass, dan sering
mati / lambat / sudah diblokir YouTube — kalau satu gagal, coba
yang lain. Jangan pakai `127.0.0.1` / `localhost` sebagai proxy:
di GitHub Actions itu artinya runner itu sendiri, bukan komputermu.

**2. Proxy custom / manual — per pesan, tanpa ubah setting**

Kirim dengan awalan `proxy`, lalu URL proxy, lalu link:

```
proxy socks5:203.0.113.10:1080 https://youtu.be/xxxx
proxy 203.0.113.10:8080 https://youtu.be/xxxx
proxy http://203.0.113.10:8080 https://www.youtube.com/watch?v=xxxx
proxy socks5://user:pass@203.0.113.10:1080 https://youtu.be/xxxx
```

Proxy custom ini dipakai untuk probe itu **dan** untuk download saat
tombol diklik (diselipkan ke tombol). Batasannya: Telegram membatasi
`callback_data` tombol maksimal 64 byte, jadi proxy custom yang
panjang (biasanya yang pakai username/password panjang) tidak muat
diselipkan — bot akan memberi peringatan di caption probe dan
download memakai proxy default. Solusinya: pakai proxy yang lebih
pendek, atau jadikan proxy itu sebagai default (`YT_PROXY`).

**Catatan jujur:** proxy bukan jaminan lolos 100%. Proxy yang IP-nya
juga sudah diblokir YouTube, proxy mati / lambat, atau proxy yang
memblokir YouTube tetap gagal. Bila proxy gagal, coba proxy lain,
atau kombinasikan dengan cookies YouTube (secret `YOUTUBE_COOKIES`,
lihat `SECRETS-CHECKLIST.md`).

## Troubleshooting

| Gejala | Cek |
|---|---|
| Cloudflare Builds gagal: `Could not detect a directory containing static files` | `npx wrangler deploy` jalan dari root repo tapi `wrangler.toml` tidak ditemukan di root — pastikan `wrangler.toml` ada di root (bukan hanya di subfolder) dengan `main = "worker/src/index.js"`, atau set *Root directory* = `worker` di Settings → Builds bila memakai struktur lama |
| Klik tombol → "Gagal memulai download (HTTP 404)" | `GITHUB_OWNER`/`GITHUB_REPO` salah, atau `download.yml` belum ada di branch `main` |
| Klik tombol → HTTP 401/403 | `GITHUB_TOKEN` kedaluwarsa / tidak punya izin Actions: write |
| Run Actions gagal di langkah kirim | Secret `TELEGRAM_BOT_TOKEN` di repo kosong / salah |
| Bot tidak merespons sama sekali | Webhook belum dipasang (langkah 5) atau `TELEGRAM_WEBHOOK_SECRET` di Worker tidak sama dengan yang dipakai saat set-webhook |
| "Sign in to confirm you're not a bot" | IP Actions / proxy diblokir YouTube. Coba proxy lain (`proxy socks5:IP:PORT <link>`), isi proxy default `YT_PROXY`, atau pasang `YOUTUBE_COOKIES` |
| Proxy custom tidak dipakai saat klik tombol | Proxy terlalu panjang untuk tombol Telegram (maks 64 byte). Pakai proxy lebih pendek, atau jadikan default lewat `YT_PROXY` |
| Mau batasi pengguna | Set var `ALLOWED_USER_IDS` di `wrangler.toml` (ID Telegram pisah koma), deploy ulang |
| Kuota Actions | Repo publik: gratis di runner standar. Repo privat: jatah plan (umumnya 2.000 menit/bulan di Free) — satu download ±1–3 menit |

## Keamanan

- Semua token disimpan sebagai **secret** (GitHub Secrets / Worker Secrets), tidak ada di kode.
- Webhook diverifikasi pakai header `X-Telegram-Bot-Api-Secret-Token`.
- Input dari Worker diteruskan ke Actions lewat `env:`, bukan ditempel ke perintah shell.
