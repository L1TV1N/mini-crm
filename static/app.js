"use strict";

// ───────────── утилиты ─────────────
const $ = (s, r = document) => r.querySelector(s);
const STATUS = { new: "Новый", in_work: "В работе", won: "Успех", lost: "Отказ" };
const STATUS_ORDER = ["new", "in_work", "won", "lost"];
const SRC = { bot: "Бот", telegram: "Telegram", manual: "Вручную", web: "Сайт" };
const VIEWS = { leads: "Лиды", board: "Доска", stats: "Сводка", settings: "Настройки" };
const TZ = -new Date().getTimezoneOffset();
const MONTHS = ["янв", "фев", "мар", "апр", "мая", "июн", "июл", "авг", "сен", "окт", "ноя", "дек"];

/** Создаёт элемент без innerHTML: весь пользовательский текст попадает в страницу только как текст. */
function h(tag, attrs, ...kids) {
  const e = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v == null || v === false) continue;
    if (k === "class") e.className = v;
    else if (k === "text") e.textContent = v;
    else if (k === "vars") for (const [n, x] of Object.entries(v)) e.style.setProperty(n, x);
    else if (k.startsWith("on")) e.addEventListener(k.slice(2), v);
    else if (k === "for") e.htmlFor = v;
    else if (k === "list" || k.includes("-")) e.setAttribute(k, v);
    else if (k in e) e[k] = v;
    else e.setAttribute(k, v);
  }
  for (const kid of kids.flat()) if (kid != null && kid !== false) e.append(kid.nodeType ? kid : String(kid));
  return e;
}

async function api(method, url, body) {
  const raw = typeof body === "string" || body instanceof ArrayBuffer;
  const r = await fetch(url, {
    method, headers: { "Content-Type": raw ? "text/csv" : "application/json" },
    body: body === undefined ? undefined : raw ? body : JSON.stringify(body),
  });
  const data = await r.json().catch(() => ({}));
  if (r.status === 401 && !url.startsWith("/api/login")) { showLogin(); throw new Error("Нужен вход"); }
  if (!r.ok) throw new Error(data.error || "Ошибка " + r.status);
  return data;
}

function toast(text, opts = {}) {
  const t = h("div", { class: "toast" + (opts.bad ? " bad" : "") + (opts.onclick ? " click" : ""), text });
  if (opts.onclick) t.onclick = () => { opts.onclick(); t.remove(); };
  $("#toasts").append(t);
  setTimeout(() => t.remove(), opts.ms || 4500);
}
const fail = (e) => toast(e.message, { bad: true, ms: 6000 });

const pad = (n) => String(n).padStart(2, "0");
const isoDay = (d) => `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
const TODAY = () => isoDay(new Date());
function ago(iso) {
  const d = new Date(iso), n = new Date();
  if (isoDay(d) === isoDay(n)) return `${pad(d.getHours())}:${pad(d.getMinutes())}`;
  if (isoDay(d) === isoDay(new Date(n - 864e5))) return "вчера";
  return `${d.getDate()} ${MONTHS[d.getMonth()]}`;
}
const fullDate = (iso) => { const d = new Date(iso); return `${d.getDate()} ${MONTHS[d.getMonth()]}, ${pad(d.getHours())}:${pad(d.getMinutes())}`; };
const dayLabel = (s) => { const [, m, d] = s.split("-"); return `${+d} ${MONTHS[+m - 1]}`; };
const money = (n) => new Intl.NumberFormat("ru-RU").format(n) + " ₽";
/** Сумма для крупной плитки: число крупно, единица мелко («1,9» + «млн ₽»), полная сумма в подсказке. */
function moneyBig(n) {
  const [num, unit] = n >= 1e6 ? [(n / 1e6).toFixed(1).replace(".", ",").replace(",0", ""), "млн ₽"]
    : n >= 1e3 ? [String(Math.round(n / 1e3)), "тыс ₽"] : [String(n), "₽"];
  return h("i", { title: money(n) }, num, h("small", { text: unit }));
}

function textOn(hex) {
  const n = parseInt(hex.slice(1), 16), [r, g, b] = [n >> 16, (n >> 8) & 255, n & 255];
  return 0.299 * r + 0.587 * g + 0.114 * b > 160 ? "#141412" : "#fff";
}
function tagEl(t, onRemove) {
  const el = h("span", { class: "tag", vars: { "--c": t.color, "--tc": textOn(t.color) } }, t.name);
  if (onRemove) el.append(h("button", { type: "button", title: "Убрать тег", text: "×", onclick: onRemove }));
  return el;
}
function dueEl(l) {
  if (!l.due_at) return null;
  const open = l.status === "new" || l.status === "in_work";
  const cls = open && l.due_at < TODAY() ? "over" : open && l.due_at === TODAY() ? "today" : "";
  return h("span", { class: "due " + cls, text: l.due_at === TODAY() ? "сегодня" : dayLabel(l.due_at) });
}

// ───────────── состояние и маршрутизация ─────────────
const S = { view: "leads", f: {}, q: "", leads: [], tags: [], info: null, rev: null, lastId: null, sel: new Set(),
  settings: null, stats: null };
let seq = 0, pollTimer = null;

function parseHash() {
  const [v, q] = location.hash.replace(/^#\/?/, "").split("?");
  S.view = VIEWS[v] ? v : "leads";
  S.f = Object.fromEntries(new URLSearchParams(q || ""));
}
function href(view, patch) {
  const f = { ...(view === "leads" || view === "board" ? S.f : {}), ...patch };
  const qs = new URLSearchParams(Object.entries(f).filter(([, v]) => v));
  return `#/${view}` + (qs.toString() ? "?" + qs : "");
}
const go = (view, patch) => { location.hash = href(view, patch); };
window.addEventListener("hashchange", () => { closeDrawer(true); parseHash(); S.sel.clear(); buildView(); load(); });

