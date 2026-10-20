// Trading Bot phone app. Plain JavaScript, no build step.
// Every screen is built with h() and textContent, never innerHTML, so text
// from the server (including AI-written names) can't inject markup.
"use strict";

// ---- time-of-day theme --------------------------------------------------------

const PHASES = [
  { name: "morning", from: 5, to: 11, greeting: "Good morning", label: "Morning theme, soft cyan until 11:00" },
  { name: "day", from: 11, to: 17, greeting: "Good afternoon", label: "Day theme, bright blue until 17:00" },
  { name: "evening", from: 17, to: 21, greeting: "Good evening", label: "Evening theme, deep blue until 21:00" },
  { name: "night", from: 21, to: 29, greeting: "Night session", label: "Night theme, midnight blue until 05:00" },
];

function phaseNow() {
  let hour = new Date().getHours();
  if (hour < 5) hour += 24;
  return PHASES.find((p) => hour >= p.from && hour < p.to) || PHASES[1];
}

function applyPhase() {
  const phase = phaseNow();
  document.documentElement.dataset.phase = phase.name;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim();
  document.querySelector('meta[name="theme-color"]').setAttribute("content", accent || "#F5F8FC");
}

// ---- helpers ------------------------------------------------------------------------

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs || {})) {
    if (v === undefined || v === null || v === false) continue;
    if (k === "class") el.className = v;
    else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
    else if (k === "value") el.value = v;
    else if (k === "checked") el.checked = !!v;
    else el.setAttribute(k, v === true ? "" : v);
  }
  for (const c of children.flat()) {
    if (c === null || c === undefined || c === false) continue;
    el.append(c instanceof Node ? c : document.createTextNode(String(c)));
  }
  return el;
}

const SVG = "http://www.w3.org/2000/svg";
function icon(paths) {
  const svg = document.createElementNS(SVG, "svg");
  svg.setAttribute("viewBox", "0 0 24 24");
  svg.setAttribute("aria-hidden", "true");
  for (const d of paths) {
    const p = document.createElementNS(SVG, "path");
    p.setAttribute("d", d);
    svg.append(p);
  }
  return svg;
}

const money = (x, sign = false) => {
  const n = Number(x || 0);
  const s = "$" + Math.abs(n).toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 });
  if (!sign) return (n < 0 ? "−" : "") + s;
  return (n > 0 ? "+" : n < 0 ? "−" : "") + s;
};
const tone = (x) => (Number(x) > 0 ? "gain" : Number(x) < 0 ? "loss" : "");
const pct = (x) => Math.round(Number(x || 0) * 100) + "%";
const timeOf = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
};
const dayOf = (iso) => {
  const d = new Date(iso);
  return isNaN(d) ? "" : d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
};

async function api(path, opts = {}) {
  const init = { method: opts.method || "GET", headers: {}, credentials: "same-origin" };
  if (init.method !== "GET") init.headers["X-Requested-With"] = "app";
  if (opts.body !== undefined) {
    init.headers["Content-Type"] = "application/json";
    init.body = JSON.stringify(opts.body);
  }
  const resp = await fetch("/api" + path, init);
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (resp.status === 401 && path !== "/login") {
    location.hash = "#/login";
    throw new Error("Please log in");
  }
  if (!resp.ok) throw new Error((data && data.detail) || `Request failed (${resp.status})`);
  return data;
}

let toastTimer;
function toast(msg) {
  document.querySelector(".toast")?.remove();
  const el = h("div", { class: "toast", role: "status" }, msg);
  document.body.append(el);
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => el.remove(), 3500);
}

async function act(button, fn, done) {
  button.disabled = true;
  try {
    const result = await fn();
    if (done) toast(done);
    return result;
  } catch (e) {
    toast(e.message);
  } finally {
    button.disabled = false;
  }
}

// ---- layout -------------------------------------------------------------------

