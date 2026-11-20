/**
 * YouTube Telegram Bot — Cloudflare Worker (webhook handler)
 * ===========================================================
 * Tugas Worker ini HANYA:
 *   1. Terima webhook Telegram
 *   2. Saat user mengirim link: kirim pesan placeholder, lalu trigger
 *      GitHub Actions mode "probe" — probe mengekstrak format yang
 *      benar-benar tersedia (s/d 8K) + perkiraan ukurannya, lalu mengirim
 *      foto + tombol format dinamis ke chat.
 *   3. Saat tombol diklik, trigger GitHub Actions mode "download"
 *      (workflow_dispatch) yang menjalankan yt-dlp + ffmpeg, lalu
 *      mengirim file ke Telegram.
 *
 * Download TIDAK dilakukan di Worker: Workers tidak bisa menjalankan
 * yt-dlp / ffmpeg, tidak punya filesystem, dan memori cuma 128 MB.
 *
 * Secrets / Vars yang dibutuhkan (set di Cloudflare Dashboard atau
 * pakai `wrangler secret put <NAMA>`):
 *   TELEGRAM_BOT_TOKEN       (secret, wajib) token dari @BotFather
 *   TELEGRAM_WEBHOOK_SECRET  (secret, wajib) string acak bebas, dipakai
 *                            Telegram untuk menandai webhook asli
 *   GITHUB_TOKEN             (secret, wajib) PAT dengan izin Actions: write
 *                            pada repo ini saja (fine-grained disarankan)
 *   GITHUB_OWNER             (var, wajib) username / org GitHub
 *   GITHUB_REPO              (var, wajib) nama repo ini
 *   GITHUB_REF               (var, opsional) default "main"
 *   ALLOWED_USER_IDS         (var, opsional) pisah koma, kosong = semua boleh
 *   YT_PROXY                 (var, opsional) proxy default yt-dlp, mis.
 *                            socks5:203.0.113.10:1080,
 *                            socks5://203.0.113.10:1080, atau
 *                            203.0.113.10:8080 (= http). User:pass
 *                            opsional. Kosong = tanpa proxy. Bila proxy
 *                            pakai username/password, JANGAN isi di sini
 *                            — isi GitHub secret YT_PROXY saja (Actions
 *                            memakainya bila Worker tidak mengirim proxy).
 *
 * Proxy per-permintaan (custom / manual), dikirim user di chat.
 * Proxy gratis (mis. ProxyScrape) umumnya tanpa user:pass:
 *   proxy socks5:203.0.113.10:1080 <link YouTube>
 *   proxy 203.0.113.10:8080 <link YouTube>
 *   proxy socks5://user:pass@IP:PORT <link YouTube>
 *   proxy http://IP:PORT <link YouTube>
 * Bila user hanya kirim <link>, dipakai YT_PROXY (default) bila ada.
 * Proxy custom diteruskan ke Actions dan diselipkan ke tombol format
 * (bila muat di batas 64 byte Telegram) supaya download memakai
 * proxy yang sama dengan probe.
 */

const CHOICES = {
  vid_best: "🎬 Video - Terbaik",
  vid_1080: "🎬 Video - 1080p",
  vid_720: "🎬 Video - 720p",
  vid_480: "🎬 Video - 480p",
  vid_360: "🎬 Video - 360p",
  aud_mp3: "🎵 Audio - MP3",
  aud_m4a: "🎵 Audio - M4A",
};

// Pilihan dinamis dari probe: "v<tinggi>" (mis. v4320, v2160, v1080)
// atau "v<tinggi>p<fps>" (mis. v1080p60, v2160p30). Hanya kombinasi
// yang benar-benar tersedia yang dibuat tombolnya oleh probe.
const DYNAMIC_CHOICE_RE = /^v(\d{3,4})(?:p(\d+))?$/;

function choiceLabel(choice) {
  if (CHOICES[choice]) return CHOICES[choice];
  const m = String(choice).match(DYNAMIC_CHOICE_RE);
  if (m) return `🎬 Video - ${m[1]}p${m[2] || ""}`;
  return null;
}