function filterQuery() {
  const p = new URLSearchParams({ tz: TZ });
  for (const [k, v] of Object.entries(S.f)) if (v && !(S.view === "board" && k === "status")) p.set(k, v);
  if (S.q) p.set("q", S.q);
  return p;
}

// ───────────── загрузка ─────────────
async function load(strict) {
  const my = ++seq;
  try {
    const jobs = [api("GET", "/api/tags"), api("GET", "/api/state?tz=" + TZ)];
    if (S.view === "leads" || S.view === "board") jobs.push(api("GET", "/api/leads?" + filterQuery()));
    if (S.view === "stats") jobs.push(api("GET", "/api/stats?days=30&tz=" + TZ));
    if (S.view === "settings") jobs.push(api("GET", "/api/settings"));
    const [tags, info, extra] = await Promise.all(jobs);
    if (my !== seq) return;
    S.tags = tags; S.info = info; S.rev = info.rev;
    S.lastId = Math.max(S.lastId ?? 0, info.stats.last_id);  // свои действия не должны давать тост «новый лид»
    if (S.view === "leads" || S.view === "board") S.leads = extra;
    if (S.view === "stats") S.stats = extra;
    if (S.view === "settings") S.settings = extra;
    renderRail(); renderBody();
  } catch (e) {
    if (strict) throw e;
    if (e.message !== "Нужен вход") fail(e);
  }
}

async function poll() {
  try {
    const info = await api("GET", "/api/state?tz=" + TZ);
    const prev = S.lastId;
    S.lastId = Math.max(S.lastId ?? 0, info.stats.last_id);
    const fresh = prev === null ? 0 : info.stats.last_id - prev;
    if (fresh > 1) toast(`Новых лидов: ${fresh}`, { onclick: () => go("leads", { status: "new" }) });
    else if (fresh === 1) {
      api("GET", "/api/leads/" + info.stats.last_id).then((l) =>
        toast(`Новый лид: ${l.name} · ${SRC[l.source]}`, { onclick: () => openDrawer(l.id) })).catch(() => {});
    }
    if (info.rev !== S.rev && !document.hidden) load();
    else {
      // перерисовываем боковую панель только если что-то изменилось, иначе пропадает фокус и наведение
      const changed = JSON.stringify([info.stats, info.bot]) !== JSON.stringify([S.info.stats, S.info.bot]);
      S.info = info; if (changed) renderRail();
    }
  } catch { /* сеть моргнула, попробуем в следующий раз */ }
}

// ───────────── вход ─────────────
function showLogin() {
  clearInterval(pollTimer); pollTimer = null;
  closeDrawer(true); $(".bulk")?.remove();
  const err = h("div", { class: "err" });
  const pw = h("input", { class: "input", type: "password", autocomplete: "current-password", required: true, autofocus: true });
  $("#root").replaceChildren(h("div", { class: "login" },
    h("div", { class: "login-art" },
      h("h1", {}, "Мини", h("br"), "CRM", h("i")),
      h("p", { text: "Заявки агентства из Telegram, с сайта и вручную. Теги, доска, переписка с клиентом." })),
    h("div", { class: "login-form" }, h("form", {
      onsubmit: async (e) => {
        e.preventDefault();
        try { await api("POST", "/api/login", { password: pw.value }); boot(); }
        catch (x) { err.textContent = x.message; pw.select(); }
      } },
      h("h2", { text: "Вход" }),
      h("div", { class: "field" }, h("label", { class: "lbl", for: "pw" }, "Пароль"), Object.assign(pw, { id: "pw" })),
      h("button", { class: "btn accent", text: "Войти" }), err))));
  pw.focus();
}

// ───────────── оболочка ─────────────
function theme() {
  const t = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = t;
  try { localStorage.setItem("theme", t); } catch { /* приватный режим */ }
}

function buildShell() {
  $("#root").replaceChildren(h("div", { class: "shell" },
    h("aside", { class: "rail", id: "rail" }), h("main", { class: "main", id: "main" })));
}

function renderRail() {
  const rail = $("#rail"); if (!rail) return;
  const { info } = S, st = info.stats;
  const nav = h("nav", { class: "nav" }, Object.entries(VIEWS).map(([v, label], i) =>
    h("a", { href: v === "leads" || v === "board" ? href(v, {}) : "#/" + v, class: S.view === v ? "on" : "" },
      h("small", { text: "0" + (i + 1) }), label,
      v === "leads" && st.new ? h("span", { class: "badge", text: st.new }) : null)));
  const kids = [h("a", { class: "brand", href: "#/leads" }, h("i"), "Мини-CRM"), nav];
  if (S.view === "leads" || S.view === "board") {
    kids.push(h("div", { class: "rail-tags" },
      h("span", { class: "lbl" }, "Теги", h("a", { href: "#/settings", text: "изменить" })),
      h("a", { class: "rtag" + (S.f.tag ? "" : " on"), href: href(S.view, { tag: "" }) }, h("i", { vars: { "--c": "#8E8B81" } }), "Все теги",
        h("b", { text: st.total })),
      S.tags.map((t) => h("a", { class: "rtag" + (S.f.tag === t.name ? " on" : ""), href: href(S.view, { tag: t.name }) },
        h("i", { vars: { "--c": t.color } }), t.name, h("b", { text: t.count })))));
  } else kids.push(h("div", { class: "spacer" }));
  const b = info.bot;
  kids.push(h("div", { class: "rail-foot" },
    h("div", { class: "bot-line" + (b.running ? " ok" : ""), title: b.error || "" }, h("i"),
      h("span", { text: b.running ? "бот @" + b.username : b.enabled ? "бот не отвечает" : "бот выключен" })),
    h("div", { class: "rail-actions" },
      h("button", { class: "btn sm", onclick: theme, text: "Тема" }),
      h("button", { class: "btn sm", text: "Выйти", onclick: async () => { await api("POST", "/api/logout"); location.reload(); } }))));
  rail.replaceChildren(...kids);
  document.title = (st.unread ? `(${st.unread}) ` : "") + "Мини-CRM";
}