const TABS = [
  { href: "#/home", label: "Home", icon: ["M3 11l9-7 9 7v9a1 1 0 0 1-1 1h-5v-6h-6v6H4a1 1 0 0 1-1-1z"] },
  { href: "#/strategy", label: "Strategy", icon: ["M4 6h10M18 6h2M4 12h4M12 12h8M4 18h12", "M16 4v4M10 10v4M18 16v4"] },
  { href: "#/lab", label: "Lab", icon: ["M9 3h6M10 3v6l-5 9a2 2 0 0 0 1.7 3h10.6a2 2 0 0 0 1.7-3l-5-9V3", "M7.5 15h9"] },
  { href: "#/trades", label: "Trades", icon: ["M8 6h12M8 12h12M8 18h12M4 6h.01M4 12h.01M4 18h.01"] },
];

function render(title, ...content) {
  document.title = title ? `${title} · Trading Bot` : "Trading Bot";
  const route = location.hash.split("/")[1] || "home";
  const nav = h("nav", { class: "tabs", "aria-label": "Main" },
    TABS.map((t) => h("a", { href: t.href, class: t.href === "#/" + route ? "on" : "" }, icon(t.icon), t.label)));
  const app = document.getElementById("app");
  app.replaceChildren(h("main", {}, ...content), route === "login" ? null : nav);
  window.scrollTo(0, 0);
}

function header(title, extra) {
  return h("div", { class: "row" }, h("h1", {}, title), extra || null);
}

function accountPill(settings) {
  const real = settings && settings.account_type === "real";
  return h("span", { class: real ? "pill real" : "pill" }, real ? "REAL" : "DEMO");
}

// ---- screens --------------------------------------------------------------------

async function loginScreen() {
  const session = await api("/session");
  if (session.logged_in) { location.hash = "#/home"; return; }
  const pw = h("input", { type: "password", id: "pw", autocomplete: "current-password", class: "wide" });
  const code = h("input", { id: "code", inputmode: "numeric", autocomplete: "one-time-code", maxlength: "6", class: "wide", placeholder: "123456" });
  const submit = h("button", { class: "primary", type: "submit" }, "Log in");
  const form = h("form", {
    onsubmit: (e) => {
      e.preventDefault();
      act(submit, async () => {
        await api("/login", { method: "POST", body: { password: pw.value, code: code.value } });
        location.hash = "#/home";
      });
    },
  },
  h("label", { for: "pw" }, "Password"), pw,
  h("label", { for: "code" }, "6-digit code from your authenticator app"), code, submit);
  render("Log in",
    h("div", { class: "stack" }, h("span", { class: "muted" }, phaseNow().greeting), h("h1", {}, "Trading Bot")),
    session.configured ? form : h("div", { class: "notice bad" },
      h("strong", {}, "Login is not set up yet"),
      "Run python -m server setup on the server and add the three lines it prints to .env."));
}

