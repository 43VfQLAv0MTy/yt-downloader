import { Container, getContainer } from "@cloudflare/containers";

// Container yang menjalankan bot Python (yt-dlp + ffmpeg)
export class YtBot extends Container {
  defaultPort = 8080;
  sleepAfter = "20m"; // container tidur kalau tidak ada aktivitas

  constructor(ctx, env) {
    super(ctx, env);
    this.envVars = {
      BOT_TOKEN: env.BOT_TOKEN,
      WEBHOOK_SECRET: env.WEBHOOK_SECRET,
      WEBHOOK_URL: env.WEBHOOK_URL,
      ALLOWED_USER_IDS: env.ALLOWED_USER_IDS ?? "",
      PORT: "8080",
    };
  }
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    const container = getContainer(env.YT_BOT);

    if (request.method === "POST" && url.pathname === "/telegram") {
      const secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token");
      if (secret !== env.WEBHOOK_SECRET) {
        return new Response("forbidden", { status: 403 });
      }
      return container.fetch(request);
    }

    // Buka URL Worker di browser = bangunkan container,
    // sehingga bot mendaftarkan webhook ke Telegram.
    await container.fetch(request).catch(() => {});
    return new Response("ok");
  },
};