function head(title, count, ...right) {
  return h("div", { class: "head" }, h("h1", {}, title, count != null ? h("sup", { id: "headCount", text: count }) : null),
    h("div", { class: "spacer" }), ...right);
}

function buildView() {
  const main = $("#main"); if (!main) return;
  if (S.view === "leads" || S.view === "board") {
    main.replaceChildren(
      head(VIEWS[S.view], S.leads.length || "", h("button", { class: "btn accent", onclick: () => openDrawer(null), text: "+ Новый лид" })),
      filterBar(), h("div", { id: "body" }));
  } else main.replaceChildren(h("div", { id: "body" }));
}

function renderBody() {
  const body = $("#body"); if (!body) return;
  const c = $("#headCount"); if (c) c.textContent = S.leads.length || "";
  const r = $("#resetBtn"); if (r) r.hidden = !filtersActive();
  ({ leads: renderTable, board: renderBoard, stats: renderStats, settings: renderSettings })[S.view](body);
  renderBulk();
}

// ───────────── фильтры ─────────────
function selectEl(key, options, placeholder) {
  return h("select", { class: "input sm", "aria-label": placeholder, onchange: (e) => go(S.view, { [key]: e.target.value }) },
    h("option", { value: "", text: placeholder }),
    options.map(([v, l]) => h("option", { value: v, text: l, selected: S.f[key] === v })));
}

function filterBar() {
  const bar = h("div", { class: "filters" });
  const search = h("input", { class: "input sm search", type: "search", placeholder: "Поиск   /", value: S.q, id: "search",
    oninput: (e) => { clearTimeout(search._t); search._t = setTimeout(() => { S.q = e.target.value.trim(); load(); }, 220); } });
  bar.append(search);
  if (S.view === "leads") {
    bar.append(h("div", { class: "seg" }, [["", "Все"], ...STATUS_ORDER.map((s) => [s, STATUS[s]])].map(([v, l]) =>
      h("button", { type: "button", class: (S.f.status || "") === v ? "on" : "", text: l, onclick: () => go("leads", { status: v }) }))));
  }
  const fx = h("div", { class: "fx" });
  fx.append(...[
    h("div", { class: "only-m" }, selectEl("tag", S.tags.map((t) => [t.name, t.name]), "Любой тег")),
    selectEl("source", Object.entries(SRC), "Любой источник"),
    selectEl("assignee", [["-", "Не назначен"], ...(S.info.managers || []).map((m) => [m, m])], "Любой ответственный"),
    selectEl("due", [["overdue", "Просрочено"], ["today", "Сегодня"], ["week", "На неделе"], ["none", "Без срока"]], "Любой срок"),
    S.view === "leads" ? selectEl("sort", [["created", "Новые сверху"], ["due", "По сроку"], ["amount", "По сумме"], ["name", "По имени"]], "Недавно обновлённые") : null,
    h("label", { class: "chk" }, h("input", { type: "checkbox", checked: S.f.unread === "1", onchange: (e) => go(S.view, { unread: e.target.checked ? "1" : "" }) }), "Непрочитанные")].filter(Boolean));
  bar.append(h("button", { type: "button", class: "btn sm only-m", text: "Фильтры", onclick: () => bar.classList.toggle("open") }), fx);
  bar.append(h("button", { type: "button", id: "resetBtn", class: "btn quiet sm", text: "Сбросить", onclick: resetFilters,
    hidden: !filtersActive() }));
  bar.append(h("div", { class: "spacer" }),
    h("a", { class: "btn sm", href: "/api/export.csv", text: "Экспорт CSV",
      onclick: (e) => { e.currentTarget.href = "/api/export.csv?" + filterQuery(); } }));
  return bar;
}

// ───────────── список ─────────────
const filtersActive = () => Object.values(S.f).some(Boolean) || !!S.q;

function resetFilters() {
  S.q = "";
  const target = "#/" + S.view;
  if (location.hash === target) { buildView(); load(); } else location.hash = target;
}

function emptyState() {
  const filtered = filtersActive();
  return h("div", { class: "empty" },
    h("b", { text: filtered ? "Ничего не нашлось" : "Пока ни одного лида" }),
    h("p", { text: filtered ? "Попробуйте убрать часть фильтров." : "Они появятся, когда кто-нибудь напишет боту, заполнит форму на сайте или вы добавите лида вручную." }),
    filtered ? h("button", { class: "btn", text: "Сбросить фильтры", onclick: resetFilters })
      : h("button", { class: "btn accent", text: "+ Новый лид", onclick: () => openDrawer(null) }));
}