const PROXY_SCHEMES = ["http:", "https:", "socks4:", "socks4a:", "socks5:", "socks5h:"];

// Normalisasi proxy ke bentuk penuh yt-dlp scheme://[user:pass@]host:port.
// Menerima bentuk penuh, shorthand, dan bare IP:PORT (dianggap http).
// Proxy gratis (mis. ProxyScrape) umumnya tanpa user:pass:
//   socks5://203.0.113.10:1080, socks5:203.0.113.10:1080,
//   203.0.113.10:8080, http:203.0.113.10:8080
// Dengan user:pass juga bisa di semua bentuk.
export function normalizeProxyUrl(raw) {
  if (!raw) return null;
  const s = String(raw).trim();
  if (s.includes("://")) {
    try {
      const u = new URL(s);
      if (!PROXY_SCHEMES.includes(u.protocol.toLowerCase()) || !u.hostname || !u.port) return null;
      const auth = u.username ? `${u.username}${u.password ? `:${u.password}` : ""}@` : "";
      return `${u.protocol.toLowerCase()}//${auth}${u.hostname}:${u.port}`;
    } catch {
      return null;
    }
  }
  const m = s.match(/^(?:(http|https|socks4a?|socks5h?)(?::\/\/|:))?(?:([^:@/\s]+)(?::([^@/\s]*))?@)?([A-Za-z0-9._-]+|\[[0-9A-Fa-f:]+\]):(\d{1,5})$/i);
  if (!m) return null;
  const scheme = (m[1] || "http").toLowerCase();
  if (!PROXY_SCHEMES.includes(`${scheme}:`)) return null;
  const user = m[2] || "";
  const password = m[3] || "";
  let host = m[4];
  const port = Number(m[5]);
  if (!(port >= 1 && port <= 65535)) return null;
  if (/^\d+\.\d+\.\d+\.\d+$/.test(host) && host.split(".").some((o) => Number(o) > 255)) return null;
  if (host.startsWith("[") && host.endsWith("]")) host = host.slice(1, -1);
  const bracketHost = host.includes(":") ? `[${host}]` : host;
  const auth = user ? `${user}${password ? `:${password}` : ""}@` : "";
  return `${scheme}://${auth}${bracketHost}:${port}`;
}

export function isValidProxyUrl(url) {
  return normalizeProxyUrl(url) !== null;
}

export function maskProxy(url) {
  const norm = normalizeProxyUrl(url);
  if (!norm) return url ? "***" : "";
  try {
    const u = new URL(norm);
    const auth = u.username ? `${u.username}:***@` : "";
    return `${u.protocol}//${auth}${u.hostname}:${u.port}`;
  } catch {
    return "***";
  }
}

/** Parse perintah proxy di awal pesan.
 *  "proxy <proxy_url> <sisa pesan>" -> { proxy, rest, error: null }
 *  "<pesan biasa>"                  -> { proxy: null, rest: pesan, error: null }
 *  "proxy ..." tapi proxy_url tidak valid / link tidak ada
 *                                   -> { proxy: null, rest, error: "pesan" }
 */
export function parseProxyCommand(text) {
  const raw = String(text || "").trim();
  const parts = raw.split(/\s+/);
  if (parts.length === 0 || parts[0].toLowerCase() !== "proxy") {
    return { proxy: null, rest: raw, error: null };
  }
  if (parts.length < 3) {
    return {
      proxy: null,
      rest: raw,
      error: "Format proxy: proxy <proxy_url> <link YouTube>\nContoh: proxy socks5:203.0.113.10:1080 https://youtu.be/xxxx\natau: proxy 203.0.113.10:8080 https://youtu.be/xxxx",
    };
  }
  const proxy = normalizeProxyUrl(parts[1]);
  if (!proxy) {
    return {
      proxy: null,
      rest: raw,
      error: `Proxy tidak valid: ${parts[1]}\nBentuk yang diterima: socks5://IP:PORT, socks5:IP:PORT, IP:PORT (= http), http://, https://, socks4://, socks5:// — user:pass opsional.`,
    };
  }
  return { proxy, rest: parts.slice(2).join(" "), error: null };
}