let poll;
async function homeScreen() {
  const [s, alerts] = await Promise.all([api("/status"), api("/alerts")]);
  const phase = phaseNow();
  const bot = s.bot, st = s.settings, today = s.today;
  const running = bot.state === "running";
  const winRate = today.wins + today.losses ? today.wins / (today.wins + today.losses) : 0;
  const lossUsed = Math.max(0, -today.pnl) + (today.open_exposure || 0);

  const startBtn = h("button", {}, running ? "Pause bot" : "Start bot");
  startBtn.addEventListener("click", () => act(startBtn,
    () => api(running ? "/bot/pause" : "/bot/start", { method: "POST" }), running ? "Bot paused" : "Bot started").then(homeScreen));
  const stopBtn = h("button", { class: "danger" }, "Stop all trading");
  stopBtn.addEventListener("click", () => {
    if (!confirm("Close every open trade and stop the bot?")) return;
    act(stopBtn, () => api("/bot/stop-all", { method: "POST" }), "Stopped. Open trades are being closed.").then(homeScreen);
  });

  const unread = alerts.filter((a) => !a.read);
  const bar = h("div", {});
  bar.style.width = Math.min(100, (lossUsed / st.max_daily_loss) * 100) + "%";

  render("Home",
    h("div", { class: "row" },
      h("div", { class: "stack" }, h("span", { class: "muted small" }, phase.greeting), h("h1", {}, "Trading Bot")),
      accountPill(st)),
    h("div", { class: "muted small" }, phase.label),
    unread.length ? h("div", { class: "notice good" },
      ...unread.slice(0, 2).map((a) => h("div", {}, h("strong", {}, a.title), h("div", {}, a.body),
        a.entry_id ? h("a", { href: `#/lab/${a.entry_id}` }, "Review it") : null)),
      h("button", { class: "linkbtn", onclick: async () => { await api("/alerts/read", { method: "POST" }); homeScreen(); } }, "Dismiss")) : null,
    s.kill_switch && !running ? h("div", { class: "notice bad" }, h("strong", {}, "Trading is stopped"),
      "Stop all trading was pressed. Start the bot to trade again.") : null,
    bot.error ? h("div", { class: "notice bad" }, h("strong", {}, "The bot stopped with an error"), bot.error) : null,
    h("div", { class: "card hero" },
      h("div", { class: "row" },
        h("span", { class: "muted small" }, st.account_type === "real" ? "Real balance" : "Demo balance"),
        h("span", { class: "small" }, h("span", { class: "dot " + (running ? "on" : bot.state === "error" ? "err" : "") }), " ",
          running ? "Running" : bot.state === "error" ? "Error" : "Stopped")),
      h("div", { class: "big" }, bot.balance != null ? money(bot.balance) : "—"),
      h("div", { class: "muted small" }, `${st.strategy_name} · ${st.symbol} · x${st.multiplier}`),
      h("div", { class: "btnrow" }, startBtn, stopBtn)),
    h("div", { class: "grid3" },
      h("div", { class: "card stat" }, h("span", { class: "muted small" }, "Today"), h("span", { class: "value " + tone(today.pnl) }, money(today.pnl, true))),
      h("div", { class: "card stat" }, h("span", { class: "muted small" }, "Trades"), h("span", { class: "value" }, today.trades)),
      h("div", { class: "card stat" }, h("span", { class: "muted small" }, "Win rate"), h("span", { class: "value" }, pct(winRate)))),
    h("div", { class: "card" },
      h("div", { class: "row small" }, h("span", { class: "muted" }, "Daily loss limit"),
        h("span", { class: "mono" }, `${money(lossUsed)} of ${money(st.max_daily_loss)}`)),
      h("div", { class: "bar" }, bar),
      today.paused_until ? h("span", { class: "muted small" }, "Paused after a losing streak until " + timeOf(today.paused_until)) : null),
    bot.open_trades.length ? h("div", { class: "card" }, h("span", { class: "muted small" }, "Open trades"),
      h("div", { class: "list" }, bot.open_trades.map((t) => h("div", { class: "item" },
        h("span", {}, `${st.symbol} · ${t.direction === "UP" ? "Up" : "Down"}`), h("span", { class: "mono" }, `stake ${money(t.stake)}`))))) : null,
    h("div", { class: "row" }, h("h2", {}, "Recent trades"), h("a", { href: "#/trades", class: "linkbtn" }, "See all")),
    h("div", { class: "card" }, tradeList(s.recent, true)));

  clearInterval(poll);
  poll = setInterval(() => { if (location.hash === "#/home" || location.hash === "") homeScreen().catch(() => {}); }, 15000);
}

const EVENT_LABEL = { opened: "Opened", closed: "Closed", skipped: "Skipped", error: "Error", dry_run: "Dry run" };

function tradeList(rows, short) {
  if (!rows.length) return h("div", { class: "empty" }, "No trades yet.");
  return h("div", { class: "list" }, rows.map((r) => {
    const dir = r.direction === "UP" ? "Up" : r.direction === "DOWN" ? "Down" : "";
    const what = [EVENT_LABEL[r.event] || r.event, dir, r.reason].filter(Boolean).join(" · ");
    return h("div", { class: "item" },
      h("div", { class: "stack" }, h("span", {}, what),
        short ? null : h("span", { class: "sub" }, `${dayOf(r.time_utc)} ${timeOf(r.time_utc)} · ${r.symbol} · ${r.account_type}${r.stake ? " · " + money(r.stake) : ""}`)),
      r.profit !== "" && r.profit !== undefined ? h("span", { class: "mono " + tone(r.profit) }, money(r.profit, true))
        : h("span", { class: "muted small" }, timeOf(r.time_utc)));
  }));
}