function renderTable(body) {
  if (!S.leads.length) return body.replaceChildren(emptyState());
  const all = h("input", { type: "checkbox", "aria-label": "Выбрать всех", checked: S.leads.every((l) => S.sel.has(l.id)),
    onchange: (e) => { S.leads.forEach((l) => (e.target.checked ? S.sel.add(l.id) : S.sel.delete(l.id))); renderBody(); } });
  const rows = S.leads.map((l) => {
    const box = h("input", { type: "checkbox", checked: S.sel.has(l.id), "aria-label": "Выбрать",
      onclick: (e) => e.stopPropagation(), onchange: (e) => { e.target.checked ? S.sel.add(l.id) : S.sel.delete(l.id); renderBody(); } });
    const shown = l.tags.slice(0, 3);
    return h("div", { class: "tr" + (l.unread ? " unread" : "") + (S.sel.has(l.id) ? " sel" : ""), tabIndex: 0, role: "button",
      onclick: () => openDrawer(l.id), onkeydown: (e) => { if (e.key === "Enter" && e.target === e.currentTarget) openDrawer(l.id); } },
      box, h("span", { class: "num", text: l.id }),
      h("div", { class: "who" }, h("b", {}, l.name, l.unread ? h("span", { class: "ub", text: l.unread }) : null), h("span", { text: l.contact || "контакта нет" })),
      h("div", { class: "req" }, l.request || h("span", { class: "mute", text: "без описания" }),
        l.last_message && l.last_message !== l.request ? h("small", { text: "↳ " + l.last_message }) : null),
      h("div", { class: "tags" }, shown.map((t) => tagEl(t)), l.tags.length > 3 ? h("span", { class: "tag more", text: "+" + (l.tags.length - 3) }) : null),
      h("div", {}, h("span", { class: "st " + l.status, text: STATUS[l.status] })),
      h("div", {}, dueEl(l), l.assignee ? h("span", { class: "sub", text: l.assignee }) : null, l.amount ? h("span", { class: "sub", text: money(l.amount) }) : null),
      h("div", { class: "when", text: ago(l.updated_at) }));
  });
  // h() пропускает null, а replaceChildren(null) напечатал бы слово «null» прямо над таблицей
  body.replaceChildren(h("div", {}, S.leads.length >= 1000 ? h("p", { class: "mute", text: "Показаны первые 1000 лидов, уточните фильтр. В экспорт попадут все." }) : null, h("div", { class: "table" },
    h("div", { class: "tr th" }, all, h("span", { text: "№" }), h("span", { text: "Клиент" }), h("span", { text: "Запрос" }),
      h("span", { text: "Теги" }), h("span", { text: "Статус" }), h("span", { text: "Срок" }), h("span", { class: "r", text: "Обновлён" })),
    rows)));
}

// ───────────── доска ─────────────
function renderBoard(body) {
  const cols = STATUS_ORDER.map((st) => {
    const items = S.leads.filter((l) => l.status === st);
    const sum = items.reduce((a, l) => a + l.amount, 0);
    const col = h("section", { class: "col", vars: { "--c": `var(--st-${st})` } },
      h("div", { class: "col-head" }, h("h3", { text: STATUS[st] }), h("b", { text: items.length }), sum ? h("span", { text: money(sum) }) : null));
    col.addEventListener("dragover", (e) => { e.preventDefault(); col.classList.add("over"); });
    col.addEventListener("dragleave", (e) => { if (!col.contains(e.relatedTarget)) col.classList.remove("over"); });
    col.addEventListener("drop", async (e) => {
      e.preventDefault(); col.classList.remove("over");
      const id = +e.dataTransfer.getData("text/plain"), lead = S.leads.find((l) => l.id === id);
      if (!lead || lead.status === st) return;
      lead.status = st; renderBody();
      try { await api("PUT", "/api/leads/" + id, { status: st }); toast(`№${id} → ${STATUS[st]}`); load(); } catch (x) { fail(x); load(); }
    });
    items.forEach((l) => {
      const card = h("article", { class: "card" + (l.unread ? " unread" : ""), draggable: true, tabIndex: 0,
        onclick: () => openDrawer(l.id), onkeydown: (e) => { if (e.key === "Enter") openDrawer(l.id); },
        ondragstart: (e) => { e.dataTransfer.setData("text/plain", l.id); e.dataTransfer.effectAllowed = "move"; card.classList.add("drag"); },
        ondragend: () => card.classList.remove("drag") },
        h("h4", { text: l.name }), h("div", { class: "c", text: l.contact || "контакта нет" }),
        l.request ? h("p", { text: l.request }) : null,
        h("div", { class: "tags" }, l.tags.slice(0, 4).map((t) => tagEl(t))),
        h("div", { class: "card-foot" }, l.due_at ? dueEl(l) : null, l.assignee ? h("span", { text: l.assignee }) : null,
          l.amount ? h("span", { class: "money", text: money(l.amount) }) : null),
        h("select", { class: "input sm mv", "aria-label": "Статус", onclick: (e) => e.stopPropagation(),
          onchange: async (e) => { try { await api("PUT", "/api/leads/" + l.id, { status: e.target.value }); load(); } catch (x) { fail(x); load(); } } },
          STATUS_ORDER.map((k) => h("option", { value: k, text: STATUS[k], selected: k === l.status }))));
      col.append(card);
    });
    if (!items.length) col.append(h("div", { class: "mute", text: "Пусто. Перетащите карточку сюда." }));
    return col;
  });
  body.replaceChildren(S.leads.length || Object.values(S.f).some(Boolean) || S.q ? h("div", { class: "board" }, cols) : emptyState());
}

// ───────────── массовые действия ─────────────
async function bulk(action, value = "") {
  try {
    const r = await api("POST", "/api/leads/bulk", { ids: [...S.sel], action, value });
    toast(`Готово: ${r.count}`); if (action === "delete") S.sel.clear(); load();
  } catch (e) { fail(e); }
}
function renderBulk() {
  $(".bulk")?.remove();
  const ids = new Set(S.leads.map((l) => l.id));
  for (const id of [...S.sel]) if (!ids.has(id)) S.sel.delete(id);
  if (!S.sel.size || S.view !== "leads") return;
  const tagIn = h("input", { class: "input", placeholder: "Тег", list: "tagList", maxLength: 32, onkeydown: (e) => { if (e.key === "Enter") addTag(); } });
  const addTag = () => { if (tagIn.value.trim()) bulk("add_tag", tagIn.value.trim()); };
  const sel = (ph, opts, fn) => h("select", { class: "input", "aria-label": ph, onchange: (e) => { if (e.target.value !== "") fn(e.target.value === "-" ? "" : e.target.value); e.target.value = ""; } },
    h("option", { value: "", text: ph }), opts.map(([v, l]) => h("option", { value: v, text: l })));
  document.body.append(h("div", { class: "bulk" }, h("b", { text: "Выбрано " + S.sel.size }),
    sel("Статус…", STATUS_ORDER.map((s) => [s, STATUS[s]]), (v) => bulk("status", v)),
    sel("Ответственный…", [["-", "Снять"], ...(S.info.managers || []).map((m) => [m, m])], (v) => bulk("assignee", v)),
    tagIn, h("button", { class: "btn sm", text: "+ Тег", onclick: addTag }),
    h("datalist", { id: "tagList" }, S.tags.map((t) => h("option", { value: t.name }))),
    h("button", { class: "btn sm", text: "Снять выбор", onclick: () => { S.sel.clear(); renderBody(); } }),
    h("button", { class: "btn sm danger", text: "Удалить", onclick: () => { if (confirm(`Удалить лидов: ${S.sel.size}? Это необратимо.`)) bulk("delete"); } })));
}