export function decodeProxyFromCallback(data) {
  // Format callback: dl:<videoId>:<choice>[:<proxy base64url tanpa padding>]
  // Proxy default tidak pernah diselipkan (lihat download.py).
  const parts = String(data || "").split(":");
  if (parts.length !== 4 || parts[0] !== "dl") return null;
  try {
    const b64 = parts[3].replace(/-/g, "+").replace(/_/g, "/");
    const padded = b64 + "=".repeat((4 - (b64.length % 4)) % 4);
    const decoded = atob(padded);
    return normalizeProxyUrl(decoded);
  } catch {
    return null;
  }
}

// ---------------------------------------------------------------- util
export function extractVideoId(text) {
  if (!text) return null;
  const patterns = [
    /(?:youtube\.com\/(?:watch\?[^ \n]*v=|shorts\/|live\/|embed\/))([\w-]{11})/i,
    /(?:youtu\.be\/)([\w-]{11})/i,
    /(?:music\.youtube\.com\/watch\?[^ \n]*v=)([\w-]{11})/i,
  ];
  for (const re of patterns) {
    const m = String(text).match(re);
    if (m) return m[1];
  }
  return null;
}

function isAllowed(env, userId) {
  const raw = (env.ALLOWED_USER_IDS || "").trim();
  if (!raw) return true;
  return raw.split(",").map((s) => s.trim()).includes(String(userId));
}