async function tradesScreen() {
  const rows = await api("/trades?limit=300");
  const closed = rows.filter((r) => r.event === "closed");
  const net = closed.reduce((a, r) => a + Number(r.profit || 0), 0);
  const exportBtn = h("button", { class: "small" }, "Export CSV");
  exportBtn.addEventListener("click", () => {
    const cols = Object.keys(rows[0] || { time_utc: "" });
    const esc = (v) => `"${String(v ?? "").replace(/"/g, '""')}"`;
    const csv = [cols.join(","), ...rows.slice().reverse().map((r) => cols.map((c) => esc(r[c])).join(","))].join("\n");
    const a = h("a", { href: URL.createObjectURL(new Blob([csv], { type: "text/csv" })), download: "trades.csv" });
    a.click();
  });
  render("Trades",
    header("Trade log", exportBtn),
    h("div", { class: "row small muted" }, h("span", {}, `${closed.length} closed trades`),
      h("span", { class: "mono" }, "net ", h("span", { class: tone(net) }, money(net, true)))),
    h("div", { class: "card" }, tradeList(rows, false)));
}

function field(labelText, input) {
  const id = "f-" + labelText.toLowerCase().replace(/[^a-z]+/g, "-");
  input.id = id;
  return h("div", { class: "field" }, h("label", { for: id }, labelText), input);
}

async function strategyScreen() {
  const [st, labData] = await Promise.all([api("/settings"), api("/lab")]);
  const options = [
    ...st.builtin_strategies.map((n) => ({ value: n, label: n.replace("_", " ") })),
    ...labData.entries.map((e) => ({ value: "lab:" + e.id, label: `${e.name} (${stageName(e.stage)})` })),
  ];
  const strategy = h("select", {}, options.map((o) => h("option", { value: o.value }, o.label)));
  strategy.value = st.strategy;
  const num = (v, step = "0.01") => h("input", { type: "number", step, min: "0", inputmode: "decimal", value: v ?? "" });
  const inputs = {
    symbol: h("input", { value: st.symbol, autocapitalize: "off" }),
    stake: num(st.stake), multiplier: num(st.multiplier, "1"),
    stop_loss: num(st.stop_loss), take_profit: num(st.take_profit),
    max_trade_minutes: num(st.max_trade_minutes, "1"),
    max_stake: num(st.max_stake), max_daily_loss: num(st.max_daily_loss),
  };
  const reverse = h("input", { type: "checkbox", checked: st.exit_on_reverse });
  const save = h("button", { class: "primary", type: "button" }, "Save settings");
  save.addEventListener("click", () => act(save, async () => {
    const body = { strategy: strategy.value, symbol: inputs.symbol.value.trim(), exit_on_reverse: reverse.checked };
    for (const k of ["stake", "multiplier", "max_stake", "max_daily_loss", "max_trade_minutes"]) body[k] = Number(inputs[k].value);
    for (const k of ["stop_loss", "take_profit"]) body[k] = inputs[k].value === "" ? "" : Number(inputs[k].value);
    if (strategy.value.startsWith("lab:")) {
      for (const k of ["stop_loss", "take_profit", "max_trade_minutes", "exit_on_reverse"]) delete body[k];
    }
    await api("/settings", { method: "PUT", body });
  }, "Saved. A running bot restarts with the new settings.").then(strategyScreen));

  const real = st.account_type === "real";
  render("Strategy",
    header("Strategy and limits", accountPill(st)),
    h("div", { class: "grid2" },
      h("button", { class: real ? "chip" : "chip on", onclick: () => { if (real) location.hash = "#/live"; } }, "Demo account"),
      h("button", { class: real ? "chip on" : "chip", onclick: () => { if (!real) location.hash = "#/live"; } }, "Real account")),
    h("div", { class: "card" },
      field("Strategy", strategy), field("Market", inputs.symbol),
      field("Stake per trade", inputs.stake), field("Multiplier", inputs.multiplier),
      st.lab_strategy ? h("div", { class: "muted small" }, "Exits come from the lab strategy itself.") : [
        field("Stop loss ($)", inputs.stop_loss), field("Take profit ($)", inputs.take_profit),
        field("Close after minutes (0 = never)", inputs.max_trade_minutes), field("Exit when trend reverses", reverse)]),
    h("span", { class: "label" }, "Safety limits"),
    h("div", { class: "card" }, field("Max stake", inputs.max_stake), field("Max daily loss", inputs.max_daily_loss)),
    save);
}

