// Aureon: single-page app (hash router) in vanilla JS + TradingView Lightweight Charts. No build step.
// Each page is a function that renders into #view. Shared state (chat history, live feed) survives navigation.

const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const inr = (n, d = 0) => (n == null || isNaN(n)) ? "–" : "₹" + Number(n).toLocaleString("en-IN", { maximumFractionDigits: d, minimumFractionDigits: d });
const pct = (n) => (n == null ? "–" : (n > 0 ? "+" : "") + Number(n).toFixed(2) + "%");
const cls = (n) => (n > 0 ? "up" : n < 0 ? "down" : "");
const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/`(.+?)`/g, "<code>$1</code>");
const when = (iso) => iso ? new Date(/[zZ+]/.test(iso.slice(-6)) ? iso : iso + "Z").toLocaleString("en-IN", { dateStyle: "medium", timeStyle: "short" }) : "–";

const S = {
  token: localStorage.aureonToken || "", me: null, cfg: null, busy: false,
  chat: [], feed: [], symbol: "RELIANCE", period: "6mo", interval: "1d", charts: [],
};

// ------------------------------------------------------------------ API
async function api(path, opts = {}) {
  const headers = { Authorization: "Bearer " + S.token, ...(opts.headers || {}) };
  if (opts.json !== undefined) { headers["Content-Type"] = "application/json"; opts.body = JSON.stringify(opts.json); }
  const r = await fetch(path, { ...opts, headers });
  if (r.status === 401 && !path.endsWith("/login")) { signOut(); throw new Error("Session expired"); }
  if (!r.ok) { let m = await r.text(); try { m = JSON.parse(m).detail || m; } catch {} throw new Error(m); }
  return r.json();
}

// ------------------------------------------------------------------ auth
$("#login-form").onsubmit = async (e) => {
  e.preventDefault(); $("#login-err").textContent = "";
  try {
    const r = await api("/api/login", { method: "POST", json: { username: $("#login-user").value, code: $("#login-pass").value } });
    S.token = localStorage.aureonToken = r.token; await start();
  } catch (err) { $("#login-err").textContent = err.message; }
};
function signOut() { localStorage.removeItem("aureonToken"); S.token = ""; location.hash = ""; location.reload(); }
$("#logout").onclick = signOut;

// ------------------------------------------------------------------ charts helpers
function mkChart(el, height) {
  const c = LightweightCharts.createChart(el, {
    width: el.clientWidth, height, layout: { background: { color: "transparent" }, textColor: "#8791a5", fontFamily: "Palatino Linotype, Palatino, Book Antiqua, Georgia, serif" },
    grid: { vertLines: { color: "#161e2d" }, horzLines: { color: "#161e2d" } }, rightPriceScale: { borderColor: "#1f2939" },
    timeScale: { borderColor: "#1f2939", timeVisible: true }, crosshair: { mode: 0 } });
  S.charts.push([c, el]); return c;
}
new ResizeObserver(() => S.charts.forEach(([c, el]) => el.isConnected && c.applyOptions({ width: el.clientWidth }))).observe(document.body);
function clearCharts() { S.charts.forEach(([c]) => { try { c.remove(); } catch {} }); S.charts = []; }
const sma = (bars, n) => bars.map((b, i) => i < n - 1 ? null : { time: b.time, value: bars.slice(i - n + 1, i + 1).reduce((a, x) => a + x.close, 0) / n }).filter(Boolean);

async function equityChart(el, height = 280) {
  const [eq, pf] = await Promise.all([api("/api/equity"), api("/api/portfolio")]);
  const c = mkChart(el, height);
  const s = c.addAreaSeries({ priceFormat: { type: "price", precision: 0, minMove: 1 }, lineColor: "#d4a84f",
    topColor: "rgba(212,168,79,.32)", bottomColor: "rgba(212,168,79,0)", lineWidth: 2 });
  const pts = []; (eq.length ? eq : [{ time: Math.floor(Date.now() / 1000), value: pf.equity }])
    .forEach((x) => { if (pts.length && pts.at(-1).time >= x.time) pts.at(-1).value = x.value; else pts.push({ ...x }); });
  s.setData(pts); s.createPriceLine({ price: pf.starting_capital, color: "#5d6679", lineStyle: 2, lineWidth: 1, title: "start" });
  c.timeScale().fitContent();
}