// ───────────── карточка лида ─────────────
let D = null; // состояние открытой карточки

function closeDrawer(force) {
  if (!D) return true;
  if (!force && D.dirty && !confirm("Закрыть без сохранения изменений?")) return false;
  $(".drawer")?.remove(); D = null;
  return true;
}

async function openDrawer(id) {
  try {
    const lead = id ? await api("GET", "/api/leads/" + id) : null;
    if (!closeDrawer()) return;
    D = { id, lead, tags: lead ? lead.tags.map((t) => t.name) : [], tab: lead && (lead.messages.length || (lead.source === "bot" && lead.tg_chat_id) || lead.business_connection_id) ? "msgs" : "log", dirty: false };
    drawDrawer();
    if (lead && lead.unread) { await api("PUT", "/api/leads/" + id, { read: true }); load(); }
  } catch (e) { fail(e); }
}

function tagColor(name) { return (S.tags.find((t) => t.name.toLowerCase() === name.toLowerCase()) || {}).color || "#FF4F1A"; }

function drawDrawer() {
  $(".drawer")?.remove();
  const { lead } = D, err = h("div", { class: "err" });
  const dirty = () => { D.dirty = true; };
  const f = {
    name: h("input", { class: "input", required: true, maxLength: 100, value: lead?.name || "", oninput: dirty, id: "f-name" }),
    contact: h("input", { class: "input", maxLength: 200, value: lead?.contact || "", placeholder: "телефон, e-mail или @username", oninput: dirty }),
    request: h("textarea", { class: "input", rows: 3, maxLength: 2000, value: lead?.request || "", oninput: dirty }),
    assignee: h("select", { class: "input", onchange: dirty }, h("option", { value: "", text: "Не назначен" }),
      [...new Set([...(S.info.managers || []), lead?.assignee].filter(Boolean))].map((m) => h("option", { value: m, text: m, selected: lead?.assignee === m }))),
    due: h("input", { class: "input", type: "date", value: lead?.due_at || "", oninput: dirty }),
    amount: h("input", { class: "input", type: "number", min: 0, step: 1000, value: lead?.amount || "", placeholder: "0", oninput: dirty }),
    note: h("textarea", { class: "input", rows: 2, maxLength: 4000, value: lead?.note || "", placeholder: "Внутренняя заметка, клиент её не видит", oninput: dirty }),
  };
  const status = lead?.status || "new";
  const statuses = h("div", { class: "statuses" }, STATUS_ORDER.map((s) =>
    h("label", {}, h("input", { type: "radio", name: "status", value: s, checked: s === status, onchange: dirty }), h("span", { text: STATUS[s] }))));
  const tagsBox = h("div", { class: "tags" });
  const tagIn = h("input", { class: "input", list: "tagSuggest", maxLength: 32, placeholder: "Тег и Enter",
    onkeydown: (e) => { if (e.key === "Enter" || e.key === ",") { e.preventDefault(); addTag(tagIn.value); tagIn.value = ""; } },
    onchange: () => { if (tagIn.value) { addTag(tagIn.value); tagIn.value = ""; } } });
  function drawTags() {
    tagsBox.replaceChildren(...D.tags.map((n, i) => tagEl({ name: n, color: tagColor(n) }, () => { D.tags.splice(i, 1); D.dirty = true; drawTags(); })));
  }
  function addTag(raw) {
    const n = raw.trim().replace(/\s+/g, " ");
    if (n && !D.tags.some((x) => x.toLowerCase() === n.toLowerCase())) { D.tags.push(n); D.dirty = true; }
    drawTags();
  }
  drawTags();

  const lower = h("div", { id: "lower" });
  const sheet = h("form", { class: "sheet", onsubmit: async (e) => {
    e.preventDefault(); addTag(tagIn.value); tagIn.value = "";
    const body = { name: f.name.value, contact: f.contact.value, request: f.request.value, note: f.note.value, assignee: f.assignee.value,
      due_at: f.due.value, amount: f.amount.value || 0, status: sheet.status.value, tags: D.tags };
    try {
      await api(lead ? "PUT" : "POST", lead ? "/api/leads/" + lead.id : "/api/leads", body);
      D.dirty = false; closeDrawer(true); toast(lead ? "Сохранено" : "Лид добавлен"); load();
    } catch (x) { err.textContent = x.message; }
  } },
    h("div", { class: "sheet-head" },
      h("div", {}, h("h2", { text: lead ? "Лид №" + lead.id : "Новый лид" }),
        lead ? h("div", { class: "meta", text: `${SRC[lead.source]} · создан ${fullDate(lead.created_at)}` }) : null),
      h("div", { class: "spacer" }), h("button", { type: "button", class: "btn quiet sm", text: "Закрыть", onclick: () => closeDrawer() })),
    h("div", { class: "sheet-body" },
      h("div", { class: "field" }, h("label", { class: "lbl", for: "f-name" }, "Имя"), f.name),
      h("div", { class: "field" }, h("label", { class: "lbl" }, "Контакт"), f.contact),
      h("div", { class: "field" }, h("label", { class: "lbl" }, "Запрос"), f.request),
      h("div", { class: "field" }, h("span", { class: "lbl" }, "Статус"), statuses),
      h("div", { class: "field tag-edit" }, h("span", { class: "lbl" }, "Теги"), tagsBox, tagIn,
        h("datalist", { id: "tagSuggest" }, S.tags.map((t) => h("option", { value: t.name })))),
      h("div", { class: "row field" }, h("div", {}, h("label", { class: "lbl" }, "Ответственный"), f.assignee),
        h("div", {}, h("label", { class: "lbl" }, "Срок"), f.due), h("div", {}, h("label", { class: "lbl" }, "Сумма, ₽"), f.amount)),
      h("div", { class: "field" }, h("label", { class: "lbl" }, "Заметка"), f.note),
      lead ? lower : null, err),
    h("div", { class: "sheet-foot" },
      lead ? h("button", { type: "button", class: "btn danger", text: "Удалить", onclick: async () => {
        if (!confirm("Удалить лида вместе с перепиской?")) return;
        try { await api("DELETE", "/api/leads/" + lead.id); closeDrawer(true); toast("Удалено"); load(); } catch (x) { fail(x); } } }) : null,
      h("div", { class: "spacer" }), h("button", { class: "btn accent", text: lead ? "Сохранить" : "Добавить лида" })));
  document.body.append(h("div", { class: "drawer" }, h("div", { class: "scrim", onclick: () => closeDrawer() }), sheet));
  if (lead) { drawLower(lower); if (lead.unread) setTimeout(() => lower.scrollIntoView({ block: "start" })); } else f.name.focus();
}