const STAGE_INDEX = { queued: 0, backtest: 0, paper: 2, ready: 4, failed: -1 };
function stageName(stage) {
  return { queued: "waiting", backtest: "testing", paper: "paper trading", ready: "ready", failed: "failed" }[stage] || stage;
}

function segments(entry) {
  const checks = entry.checks || [];
  const okCount = ["backtest", "holdout", "paper"].filter((k) => checks.find((c) => c.key === k && c.ok)).length;
  const done = entry.stage === "ready" ? 4 : okCount;
  const failAt = entry.stage === "failed" ? Math.min(okCount, 3) : -1;
  const now = entry.stage === "failed" || entry.stage === "ready" ? -1 : done;
  return h("div", { class: "segs", "aria-hidden": "true" }, [0, 1, 2, 3].map((i) =>
    h("div", { class: i < done ? "done" : i === failAt ? "fail" : i === now ? "now" : "" })));
}

let aiRejected = [];

async function labScreen() {
  const data = await api("/lab");
  const opts = data.options;
  const refresh = () => labScreen().catch(() => {});
  const aiBtn = h("button", { class: "small" }, "AI ideas");
  aiBtn.addEventListener("click", () => act(aiBtn, async () => {
    const r = await api("/ai/ideas", { method: "POST", body: { count: 3 } });
    aiRejected = r.rejected;
    toast(`${r.added.length} new idea${r.added.length === 1 ? "" : "s"} queued for testing` +
      (r.rejected.length ? `, ${r.rejected.length} rejected` : ""));
  }).then(refresh));
  const rejectedNote = aiRejected.length ? h("div", { class: "notice bad" },
    h("strong", {}, "Rejected AI ideas"),
    aiRejected.map((reason) => h("span", { class: "mono small" }, reason))) : null;

  const saveOptions = (changes, el) => act(el, () => api("/lab/settings", { method: "PUT", body: changes })).then(refresh);
  const auto = h("input", { type: "checkbox", id: "auto-ideas", checked: opts.auto_ideas });
  auto.addEventListener("change", () => saveOptions({ auto_ideas: auto.checked }, auto));
  const keep = h("select", { id: "delete-failed", "aria-label": "Delete failed ideas after" },
    [[0, "Never"], [1, "1 day"], [3, "3 days"], [7, "7 days"], [30, "30 days"]].map(([v, label]) =>
      h("option", { value: String(v), selected: opts.delete_failed_days === v }, label)));
  keep.addEventListener("change", () => saveOptions({ delete_failed_days: Number(keep.value) }, keep));
  const perDay = h("input", { type: "number", id: "ai-per-day", min: "1", max: "500", step: "1",
    inputmode: "numeric", value: String(opts.ai_ideas_per_day) });
  perDay.addEventListener("change", () => saveOptions({ ai_ideas_per_day: Math.round(Number(perDay.value)) }, perDay));
  const optionsCard = h("div", { class: "card" },
    data.ai.configured ? h("div", { class: "field" },
      h("label", { for: "auto-ideas" }, "Keep the AI coming up with ideas"), auto) : null,
    data.ai.configured && opts.auto_ideas ? h("span", { class: "sub mono small muted" },
      `${data.auto_status} · ${data.ai.remaining} AI ideas left today`) : null,
    data.ai.configured ? h("div", { class: "field" }, h("label", { for: "ai-per-day" }, "AI ideas per day"), perDay) : null,
    h("div", { class: "field" }, h("label", { for: "delete-failed" }, "Delete failed ideas after"), keep));

  const failed = data.entries.filter((e) => e.stage === "failed");
  const clearBtn = h("button", { class: "small danger" }, `Delete all failed (${failed.length})`);
  clearBtn.addEventListener("click", () => {
    if (!confirm(`Delete all ${failed.length} failed strategies?`)) return;
    act(clearBtn, () => api("/lab/delete-failed", { method: "POST" })).then(refresh);
  });

  const stageColor = (e) => (e.stage === "ready" ? "gain" : e.stage === "failed" ? "loss" : "");
  const card = (e) => {
    const del = h("button", { class: "small", "aria-label": `Delete ${e.name}` }, "Delete");
    del.addEventListener("click", () => {
      if (e.stage !== "failed" && !confirm(`Delete ${e.name}? It has not failed.`)) return;
      act(del, () => api("/lab/" + e.id, { method: "DELETE" })).then(refresh);
    });
    return h("div", { class: "card" },
      h("a", { href: `#/lab/${e.id}`, class: "cardlink" },
        h("div", { class: "row" }, h("strong", {}, e.name), h("span", { class: "small " + stageColor(e) }, stageName(e.stage))),
        segments(e),
        h("span", { class: "sub mono small muted" }, e.status)),
      h("div", { class: "row" },
        h("span", { class: "small muted" }, (e.source === "ai" ? "AI idea · " : "") + e.symbols.join(", ")),
        del));
  };

  render("Lab",
    header("Strategy lab", h("div", { class: "btnrow" },
      data.ai.configured ? aiBtn : null,
      h("a", { href: "#/builder", class: "btn small primary" }, "+ New"))),
    optionsCard,
    rejectedNote,
    failed.length ? h("div", { class: "row" }, h("span", { class: "small muted" }, `${data.entries.length} strategies`), clearBtn) : null,
    h("div", { class: "segs muted small" }, ["Backtest", "Unseen data", "Live prices", "Ready"].map((t) => h("span", { class: "small muted" }, t))),
    data.entries.length ? data.entries.map(card) :
      h("div", { class: "card empty" }, "No strategies yet. Tap + New to build one", data.ai.configured ? ", or ask the AI for ideas." : "."),
    h("div", { class: "muted small" }, `A backtest now needs a score of ${data.required_t} to pass. The bar rises as more ideas are tried, so lucky winners don't slip through.`));
  clearInterval(poll);
  poll = setInterval(() => {
    if (location.hash !== "#/lab" || ["SELECT", "INPUT"].includes(document.activeElement?.tagName)) return;
    const y = window.scrollY;
    labScreen().then(() => window.scrollTo(0, y)).catch(() => {});
  }, 15000);
}