async function candleChart(el, symbol, period, interval, height = 420) {
  const d = await api(`/api/candles/${symbol}?period=${period}&interval=${interval}`);
  const c = mkChart(el, height);
  const cs = c.addCandlestickSeries({ upColor: "#2ec27e", downColor: "#f0565a", borderVisible: false, wickUpColor: "#2ec27e", wickDownColor: "#f0565a" });
  const vol = c.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "v" });
  c.priceScale("v").applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
  cs.setData(d.bars);
  vol.setData(d.bars.map((b) => ({ time: b.time, value: b.volume, color: b.close >= b.open ? "rgba(46,194,126,.3)" : "rgba(240,86,90,.3)" })));
  c.addLineSeries({ color: "#f2a93b", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(sma(d.bars, 20));
  c.addLineSeries({ color: "#b48cff", lineWidth: 1, priceLineVisible: false, lastValueVisible: false }).setData(sma(d.bars, 50));
  const times = new Set(d.bars.map((b) => b.time));
  cs.setMarkers(d.markers.filter((m) => times.has(m.time)).sort((a, b) => (a.time > b.time ? 1 : -1)).map((m) => ({
    time: m.time, position: m.side === "BUY" ? "belowBar" : "aboveBar", color: m.side === "BUY" ? "#2ec27e" : "#f0565a",
    shape: m.side === "BUY" ? "arrowUp" : "arrowDown", text: `${m.side} ${m.qty}` })));
  c.timeScale().fitContent();
  return d;
}

// ------------------------------------------------------------------ approvals (shown on every page)
async function renderApprovals() {
  const list = await api("/api/approvals").catch(() => []);
  $("#approval-bar").innerHTML = list.map((a) => a.actions.map((act) => {
    const g = act.args;
    return `<div class="approval" data-id="${a.id}">
      <div><div class="ttl">⏸ Your approval is needed · say "approve" or "reject"</div>
        <div class="order"><span class="${g.side === "BUY" ? "up" : "down"}">${esc(g.side)}</span> ${esc(g.quantity)} × ${esc(g.symbol)}
          <span class="tag">${esc(g.product || "CNC")}</span> <span class="tag">${esc(g.order_type || "MARKET")}${g.limit_price ? " @ " + inr(g.limit_price, 2) : ""}</span>
          <span class="tiny muted">SL ${inr(g.stop_loss, 2)} · TGT ${inr(g.target, 2)}</span></div>
        <div class="why">${md(g.rationale || "")}</div></div>
      <div class="acts"><input placeholder="Note (optional)" /><button class="btn ok">Approve</button><button class="btn danger">Reject</button></div></div>`;
  }).join("")).join("");
  $$(".approval").forEach((el) => {
    const note = () => $("input", el).value;
    $(".ok", el).onclick = () => decide(el.dataset.id, true, note());
    $(".danger", el).onclick = () => decide(el.dataset.id, false, note());
  });
}
async function decide(id, approve, note) {
  pushChat("user", (approve ? "✅ Approve" : "❌ Reject") + (note ? ": " + note : ""));
  $("#approval-bar").innerHTML = "";
  await runAgent(() => api(`/api/approvals/${id}`, { method: "POST", json: { approve, note } }));
}

// ------------------------------------------------------------------ talking to the Fund Manager
function pushChat(who, text) { S.chat.push({ who, text }); const box = $("#chat"); if (box) appendMsg(box, who, text); }
function appendMsg(box, who, text) {
  const d = document.createElement("div"); d.className = "msg " + who; d.innerHTML = md(text); box.appendChild(d); box.scrollTop = 1e9; return d;
}
// ---- text-to-speech: can always be stopped (Stop button, toggle off, or starting to record)
function stopSpeaking() {
  try { window.speechSynthesis?.cancel(); } catch {}
  $("#stop-speak")?.classList.add("hidden");
}
function speak(text) {
  if (!S.cfg?.prefs?.speak_replies || !window.speechSynthesis || !text) return;
  stopSpeaking();
  const u = new SpeechSynthesisUtterance(text.replace(/[*`#|_]/g, "").slice(0, 900));
  u.lang = "en-IN"; u.rate = 1.04;
  u.onend = u.onerror = () => $("#stop-speak")?.classList.add("hidden");
  $("#stop-speak")?.classList.remove("hidden");
  speechSynthesis.speak(u);
}
async function setSpeak(on) {
  S.cfg.prefs.speak_replies = on;          // takes effect immediately, before the server round-trip
  if (!on) stopSpeaking();
  try { S.cfg.prefs = await api("/api/me/settings", { method: "PUT", json: { speak_replies: on } }); } catch {}
}
function toast(html, ms = 9000) { const t = $("#toast"); t.innerHTML = html; t.classList.remove("hidden"); clearTimeout(t._h); t._h = setTimeout(() => t.classList.add("hidden"), ms); }
async function runAgent(call) {
  if (S.busy) return toast("The Fund Manager is still working on your last request…");
  S.busy = true; const box = $("#chat"); const thinking = box ? appendMsg(box, "bot thinking", "Working with the team") : null;
  if (!box) toast("<b>Fund Manager</b> is working on it… follow along on the AI Desk.", 4000);
  try {
    const r = await call(); $$(".msg.thinking").forEach((x) => x.remove());
    if (r.transcript) pushChat("user", "🎙 " + r.transcript);
    const text = r.error || r.reply || (r.pending_approval ? "I have a trade for you to review." : "Done.");
    pushChat("bot", text); speak(text); if (!$("#chat")) toast("<b>Fund Manager:</b> " + md(text.slice(0, 400)));
  } catch (e) { $$(".msg.thinking").forEach((x) => x.remove()); pushChat("bot", "Error: " + e.message); }
  S.busy = false; renderApprovals(); if (S.refresh) S.refresh();
}
const ask = (m) => { pushChat("user", m); return runAgent(() => api("/api/chat", { method: "POST", json: { message: m } })); };

// voice: the floating mic and the desk mic share one recorder
// ---- voice recording: picks a format this browser supports (Chrome/Edge: webm, Firefox: ogg, Safari: mp4)
let rec, chunks = [], recStart = 0;
function pickAudioFormat() {
  const options = [["audio/webm;codecs=opus", "webm"], ["audio/webm", "webm"], ["audio/ogg;codecs=opus", "ogg"],
    ["audio/mp4", "m4a"], ["audio/mpeg", "mp3"]];
  for (const [mime, ext] of options) if (window.MediaRecorder?.isTypeSupported?.(mime)) return { mime, ext };
  return { mime: "", ext: "webm" };
}
async function toggleMic() {
  if (rec && rec.state === "recording") return rec.stop();
  stopSpeaking();
  if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder)
    return toast("This browser can't record audio here. Use Chrome or Edge over https, or type your request.");
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: { echoCancellation: true, noiseSuppression: true } });
  } catch (e) {
    const why = e.name === "NotAllowedError" ? "Microphone permission was blocked. Click the 🔒 icon in the address bar → allow Microphone → reload."
      : e.name === "NotFoundError" ? "No microphone was found on this device." : "Microphone unavailable: " + e.message;
    return toast(esc(why), 12000);
  }
  const fmt = pickAudioFormat();
  rec = fmt.mime ? new MediaRecorder(stream, { mimeType: fmt.mime }) : new MediaRecorder(stream);
  chunks = [];
  rec.ondataavailable = (e) => { if (e.data && e.data.size) chunks.push(e.data); };
  rec.onstop = () => {
    stream.getTracks().forEach((t) => t.stop()); $$(".fab, .mic").forEach((b) => b.classList.remove("rec"));
    const blob = new Blob(chunks, { type: rec.mimeType || fmt.mime || "audio/webm" });
    if (Date.now() - recStart < 600 || blob.size < 1000)
      return toast("That was too short. Click the mic, speak, then click it again to send.", 6000);
    toast("Transcribing with Whisper…", 4000);
    const fd = new FormData(); fd.append("audio", blob, "voice." + fmt.ext);
    runAgent(() => api("/api/voice", { method: "POST", body: fd }));
  };
  rec.start(250); recStart = Date.now();           // 250 ms chunks: nothing is lost if the tab hiccups
  $$(".fab, .mic").forEach((b) => b.classList.add("rec"));
  toast("🎙 Listening… click the mic again to send.", 60000);
}
$("#fab").onclick = toggleMic;
$("#stop-speak").onclick = stopSpeaking;

// ------------------------------------------------------------------ live events
const AGENT = { "hedge-fund-manager": "Fund Manager", "macro-strategist": "Macro Strategist", "research-analyst": "Research Analyst",
  "fundamental-analyst": "Fundamental Analyst", "quant-strategist": "Quant Strategist", "bear-case-analyst": "Bear-Case Analyst",
  "risk-manager": "Risk Manager", "compliance-officer": "Compliance Officer", "execution-trader": "Execution Trader",
  "performance-analyst": "Performance Analyst" };
function feedItem(agent, text, who) { S.feed.push({ agent, text, who }); if (S.feed.length > 300) S.feed.shift(); const f = $("#feed"); if (f) appendFeed(f, S.feed.at(-1)); }
function appendFeed(f, e) {
  const d = document.createElement("div"); d.className = "ev " + e.agent;
  d.innerHTML = `<div class="who">${esc(AGENT[e.agent] || e.agent)}${e.who && S.me.role === "admin" ? ` <span class="tag">${esc(e.who)}</span>` : ""}</div><div class="txt">${esc(e.text)}</div>`;
  f.appendChild(d); f.scrollTop = 1e9;
}
function connectWS() {
  const ws = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/ws?token=${encodeURIComponent(S.token)}`);
  ws.onopen = () => $("#ws-pill").classList.add("on");
  ws.onclose = () => { $("#ws-pill").classList.remove("on"); if (S.token) setTimeout(connectWS, 3000); };
  ws.onmessage = (m) => {
    const e = JSON.parse(m.data);
    if (e.type === "step") feedItem(e.agent, (e.kind === "tool_result" ? `↳ ${e.tool}: ` : e.kind === "tool_call" ? "⚙ " : "💭 ") + e.text, e.account);
    else if (e.type === "run_started") feedItem("hedge-fund-manager", `▶ ${e.trigger}: ${e.input}`, e.account);
    else if (e.type === "approval_required" && e.account === S.me.name) renderApprovals();
    else if (e.type === "settings_changed") loadConfig();
  };
}

// ------------------------------------------------------------------ pages
const PAGES = {
  "/dashboard": ["Dashboard", pageDashboard], "/markets": ["Markets", pageMarkets], "/portfolio": ["Portfolio", pagePortfolio],
  "/orders": ["Orders", pageOrders], "/desk": ["AI Desk", pageDesk], "/memory": ["Memory", pageMemory], "/settings": ["Settings", pageSettings],
  "/admin": ["Broker console", pageAdmin, true], "/admin/orders": ["All orders", pageAdminOrders, true],
  "/admin/runs": ["Agent runs", pageAdminRuns, true], "/admin/settings": ["House rules", pageAdminSettings, true],
};
async function route() {
  let path = location.hash.slice(1) || (S.me.role === "admin" ? "/admin" : "/dashboard");
  let page = PAGES[path]; if (!page || (page[2] && S.me.role !== "admin")) { path = "/dashboard"; page = PAGES[path]; }
  $$("nav a").forEach((a) => a.classList.toggle("on", a.getAttribute("href") === "#" + path));
  $("#page-title").textContent = page[0]; clearCharts(); S.refresh = null;
  $("#fab").classList.toggle("hidden", ["/dashboard", "/desk"].includes(path));  // those pages have their own mic
  const v = $("#view"); v.innerHTML = ""; v.style.animation = "none"; v.offsetHeight; v.style.animation = "";
  try { await page[1](v); } catch (e) { v.innerHTML = `<div class="card empty">Couldn't load this page: ${esc(e.message)}</div>`; }
}
window.addEventListener("hashchange", route);

function kpis(p) {
  return `<div class="kpis">
    <div class="kpi accent"><span>Equity</span><b>${inr(p.equity)}</b><small class="${cls(p.total_pnl)}">${pct(p.total_return_pct)} all-time</small></div>
    <div class="kpi"><span>Total P&amp;L</span><b class="${cls(p.total_pnl)}">${inr(p.total_pnl)}</b><small class="muted">realized ${inr(p.realized_pnl)}</small></div>
    <div class="kpi"><span>Cash</span><b>${inr(p.cash)}</b><small class="muted">${(p.cash / p.equity * 100).toFixed(1)}% of equity</small></div>
    <div class="kpi"><span>Invested</span><b>${inr(p.market_value)}</b><small class="muted">${p.positions.length} positions</small></div>
    <div class="kpi"><span>Charges paid</span><b>${inr(p.fees_paid)}</b><small class="muted">STT · GST · stamp</small></div></div>`;
}
function positionsTable(p, short) {
  return p.positions.length ? `<div class="table-wrap ${short ? "short" : ""}"><table><thead><tr><th>Symbol</th><th>Qty</th><th>Avg</th><th>LTP</th><th>Value</th><th>P&amp;L</th></tr></thead><tbody>
    ${p.positions.map((x) => `<tr class="click" data-sym="${esc(x.symbol)}"><td><b>${esc(x.symbol)}</b></td><td class="num">${x.quantity}</td><td class="num">${inr(x.avg_price, 2)}</td>
    <td class="num">${inr(x.ltp, 2)}</td><td class="num">${inr(x.market_value)}</td><td class="num ${cls(x.unrealized_pnl)}">${inr(x.unrealized_pnl)} <small>${pct(x.unrealized_pct)}</small></td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">No open positions. Ask the Fund Manager for an idea.</div>`;
}
function bindSymbolRows(root) { $$("tr[data-sym]", root).forEach((tr) => tr.onclick = () => { S.symbol = tr.dataset.sym; location.hash = "/markets"; }); }
function ordersTable(o, showUser) {
  return o.length ? `<div class="table-wrap"><table><thead><tr><th>Time</th>${showUser ? "<th>Client</th>" : ""}<th>Symbol</th><th>Side</th><th>Qty</th><th>Product</th><th>Price</th><th>Status</th><th>Charges</th><th>P&amp;L</th><th>Rationale</th></tr></thead><tbody>
    ${o.map((x) => `<tr><td>${when(x.created_at)}</td>${showUser ? `<td>${esc(x.account_id)}</td>` : ""}<td><b>${esc(x.symbol)}</b></td><td class="${x.side === "BUY" ? "up" : "down"}">${x.side}</td>
    <td class="num">${x.quantity}</td><td>${x.product}</td><td class="num">${inr(x.fill_price ?? x.limit_price, 2)}</td><td><span class="st ${x.status}">${x.status}</span></td>
    <td class="num">${inr(x.fees, 2)}</td><td class="num ${cls(x.realized_pnl)}">${x.realized_pnl ? inr(x.realized_pnl) : ""}</td><td class="wrap">${esc(x.reject_reason || x.rationale || "")}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">No orders yet.</div>`;
}
function chatBox(short) {
  return `<div class="chat ${short ? "short" : ""}" id="chat"></div>
    <div class="suggest">${["Scan the watchlist and find me the best swing trade", "How is my portfolio doing?", "Analyse GOLD for an intraday trade",
      "What lessons have you learned so far?", "Review my performance this month"].map((s) => `<button>${s}</button>`).join("")}</div>
    <form class="composer" id="composer"><button type="button" class="mic" title="Speak">🎙</button><input id="ask" placeholder="Ask the Fund Manager anything…" autocomplete="off" /><button class="btn gold">Send</button></form>`;
}
function bindChat(root) {
  const box = $("#chat", root);
  if (!S.chat.length) S.chat.push({ who: "bot", text: `Welcome${S.cfg.prefs.display_name ? ", " + S.cfg.prefs.display_name : ""}. I'm your Hedge Fund Manager at Aureon. My research, quant, risk and execution team covers NSE stocks and MCX commodities, and nothing trades without your approval. What shall we look at?` });
  S.chat.forEach((m) => appendMsg(box, m.who, m.text));
  if (S.busy) appendMsg(box, "bot thinking", "Working with the team");
  $(".suggest", root).onclick = (e) => e.target.tagName === "BUTTON" && ask(e.target.textContent);
  $("#composer", root).onsubmit = (e) => { e.preventDefault(); const m = $("#ask").value.trim(); if (m) { $("#ask").value = ""; ask(m); } };
  $(".mic", root).onclick = toggleMic;
}

async function pageDashboard(v) {
  const p = await api("/api/portfolio");
  v.innerHTML = `${kpis(p)}
    <div class="g3"><div class="card"><div class="card-head"><h3>Equity curve</h3><span class="sub">Paper account · started at ${inr(p.starting_capital)}</span></div><div id="eq" class="chart" style="height:470px"></div></div>
      <div class="card"><div class="card-head"><h3>Talk to your Fund Manager</h3><a class="sub gold" href="#/desk">Open AI Desk →</a></div>${chatBox(true)}</div></div>
    <div class="g2"><div class="card"><div class="card-head"><h3>Positions</h3><a class="sub gold" href="#/portfolio">Details →</a></div>${positionsTable(p, true)}</div>
      <div class="card"><div class="card-head"><h3>Live agent activity</h3><span class="sub">What the team is doing right now</span></div><div class="feed short" id="feed"></div></div></div>`;
  bindChat(v); bindSymbolRows(v); S.feed.forEach((e) => appendFeed($("#feed"), e)); await equityChart($("#eq"), 470);
  S.refresh = () => location.hash === "#/dashboard" && route();
}

async function pageMarkets(v) {
  const syms = [...new Set([...S.cfg.watchlist, ...S.cfg.commodities])];
  v.innerHTML = `<div class="g-side"><div class="card">
      <div class="card-head"><div class="symhead"><select id="sym">${syms.map((s) => `<option ${s === S.symbol ? "selected" : ""}>${s}</option>`).join("")}</select>
        <b id="px">–</b><span id="chg" class="mono"></span></div>
        <div class="seg" id="tf">${[["5d", "5m", "5m"], ["1mo", "15m", "15m"], ["6mo", "1d", "6M"], ["2y", "1d", "2Y"]].map(([p, i, l]) =>
          `<button data-p="${p}" data-i="${i}" class="${p === S.period && i === S.interval ? "on" : ""}">${l}</button>`).join("")}</div></div>
      <div id="cc" class="chart"></div>
      <div class="legend"><span><i style="background:#f2a93b"></i>SMA 20</span><span><i style="background:#b48cff"></i>SMA 50</span><span class="up">▲ your buys</span><span class="down">▼ your sells</span></div>
      <div class="chips" id="tech"></div>
      <div class="suggest" style="margin-bottom:0"><button id="ask-sym">🎩 Ask the Fund Manager to analyse this</button></div></div>
    <div class="card"><div class="card-head"><h3>Watchlist</h3><span class="sub">${esc(S.cfg.data_provider)}</span></div>
      <div class="table-wrap"><table><thead><tr><th>Symbol</th><th>LTP</th><th>Day</th></tr></thead><tbody id="wl">
      ${syms.map((s) => `<tr class="click" data-sym="${s}"><td><b>${s}</b> ${S.cfg.commodities.includes(s) ? '<span class="tag">MCX</span>' : ""}</td><td class="num" data-ltp>…</td><td class="num" data-chg></td></tr>`).join("")}
      </tbody></table></div></div></div>`;
  const draw = async () => {
    clearCharts(); const d = await candleChart($("#cc"), S.symbol, S.period, S.interval); const t = d.technicals || {}, last = d.bars.at(-1);
    $("#px").textContent = inr(last?.close, 2); $("#chg").textContent = pct(t.change_1d_pct); $("#chg").className = "mono " + cls(t.change_1d_pct);
    $("#tech").innerHTML = t.rule_signal ? [`<span class="chip ${t.rule_signal}">Signal ${t.rule_signal} (${t.rule_score})</span>`, `RSI ${t.rsi14}`, `ATR ${t.atr_pct}%`,
      `MACD ${t.macd}`, `5d ${pct(t.change_5d_pct)}`, `20d ${pct(t.change_20d_pct)}`, `52w ${inr(t.low_52w)}–${inr(t.high_52w)}`]
      .map((x, i) => i ? `<span class="chip">${x}</span>` : x).join("") : "";
  };
  $("#sym").onchange = (e) => { S.symbol = e.target.value; draw(); };
  $$("#tf button").forEach((b) => b.onclick = () => { $$("#tf button").forEach((x) => x.classList.remove("on")); b.classList.add("on"); S.period = b.dataset.p; S.interval = b.dataset.i; draw(); });
  $$("#wl tr").forEach((tr) => tr.onclick = () => { S.symbol = tr.dataset.sym; $("#sym").value = S.symbol; draw(); });
  $("#ask-sym").onclick = () => { location.hash = "/desk"; setTimeout(() => ask(`Analyse ${S.symbol}: should we trade it now? Give me intraday and swing views.`), 50); };
  await draw();
  for (const tr of $$("#wl tr")) api(`/api/quote/${tr.dataset.sym}`).then((q) => {
    $("[data-ltp]", tr).textContent = inr(q.ltp, 2); const c = $("[data-chg]", tr); c.textContent = pct(q.change_pct); c.className = "num " + cls(q.change_pct);
  }).catch(() => ($("[data-ltp]", tr).textContent = "n/a"));
}

async function pagePortfolio(v) {
  const p = await api("/api/portfolio");
  const total = p.equity || 1;
  const rows = [...p.positions.map((x) => [x.symbol, Math.abs(x.market_value)]), ["Cash", p.cash]];
  v.innerHTML = `${kpis(p)}<div class="g3"><div class="card"><div class="card-head"><h3>Open positions</h3><span class="sub">Click a row to open its chart</span></div>${positionsTable(p)}</div>
    <div class="card"><div class="card-head"><h3>Allocation</h3></div><div class="alloc">${rows.map(([s, val]) => `<div class="row"><span>${esc(s)}</span>
      <div class="bar"><i class="${s === "Cash" ? "cash" : ""}" style="width:${Math.min(100, val / total * 100).toFixed(1)}%"></i></div><span class="mono tiny">${(val / total * 100).toFixed(1)}%</span></div>`).join("")}</div></div></div>
    <div class="card"><div class="card-head"><h3>Equity curve</h3></div><div id="eq" class="chart md"></div></div>`;
  bindSymbolRows(v); await equityChart($("#eq"));
}

async function pageOrders(v) {
  const o = await api("/api/orders");
  const filled = o.filter((x) => x.status === "FILLED");
  v.innerHTML = `<div class="kpis"><div class="kpi"><span>Orders</span><b>${o.length}</b></div><div class="kpi"><span>Filled</span><b class="up">${filled.length}</b></div>
    <div class="kpi"><span>Rejected by risk</span><b class="down">${o.filter((x) => x.status === "REJECTED").length}</b></div>
    <div class="kpi"><span>Realized P&amp;L</span><b class="${cls(filled.reduce((a, x) => a + x.realized_pnl, 0))}">${inr(filled.reduce((a, x) => a + x.realized_pnl, 0))}</b></div></div>
    <div class="card"><div class="card-head"><h3>Order blotter</h3><span class="sub">Every paper order, with the agents' rationale</span></div>${ordersTable(o)}</div>`;
}

async function pageDesk(v) {
  const models = S.cfg.models; const roles = {
    "Hedge Fund Manager": "Plans the work, delegates and makes the final call", "Macro Strategist": "Market regime: NIFTY, VIX, rupee, crude",
    "Research Analyst": "Technicals, news and FinBERT sentiment", "Fundamental Analyst": "Valuation, quality and results dates",
    "Quant Strategist": "Backtests, ATR sizing, entry, stop and target", "Bear-Case Analyst": "Argues against every trade idea; gets its evidence over MCP",
    "Risk Manager": "Exposure checks; can veto trades", "Compliance Officer": "Pre-trade rules and audit; can block",
    "Execution Trader": "Order ticket and charges", "Performance Analyst": "Reviews results; writes lessons to memory",
    "Fin-R1 (finance LLM)": "Finance-tuned reasoning, consulted as an expert", "FinBERT (sentiment)": "Scores financial news headlines", "Whisper (voice)": "Speech to text" };
  v.innerHTML = `<div class="g-side"><div class="card"><div class="card-head"><h3>🎩 The Hedge Fund Manager</h3>
      <label class="switch tiny"><input type="checkbox" id="tts" ${S.cfg.prefs.speak_replies ? "checked" : ""}/><i></i>Speak replies</label></div>${chatBox()}</div>
    <div class="card"><div class="card-head"><h3>Live agent activity</h3></div><div class="feed" id="feed"></div></div></div>
    <div class="card"><div class="card-head"><h3>Your AI team</h3><span class="sub">Ten agents, each on its own model</span></div>
      <div class="team">${Object.entries(models).map(([k, m]) => `<div class="agent"><b>${esc(k)}</b><code>${esc(m.replace(/^google_genai:/, "Gemini · ").replace(/^ollama:/, "Ollama · "))}</code><p>${esc(roles[k] || "")}</p></div>`).join("")}</div></div>
    <div class="card"><div class="card-head"><h3>MCP server: ${esc(S.cfg.mcp.server)}</h3><span class="sub">Model Context Protocol · Streamable HTTP</span></div>
      <p class="muted" style="margin-top:0">Aureon publishes its read-only market tools over MCP. The Bear-Case Analyst calls them through MCP,
        and any MCP client (MCP Inspector, Claude Desktop, other agents) can connect with your access code as a Bearer token.</p>
      <div class="chips">${S.cfg.mcp.tools.map((t) => `<span class="chip">${esc(t)}</span>`).join("")}</div>
      <pre class="mem" style="margin-top:12px">URL:    ${esc(location.origin)}/mcp/
Header: Authorization: Bearer &lt;your access code&gt;

Try it:  npx @modelcontextprotocol/inspector
         → Transport: Streamable HTTP → paste the URL → add the header → Connect → Tools</pre></div>`;
  bindChat(v); S.feed.forEach((e) => appendFeed($("#feed"), e));
  $("#tts").onchange = (e) => setSpeak(e.target.checked);
}

async function pageMemory(v) {
  const mem = await api("/api/memory");
  v.innerHTML = `<div class="card"><div class="card-head"><h3>Long-term memory</h3><span class="sub">Written by the Fund Manager with Deep Agents' filesystem tools and kept in the LangGraph store</span></div>
    <p class="muted" style="margin-top:0">The <b>playbook</b> holds your preferences and lessons learned and is loaded into every conversation. The <b>journal</b> records each trade thesis and your decision on it.</p></div>
    ${mem.map((m) => `<div class="card"><div class="card-head"><h3 class="mono">${esc(m.path)}</h3><span class="sub">updated ${when(m.updated_at)}</span></div><pre class="mem">${esc(m.content)}</pre></div>`).join("")}`;
}

async function pageSettings(v) {
  const p = await api("/api/me/settings"); const L = S.cfg.limits;
  v.innerHTML = `<div class="card"><div class="card-head"><h3>Profile &amp; trading preferences</h3><span class="sub">The Fund Manager reads these before every conversation</span></div>
    <form class="form" id="prefs">
      <label>Display name<input name="display_name" value="${esc(p.display_name)}" maxlength="40" /></label>
      <label>Risk profile <span class="hint">Scales the house limits (${(L.max_position_pct * 100).toFixed(0)}% per position, ${inr(L.max_order_value)} per order)</span>
        <select name="risk_profile">${Object.entries(S.cfg.risk_profiles).map(([k, m]) => `<option value="${k}" ${k === p.risk_profile ? "selected" : ""}>${k[0].toUpperCase() + k.slice(1)} · ${m * 100}% of limits</option>`).join("")}</select></label>
      <label>Trading style<select name="trading_style">${["intraday", "swing", "positional", "mixed"].map((s) => `<option ${s === p.trading_style ? "selected" : ""}>${s}</option>`).join("")}</select></label>
      <label>Default product<select name="default_product"><option value="CNC" ${p.default_product === "CNC" ? "selected" : ""}>CNC · delivery / swing</option><option value="MIS" ${p.default_product === "MIS" ? "selected" : ""}>MIS · intraday</option></select></label>
      <label>Default chart symbol<input name="default_symbol" value="${esc(p.default_symbol)}" /></label>
      <label class="switch" style="flex-direction:row;align-self:end"><input type="checkbox" name="speak_replies" ${p.speak_replies ? "checked" : ""} /><i></i>Read replies aloud</label>
      <label class="full">Notes for your Fund Manager <span class="hint">e.g. "Avoid PSU banks", "I prefer IT and FMCG", "Explain like I'm new to trading"</span>
        <textarea name="notes_for_manager" rows="3" maxlength="500">${esc(p.notes_for_manager)}</textarea></label>
    </form><div class="form-actions"><span class="tiny muted" id="saved"></span><button class="btn gold" id="save">Save preferences</button></div></div>
    <div class="card"><div class="card-head"><h3>Paper account</h3></div>
      <div class="killswitch"><div><b>Reset paper account</b><div class="tiny muted">Clears your orders, positions and equity history and restores ${inr(S.cfg.starting_capital)}. The agents' memory is kept.</div></div>
      <button class="btn danger" id="reset">Reset account</button></div></div>`;
  $("#save").onclick = async () => {
    const f = $("#prefs"), body = Object.fromEntries(new FormData(f)); body.speak_replies = f.speak_replies.checked;
    if (!body.speak_replies) stopSpeaking();
    S.cfg.prefs = await api("/api/me/settings", { method: "PUT", json: body }); S.symbol = S.cfg.prefs.default_symbol || S.symbol;
    $("#saved").textContent = "Saved ✓"; setMe();
  };
  $("#reset").onclick = async () => { if (confirm("Reset your paper account to starting capital?")) { await api("/api/me/reset", { method: "POST" }); toast("Account reset."); } };
}

// ------------------------------------------------------------------ admin (broker) pages
async function pageAdmin(v) {
  const o = await api("/api/admin/overview"); const halted = o.settings.trading_halted;
  v.innerHTML = `<div class="kpis"><div class="kpi accent"><span>Client AUM (paper)</span><b>${inr(o.aum)}</b><small class="${cls(o.total_pnl)}">${inr(o.total_pnl)} total P&amp;L</small></div>
    <div class="kpi"><span>Clients</span><b>${o.clients.length}</b></div><div class="kpi"><span>Awaiting approval</span><b class="gold">${o.pending}</b></div>
    <div class="kpi"><span>Orders</span><b>${o.orders}</b></div><div class="kpi"><span>Agent runs</span><b>${o.runs}</b><small class="${o.errors ? "down" : "muted"}">${o.errors} errors</small></div>
    <div class="kpi ${o.llm.cost_usd >= o.llm.budget_usd ? "" : "accent"}"><span>AI spend today (est.)</span><b class="${o.llm.cost_usd >= o.llm.budget_usd ? "down" : ""}">$${(o.llm.cost_usd || 0).toFixed(3)}</b>
      <small class="muted">of $${o.llm.budget_usd.toFixed(2)} cap · ${o.llm.calls || 0} calls</small></div></div>
    <div class="card"><div class="card-head"><h3>AI usage today</h3><div><span class="sub">Estimated from tokens; GCP billing is authoritative</span>
      <button class="btn sm" id="llmcheck" style="margin-left:10px">Test all models</button>
      <button class="btn sm gold" id="diag" style="margin-left:6px">Run diagnostics</button></div></div>
      <div id="diag-out"></div>
      <div class="bar" style="height:12px;margin-bottom:12px"><i style="width:${Math.min(100, (o.llm.cost_usd || 0) / o.llm.budget_usd * 100).toFixed(1)}%"></i></div>
      <div class="g2"><div class="table-wrap short"><table><thead><tr><th>Model</th><th>Calls</th><th>Est. cost</th></tr></thead><tbody>
      ${Object.entries(o.llm.by_model || {}).map(([m, x]) => `<tr><td>${esc(m)}</td><td class="num">${x.calls}</td><td class="num">$${x.cost_usd.toFixed(4)}</td></tr>`).join("") || '<tr><td colspan="3" class="muted">No AI calls yet today</td></tr>'}
      </tbody></table></div><div class="table-wrap short"><table><thead><tr><th>Client</th><th>Est. cost</th></tr></thead><tbody>
      ${Object.entries(o.llm.by_user || {}).sort((a, b) => b[1] - a[1]).map(([u, c]) => `<tr><td>${esc(u)}</td><td class="num">$${c.toFixed(4)}</td></tr>`).join("") || '<tr><td colspan="2" class="muted">–</td></tr>'}
      </tbody></table></div></div><pre class="mem hidden" id="llmcheck-out" style="margin-top:12px"></pre></div>
    <div class="card killswitch ${halted ? "on" : ""}"><div><h3 style="margin:0 0 4px">⛔ Kill switch</h3><div class="tiny muted">Instantly blocks every new order for all clients. Agents can still research and answer questions.</div></div>
      <button class="btn ${halted ? "ok" : "danger solid"}" id="halt">${halted ? "Resume trading" : "Halt all trading"}</button></div>
    <div class="card"><div class="card-head"><h3>Clients</h3><span class="sub">${esc(o.market.now_ist)} IST · ${o.market.is_open ? "market open" : "market closed"} · data: ${esc(o.data_provider)}</span></div>
      ${o.clients.length ? `<div class="table-wrap"><table><thead><tr><th>Client</th><th>Risk</th><th>Equity</th><th>P&amp;L</th><th>Positions</th><th>Pending</th><th>Last active</th><th></th></tr></thead><tbody>
      ${o.clients.map((c) => `<tr><td><b>${esc(c.name)}</b> <span class="tiny muted">@${esc(c.user)}</span> ${c.is_admin ? '<span class="tag admin">master</span>' : ""}</td><td><span class="tag">${esc(c.risk_profile)}</span></td>
        <td class="num">${inr(c.equity)}</td><td class="num ${cls(c.pnl)}">${inr(c.pnl)} <small>${pct(c.return_pct)}</small></td><td class="num">${c.positions}</td>
        <td class="num ${c.pending ? "gold" : ""}">${c.pending}</td><td class="tiny">${when(c.last_active)}</td><td><button class="btn sm danger" data-reset="${esc(c.user)}">Reset</button></td></tr>`).join("")}
      </tbody></table></div>` : `<div class="empty">No clients yet. Share the website and the client access code.</div>`}</div>
    <div class="card"><div class="card-head"><h3>Live activity, all clients</h3></div><div class="feed short" id="feed"></div></div>`;
  S.feed.forEach((e) => appendFeed($("#feed"), e));
  $("#llmcheck").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Testing…";
    const r = await api("/api/admin/llm-check").catch((err) => ({ error: err.message }));
    const out = $("#llmcheck-out"); out.classList.remove("hidden"); out.textContent = JSON.stringify(r, null, 2);
    e.target.disabled = false; e.target.textContent = "Test all models";
  };
  $("#diag").onclick = async (e) => {
    e.target.disabled = true; e.target.textContent = "Running (about 1–2 min)…";
    try {
      const r = await api("/api/admin/diagnostics");
      $("#diag-out").innerHTML = `<div class="table-wrap" style="margin:12px 0"><table><thead><tr><th>Check</th><th>Result</th><th>Time</th><th>Detail</th></tr></thead><tbody>
        ${r.results.map((x) => `<tr><td>${esc(x.check)}</td><td><span class="st ${x.ok ? "FILLED" : "REJECTED"}">${x.ok ? "PASS" : "FAIL"}</span></td>
        <td class="num">${x.seconds}s</td><td class="wrap">${esc(x.detail)}</td></tr>`).join("")}</tbody></table></div>
        <p class="tiny ${r.critical_ok ? "up" : "down"}">${r.all_ok ? "Everything passed." : r.critical_ok ? "Core systems OK. Failures above are optional or have fallbacks." : "A core system failed. Send the FAIL rows to support."}</p>`;
    } catch (err) { $("#diag-out").innerHTML = `<p class="down">${esc(err.message)}</p>`; }
    e.target.disabled = false; e.target.textContent = "Run diagnostics";
  };
  $("#halt").onclick = async () => { await api("/api/admin/settings", { method: "PUT", json: { trading_halted: !halted } }); await loadConfig(); route(); };
  $$("[data-reset]").forEach((b) => b.onclick = async () => { if (confirm(`Reset ${b.dataset.reset}'s paper account?`)) { await api(`/api/admin/users/${b.dataset.reset}/reset`, { method: "POST" }); route(); } });
}
async function pageAdminOrders(v) {
  const o = await api("/api/admin/orders");
  v.innerHTML = `<div class="card"><div class="card-head"><h3>All client orders</h3><span class="sub">${o.length} most recent</span></div>${ordersTable(o, true)}</div>`;
}
async function pageAdminRuns(v) {
  const runs = await api("/api/admin/runs");
  v.innerHTML = `<div class="card"><div class="card-head"><h3>Agent runs</h3><span class="sub">Full traces are in LangSmith · click a row for its steps</span></div>
    ${runs.length ? `<div class="table-wrap"><table><thead><tr><th>Started</th><th>Client</th><th>Trigger</th><th>Request</th><th>Status</th><th>Steps</th><th>Time</th></tr></thead><tbody>
    ${runs.map((r) => `<tr class="click" data-run="${r.id}"><td>${when(r.started_at)}</td><td>${esc(r.user)}</td><td><span class="tag">${esc(r.trigger)}</span></td>
      <td class="wrap">${esc(r.input)}</td><td><span class="st ${r.status}">${esc(r.status)}</span></td><td class="num">${r.steps}</td><td class="num">${r.seconds ?? "…"}s</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">No agent runs yet.</div>`}</div>`;
  $$("[data-run]").forEach((tr) => tr.onclick = async () => {
    const r = await api(`/api/admin/runs/${tr.dataset.run}`);
    drawer(`Run ${r.id} · ${r.user}`, `<p><b>Request:</b> ${esc(r.input)}</p><div class="feed" style="height:auto;max-height:50vh">${(r.steps || []).map((s) =>
      `<div class="ev ${s.agent}"><div class="who">${esc(AGENT[s.agent] || s.agent)} · ${esc(s.kind)}</div><div class="txt">${esc(s.text)}</div></div>`).join("")}</div>
      <p><b>Reply:</b></p><pre class="mem">${esc(r.output || "")}</pre>`);
  });
}
async function pageAdminSettings(v) {
  const g = await api("/api/admin/settings");
  const num = (k, label, hint, step = "any") => `<label>${label} <span class="hint">${hint}</span><input name="${k}" type="number" step="${step}" value="${g[k]}" /></label>`;
  v.innerHTML = `<div class="card"><div class="card-head"><h3>House risk rules</h3><span class="sub">Hard limits enforced in code on every order, for every client</span></div>
    <form class="form" id="house">
      ${num("max_position_pct", "Max position size", "fraction of equity, e.g. 0.10 = 10%")}
      ${num("max_order_value", "Max order value (₹)", "per single order", "1000")}
      ${num("max_daily_loss_pct", "Daily loss limit", "fraction, e.g. 0.02 = 2%; then only risk-reducing orders")}
      ${num("max_open_positions", "Max open positions", "per client", "1")}
      ${num("min_cash_buffer_pct", "Min cash buffer", "fraction of equity kept in cash")}
      ${num("starting_capital", "Starting capital (₹)", "for new and reset accounts", "10000")}
      <div class="full nav-title admin" style="padding:6px 0 0">AI spending limits</div>
      ${num("llm_daily_budget_usd", "Daily AI budget (USD)", "all clients combined; agents pause when reached, reset at midnight IST", "0.5")}
      ${num("llm_user_daily_runs", "Requests per client per day", "chat + voice; admins exempt; scheduled scans don't count", "1")}
      ${num("llm_max_calls_per_run", "Max AI calls per request", "stops runaway agent loops (a full trade idea needs about 15–30)", "1")}
      <label class="full">Watchlist <span class="hint">comma-separated NSE symbols and MCX names (GOLD, SILVER, CRUDEOIL, NATURALGAS, COPPER)</span><input name="watchlist" value="${esc(g.watchlist.join(", "))}" /></label>
      <label class="switch"><input type="checkbox" name="enforce_market_hours" ${g.enforce_market_hours ? "checked" : ""} /><i></i>Only fill orders during market hours</label>
      <label class="switch"><input type="checkbox" name="trading_halted" ${g.trading_halted ? "checked" : ""} /><i></i>Kill switch: halt all trading</label>
    </form><div class="form-actions"><span class="tiny muted" id="saved"></span><button class="btn gold" id="save">Save house rules</button></div></div>`;
  $("#save").onclick = async () => {
    const f = $("#house"), b = Object.fromEntries(new FormData(f));
    b.enforce_market_hours = f.enforce_market_hours.checked; b.trading_halted = f.trading_halted.checked;
    await api("/api/admin/settings", { method: "PUT", json: b }); await loadConfig(); $("#saved").textContent = "Saved ✓ and live for all clients";
  };
}

function drawer(title, html) { $("#drawer-title").textContent = title; $("#drawer-body").innerHTML = html; $("#drawer").classList.remove("hidden"); }
$("#drawer-close").onclick = () => $("#drawer").classList.add("hidden");

// ------------------------------------------------------------------ boot
function setMe() {
  const n = S.cfg.prefs.display_name || S.me.name;
  $("#me-name").textContent = n; $("#me-avatar").textContent = n[0].toUpperCase();
  $("#me-role").textContent = S.me.role === "admin" ? "Master · admin" : "Client";
}
async function loadConfig() {
  S.cfg = await api("/api/config"); S.me = { name: S.cfg.user, role: S.cfg.role };
  const m = S.cfg.market, mp = $("#market-pill");
  mp.textContent = m.is_open ? "● NSE open" : "NSE closed"; mp.className = "pill " + (m.is_open ? "open" : "");
  $("#halt-pill").classList.toggle("hidden", !S.cfg.trading_halted);
}
async function start() {
  await loadConfig();
  S.symbol = S.cfg.prefs.default_symbol || S.symbol;
  $("#login").classList.add("hidden"); $("#shell").classList.remove("hidden");
  $("#nav-admin").classList.toggle("hidden", S.me.role !== "admin"); setMe();
  connectWS(); renderApprovals(); route();
  setInterval(() => { loadConfig().catch(() => {}); renderApprovals(); }, 60000);
}
if (S.token) start().catch(() => signOut());