function drawLower(box) {
  const { lead } = D;
  const tab = (id, label) => h("button", { type: "button", class: D.tab === id ? "on" : "", text: label, onclick: () => { D.tab = id; drawLower(box); } });
  let content;
  if (D.tab === "msgs") {
    const canReply = (lead.source === "bot" && lead.tg_chat_id) || (lead.source === "telegram" && lead.business_connection_id);
    const list = h("div", { class: "msgs" }, lead.messages.map((m) => h("div", { class: "msg " + m.direction }, m.text, h("small", { text: fullDate(m.ts) }))));
    const ta = h("textarea", { class: "input", rows: 3, maxLength: 4000, placeholder: "Ответ клиенту. Уйдёт в Telegram прямо отсюда", disabled: !canReply });
    const send = h("button", { type: "button", class: "btn accent sm", text: "Отправить", disabled: !canReply, onclick: async () => {
      if (!ta.value.trim()) return;
      send.disabled = true;
      try {
        const upd = await api("POST", `/api/leads/${lead.id}/reply`, { text: ta.value });
        const form = box.closest("form");
        if (form && form.status.value === lead.status) form.status.value = upd.status;  // если человек сам не менял статус
        D.lead = upd; toast("Отправлено"); drawLower(box); load();
      } catch (x) { fail(x); send.disabled = false; }
    } });
    ta.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) send.click(); });
    content = h("div", {}, lead.messages.length ? list : h("p", { class: "mute", text: "Сообщений пока нет." }),
      h("div", { class: "composer" }, ta, h("div", { class: "row" }, h("small", { text: canReply ? "Ctrl+Enter — отправить" : "Для этого источника ответ из CRM недоступен: у лида нет Telegram-чата." }), send)));
    setTimeout(() => { list.scrollTop = list.scrollHeight; });
  } else if (D.tab === "log") {
    content = h("div", { class: "log" }, lead.events.map((e) => h("div", {}, h("time", { text: fullDate(e.ts) }), h("span", { text: e.text }))));
  } else {
    content = lead.related.length ? h("div", {}, lead.related.map((r) =>
      h("div", { class: "rel", onclick: () => { D.dirty = false; openDrawer(r.id); } }, h("b", { text: `№${r.id} ${r.name}` }),
        h("span", { class: "st " + r.status, text: STATUS[r.status] }), h("span", { class: "mute", text: `${SRC[r.source]} · ${fullDate(r.created_at)}` }))))
      : h("p", { class: "mute", text: "Других заявок от этого человека нет." });
  }
  box.replaceChildren(h("div", { class: "tabs" }, tab("msgs", `Переписка${lead.messages.length ? " · " + lead.messages.length : ""}`), tab("log", "История"),
    tab("rel", `Связанные${lead.related.length ? " · " + lead.related.length : ""}`)), content);
}

