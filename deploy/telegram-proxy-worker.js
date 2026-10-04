// Прокси до api.telegram.org для серверов, где Telegram закрыт хостингом.
// Cloudflare Workers: создать воркер, вставить этот код, в настройках воркера добавить переменную BOT_ID
// (цифры токена до двоеточия). Адрес воркера идёт в TG_API_BASE на сервере, например https://tg.имя.workers.dev
// Воркер пропускает только запросы своего бота, поэтому открытым прокси для чужих токенов не становится.
export default {
  async fetch(request, env) {
    const url = new URL(request.url);
    if (!env.BOT_ID || !url.pathname.startsWith(`/bot${env.BOT_ID}:`)) {
      return new Response("not found", { status: 404 });
    }
    return fetch(`https://api.telegram.org${url.pathname}${url.search}`, {
      method: request.method,
      headers: { "Content-Type": request.headers.get("Content-Type") || "application/json" },
      body: request.method === "GET" || request.method === "HEAD" ? undefined : request.body,
    });
  },
};