function statBlock(title, s) {
  if (!s) return null;
  return h("div", { class: "card" }, h("span", { class: "muted small" }, title),
    h("div", { class: "row" }, h("span", { class: "mono " + tone(s.net) }, money(s.net, true)),
      h("span", { class: "small muted" }, `${s.trades} trades · win ${pct(s.win_rate)}${s.days !== undefined ? ` · ${s.days} days` : ""}`)),
    s.by_symbol ? h("div", { class: "list" }, Object.entries(s.by_symbol).map(([sym, x]) => h("div", { class: "item small" },
      h("span", { class: "mono" }, sym), h("span", { class: "mono " + tone(x.net) }, `${x.trades} · ${money(x.net, true)}`)))) : null);
}

function checkList(checks) {
  return h("div", { class: "card" }, checks.map((c) => h("div", { class: "checkitem" },
    h("span", { class: "mark " + (c.ok ? "ok" : "no"), role: "img", "aria-label": c.ok ? "Passed" : "Not passed" }, c.ok ? "✓" : "✕"),
    h("div", { class: "stack" }, h("strong", { class: "small" }, c.label), h("span", { class: "sub mono small muted" }, c.detail)))));
}

async function labDetailScreen(id) {
  const [e, st] = await Promise.all([api("/lab/" + id), api("/settings")]);
  const use = h("button", { class: "primary" }, st.strategy === "lab:" + e.id ? "The bot uses this strategy" : "Use this strategy");
  use.disabled = st.strategy === "lab:" + e.id;
  use.addEventListener("click", () => {
    if (e.stage !== "ready" && !confirm("This strategy has not passed every check. Use it anyway?")) return;
    act(use, () => api(`/lab/${e.id}/use`, { method: "POST" }), "The bot now uses this strategy").then(() => labDetailScreen(id));
  });
  const del = h("button", { class: "danger" }, "Delete");
  del.addEventListener("click", () => {
    if (!confirm("Delete this strategy from the lab?")) return;
    act(del, () => api("/lab/" + e.id, { method: "DELETE" })).then(() => { location.hash = "#/lab"; });
  });
  const r = e.results || {};
  render(e.name,
    h("a", { href: "#/lab", class: "linkbtn" }, "← Lab"),
    h("h1", {}, e.name),
    h("div", { class: "muted small" }, `${e.source === "ai" ? "AI idea" : "Built in the app"} · ${e.symbols.join(", ")}`),
    h("div", { class: "card" }, h("span", { class: "small" }, e.description), e.note ? h("span", { class: "muted small" }, e.note) : null),
    h("div", { class: "notice " + (e.stage === "ready" ? "good" : e.stage === "failed" ? "bad" : "info") },
      h("strong", {}, e.stage === "ready" ? "Ready to go live" : e.stage === "failed" ? "Failed" : "In testing"), e.status),
    e.checks.length ? checkList(e.checks) : null,
    statBlock("Backtest (older 75% of prices, after costs)", r.backtest),
    statBlock("Unseen data (newest 25%)", r.holdout),
    statBlock("Live prices since passing", r.paper),
    h("div", { class: "btnrow" }, use, del));
}