// ───────────── сводка ─────────────
function renderStats(body) {
  const s = S.stats; if (!s) return;
  const kpi = (v, label, extra) => h("div", { class: "kpi" + (extra ? " " + extra : "") }, h("b", {}, v), h("span", { text: label }));
  const max = Math.max(1, ...s.by_day.map((d) => d.count));
  const bars = h("div", { class: "bars" }, s.by_day.map((d, i) =>
    h("div", { class: (d.count ? "" : "zero ") + (i === s.by_day.length - 1 ? "today" : ""), title: `${dayLabel(d.date)}: ${d.count}`,
      vars: d.count ? { height: Math.max(4, (d.count / max) * 100) + "%" } : {} })));
  const hbars = (rows, link) => rows.map(([label, n, color, to]) => h(to ? "a" : "div", { class: "hbar", href: to },
    h("span", { text: label }), h("div", { class: "track" }, h("i", { vars: { "--c": color || "var(--ink)", width: (n / Math.max(1, ...rows.map((r) => r[1]))) * 100 + "%" } })), h("b", { text: n })));
  body.replaceChildren(
    head("Сводка", null, h("span", { class: "hint", text: "Показатели за всё время, график за 30 дней" })),
    h("div", { class: "kpis" },
      kpi(s.total, "Лидов всего"), kpi(s.new, "Новых, без реакции", s.new ? "hot" : ""), kpi(s.by_status.in_work, "В работе"),
      kpi(s.conversion == null ? "—" : [s.conversion, h("small", { text: "%" })], "Конверсия: успех / (успех + отказ)"),
      kpi(moneyBig(s.pipeline), "В воронке"), kpi(moneyBig(s.amount.won), "Выиграно"),
      s.overdue ? kpi(s.overdue, "Просрочено", "hot") : null),
    h("div", { class: "grid2" },
      h("section", { class: "panel" }, h("h2", {}, "Новые лиды по дням", h("small", { text: `за 30 дней: ${s.period_total}` })), bars,
        h("div", { class: "axis" }, h("span", { text: dayLabel(s.by_day[0].date) }), h("span", { text: dayLabel(s.by_day[15].date) }), h("span", { text: "сегодня" }))),
      h("section", { class: "panel" }, h("h2", { text: "Источники" }),
        hbars(Object.entries(s.by_source).map(([k, n]) => [SRC[k], n, null, "#/leads?source=" + k])), h("div", { class: "gap" }),
        h("h2", { text: "Статусы" }), hbars(STATUS_ORDER.map((k) => [STATUS[k], s.by_status[k], `var(--st-${k})`, "#/leads?status=" + k])))),
    h("div", { class: "grid2" },
      h("section", { class: "panel" }, h("h2", { text: "Популярные теги" }),
        s.top_tags.length ? hbars(s.top_tags.map((t) => [t.name, t.count, t.color, "#/leads?tag=" + encodeURIComponent(t.name)])) : h("p", { class: "mute", text: "Тегов пока нет." })),
      h("section", { class: "panel" }, h("h2", { text: "По ответственным" }),
        h("table", { class: "mgr" }, h("thead", {}, h("tr", {}, h("th", { text: "Кто" }), h("th", { text: "Лидов" }), h("th", { text: "Успех" }))),
          h("tbody", {}, s.by_assignee.map((a) => h("tr", {}, h("td", { text: a.name }), h("td", { text: a.n }), h("td", { text: a.won || 0 }))))))));
}

// ───────────── настройки ─────────────
async function copy(text) {
  try { await navigator.clipboard.writeText(text); toast("Скопировано"); }
  catch { toast("Не удалось скопировать, выделите текст вручную", { bad: true }); }
}
const codeBox = (text) => h("div", { class: "code" }, h("span", { text }), h("button", { type: "button", class: "btn sm", text: "Копировать", onclick: () => copy(text) }));