function escapeHtml(s) {
  return String(s || "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

async function tg(env, method, payload) {
  const res = await fetch(`https://api.telegram.org/bot${env.TELEGRAM_BOT_TOKEN}/${method}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json().catch(() => ({}));
  if (!data.ok) console.error("Telegram API error", method, JSON.stringify(data));
  return data;
}

async function triggerGithubActions(env, { chatId, videoId, choice, mode = "download", probeMessageId = "", proxy = "" }) {
  const owner = env.GITHUB_OWNER;
  const repo = env.GITHUB_REPO;
  const ref = env.GITHUB_REF || "main";
  // Proxy yang dikirim ke Actions: custom (dari user / tombol) dulu,
  // lalu YT_PROXY dari wrangler.toml (default). Bila keduanya kosong,
  // Actions memakai GitHub secret YT_PROXY bila ada.
  const effectiveProxy = normalizeProxyUrl(proxy) || normalizeProxyUrl(env.YT_PROXY) || "";
  const apiUrl = `https://api.github.com/repos/${owner}/${repo}/actions/workflows/download.yml/dispatches`;
  const res = await fetch(apiUrl, {
    method: "POST",
    headers: {
      authorization: `Bearer ${env.GITHUB_TOKEN}`,
      accept: "application/vnd.github+json",
      "x-github-api-version": "2022-11-28",
      "user-agent": "yt-bot-cloudflare-worker",
      "content-type": "application/json",
    },
    body: JSON.stringify({
      ref,
      inputs: {
        chat_id: String(chatId),
        youtube_url: `https://www.youtube.com/watch?v=${videoId}`,
        format_choice: choice,
        mode,
        probe_message_id: String(probeMessageId || ""),
        proxy: effectiveProxy,
      },
    }),
  });
  // Sukses workflow_dispatch = HTTP 204 No Content
  return { ok: res.status === 204, status: res.status, body: await res.text().catch(() => "") };
}

// ---------------------------------------------------------------- handlers
async function handleMessage(env, message) {
  const chatId = message.chat.id;
  const userId = message.from?.id;
  const text = message.text || "";

  if (!isAllowed(env, userId)) {
    await tg(env, "sendMessage", { chat_id: chatId, text: "⛔ Kamu tidak punya akses ke bot ini." });
    return;
  }

  if (text.startsWith("/start") || text.startsWith("/help")) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      parse_mode: "HTML",
      text:
        "👋 <b>YouTube Downloader Bot</b>\n\n" +
        "Kirimkan link YouTube, nanti aku cek dulu kualitas yang tersedia " +
        "(sampai 8K bila ada) lengkap dengan fps dan perkiraan ukurannya. " +
        "Hanya resolusi + fps yang benar-benar tersedia yang ditampilkan:\n" +
        "🎬 <b>Video</b> — mis. 1080p60, 2160p30, 4320p60\n" +
        "🎵 <b>Audio</b> — MP3 / M4A\n\n" +
        "🔒 <b>Proxy (anti bot-check YouTube)</b>\n" +
        "Kirim link saja = pakai proxy default (YT_PROXY) bila sudah diset.\n" +
        "Proxy custom per-permintaan:\n" +
        "<code>proxy socks5:IP:PORT https://youtu.be/xxxx</code>\n" +
        "Skema: http://, https://, socks4://, socks5:// —\n" +
        "bisa juga tanpa // dan tanpa user:pass, mis. socks5:IP:PORT\n" +
        "atau IP:PORT saja (= http)\n\n" +
        "⚠️ Pengecekan &amp; download dijalankan di GitHub Actions, jadi " +
        "masing-masing butuh sekitar 1-3 menit. Batas file 50 MB.",
    });
    return;
  }

  // Perintah proxy custom: "proxy <proxy_url> <link>"
  // Bila formatnya salah, balas cara pakainya dan jangan trigger probe.
  const parsed = parseProxyCommand(text);
  if (parsed.error) {
    await tg(env, "sendMessage", { chat_id: chatId, text: `❌ ${parsed.error}` });
    return;
  }
  const customProxy = parsed.proxy || "";
  const effectiveText = parsed.rest;

  const videoId = extractVideoId(effectiveText);
  if (!videoId) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text: "Itu bukan link YouTube yang valid. Kirim link seperti:\nhttps://www.youtube.com/watch?v=... atau https://youtu.be/...\n\nProxy custom:\nproxy socks5://IP:PORT https://youtu.be/...",
    });
    return;
  }

  // Placeholder dulu, lalu probe di GitHub Actions yang menggantinya dengan
  // foto + tombol format dinamis (resolusi + fps yang tersedia + ukuran).
  const proxyNote = customProxy
    ? `\n🔒 Proxy: ${maskProxy(customProxy)}`
    : (env.YT_PROXY ? `\n🔒 Proxy default: ${maskProxy(env.YT_PROXY)}` : "");
  const placeholder = await tg(env, "sendMessage", {
    chat_id: chatId,
    text: `🔍 Sedang memeriksa kualitas & ukuran yang tersedia...${proxyNote}\n(sekitar 1 menit, prosesnya di GitHub Actions)`,
  });
  const probeMessageId = placeholder?.result?.message_id || "";

  const result = await triggerGithubActions(env, {
    chatId, videoId, choice: "probe", mode: "probe", probeMessageId, proxy: customProxy,
  });
  if (!result.ok) {
    console.error("GitHub dispatch probe gagal", result.status, result.body);
    const errText =
      `❌ Gagal memulai pengecekan di GitHub Actions (HTTP ${result.status}).\n` +
      "Cek: GITHUB_TOKEN masih berlaku? Nama repo di GITHUB_OWNER/GITHUB_REPO sudah benar? " +
      "Workflow download.yml sudah ada di branch main?";
    if (probeMessageId) {
      await tg(env, "editMessageText", { chat_id: chatId, message_id: probeMessageId, text: errText }).catch(() => {});
    } else {
      await tg(env, "sendMessage", { chat_id: chatId, text: errText });
    }
  }
}