async function builderScreen() {
  const opts = await api("/lab/options");
  const rules = [];
  const ruleBox = h("div", { class: "stack" });
  const name = h("input", { class: "wide", placeholder: "London breakout", maxlength: "60" });
  const candle = h("select", {}, opts.candle_sizes.map((s) => h("option", { value: s },
    s < 3600 ? `${s / 60} min` : s < 86400 ? `${s / 3600} hour${s > 3600 ? "s" : ""}` : "1 day")));
  candle.value = "900";
  const markets = new Set([opts.symbols[0]]);
  const marketRow = h("div", { class: "chips" });
  const drawMarkets = () => marketRow.replaceChildren(...opts.symbols.map((s) => h("button", {
    class: "chip" + (markets.has(s) ? " on" : ""), type: "button",
    onclick: () => { markets.has(s) ? markets.delete(s) : markets.add(s); drawMarkets(); },
  }, s.replace(/^frx|^cry/, ""))));
  drawMarkets();

  const drawRules = () => ruleBox.replaceChildren(...rules.map((r, i) => {
    const meta = opts.rules[r.type];
    return h("div", { class: "rule" },
      h("div", { class: "row" }, h("span", { class: "small" }, i ? "and " + meta.label.charAt(0).toLowerCase() + meta.label.slice(1) : meta.label),
        h("button", { class: "linkbtn small", type: "button", onclick: () => { rules.splice(i, 1); drawRules(); } }, "Remove")),
      h("div", { class: "params" }, Object.entries(meta.params).map(([p, m]) => {
        const input = h("input", { type: "number", min: m.min, max: m.max, step: m.kind === "int" ? "1" : "0.1", value: r[p] });
        input.addEventListener("input", () => { r[p] = Number(input.value); });
        return h("label", {}, p.replace("_", " "), input);
      })));
  }));
  const picker = h("select", {}, h("option", { value: "" }, "+ Add a rule"),
    Object.entries(opts.rules).map(([k, v]) => h("option", { value: k }, v.label)));
  picker.addEventListener("change", () => {
    if (!picker.value) return;
    const r = { type: picker.value };
    for (const [p, m] of Object.entries(opts.rules[picker.value].params)) r[p] = m.default;
    rules.push(r);
    picker.value = "";
    drawRules();
  });
  rules.push({ type: "breakout", lookback: 8 }, { type: "session", start_hour: 7, end_hour: 12 });
  drawRules();

  const num = (v, step = "0.01") => h("input", { type: "number", min: "0", step, value: v });
  const sl = num("0.50"), tp = num("1.00"), mins = num("480", "1"), mult = num("100", "1");
  const rev = h("input", { type: "checkbox" });
  const run = h("button", { class: "primary", type: "button" }, "Run tests");
  run.addEventListener("click", () => act(run, async () => {
    const body = {
      name: name.value, symbols: [...markets], multiplier: Number(mult.value),
      spec: {
        candle_seconds: Number(candle.value), entry: rules,
        exit: { stop_loss: Number(sl.value) || null, take_profit: Number(tp.value) || null,
          max_trade_minutes: Number(mins.value) || 0, exit_on_reverse: rev.checked },
      },
    };
    const entry = await api("/lab", { method: "POST", body });
    location.hash = "#/lab/" + entry.id;
  }, "Queued for testing"));

  render("New strategy",
    h("a", { href: "#/lab", class: "linkbtn" }, "← Lab"),
    h("h1", {}, "New strategy"),
    h("div", { class: "card" }, h("label", { for: "b-name", class: "small muted" }, "Name"), Object.assign(name, { id: "b-name" }),
      field("Candles", candle), field("Multiplier", mult)),
    h("span", { class: "label" }, "Markets"), marketRow,
    h("span", { class: "label" }, "Enter when"), ruleBox, picker,
    h("span", { class: "label" }, "Exit when"),
    h("div", { class: "card" }, field("Stop loss ($)", sl), field("Take profit ($)", tp),
      field("Close after minutes (0 = never)", mins), field("Exit when the rules flip", rev)),
    h("div", { class: "muted small" }, "Tested on the older 75% of prices with trading costs, then on the newest 25% it never saw. If it passes both, it paper trades live prices for 14 days."),
    run);
}