function renderSettings(body) {
  const s = S.settings; if (!s) return;
  const bot = S.info.bot, user = bot.username || s.bot.username;
  const section = (n, title, ...kids) => h("section", { class: "set" }, h("b", { text: n }), h("div", {}, h("h2", { text: title }), ...kids));
  const redo = async (fn) => { try { await fn(); await load(); } catch (e) { fail(e); } };

  // 01 бот и метки
  const label = h("input", { class: "input", placeholder: "метка кампании, например instagram", maxLength: 32 });
  const link = h("div", {});
  const drawLink = () => {
    const l = label.value.replace(/[^\w.\-]/g, "");
    link.replaceChildren(user ? codeBox(`https://t.me/${user}` + (l ? `?start=${l}` : "")) : h("p", { class: "mute", text: "Ссылка появится, когда бот запустится." }));
  };
  label.oninput = drawLink; drawLink();
  const s1 = section("01", "Telegram-бот",
    h("div", { class: "stat-pill" + (bot.running ? " ok" : ""), text: bot.running ? "Работает: @" + bot.username : "Не работает: " + (bot.error || "нет токена") }),
    h("p", { text: "Человек пишет боту, тот спрашивает имя, контакт и запрос. Лид появляется с тегом «Бот». Потом человек может дописывать в тот же чат, а вы отвечать из карточки лида." }),
    h("span", { class: "lbl", text: "Ссылка на бота с меткой" }), label, link,
    h("p", { text: "Метка превращается в тег: поставьте разные ссылки в Instagram, на сайт и в визитку, и в CRM будет видно, откуда пришёл каждый лид." }));

  // 02 уведомления
  const n = s.notify;
  const s2 = section("02", "Уведомления в Telegram",
    h("div", { class: "stat-pill" + (n.connected ? " ok" : ""), text: n.connected ? "Подключены" : "Не подключены" }),
    h("p", { text: "Бот напишет вам, когда придёт новый лид или клиент ответит. Откройте бота со своего аккаунта и отправьте ему команду:" }),
    codeBox("/connect " + n.code),
    h("div", { class: "inline" },
      h("button", { class: "btn sm", text: "Новый код", onclick: () => redo(() => api("POST", "/api/notify/code")) }),
      n.connected ? h("button", { class: "btn sm danger", text: "Отключить уведомления", onclick: () => redo(() => api("POST", "/api/notify/disconnect")) }) : null),
    h("p", { text: "Код одноразовый: после подключения он меняется." }));

  // 03 Business
  const s3 = section("03", "Ваш обычный Telegram",
    h("p", { text: "Через Telegram Business (нужен Premium) этот же бот читает личные сообщения вашего аккаунта. Каждый новый собеседник становится лидом с тегом «Telegram», а отвечать можно прямо из CRM." }),
    h("ol", {}, h("li", { text: "Один раз в @BotFather: /mybots → ваш бот → Bot Settings → Business Mode → включить. Без этого бота нельзя подключить к аккаунту" }),
      h("li", { text: "Telegram → Настройки → Telegram для бизнеса → Чат-боты" }), h("li", { text: user ? "Выберите @" + user : "Выберите бота" }),
      h("li", { text: "Дайте доступ к сообщениям и возможность отвечать" }), h("li", { text: "Напишите себе с другого аккаунта: лид появится через пару секунд" })),
    s.business.length ? s.business.map((b) => h("div", { class: "list-row" }, h("span", { class: "k", text: b.username ? "@" + b.username : "ID " + b.user_id }), h("span", { class: "mute", text: b.enabled ? "подключён" : "отключён" }))) : h("p", { text: "Подключённых аккаунтов пока нет." }));

  // 04 форма
  const url = location.origin + "/form";
  const s4 = section("04", "Форма заявки для сайта",
    h("p", { text: "Публичная страница без входа. Заявки с неё приходят с тегом «Сайт». Добавьте ?ref=метка, чтобы пометить источник." }),
    codeBox(url), codeBox(`<iframe src="${url}" width="100%" height="560" style="border:0"></iframe>`),
    h("a", { class: "btn sm", href: "/form", target: "_blank", rel: "noopener", text: "Открыть форму" }));

  // 05 правила
  const kw = h("input", { class: "input", placeholder: "слово в запросе, например сайт", maxLength: 60 });
  const tg = h("input", { class: "input", placeholder: "тег", maxLength: 32 });
  const s5 = section("05", "Автотеги",
    h("p", { text: "Если в имени или запросе нового лида есть слово, он сам получит тег. Регистр не важен." }),
    s.rules.map((r) => h("div", { class: "list-row" }, h("span", { class: "k", text: r.keyword }), h("span", { class: "arrow", text: "→" }), h("span", { text: r.tag }),
      h("div", { class: "spacer" }), h("button", { class: "btn quiet sm", text: "Удалить", onclick: () => redo(() => api("DELETE", "/api/rules/" + r.id)) }))),
    h("form", { class: "inline mt", onsubmit: (e) => { e.preventDefault(); redo(() => api("POST", "/api/rules", { keyword: kw.value, tag: tg.value })); } },
      kw, tg, h("button", { class: "btn sm", text: "Добавить правило" })));

  // 06 команда
  const mn = h("input", { class: "input", placeholder: "имя менеджера", maxLength: 40 });
  const s6 = section("06", "Команда",
    h("p", { text: "Менеджеров можно назначать ответственными за лида, фильтровать по ним и смотреть, кто сколько закрыл." }),
    s.managers.map((m) => h("div", { class: "list-row" }, h("span", { class: "k", text: m.name }), h("div", { class: "spacer" }),
      h("button", { class: "btn quiet sm", text: "Удалить", onclick: () => { if (confirm("Удалить менеджера? Его лиды останутся без ответственного.")) redo(() => api("DELETE", "/api/managers/" + m.id)); } }))),
    h("form", { class: "inline mt", onsubmit: (e) => { e.preventDefault(); redo(() => api("POST", "/api/managers", { name: mn.value })); } },
      mn, h("button", { class: "btn sm", text: "Добавить" })));

  // 07 теги
  const s7 = section("07", "Теги",
    h("p", { text: "Переименуйте тег или перекрасьте его. Если новое имя совпадёт с другим тегом, они объединятся." }),
    S.tags.length ? S.tags.map((t) => {
      const name = h("input", { class: "input sm", value: t.name, maxLength: 32, "aria-label": "Название тега",
        onchange: () => redo(() => api("PUT", "/api/tags/" + t.id, { name: name.value })) });
      return h("div", { class: "trow" },
        h("input", { type: "color", value: t.color, "aria-label": "Цвет", onchange: (e) => redo(() => api("PUT", "/api/tags/" + t.id, { color: e.target.value })) }),
        name, h("b", { text: t.count }),
        h("button", { class: "btn quiet sm", title: "Удалить тег", text: "Удалить", onclick: () => { if (confirm(`Удалить тег «${t.name}»? С лидов он тоже пропадёт.`)) redo(() => api("DELETE", "/api/tags/" + t.id)); } }));
    }) : h("p", { text: "Тегов пока нет." }));

  // 08 данные
  const file = h("input", { type: "file", accept: ".csv,text/csv", hidden: true, onchange: async () => {
    const fl = file.files[0]; if (!fl) return;
    try { const r = await api("POST", "/api/import", await fl.arrayBuffer()); toast(`Импортировано: ${r.created}` + (r.skipped ? `, пропущено: ${r.skipped}` : "")); load(); }
    catch (e) { fail(e); } file.value = "";
  } });
  const s8 = section("08", "Данные",
    h("p", { text: "Экспорт открывается в Excel. Для импорта нужна колонка «Имя», остальные необязательны: «Контакт», «Запрос», «Теги» (через запятую). Разделитель — запятая, точка с запятой или табуляция." }),
    h("div", { class: "inline" }, h("a", { class: "btn sm", href: "/api/export.csv?tz=" + TZ, text: "Скачать все лиды, CSV" }),
      h("button", { class: "btn sm", text: "Загрузить CSV", onclick: () => file.click() }), file));

  body.replaceChildren(head("Настройки"), h("div", { class: "sets" }, s1, s2, s3, s4, s5, s6, s7, s8));
}

// ───────────── клавиши ─────────────
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") return closeDrawer();
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName) || e.ctrlKey || e.metaKey || e.altKey;
  if (typing || !S.info || D) return;
  if (e.key === "/") { const s = $("#search"); if (s) { e.preventDefault(); s.focus(); } }
  else if (e.key === "n") { e.preventDefault(); openDrawer(null); }
  else if ("1234".includes(e.key)) go(Object.keys(VIEWS)[+e.key - 1]);
});
window.addEventListener("beforeunload", (e) => { if (D?.dirty) { e.preventDefault(); } });

// ───────────── запуск ─────────────
async function boot() {
  try {
    parseHash();
    buildShell();
    await load(true);
    buildView(); renderBody();
    clearInterval(pollTimer); pollTimer = setInterval(poll, 5000);
  } catch (e) {
    if (e.message === "Нужен вход") return;
    $("#root").replaceChildren(h("div", { class: "empty fatal" }, h("b", { text: "Не удалось загрузить данные" }),
      h("p", { text: e.message }), h("button", { class: "btn accent", text: "Повторить", onclick: boot })));
  }
}
api("GET", "/api/state").then(boot).catch((e) => { if (e.message === "Нужен вход") return; boot(); });