async function handleCallback(env, query) {
  const data = query.data || "";
  const chatId = query.message?.chat?.id;
  const messageId = query.message?.message_id;
  if (!chatId) return;

  if (!isAllowed(env, query.from?.id)) {
    await tg(env, "answerCallbackQuery", { callback_query_id: query.id, text: "⛔ Tidak punya akses.", show_alert: true });
    return;
  }

  const parts = data.split(":");
  if ((parts.length !== 3 && parts.length !== 4) || parts[0] !== "dl") return;
  const [, videoId, choice] = parts;
  // Proxy custom dari probe, bila diselipkan ke tombol (lihat download.py).
  // Bila tidak ada, triggerGithubActions akan pakai YT_PROXY (default).
  const customProxy = decodeProxyFromCallback(data) || "";

  if (choice === "cancel") {
    await tg(env, "answerCallbackQuery", { callback_query_id: query.id });
    if (messageId) {
      await tg(env, "editMessageCaption", { chat_id: chatId, message_id: messageId, caption: "✖️ Dibatalkan." }).catch(() => {});
    }
    return;
  }
  const label = choiceLabel(choice);
  if (!label) return;

  await tg(env, "answerCallbackQuery", { callback_query_id: query.id, text: `${label} — diproses...` });

  const result = await triggerGithubActions(env, { chatId, videoId, choice, proxy: customProxy });
  if (result.ok) {
    await tg(env, "sendMessage", {
      chat_id: chatId,
      parse_mode: "HTML",
      text:
        `⏳ <b>${label}</b> sedang diproses di GitHub Actions.` +
        (customProxy ? `\n🔒 Proxy: ${escapeHtml(maskProxy(customProxy))}` : "") +
        "\nFile akan dikirim ke chat ini setelah selesai (biasanya 1-3 menit). Mohon tunggu, jangan spam ya.",
    });
  } else {
    console.error("GitHub dispatch gagal", result.status, result.body);
    await tg(env, "sendMessage", {
      chat_id: chatId,
      text:
        `❌ Gagal memulai download di GitHub Actions (HTTP ${result.status}).\n` +
        "Cek: GITHUB_TOKEN masih berlaku? Nama repo di GITHUB_OWNER/GITHUB_REPO sudah benar? " +
        "Workflow download.yml sudah ada di branch main?",
    });
  }
}

// ---------------------------------------------------------------- entry
export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      return new Response("YouTube Telegram Bot Worker — OK", { status: 200 });
    }

    // Daftarkan webhook ke URL Worker ini sendiri. Lindungi dengan secret yang sama:
    // buka /set-webhook?secret=<TELEGRAM_WEBHOOK_SECRET>
    if (request.method === "GET" && url.pathname === "/set-webhook") {
      if (!env.TELEGRAM_WEBHOOK_SECRET || url.searchParams.get("secret") !== env.TELEGRAM_WEBHOOK_SECRET) {
        return new Response("Forbidden", { status: 403 });
      }
      const webhookUrl = `${url.origin}/webhook`;
      const data = await tg(env, "setWebhook", {
        url: webhookUrl,
        secret_token: env.TELEGRAM_WEBHOOK_SECRET,
        drop_pending_updates: true,
        allowed_updates: ["message", "callback_query"],
      });
      return Response.json({ webhook: webhookUrl, telegram: data });
    }

    if (request.method === "POST" && url.pathname === "/webhook") {
      // Verifikasi webhook asli dari Telegram
      if (env.TELEGRAM_WEBHOOK_SECRET) {
        const secret = request.headers.get("x-telegram-bot-api-secret-token");
        if (secret !== env.TELEGRAM_WEBHOOK_SECRET) return new Response("Forbidden", { status: 403 });
      }
      let update;
      try {
        update = await request.json();
      } catch {
        return new Response("Bad Request", { status: 400 });
      }
      // Balas 200 ke Telegram secepatnya, proses lanjut di background
      ctx.waitUntil(
        (async () => {
          try {
            if (update.message) await handleMessage(env, update.message);
            else if (update.callback_query) await handleCallback(env, update.callback_query);
          } catch (e) {
            console.error("handle update gagal", e);
          }
        })()
      );
      return new Response("OK", { status: 200 });
    }

    return new Response("Not Found", { status: 404 });
  },
};