async function liveScreen() {
  const st = await api("/settings");
  const real = st.account_type === "real";
  if (real) {
    const back = h("button", { class: "primary" }, "Switch back to demo");
    back.addEventListener("click", () => act(back, () => api("/account", { method: "POST", body: { account_type: "demo" } }),
      "Back on the demo account").then(() => { location.hash = "#/strategy"; }));
    render("Real account", h("a", { href: "#/strategy", class: "linkbtn" }, "← Back"),
      h("h1", {}, "You're on your real account"),
      h("div", { class: "muted" }, "The bot trades real money with the current strategy and limits."), back);
    return;
  }
  let entry = null;
  if (st.lab_strategy) entry = await api("/lab/" + st.strategy.slice(4));
  const ready = entry && entry.stage === "ready";
  const passed = entry ? entry.checks.filter((c) => c.ok).length : 0;
  const ack = h("input", { type: "checkbox", id: "ack" });
  const typed = h("input", { id: "typed", class: "wide", placeholder: "REAL", autocapitalize: "characters" });
  const go = h("button", { class: "primary" }, ready ? "Switch to real account" : "Switch anyway");
  go.addEventListener("click", () => act(go, () => api("/account", {
    method: "POST", body: { account_type: "real", confirm: typed.value, acknowledged: ack.checked },
  }), "Switched to your real account").then((r) => { if (r) location.hash = "#/home"; }));

  render("Switch to real",
    h("a", { href: "#/strategy", class: "linkbtn" }, "← Back"),
    h("h1", {}, "Switch to your real account?"),
    h("div", { class: "muted" }, `The bot will trade real money with ${st.strategy_name} on ${st.symbol}, ${money(st.stake)} stakes and a ${money(st.max_daily_loss)} daily loss limit.`),
    entry ? h("div", { class: "notice " + (ready ? "good" : "bad") },
      h("strong", {}, ready ? "Ready to go live" : "Not ready for real money"),
      ready ? "All 4 readiness checks passed. Start small and watch the first week."
        : `${passed} of 4 readiness checks passed. You can still switch, but this strategy is likely to lose money.`)
      : h("div", { class: "notice bad" }, h("strong", {}, "Not tested in the lab"),
        "Built-in strategies lost money in every test so far. Test it in the lab before risking real money."),
    entry ? checkList(entry.checks) : null,
    h("label", { class: "check", for: "ack" }, ack, "I understand this trades real money and I can lose some or all of it."),
    h("label", { for: "typed", class: "small muted" }, "Type REAL to confirm"), typed,
    go,
    h("a", { href: "#/home", class: "btn" }, "Stay on demo"));
}

// ---- router ------------------------------------------------------------------------

async function route() {
  applyPhase();
  clearInterval(poll);
  const parts = location.hash.replace(/^#\/?/, "").split("/");
  const screens = {
    login: loginScreen, home: homeScreen, "": homeScreen, trades: tradesScreen, strategy: strategyScreen,
    lab: () => (parts[1] ? labDetailScreen(parts[1]) : labScreen()), builder: builderScreen, live: liveScreen,
  };
  const screen = screens[parts[0]] || homeScreen;
  try {
    await screen();
  } catch (e) {
    if (e.message !== "Please log in") render("Error", h("div", { class: "notice bad" }, h("strong", {}, "Something went wrong"), e.message));
  }
}

window.addEventListener("hashchange", route);
setInterval(applyPhase, 60000);
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/sw.js").catch(() => {});
route();
