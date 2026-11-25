// LeadPilot dealer console: a small hash-routed app over the /admin API.
import { request, ApiError } from "./api.js";
import { $, $$, esc, md, cls, pkr, when, dayLabel, parseDate, toast, busy, localISO, themeToggle } from "./ui.js";
import { API } from "./config.js";

const S_KEY = "lp_console_session";
let session = null;
try { session = JSON.parse(sessionStorage.getItem(S_KEY)); } catch (e) {}
let me = null;
let lastLoad = 0;
let navToken = 0;           // newest navigation wins; slower, older responses are dropped
let navCtrl = null;          // removes the previous view's event listeners
let meAt = 0;                // when /admin/me (plan and usage for the banner) was last fetched

// ───────── API with session ─────────
async function api(path, opts = {}) {
  try {
    return await request(path, { ...opts, headers: { Authorization: `Bearer ${session?.session || ""}` } });
  } catch (e) {
    if (e instanceof ApiError && e.status === 401) { signOut("Your session has ended. Please sign in again."); }
    throw e;
  }
}

// ───────── sign in / out ─────────
function showLogin(message = "") {
  $("#app-view").hidden = true; $("#login-view").hidden = false;
  $("#login-error").textContent = message;
  setTimeout(() => $("#key").focus(), 50);
}
function signOut(message = "") {
  session = null; me = null;
  try { sessionStorage.removeItem(S_KEY); } catch (e) {}
  showLogin(message);
}
$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = $("#key").value.trim(), err = $("#login-error");
  err.textContent = "";
  if (!key) { err.textContent = "Enter your dealer key."; $("#key").focus(); return; }
  const btn = $("#login-form button[type=submit]");
  try {
    const s = await busy(btn, "Signing in…", () => request("/admin/login", { method: "POST", body: { key } }));
    session = { session: s.session, expires_at: s.expires_at, business: s.business };
    try { sessionStorage.setItem(S_KEY, JSON.stringify(session)); } catch (ex) {}
    $("#key").value = "";
    await boot();
  } catch (ex) {
    err.textContent = ex.status === 401 ? "That key isn't valid. Check it and try again." : ex.message;
  }
});
$("#logout").addEventListener("click", async () => {
  try { await api("/admin/logout", { method: "POST" }); } catch (e) {}
  signOut("You've signed out.");
});

async function boot() {
  try { me = await api("/admin/me"); } catch (e) { if (!(e instanceof ApiError && e.status === 401)) showLogin(e.message); return; }
  $("#who-name").textContent = me.name;
  $("#who-role").textContent = me.business === "*" ? "LeadPilot team" : "Dealer";
  $("#nav-pilots").hidden = me.business !== "*";
  $("#nav-dealers").hidden = me.business !== "*";
  $("#nav-usage").hidden = me.business === "*";
  meAt = Date.now();
  $("#login-view").hidden = true; $("#app-view").hidden = false;
  if (!location.hash) location.replace("#/overview");
  route();
}

// ───────── routing ─────────
const VIEWS = { overview, pipeline, leads, lead: leadDetail, bookings, followups, settings, pilots, usage,
  dealers: dealersView, dealer: dealerDetail, newdealer: newDealer };
const TITLES = { overview: "Overview", pipeline: "Pipeline", leads: "Leads", lead: "Lead", bookings: "Bookings",
  followups: "Follow-ups", settings: "Install & settings", pilots: "Pilot requests", usage: "Plan & usage",
  dealers: "Dealers", dealer: "Dealer", newdealer: "New dealer" };
function parseHash() {
  const [path, qs] = location.hash.replace(/^#\/?/, "").split("?");
  const [name, id] = path.split("/");
  const params = new URLSearchParams(qs || "");
  if (name === "leads" && id) return { view: "lead", id: decodeURIComponent(id), params };
  if (name === "dealers" && id === "new") return { view: "newdealer", id: null, params };
  if (name === "dealers" && id) return { view: "dealer", id: decodeURIComponent(id), params };
  return { view: VIEWS[name] ? name : "overview", id: null, params };
}
async function route(silent = false) {
  if (!me) return;
  const r = parseHash();
  r.token = ++navToken;
  navCtrl?.abort(); navCtrl = new AbortController(); r.signal = navCtrl.signal;
  r.stale = () => r.token !== navToken;
  $("#page-title").textContent = TITLES[r.view];
  document.title = `${TITLES[r.view]} · LeadPilot console`;
  const navKey = r.view === "lead" ? "leads" : ["dealer", "newdealer"].includes(r.view) ? "dealers" : r.view;
  $$(".rail-nav a").forEach((a) => a.dataset.route === navKey ? a.setAttribute("aria-current", "page") : a.removeAttribute("aria-current"));
  $(".rail").classList.remove("open");
  const view = $("#view");
  if (!silent) { view.classList.remove("view-enter"); view.innerHTML = loading(); void view.offsetWidth; view.classList.add("view-enter"); }
  try {
    if (me.business !== "*" && Date.now() - meAt > 120000) { me = await api("/admin/me"); meAt = Date.now(); }
    await VIEWS[r.view](view, r);
    if (r.stale()) return;
    if (me.business !== "*" && r.view !== "usage") view.insertAdjacentHTML("afterbegin", bannerFor(me.plan));
    $$("[data-w]", view).forEach((el) => { el.style.width = `${Number(el.dataset.w) || 0}%`; });
    lastLoad = Date.now(); stamp();
    if (!silent) view.focus({ preventScroll: true });
  } catch (e) {
    if (r.stale() || (e instanceof ApiError && e.status === 401)) return;
    view.innerHTML = `<div class="error-state" role="alert"><strong>Couldn't load this page.</strong> ${esc(e.message)}
      <div class="mt"><button class="btn btn-sm" data-act="retry">Try again</button></div></div>`;
  }
}
window.addEventListener("hashchange", () => route());
function stamp() { $("#updated").textContent = `Updated ${new Date(lastLoad).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`; }
const loading = () => `<div class="kpis" aria-hidden="true">${Array.from({ length: 6 }, () => `<div class="kpi"><dt class="skeleton">&nbsp;</dt><dd class="skeleton">00</dd></div>`).join("")}</div>
  <div class="panel"><div class="panel-body"><p class="skeleton">&nbsp;</p><p class="skeleton">&nbsp;</p><p class="skeleton">&nbsp;</p></div></div><span class="sr-only">Loading…</span>`;

$("#refresh").addEventListener("click", () => route());
$("#view").addEventListener("click", (e) => { if (e.target.closest("[data-act=retry]")) route(); });
$("#rail-toggle").addEventListener("click", () => {
  const open = $(".rail").classList.toggle("open"); $("#rail-toggle").setAttribute("aria-expanded", String(open));
});
$("#search").addEventListener("submit", (e) => {
  e.preventDefault(); location.hash = `#/leads?q=${encodeURIComponent($("#q").value.trim())}`;
});
document.addEventListener("keydown", (e) => {
  const typing = /INPUT|TEXTAREA|SELECT/.test(document.activeElement?.tagName);
  if (e.key === "/" && !typing && me) { e.preventDefault(); $("#q").focus(); }
});
// Gentle auto-refresh: only while the tab is visible and the dealer isn't editing something.
setInterval(() => {
  const r = parseHash();
  const editing = $("#view").contains(document.activeElement) && /TEXTAREA|SELECT|INPUT/.test(document.activeElement.tagName);
  if (me && !document.hidden && !editing && ["overview", "pipeline", "followups", "bookings"].includes(r.view) && Date.now() - lastLoad > 55000) route(true);
}, 15000);

// ───────── shared bits ─────────
const scoreTag = (s) => `<span class="tag ${cls(s)}">${esc(s || "cold")}</span>`;
const statusTag = (s) => `<span class="tag ${cls(s)}">${esc(s)}</span>`;
const leadName = (l) => l.name || (l.phone ? l.phone : "Anonymous buyer");
const wants = (l) => l.category === "car" ? "Car" : l.category === "property" ? "Property" : "Not said";
const empty = (title, text, action = "") => `<div class="empty-state"><p class="h3">${title}</p><p>${text}</p>${action}</div>`;
function setBadge(n) { const b = $("#fu-badge"); b.textContent = n; b.hidden = !n; }

// ───────── overview ─────────
async function overview(view, r) {
  const [s, hot, books] = await Promise.all([api("/admin/stats"), api("/admin/leads?score=hot&limit=40"), api("/admin/bookings?limit=200")]);
  if (r.stale()) return;
  setBadge(s.pending_followups);
  const total = s.leads || 1;
  const today = localISO();
  const upcoming = books.filter((b) => b.status === "confirmed" && b.date >= today).slice(0, 5);
  const attention = hot.filter((l) => l.status !== "booked" && l.status !== "lost").slice(0, 6);
  const kpi = (label, value, sub, extra = "", klass = "") => `<div class="kpi ${klass}"><dt>${label}</dt><dd>${value}</dd><small>${sub}</small>${extra}</div>`;
  view.innerHTML = `
    <dl class="kpis">
      ${kpi("Leads", s.leads, `${s.hot} hot · ${s.warm} warm · ${s.cold} cold`,
        `<div class="mix" aria-hidden="true"><span class="h" data-w="${100 * s.hot / total}"></span><span class="w" data-w="${100 * s.warm / total}"></span><span class="c" data-w="${100 * s.cold / total}"></span></div>`)}
      ${kpi("Visits booked", s.bookings, `${s.upcoming_bookings} upcoming`, "", "signal")}
      ${kpi("Conversion", `${s.conversion_pct}%`, "of leads booked a visit")}
      ${kpi("First reply", s.avg_first_response_s != null ? `${s.avg_first_response_s}s` : "–", "average, web form leads")}
      ${kpi("Reply time", s.avg_reply_s != null ? `${s.avg_reply_s}s` : "–", `${s.messages} messages · ${s.blocked} blocked`)}
      ${kpi("AI cost", `$${Number(s.cost_usd).toFixed(3)}`, `${(s.tokens_in / 1000).toFixed(0)}k in · ${(s.tokens_out / 1000).toFixed(0)}k out tokens`)}
    </dl>
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><h2>Last 14 days</h2>
        <div class="legend"><span class="l1"><i></i>New leads</span><span class="l2"><i></i>Messages</span></div></div>
        <div class="panel-body">${chart(s.series)}</div></section>
      <section class="panel"><div class="panel-head"><h2>Pipeline</h2><a class="small" href="#/pipeline">Open board →</a></div>
        <div class="panel-body funnel">${["new", "qualified", "booked", "lost"].map((k) => `
          <div class="funnel-row ${k}"><span>${k[0].toUpperCase() + k.slice(1)}</span><span class="track"><span class="fill" data-w="${Math.round(100 * (s.funnel[k] || 0) / total)}"></span></span><span class="n">${s.funnel[k] || 0}</span></div>`).join("")}
        </div></section>
    </div>
    <div class="grid-2b mt">
      <section class="panel"><div class="panel-head"><h2>Needs attention</h2><span class="muted">Hot leads without a visit</span></div>
        ${attention.length ? `<ul class="list">${attention.map((l) => `<li><a href="#/leads/${encodeURIComponent(l.user_id)}">
          <span class="t">${esc(leadName(l))}</span><span class="r">${scoreTag(l.score)}<span class="mono small muted">${when(l.last_customer_at || l.created_at)}</span></span>
          <span class="s">${esc(wants(l))} · ${l.budget_pkr ? pkr(l.budget_pkr) : "budget not given"} · ${esc(l.timeline || "timeline not given")}</span></a></li>`).join("")}</ul>`
          : empty("All caught up.", "Every hot lead has a visit booked.")}
        ${s.pending_followups ? `<div class="panel-body"><a class="btn btn-sm btn-primary" href="#/followups">${s.pending_followups} follow-up${s.pending_followups > 1 ? "s" : ""} waiting for approval →</a></div>` : ""}
      </section>
      <section class="panel"><div class="panel-head"><h2>Upcoming visits</h2><a class="small" href="#/bookings">All bookings →</a></div>
        ${upcoming.length ? `<ul class="list">${upcoming.map((b) => `<li><a href="#/leads/${encodeURIComponent(b.user_id)}">
          <span class="t">${esc(b.title)} <span class="mono muted small">${esc(b.listing_id)}</span></span><span class="r"><span class="mono">${esc(b.time)}</span><span class="small muted">${esc(dayLabel(b.date))}</span></span>
          <span class="s">${esc(b.kind)} · ${esc(b.name || "Buyer")}${b.phone ? " · " + esc(b.phone) : ""}</span></a></li>`).join("")}</ul>`
          : empty("No visits coming up.", "Bookings made by the assistant appear here.")}
      </section>
    </div>`;
}
function chart(series) {
  const W = 640, H = 220, L = 30, R = 34, T = 12, B = 26, n = series.length;
  const maxL = Math.max(1, ...series.map((d) => d.leads)), maxM = Math.max(1, ...series.map((d) => d.messages));
  const bw = (W - L - R) / n, y = (v, m) => T + (H - T - B) * (1 - v / m);
  const bars = series.map((d, i) => `<rect class="bar" x="${L + i * bw + bw * 0.22}" y="${y(d.leads, maxL)}" width="${bw * 0.56}" height="${H - B - y(d.leads, maxL)}"><title>${d.date}: ${d.leads} new leads, ${d.messages} messages</title></rect>`).join("");
  const pts = series.map((d, i) => [L + i * bw + bw / 2, y(d.messages, maxM)]);
  const line = `<path class="line" d="${pts.map((p, i) => `${i ? "L" : "M"}${p[0].toFixed(1)} ${p[1].toFixed(1)}`).join(" ")}"/>` + pts.map((p) => `<circle class="pt" cx="${p[0]}" cy="${p[1]}" r="2.5"/>`).join("");
  const grid = [0, 0.5, 1].map((f) => `<line class="grid" x1="${L}" x2="${W - R}" y1="${y(maxL * f, maxL)}" y2="${y(maxL * f, maxL)}"/><text class="axis" x="${L - 6}" y="${y(maxL * f, maxL) + 3}" text-anchor="end">${Math.round(maxL * f)}</text><text class="axis" x="${W - R + 6}" y="${y(maxM * f, maxM) + 3}">${Math.round(maxM * f)}</text>`).join("");
  const labels = series.map((d, i) => (n - 1 - i) % 2 === 0 ? `<text class="axis" x="${L + i * bw + bw / 2}" y="${H - 8}" text-anchor="middle">${parseDate(d.date).toLocaleDateString([], { day: "numeric", month: "short" })}</text>` : "").join("");
  return `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="New leads and messages per day for the last 14 days">${grid}${bars}${line}${labels}</svg>`;
}

// ───────── pipeline (drag to change status) ─────────
const STAGES = [["new", "New"], ["qualified", "Qualified"], ["booked", "Booked"], ["lost", "Lost"]];
async function pipeline(view, r) {
  const list = await api("/admin/leads?limit=200");
  if (r.stale()) return;
  const order = { hot: 0, warm: 1, cold: 2 };
  view.innerHTML = `<p class="muted small">Drag a card to move a lead between stages. Hot leads sit on top.</p><div class="board">${STAGES.map(([k, label]) => {
    const items = list.filter((l) => l.status === k).sort((a, b) => (order[a.score] ?? 3) - (order[b.score] ?? 3));
    return `<section class="col ${k}" data-status="${k}" aria-label="${label}"><div class="col-head"><h2>${label}</h2><span class="n">${items.length}</span></div>
      <div class="col-body">${items.map(card).join("") || `<p class="col-empty">No leads here</p>`}</div></section>`;
  }).join("")}</div>`;
  let dragged = null;
  view.addEventListener("dragstart", (e) => { const c = e.target.closest(".lead-card"); if (!c) return; dragged = c; c.classList.add("dragging"); e.dataTransfer.setData("text/plain", c.dataset.id); }, { signal: r.signal });
  view.addEventListener("dragend", () => { dragged?.classList.remove("dragging"); $$(".col", view).forEach((c) => c.classList.remove("drop")); }, { signal: r.signal });
  view.addEventListener("dragover", (e) => { const col = e.target.closest(".col"); if (!col) return; e.preventDefault(); $$(".col", view).forEach((c) => c.classList.toggle("drop", c === col)); }, { signal: r.signal });
  view.addEventListener("drop", async (e) => {
    const col = e.target.closest(".col"); if (!col || !dragged) return;
    e.preventDefault();
    const id = dragged.dataset.id, status = col.dataset.status;
    if (dragged.closest(".col") === col) return;
    try {
      await api(`/admin/leads/${encodeURIComponent(id)}/status`, { method: "POST", body: { status } });
      toast(`Moved to ${status}`); route(true);
    } catch (ex) { toast(ex.message, { error: true }); }
  }, { signal: r.signal });
}
function card(l) {
  return `<a class="lead-card" draggable="true" data-id="${esc(l.user_id)}" href="#/leads/${encodeURIComponent(l.user_id)}">
    <span class="top"><strong>${esc(leadName(l))}</strong>${scoreTag(l.score)}</span>
    <span class="meta">${esc(wants(l))}${l.budget_pkr ? " · " + pkr(l.budget_pkr) : ""}${l.timeline ? " · " + esc(l.timeline) : ""}</span>
    <span class="foot"><span>${esc(l.source || "chat")}</span><span>${when(l.last_customer_at || l.created_at)}</span></span></a>`;
}

// ───────── leads table ─────────
async function leads(view, r) {
  const q = r.params.get("q") || "", status = r.params.get("status") || "", score = r.params.get("score") || "";
  $("#q").value = q;
  let offset = 0;
  const qs = () => new URLSearchParams(Object.entries({ q, status, score, limit: 100, offset }).filter(([, v]) => v !== "")).toString();
  const first = await api(`/admin/leads?${qs()}`);
  if (r.stale()) return;
  const opt = (v, cur, label) => `<option value="${v}" ${v === cur ? "selected" : ""}>${label}</option>`;
  view.innerHTML = `
    <form class="table-tools" id="lead-filters">
      <div class="field"><label for="lf-status">Stage</label><select class="select" id="lf-status">${opt("", status, "All stages")}${STAGES.map(([k, l]) => opt(k, status, l)).join("")}</select></div>
      <div class="field"><label for="lf-score">Score</label><select class="select" id="lf-score">${opt("", score, "All scores")}${["hot", "warm", "cold"].map((k) => opt(k, score, k[0].toUpperCase() + k.slice(1))).join("")}</select></div>
      ${q ? `<p class="small">Searching for <strong>“${esc(q)}”</strong> · <a href="#/leads">clear</a></p>` : ""}
    </form>
    ${first.length ? `<div class="table-wrap"><table><thead><tr><th>Lead</th><th>Wants</th><th>Budget</th><th>Timeline</th><th>Score</th><th>Stage</th><th>Last active</th></tr></thead>
      <tbody id="lead-rows">${first.map(row).join("")}</tbody></table></div>
      ${first.length === 100 ? `<div class="more"><button class="btn btn-sm" id="more">Load more</button></div>` : ""}`
      : empty(q || status || score ? "No leads match." : "No leads yet.", q || status || score ? "Try a different search or filter." : "Leads appear here the moment a buyer chats or fills a form.")}`;
  $("#lead-filters").addEventListener("change", () => {
    const p = new URLSearchParams(Object.entries({ q, status: $("#lf-status").value, score: $("#lf-score").value }).filter(([, v]) => v));
    location.hash = `#/leads${p.toString() ? "?" + p : ""}`;
  });
  $("#more")?.addEventListener("click", async (e) => {
    offset += 100;
    const next = await busy(e.target, "Loading…", () => api(`/admin/leads?${qs()}`));
    $("#lead-rows").insertAdjacentHTML("beforeend", next.map(row).join(""));
    if (next.length < 100) e.target.remove();
  });
  view.addEventListener("click", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr) location.hash = `#/leads/${encodeURIComponent(tr.dataset.id)}`; }, { signal: r.signal });
  view.addEventListener("keydown", (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && e.key === "Enter") location.hash = `#/leads/${encodeURIComponent(tr.dataset.id)}`; }, { signal: r.signal });
}
function row(l) {
  return `<tr class="clickable" tabindex="0" data-id="${esc(l.user_id)}"><td><strong>${esc(leadName(l))}</strong><span class="sub">${esc(l.phone || l.source || "")}</span></td>
    <td>${esc(wants(l))}<span class="sub">${esc((l.preferences || "").slice(0, 46))}</span></td><td class="num">${l.budget_pkr ? pkr(l.budget_pkr, { short: true }) : "–"}</td>
    <td>${esc(l.timeline || "–")}</td><td>${scoreTag(l.score)}</td><td>${statusTag(l.status)}</td><td class="num">${when(l.last_customer_at || l.created_at)}</td></tr>`;
}

// ───────── lead detail ─────────
const TOOL_LABEL = { search_inventory: "Searched inventory", get_listing: "Opened a listing", check_availability: "Checked availability",
  book_visit: "Booked a visit", my_bookings: "Listed bookings", cancel_booking: "Cancelled a booking", save_lead_details: "Saved lead details", search_faq: "Searched policies" };
async function leadDetail(view, r) {
  const d = await api(`/admin/leads/${encodeURIComponent(r.id)}`);
  if (r.stale()) return;
  const l = d.lead;
  $("#page-title").textContent = leadName(l);
  const threads = Object.keys(d.conversations);
  const mem = Object.entries(d.memory).filter(([k]) => k !== "budget_pkr");
  view.innerHTML = `
    <a class="back" href="#/leads">← All leads</a>
    <header class="lead-head">
      <div><span class="mono small muted">${esc(l.user_id)} · via ${esc(l.source || "chat")} · since ${when(l.created_at)}</span>
        <h2>${esc(leadName(l))}</h2>
        <div class="facts">${scoreTag(l.score)}<span>${esc(wants(l))}</span>${l.budget_pkr ? `<span class="mono">${pkr(l.budget_pkr)}</span>` : ""}
          ${l.timeline ? `<span>${esc(l.timeline)}</span>` : ""}${l.phone ? `<a href="tel:${esc(l.phone.replace(/[^0-9+]/g, ""))}">${esc(l.phone)}</a>` : ""}
          ${l.first_response_ms ? `<span class="mono muted">first reply ${(l.first_response_ms / 1000).toFixed(1)}s</span>` : ""}</div></div>
      <div class="lead-actions">
        <div class="field"><label for="ld-status">Stage</label><select class="select" id="ld-status">${STAGES.map(([k, lab]) => `<option value="${k}" ${k === l.status ? "selected" : ""}>${lab}</option>`).join("")}</select></div>
        <button class="btn btn-danger" id="ld-delete" title="When a buyer asks you to delete their data">Delete data</button>
      </div>
    </header>
    <div class="detail-grid">
      <section class="panel">
        <div class="panel-head"><h2>Conversations</h2><span class="muted">${threads.length} thread${threads.length === 1 ? "" : "s"}</span></div>
        ${threads.length ? `<div class="tabs" role="tablist">${threads.map((t, i) => `<button role="tab" data-thread="${esc(t)}" aria-selected="${i === threads.length - 1}">${esc(t)}</button>`).join("")}</div>
          <div class="transcript" id="transcript"></div>` : empty("No messages yet.", "This lead came in but hasn't chatted.")}
      </section>
      <div class="stack gap-20">
        <section class="panel"><div class="panel-head"><h2>What LeadPilot remembers</h2><span class="muted">long-term memory</span></div>
          <div class="panel-body">${mem.length ? `<dl class="kv">${mem.map(([k, v]) => `<dt>${esc(k.replace(/_/g, " "))}</dt><dd>${esc(v)}</dd>`).join("")}</dl>` : `<p class="muted">Nothing saved yet.</p>`}</div></section>
        <section class="panel"><div class="panel-head"><h2>Bookings</h2></div>
          ${d.bookings.length ? `<ul class="list">${d.bookings.map((b) => `<li><div class="row"><span class="t">${esc(b.title)} <span class="mono small muted">${esc(b.listing_id)}</span></span>
            <span class="r">${statusTag(b.status)}</span><span class="s">${esc(b.kind)} · ${esc(dayLabel(b.date))} at ${esc(b.time)}</span></div></li>`).join("")}</ul>` : `<p class="panel-body muted">No visits booked yet.</p>`}</section>
        <section class="panel"><div class="panel-head"><h2>Follow-ups</h2></div>
          ${d.followups.length ? `<ul class="list">${d.followups.map((f) => `<li><div class="row"><span class="t">Follow-up ${esc(f.step)} of 3</span><span class="r">${statusTag(f.status)}</span>
            <span class="s">${esc(f.message.slice(0, 120))}${f.message.length > 120 ? "…" : ""}</span></div></li>`).join("")}</ul>` : `<p class="panel-body muted">None yet.</p>`}</section>
        <section class="panel"><div class="panel-head"><h2>Agent activity</h2><span class="muted">tool audit log</span></div>
          <div class="panel-body">${d.tool_audit.length ? `<ol class="timeline">${d.tool_audit.map((a) => `<li><div><strong>${esc(TOOL_LABEL[a.tool] || a.tool)}</strong>
            <span class="when">${when(a.created_at)}</span><code>${esc(JSON.stringify(a.args)).slice(0, 140)}</code></div></li>`).join("")}</ol>` : `<p class="muted">No tool calls yet.</p>`}</div></section>
      </div>
    </div>`;
  const showThread = (t) => {
    $$(".tabs [role=tab]", view).forEach((b) => b.setAttribute("aria-selected", String(b.dataset.thread === t)));
    const box = $("#transcript");
    box.innerHTML = (d.conversations[t] || []).map((m) => `<div class="tmsg ${m.role === "user" ? "user" : "assistant"} ${m.kind === "followup" ? "followup" : ""}">
      <span class="who">${m.role === "user" ? "Buyer" : m.kind === "followup" ? "Follow-up (approved)" : m.kind === "guard" ? "Guard" : "LeadPilot"}</span>
      <div class="b">${m.role === "user" ? esc(m.text) : md(m.text)}</div></div>`).join("") || `<p class="muted">Empty thread.</p>`;
    box.scrollTop = box.scrollHeight;
  };
  if (threads.length) showThread(threads[threads.length - 1]);
  $(".tabs", view)?.addEventListener("click", (e) => { const b = e.target.closest("[data-thread]"); if (b) showThread(b.dataset.thread); });
  $("#ld-status").addEventListener("change", async (e) => {
    try { await api(`/admin/leads/${encodeURIComponent(l.user_id)}/status`, { method: "POST", body: { status: e.target.value } }); toast(`Stage set to ${e.target.value}`); }
    catch (ex) { toast(ex.message, { error: true }); e.target.value = l.status; }
  });
  $("#ld-delete").addEventListener("click", async (e) => {
    if (!confirm(`Permanently delete everything about ${leadName(l)}: conversations, memory, bookings and follow-ups? This can't be undone.`)) return;
    try { await busy(e.target, "Deleting…", () => api(`/admin/leads/${encodeURIComponent(l.user_id)}`, { method: "DELETE" })); toast("Lead data deleted"); location.hash = "#/leads"; }
    catch (ex) { toast(ex.message, { error: true }); }
  });
}

// ───────── bookings agenda ─────────
async function bookings(view, r) {
  const all = await api("/admin/bookings?limit=200");
  if (r.stale()) return;
  const mode = r.params.get("show") || "upcoming";
  const today = localISO();
  const list = all.filter((b) => mode === "all" ? true : mode === "past" ? b.date < today : b.date >= today && b.status !== "cancelled");
  if (mode === "past") list.reverse();
  const days = [...new Set(list.map((b) => b.date))];
  view.innerHTML = `<div class="seg" role="group" aria-label="Show">${[["upcoming", "Upcoming"], ["past", "Past"], ["all", "All"]].map(([k, l]) =>
      `<button aria-pressed="${k === mode}" data-show="${k}">${l}</button>`).join("")}</div>
    ${days.length ? days.map((day) => `<section class="agenda-day"><div class="agenda-date"><strong>${esc(dayLabel(day))}</strong><span>${esc(day)}</span></div>
      <div class="agenda-items">${list.filter((b) => b.date === day).map((b) => `<div class="appt">
        <span class="time">${esc(b.time)}</span>
        <div class="what"><strong>${esc(b.title)} <span class="mono small muted">${esc(b.listing_id)}</span></strong>
          <span>${esc(b.kind)} · <a href="#/leads/${encodeURIComponent(b.user_id)}">${esc(b.name || "Buyer")}</a>${b.phone ? " · " + esc(b.phone) : ""} · ${esc(b.location || "")}</span></div>
        <div class="acts">${statusTag(b.status)}${b.status === "confirmed" ? `<button class="btn btn-sm" data-bstat="completed" data-id="${esc(b.id)}">Mark done</button>
          <button class="btn btn-sm btn-danger" data-bstat="cancelled" data-id="${esc(b.id)}">Cancel</button>` : ""}</div></div>`).join("")}</div></section>`).join("")
      : empty(mode === "past" ? "No past visits." : "No upcoming visits.", "Visits and test drives booked by the assistant appear here.")}`;
  view.addEventListener("click", async (e) => {
    const s = e.target.closest("[data-show]"); if (s) { location.hash = `#/bookings?show=${s.dataset.show}`; return; }
    const b = e.target.closest("[data-bstat]"); if (!b) return;
    if (b.dataset.bstat === "cancelled" && !confirm("Cancel this visit? Let the buyer know as well.")) return;
    try { await busy(b, "Saving…", () => api(`/admin/bookings/${b.dataset.id}/status`, { method: "POST", body: { status: b.dataset.bstat } }));
      toast(b.dataset.bstat === "completed" ? "Marked as done" : "Visit cancelled"); route(true); }
    catch (ex) { toast(ex.message, { error: true }); }
  }, { signal: r.signal });
}

// ───────── follow-ups (human approval) ─────────
async function followups(view, r) {
  const list = await api("/admin/followups?limit=200");
  if (r.stale()) return;
  const drafts = list.filter((f) => f.status === "draft"), history = list.filter((f) => f.status !== "draft");
  setBadge(drafts.length);
  view.innerHTML = `
    <div class="fu-bar"><button class="btn btn-primary" data-run="0">Run follow-up agent</button>
      <button class="btn" data-run="1" title="Skip the day 1/3/7 wait. Useful for demos.">Draft now, ignore wait</button>
      <span class="small muted">Drafts nudges for quiet leads (day 1, 3 and 7), up to 20 per run. Nothing is sent until you approve it.</span></div>
    <section class="panel"><div class="panel-head"><h2>Waiting for your approval</h2><span class="muted">${drafts.length}</span></div>
      ${drafts.length ? drafts.map((f) => `<article class="draft" data-id="${esc(f.id)}">
        <div class="top"><a href="#/leads/${encodeURIComponent(f.user_id)}">${esc(f.name || "Buyer")}</a>${scoreTag(f.score)}<span class="mono small muted">follow-up ${esc(f.step)} of 3 · drafted ${when(f.created_at)}</span></div>
        <label class="sr-only" for="fu-${esc(f.id)}">Message</label>
        <textarea class="textarea" id="fu-${esc(f.id)}" maxlength="1000">${esc(f.message)}</textarea>
        <div class="acts"><button class="btn btn-sm btn-primary" data-approve="${esc(f.id)}">Approve &amp; send</button><button class="btn btn-sm" data-skip="${esc(f.id)}">Skip</button>
          <span class="count">${f.message.length}/1000</span></div></article>`).join("")
        : empty("No drafts waiting.", "Run the follow-up agent to draft nudges for leads who went quiet.")}
    </section>
    <section class="panel mt"><div class="panel-head"><h2>History</h2></div>
      ${history.length ? `<div class="table-wrap"><table><thead><tr><th>Lead</th><th>Message</th><th>Status</th><th>When</th></tr></thead><tbody>
        ${history.map((f) => `<tr><td>${esc(f.name || "Buyer")}</td><td>${esc(f.message)}</td><td>${statusTag(f.status)}</td><td class="num">${when(f.sent_at || f.created_at)}</td></tr>`).join("")}</tbody></table></div>`
        : `<p class="panel-body muted">Sent and skipped follow-ups appear here.</p>`}</section>`;
  view.addEventListener("input", (e) => { const t = e.target.closest(".draft textarea"); if (t) t.closest(".draft").querySelector(".count").textContent = `${t.value.length}/1000`; }, { signal: r.signal });
  view.addEventListener("click", async (e) => {
    const run = e.target.closest("[data-run]"), ap = e.target.closest("[data-approve]"), sk = e.target.closest("[data-skip]");
    try {
      if (run) {
        const r = await busy(run, "Drafting…", () => api(`/admin/followups/run?ignore_wait=${run.dataset.run === "1"}`, { method: "POST", timeout: 180000 }));
        toast(r.drafted ? `${r.drafted} follow-up${r.drafted > 1 ? "s" : ""} drafted` : "No leads are due for a follow-up right now"); route(true);
      } else if (ap) {
        const text = $(`#fu-${ap.dataset.approve}`).value.trim();
        if (!text) { toast("The message is empty.", { error: true }); return; }
        await busy(ap, "Sending…", () => api(`/admin/followups/${ap.dataset.approve}/approve`, { method: "POST", body: { message: text } }));
        toast("Sent to the buyer's chat"); route(true);
      } else if (sk) {
        await busy(sk, "Skipping…", () => api(`/admin/followups/${sk.dataset.skip}/skip`, { method: "POST" }));
        toast("Skipped"); route(true);
      }
    } catch (ex) { toast(ex.message, { error: true }); }
  }, { signal: r.signal });
}

// ───────── install & settings ─────────
async function settings(view, r) {
  const dealers = me.business === "*" ? (await api("/admin/owner/overview")).dealers : [{ id: me.business, name: me.name, widget_key: me.widget_key }];
  if (r.stale()) return;
  const keyOf = (id) => (dealers.find((d) => d.id === id) || dealers[0] || {}).widget_key;
  const mine = dealers[0]?.id;
  const snippet = (id) => snippetFor(keyOf(id));
  const exp = session?.expires_at ? new Date(session.expires_at * 1000) : null;
  view.innerHTML = `
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><h2>Install the chat on your website</h2></div>
        <div class="panel-body">
          <ol class="steps"><li><span>Paste these two lines before <code>&lt;/body&gt;</code> on every page of your website.</span></li>
            <li><span>The chat button appears bottom-right. It only shows and books <strong>your</strong> stock.</span></li>
            <li><span>New leads appear in this console as they come in.</span></li></ol>
          ${me.business === "*" ? `<div class="field narrow"><label for="snip-dealer">Dealer</label>
            <select class="select" id="snip-dealer">${dealers.map((d) => `<option value="${esc(d.id)}">${esc(d.name)}</option>`).join("")}</select></div>` : ""}
          <div class="snippet-bar"><span class="mono small">HTML</span><button class="btn btn-sm" id="copy">Copy snippet</button></div><div class="snippet" id="snippet">${snippet(mine)}</div>
          <p class="small muted mt">The key in the snippet is public: it only names your business. Your console key is different; never put it on a website.</p>
        </div></section>
      <div class="stack gap-20">
        <section class="panel"><div class="panel-head"><h2>Your session</h2></div>
          <div class="panel-body"><dl class="kv"><dt>Signed in as</dt><dd>${esc(me.name)}</dd><dt>Access</dt><dd>${me.business === "*" ? "All dealers" : "Your leads only"}</dd>
            <dt>Expires</dt><dd>${exp ? exp.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : "–"}</dd></dl>
            <p class="small muted">Sessions last 8 hours. Signing out ends the session on the server.</p></div></section>
        <section class="panel"><div class="panel-head"><h2>Agent API</h2></div>
          <div class="panel-body"><dl class="kv"><dt>Chat</dt><dd><code>POST /chat</code></dd><dt>Health</dt><dd><code>GET /health</code></dd><dt>Base URL</dt><dd><code>${esc(API)}</code></dd></dl></div></section>
        <section class="panel"><div class="panel-head"><h2>Data &amp; privacy</h2></div>
          <div class="panel-body small"><p>Buyers can delete their own data from the chat. To handle a deletion request yourself, open the lead and choose <strong>Delete data</strong>.</p>
            <p><a href="/privacy.html" target="_blank" rel="noopener">Privacy policy</a> · <a href="/.well-known/security.txt">Report a vulnerability</a></p></div></section>
      </div>
    </div>`;
  $("#snip-dealer")?.addEventListener("change", (e) => { $("#snippet").innerHTML = snippet(e.target.value); });
  view.addEventListener("click", async (e) => {
    if (!e.target.closest("#copy")) return;
    try { await navigator.clipboard.writeText($("#snippet").textContent.trim()); toast("Snippet copied"); }
    catch (ex) { toast("Select the snippet and copy it manually.", { error: true }); }
  }, { signal: r.signal });
}

// ───────── pilot requests (LeadPilot team only) ─────────
async function pilots(view, r) {
  if (me.business !== "*") { view.innerHTML = empty("Not available.", "Only the LeadPilot team can see pilot requests."); return; }
  const list = await api("/admin/pilot-requests");
  if (r.stale()) return;
  view.innerHTML = list.length ? `<div class="table-wrap"><table><thead><tr><th>Dealership</th><th>Contact</th><th>Sells</th><th>Leads / month</th><th>Message</th><th>Received</th><th>Status</th></tr></thead>
    <tbody>${list.map((p) => `<tr><td><strong>${esc(p.company)}</strong></td><td>${esc(p.name)}<span class="sub">${esc(p.phone)}${p.email ? " · " + esc(p.email) : ""}</span></td>
      <td>${esc(p.dealer_type)}</td><td class="num">${esc(p.monthly_leads || "–")}</td><td>${esc(p.message || "–")}</td><td class="num">${when(p.created_at)}</td>
      <td><select class="select" data-pilot="${esc(p.id)}" aria-label="Status for ${esc(p.company)}">${["new", "contacted", "closed"].map((s) => `<option ${s === p.status ? "selected" : ""}>${s}</option>`).join("")}</select></td></tr>`).join("")}</tbody></table></div>`
    : empty("No pilot requests yet.", "Requests from the landing page appear here.");
  view.addEventListener("change", async (e) => {
    const s = e.target.closest("[data-pilot]"); if (!s) return;
    try { await api(`/admin/pilot-requests/${s.dataset.pilot}/status`, { method: "POST", body: { status: s.value } }); toast("Updated"); }
    catch (ex) { toast(ex.message, { error: true }); }
  }, { signal: r.signal });
}

// ───────── plans and usage ─────────
const n = (v) => Number(v || 0).toLocaleString();
const usd = (v, d = 2) => `$${Number(v || 0).toFixed(d)}`;
const levelOf = (s) => (s.limit == null ? "" : s.percent >= 100 ? "critical" : s.percent >= 80 ? "warning" : "");
function widths(root) { $$("[data-w]", root).forEach((el) => { el.style.width = `${Math.min(100, Number(el.dataset.w) || 0)}%`; }); }
function meter(s) {
  if (s.limit == null) return `<div class="meter"><div class="big">${n(s.conversations)}<small>conversations in ${esc(s.month)} · unlimited plan</small></div></div>`;
  const pct = Math.min(100, s.percent || 0);
  return `<div class="meter ${levelOf(s)}"><div class="big">${n(s.conversations)}<small>of ${n(s.limit)} conversations · ${esc(s.month)}</small></div>
    <div class="track" role="progressbar" aria-label="Conversations used this month" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${pct}"><span class="fill" data-w="${pct}"></span></div>
    <div class="ticks"><span>0</span><span>80% · warning</span><span>100% · capture-only</span></div></div>`;
}
function planFacts(s) {
  return `<dl class="kv"><dt>Plan</dt><dd>${esc(s.plan_name)} · ${s.price_usd ? `$${s.price_usd}/month` : "free"}</dd>
    <dt>Included</dt><dd>${s.included == null ? "Unlimited" : `${n(s.included)} conversations / month`}</dd>
    <dt>Extra credits</dt><dd>${n(s.extra_credits)} left${s.credits_used_this_month ? ` · ${n(s.credits_used_this_month)} used this month` : ""}</dd>
    <dt>Capture-only</dt><dd>${n(s.blocked)} new chat${s.blocked === 1 ? "" : "s"} answered without AI this month</dd>
    <dt>Follow-ups</dt><dd>${s.followups ? "Included" : "Not on this plan"}</dd>
    <dt>Status</dt><dd>${statusTag(s.status)}</dd></dl>`;
}
function bannerFor(s) {
  if (!s || s.limit == null) return "";
  if (s.status !== "active") return `<div class="plan-banner critical" role="status"><strong>Your account is paused.</strong> New buyers get a "we'll call you back" reply and their details are saved. Contact LeadPilot to switch the AI back on.</div>`;
  const lvl = levelOf(s);
  if (!lvl) return "";
  return `<div class="plan-banner ${lvl}" role="status"><strong>${lvl === "critical" ? "Monthly limit reached." : `${Math.floor(s.percent)}% of this month's conversations used.`}</strong>
    ${lvl === "critical" ? "New buyers get a capture-only reply until you upgrade or add credits." : `${n(s.conversations)} of ${n(s.limit)}.`}<a href="#/usage">Plan &amp; usage →</a></div>`;
}

async function usage(view, r) {
  if (me.business === "*") { location.replace("#/dealers"); return; }
  const u = await api("/admin/usage");
  if (r.stale()) return;
  me.plan = u.summary;
  const s = u.summary;
  const unread = u.alerts.filter((a) => !a.read).length;
  view.innerHTML = `
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><h2>This month</h2><span class="muted">resets on the 1st (UTC)</span></div>
        <div class="panel-body">${meter(s)}
          <p class="small muted mt">One conversation = one buyer chat thread in a month, however many messages it has. Messages our safety guard blocks never count.
          At 100%, conversations already started can finish; new ones get a polite "a sales officer will call you" reply and the lead is still saved here.</p></div></section>
      <section class="panel"><div class="panel-head"><h2>Your plan</h2></div><div class="panel-body">${planFacts(s)}
        <p class="small muted">To upgrade or buy extra conversations, contact the LeadPilot team. Changes apply instantly.</p></div></section>
    </div>
    <section class="panel mt"><div class="panel-head"><h2>Alerts</h2>${unread ? `<button class="btn btn-sm" id="mark-read">Mark ${unread} as read</button>` : `<span class="muted">all read</span>`}</div>
      ${u.alerts.length ? `<ul class="alerts">${u.alerts.map((a) => `<li class="${a.read ? "" : "unread"}">${statusTag(a.level)}<span>${esc(a.message)}</span><span class="mono small muted">${when(a.created_at)}</span></li>`).join("")}</ul>`
        : `<p class="panel-body muted">No alerts. You'll see one here at 80% and at 100% of your monthly conversations.</p>`}</section>
    <div class="grid-2 mt">
      <section class="panel"><div class="panel-head"><h2>Recent months</h2></div>
        ${u.history.length ? `<div class="table-wrap"><table><thead><tr><th>Month</th><th>Conversations</th><th>From credits</th><th>Capture-only</th><th>AI replies</th></tr></thead><tbody>
          ${u.history.map((h) => `<tr><td class="mono">${esc(h.month)}</td><td class="num">${n(h.conversations)}</td><td class="num">${n(h.credits_used)}</td><td class="num">${n(h.blocked)}</td><td class="num">${n(h.ai_turns)}</td></tr>`).join("")}</tbody></table></div>`
          : `<p class="panel-body muted">Usage appears after the first conversation.</p>`}</section>
      <section class="panel"><div class="panel-head"><h2>Plans</h2></div>${plansTable(u.plans, s.plan)}</section>
    </div>`;
  $("#mark-read")?.addEventListener("click", async (e) => {
    try { await busy(e.target, "Saving…", () => api("/admin/alerts/read", { method: "POST" })); route(true); } catch (ex) { toast(ex.message, { error: true }); }
  });
}
function plansTable(plans, current) {
  const rows = Object.entries(plans).filter(([k]) => k !== "internal");
  return `<div class="table-wrap"><table><thead><tr><th>Plan</th><th>Price</th><th>Conversations</th><th>Follow-ups</th><th>Websites</th></tr></thead><tbody>
    ${rows.map(([k, p]) => `<tr><td><strong>${esc(p.name)}</strong>${k === current ? ` <span class="tag ok">current</span>` : ""}</td><td class="num">${p.price_usd ? `$${p.price_usd}` : "Free"}</td>
      <td class="num">${p.conversations == null ? "Unlimited" : n(p.conversations)}</td><td>${p.followups ? "Yes" : "–"}</td><td class="num">${p.websites == null ? "Unlimited" : p.websites}</td></tr>`).join("")}</tbody></table></div>`;
}

// ───────── owner: dealers ─────────
const ownerOnly = (view) => { view.innerHTML = empty("Not available.", "Only the LeadPilot team can manage dealers."); };
const snippetFor = (key) => `<span class="k">&lt;script</span> src=<span class="s">"${esc(location.origin)}/assets/widget/leadpilot-chat.js"</span> defer<span class="k">&gt;&lt;/script&gt;</span>
<span class="k">&lt;leadpilot-chat</span> key=<span class="s">"${esc(key || "")}"</span><span class="k">&gt;&lt;/leadpilot-chat&gt;</span>`;

async function dealersView(view, r) {
  if (me.business !== "*") return ownerOnly(view);
  const o = await api("/admin/owner/overview");
  if (r.stale()) return;
  const paying = o.dealers.filter((d) => !d.showcase);
  view.innerHTML = `
    <dl class="kpis owner-kpis">
      <div class="kpi"><dt>Dealers</dt><dd>${paying.length}</dd><small>${paying.filter((d) => d.status === "active").length} active · ${o.dealers.length - paying.length} showcase</small></div>
      <div class="kpi signal"><dt>Revenue</dt><dd>${usd(o.revenue_usd, 0)}</dd><small>plans of active dealers · ${esc(o.month)}</small></div>
      <div class="kpi"><dt>AI cost</dt><dd>${usd(o.ai_cost_usd, 3)}</dd><small>model tokens this month</small></div>
      <div class="kpi"><dt>Margin</dt><dd>${usd(o.margin_usd)}</dd><small>${o.chat_requires_key ? "/chat needs a dealer key" : "/chat open (grading mode)"}</small></div>
    </dl>
    <div class="fu-bar"><a class="btn btn-primary" href="#/dealers/new">New dealer</a>
      <span class="small muted">Create a dealer, give them their console key, then import their listings. No code change or redeploy needed.</span></div>
    <div class="table-wrap"><table><thead><tr><th>Dealer</th><th>Plan</th><th>Status</th><th>This month</th><th>Credits</th><th>Leads</th><th>Listings</th><th>AI cost</th></tr></thead><tbody>
      ${o.dealers.map((d) => { const s = d.usage; return `<tr class="clickable" tabindex="0" data-id="${esc(d.id)}">
        <td><strong>${esc(d.name)}</strong><span class="sub mono">${esc(d.id)}${d.showcase ? " · showcase" : ""}</span></td>
        <td>${esc(s.plan_name)}<span class="sub">${s.price_usd ? `$${s.price_usd}/mo` : "free"}</span></td><td>${statusTag(d.status || "active")}</td>
        <td class="num">${n(s.conversations)}${s.limit == null ? "" : ` / ${n(s.limit)}`}<span class="sub">${s.limit == null ? "unlimited" : `${s.percent}%`}${s.blocked ? ` · ${n(s.blocked)} capture-only` : ""}</span></td>
        <td class="num">${n(d.extra_credits)}</td><td class="num">${n(d.leads)}</td><td class="num">${n(d.listings)}</td><td class="num">${usd(s.cost_usd, 3)}</td></tr>`; }).join("")}
    </tbody></table></div>`;
  const go = (e) => { const tr = e.target.closest("tr[data-id]"); if (tr && (e.type === "click" || e.key === "Enter")) location.hash = `#/dealers/${encodeURIComponent(tr.dataset.id)}`; };
  view.addEventListener("click", go, { signal: r.signal });
  view.addEventListener("keydown", go, { signal: r.signal });
}

const PLAN_OPTS = (cur) => ["pilot", "starter", "growth", "scale", "internal"].map((p) => `<option value="${p}" ${p === cur ? "selected" : ""}>${p[0].toUpperCase() + p.slice(1)}</option>`).join("");
const val = (form, name) => (form.elements[name]?.value || "").trim();
const orNull = (v) => (v === "" ? null : v);

function keyBox(title, key, note) {
  return `<div class="key-box" role="status"><strong>${esc(title)}</strong><code>${esc(key)}</code>
    <div class="acts-row"><button class="btn btn-sm" data-copy="${esc(key)}">Copy</button><span class="small">${note}</span></div></div>`;
}

async function newDealer(view, r) {
  if (me.business !== "*") return ownerOnly(view);
  $("#page-title").textContent = "New dealer";
  view.innerHTML = `<a class="back" href="#/dealers">← All dealers</a>
    <div class="grid-2"><section class="panel"><div class="panel-head"><h2>Dealer details</h2></div><div class="panel-body">
      <form id="new-dealer" class="form-grid" novalidate>
        <div class="field"><label for="nd-name">Business name</label><input class="input" id="nd-name" name="name" required minlength="2" maxlength="120" placeholder="Arani Real Estate"></div>
        <div class="field"><label for="nd-id">Dealer id</label><input class="input mono" id="nd-id" name="id" required pattern="[a-z][a-z0-9\\-]{1,38}[a-z0-9]" maxlength="40" placeholder="arani">
          <span class="hint">Lowercase letters, numbers and dashes. Can't be changed.</span></div>
        <div class="field"><label for="nd-cat">Sells</label><select class="select" id="nd-cat" name="category"><option value="property">Property</option><option value="car">Cars</option></select></div>
        <div class="field"><label for="nd-prefix">Listing prefix</label><input class="input mono" id="nd-prefix" name="listing_prefix" required pattern="[A-Za-z]{1,3}" maxlength="3" placeholder="AR">
          <span class="hint">1–3 letters. Listing ids become AR001, AR002…</span></div>
        <div class="field"><label for="nd-plan">Plan</label><select class="select" id="nd-plan" name="plan">${PLAN_OPTS("pilot")}</select></div>
        <div class="field"><label for="nd-city">City</label><input class="input" id="nd-city" name="city" maxlength="60" placeholder="Lahore"></div>
        <div class="field"><label for="nd-phone">Phone</label><input class="input" id="nd-phone" name="phone" maxlength="40"></div>
        <div class="field"><label for="nd-color">Brand colour</label><input class="input" id="nd-color" name="brand_color" type="color" value="#ff4f00"></div>
        <div class="field full"><label for="nd-tag">Tagline</label><input class="input" id="nd-tag" name="tagline" maxlength="240" placeholder="Plots and houses in DHA and Bahria Town"></div>
        <div class="field full"><label for="nd-origins">Websites allowed to use the chat</label><input class="input mono" id="nd-origins" name="allowed_origins" maxlength="1000" placeholder="https://arani.pk, https://www.arani.pk">
          <span class="hint">Optional. When set, other websites can't use this dealer's chat key.</span></div>
        <div class="field full"><label for="nd-pol">Policies &amp; FAQ</label><textarea class="textarea" id="nd-pol" name="policies" rows="6" maxlength="20000" placeholder="Payment plans, documents needed, token money, office hours…"></textarea>
          <span class="hint">The assistant answers policy questions only from this text. Leave empty and it will say a sales officer will confirm.</span></div>
        <p class="field-error full" id="nd-error" role="alert"></p>
        <div class="full"><button class="btn btn-primary" type="submit">Create dealer</button></div>
      </form></div></section>
      <section class="panel"><div class="panel-head"><h2>What happens next</h2></div><div class="panel-body small">
        <ol class="steps"><li><span>You get the dealer's <strong>console key</strong>. It's shown once; only a hash is stored. Send it to the dealer privately.</span></li>
          <li><span>Import their listings from a CSV on the dealer's page.</span></li>
          <li><span>Give them the two-line install snippet with their public <strong>widget key</strong>.</span></li>
          <li><span>Usage is counted against their plan. They get alerts at 80% and 100%, then new chats switch to capture-only.</span></li></ol>
        <div id="nd-result" class="mt"></div></div></section></div>`;
  const form = $("#new-dealer");
  $("#nd-name").addEventListener("input", (e) => {
    const id = $("#nd-id");
    if (!id.dataset.touched) id.value = e.target.value.toLowerCase().replace(/[^a-z0-9]+/g, "-").replace(/^-+|-+$/g, "").replace(/^[^a-z]+/, "").slice(0, 40);
  }, { signal: r.signal });
  $("#nd-id").addEventListener("input", (e) => { e.target.dataset.touched = "1"; }, { signal: r.signal });
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const err = $("#nd-error"); err.textContent = "";
    const bad = $$("[required], [pattern]", form).filter((i) => !i.checkValidity());
    $$(".input", form).forEach((i) => i.removeAttribute("aria-invalid"));
    if (bad.length) { bad.forEach((i) => i.setAttribute("aria-invalid", "true")); err.textContent = "Please check the highlighted fields."; bad[0].focus(); return; }
    const body = { id: val(form, "id"), name: val(form, "name"), category: val(form, "category"), listing_prefix: val(form, "listing_prefix"),
      plan: val(form, "plan"), city: orNull(val(form, "city")), phone: orNull(val(form, "phone")), brand_color: val(form, "brand_color"),
      tagline: orNull(val(form, "tagline")), allowed_origins: orNull(val(form, "allowed_origins")), policies: orNull(val(form, "policies")) };
    try {
      const out = await busy($("button[type=submit]", form), "Creating…", () => api("/admin/owner/dealers", { method: "POST", body }));
      $$("input, select, textarea, button", form).forEach((x) => { x.disabled = true; });
      $("#nd-result").innerHTML = keyBox("Console key (shown once)", out.console_key, "Copy it now and send it to the dealer privately. You can issue a new one later, which signs out the old one.")
        + `<p class="small mt">Install snippet for their website:</p><div class="snippet">${snippetFor(out.dealer.widget_key)}</div>
          <p class="mt"><a class="btn btn-sm btn-primary" href="#/dealers/${encodeURIComponent(out.dealer.id)}">Open ${esc(out.dealer.name)} → import listings</a></p>`;
      toast(`${out.dealer.name} created`);
    } catch (ex) { err.textContent = ex.message; }
  }, { signal: r.signal });
  view.addEventListener("click", copyHandler, { signal: r.signal });
}

async function copyHandler(e) {
  const b = e.target.closest("[data-copy]"); if (!b) return;
  try { await navigator.clipboard.writeText(b.dataset.copy); toast("Copied"); } catch (ex) { toast("Select the text and copy it manually.", { error: true }); }
}

// CSV → listing rows. Accepts quoted fields, "price" or "price_pkr", and prices like "2.5 crore" or "85 lakh".
function parseCSV(text) {
  const out = []; let row = [], cell = "", q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) { if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c; }
    else if (c === '"') q = true;
    else if (c === ",") { row.push(cell); cell = ""; }
    else if (c === "\n" || c === "\r") { if (c === "\r" && text[i + 1] === "\n") i++; row.push(cell); out.push(row); row = []; cell = ""; }
    else cell += c;
  }
  if (cell || row.length) { row.push(cell); out.push(row); }
  return out.filter((r) => r.some((x) => x.trim()));
}
function parsePrice(v) {
  const s = String(v || "").toLowerCase().replace(/pkr|rs\.?|,/g, "").trim();
  const m = s.match(/^([\d.]+)\s*(crore|cr|lakh|lac|million|m)?$/);
  if (!m) return NaN;
  const mult = { crore: 1e7, cr: 1e7, lakh: 1e5, lac: 1e5, million: 1e6, m: 1e6 }[m[2]] || 1;
  return Math.round(parseFloat(m[1]) * mult);
}
function csvToRows(text) {
  const table = parseCSV(text.replace(/^﻿/, ""));
  if (table.length < 2) return { rows: [], errors: ["The file needs a header row and at least one listing."] };
  const head = table[0].map((h) => h.trim().toLowerCase().replace(/[^a-z_]/g, "_"));
  const col = (...names) => head.findIndex((h) => names.includes(h));
  const ix = { id: col("id", "listing_id"), title: col("title", "name"), location: col("location", "area"), price: col("price_pkr", "price"),
    bedrooms: col("bedrooms", "beds"), year: col("year", "model_year"), details: col("details", "description") };
  if (ix.title < 0 || ix.price < 0) return { rows: [], errors: ['The header must include "title" and "price" columns.'] };
  const rows = [], errors = [];
  table.slice(1).forEach((r, i) => {
    const get = (k) => (ix[k] >= 0 ? (r[ix[k]] || "").trim() : "");
    const price = parsePrice(get("price"));
    const row = { title: get("title"), location: get("location"), price_pkr: price, details: get("details") };
    if (get("id")) row.id = get("id").toUpperCase();
    if (get("bedrooms")) row.bedrooms = parseInt(get("bedrooms"), 10);
    if (get("year")) row.year = parseInt(get("year"), 10);
    if (row.title.length < 2 || !(price > 0)) errors.push(`Line ${i + 2}: needs a title and a price (got "${get("price")}").`);
    else rows.push(row);
  });
  return { rows, errors };
}

async function dealerDetail(view, r) {
  if (me.business !== "*") return ownerOnly(view);
  const x = await api(`/admin/owner/dealers/${encodeURIComponent(r.id)}`);
  if (r.stale()) return;
  const d = x.dealer, s = x.usage;
  $("#page-title").textContent = d.name;
  const avail = x.listings.filter((l) => l.status === "available");
  const inp = (name, label, value, extra = "") => `<div class="field"><label for="ed-${name}">${label}</label><input class="input" id="ed-${name}" name="${name}" value="${esc(value ?? "")}" ${extra}></div>`;
  view.innerHTML = `<a class="back" href="#/dealers">← All dealers</a>
    <header class="lead-head"><div><span class="mono small muted">${esc(d.id)} · ${esc(d.category)} · prefix ${esc(d.listing_prefix || "–")} · since ${when(d.created_at)}</span>
      <h2>${esc(d.name)}</h2><div class="facts">${statusTag(d.status || "active")}<span>${esc(s.plan_name)}</span>${d.showcase ? "<span>showcase</span>" : ""}${d.city ? `<span>${esc(d.city)}</span>` : ""}</div></div>
      <div class="lead-actions"><button class="btn ${d.status === "suspended" ? "btn-primary" : "btn-danger"}" id="toggle-status">${d.status === "suspended" ? "Reactivate" : "Suspend"}</button></div></header>
    <div class="grid-2">
      <section class="panel"><div class="panel-head"><h2>Usage this month</h2><span class="muted">AI cost ${usd(s.cost_usd, 4)}</span></div><div class="panel-body">${meter(s)}<div class="mt">${planFacts(s)}</div></div></section>
      <div class="stack gap-20">
        <section class="panel"><div class="panel-head"><h2>Add credits</h2></div><div class="panel-body">
          <form id="credits" class="form-grid"><div class="field"><label for="cr-amt">Conversations</label><input class="input" id="cr-amt" name="amount" type="number" step="1" min="-100000" max="100000" required placeholder="500"></div>
            <div class="field"><label for="cr-why">Reason</label><input class="input" id="cr-why" name="reason" required minlength="3" maxlength="200" placeholder="Paid top-up, invoice #12"></div>
            <div class="full"><button class="btn btn-sm btn-primary" type="submit">Add credits</button> <span class="small muted">Credits are used after the plan allowance and never expire. Use a negative number to remove.</span></div></form>
          ${x.ledger.length ? `<ul class="list mt">${x.ledger.slice(0, 6).map((l) => `<li><div class="row"><span class="t mono">${l.delta > 0 ? "+" : ""}${n(l.delta)}</span><span class="r mono small muted">${when(l.created_at)}</span><span class="s">${esc(l.reason)}</span></div></li>`).join("")}</ul>` : ""}
        </div></section>
        <section class="panel"><div class="panel-head"><h2>Keys</h2></div><div class="panel-body">
          <dl class="kv"><dt>Widget key</dt><dd><code>${esc(d.widget_key || "–")}</code></dd><dt>Console key</dt><dd>${d.has_console_key ? "Issued (only a hash is stored)" : "Uses a key from ADMIN_TOKENS"}</dd></dl>
          <div class="acts-row mt"><button class="btn btn-sm" data-rotate="console">New console key</button><button class="btn btn-sm" data-rotate="widget">New widget key</button></div>
          <div id="key-out" class="mt"></div>
          <p class="small muted mt">Install snippet:</p><div class="snippet">${snippetFor(d.widget_key)}</div></div></section>
      </div>
    </div>
    <section class="panel mt"><div class="panel-head"><h2>Profile &amp; plan</h2></div><div class="panel-body">
      <form id="edit" class="form-grid">
        ${inp("name", "Business name", d.name, 'required minlength="2" maxlength="120"')}
        <div class="field"><label for="ed-plan">Plan</label><select class="select" id="ed-plan" name="plan">${PLAN_OPTS(d.plan)}</select></div>
        ${inp("city", "City", d.city, 'maxlength="60"')}${inp("phone", "Phone", d.phone, 'maxlength="40"')}
        <div class="field"><label for="ed-color">Brand colour</label><input class="input" id="ed-color" name="brand_color" type="color" value="${esc(d.brand_color || "#ff4f00")}"></div>
        ${inp("tagline", "Tagline", d.tagline, 'maxlength="240"')}
        <div class="field full"><label for="ed-origins">Websites allowed to use the chat</label><input class="input mono" id="ed-origins" name="allowed_origins" value="${esc(d.allowed_origins || "")}" maxlength="1000" placeholder="https://arani.pk"></div>
        <div class="field full"><label for="ed-pol">Policies &amp; FAQ</label><textarea class="textarea" id="ed-pol" name="policies" rows="6" maxlength="20000">${esc(d.policies || "")}</textarea></div>
        <div class="full"><button class="btn btn-primary" type="submit">Save changes</button></div>
      </form></div></section>
    <section class="panel mt"><div class="panel-head"><h2>Listings</h2><span class="muted">${avail.length} available · ${x.listings.length - avail.length} archived</span></div><div class="panel-body">
      ${d.category === "all" ? `<p class="muted">The platform demo dealer has no listings of its own.</p>` : `
      <form id="import" class="form-grid">
        <div class="field"><label for="im-file">CSV file</label><input class="input" id="im-file" type="file" accept=".csv,text/csv" required></div>
        <div class="field"><label for="im-mode">Mode</label><select class="select" id="im-mode"><option value="append">Add / update (keep others)</option><option value="replace">Replace all (archive listings not in the file)</option></select></div>
        <p class="hint full">Columns: <code>title, price</code> (required) and <code>id, location, ${d.category === "car" ? "year" : "bedrooms"}, details</code>. Prices can be numbers or like “2.5 crore” / “85 lakh”. Rows without an id get ${esc(d.listing_prefix || "")}001, ${esc(d.listing_prefix || "")}002…</p>
        <div class="full" id="im-preview"></div>
        <div class="full"><button class="btn btn-primary" type="submit" id="im-go" disabled>Import</button></div>
      </form>`}
      ${x.listings.length ? `<div class="table-wrap mt"><table><thead><tr><th>Id</th><th>Title</th><th>Location</th><th>Price</th><th>Status</th></tr></thead><tbody>
        ${x.listings.map((l) => `<tr><td class="mono">${esc(l.id)}</td><td>${esc(l.title)}</td><td>${esc(l.location || "–")}</td><td class="num">${pkr(l.price_pkr, { short: true })}</td><td>${statusTag(l.status)}</td></tr>`).join("")}</tbody></table></div>` : ""}
    </div></section>`;

  const post = (path, body) => api(`/admin/owner/dealers/${encodeURIComponent(d.id)}${path}`, { method: "POST", body });
  view.addEventListener("click", copyHandler, { signal: r.signal });
  $("#toggle-status").addEventListener("click", async (e) => {
    const to = d.status === "suspended" ? "active" : "suspended";
    if (to === "suspended" && !confirm(`Suspend ${d.name}? They are signed out, and new buyers get capture-only replies (no AI).`)) return;
    try { await busy(e.target, "Saving…", () => post("", { status: to })); toast(to === "suspended" ? "Dealer suspended" : "Dealer reactivated"); route(true); }
    catch (ex) { toast(ex.message, { error: true }); }
  });
  $("#credits").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target, amount = parseInt(val(f, "amount"), 10);
    if (!amount || val(f, "reason").length < 3) { toast("Enter a non-zero amount and a reason.", { error: true }); return; }
    try { await busy($("button", f), "Adding…", () => post("/credits", { amount, reason: val(f, "reason") })); toast(`${amount > 0 ? "Added" : "Removed"} ${n(Math.abs(amount))} credits`); route(true); }
    catch (ex) { toast(ex.message, { error: true }); }
  });
  view.addEventListener("click", async (e) => {
    const b = e.target.closest("[data-rotate]"); if (!b) return;
    const kind = b.dataset.rotate;
    const warn = kind === "console" ? "Issue a new console key? The old key stops working and the dealer is signed out."
      : "Issue a new widget key? The chat on the dealer's website stops working until they paste the new snippet.";
    if (!confirm(warn)) return;
    try {
      const out = await busy(b, "Issuing…", () => post("/rotate", { kind }));
      $("#key-out").innerHTML = keyBox(kind === "console" ? "New console key (shown once)" : "New widget key", out.key,
        kind === "console" ? "Send it to the dealer privately." : "Update the snippet on the dealer's website.");
      if (kind === "widget") d.widget_key = out.key;
    } catch (ex) { toast(ex.message, { error: true }); }
  }, { signal: r.signal });
  $("#edit").addEventListener("submit", async (e) => {
    e.preventDefault();
    const f = e.target;
    const body = { name: val(f, "name"), plan: val(f, "plan"), city: orNull(val(f, "city")), phone: orNull(val(f, "phone")), brand_color: val(f, "brand_color"),
      tagline: orNull(val(f, "tagline")), allowed_origins: val(f, "allowed_origins"), policies: orNull(val(f, "policies")) };
    try { await busy($("button[type=submit]", f), "Saving…", () => post("", body)); toast("Saved"); route(true); }
    catch (ex) { toast(ex.message, { error: true }); }
  });
  const imp = $("#import");
  if (!imp) return;
  let parsed = { rows: [], errors: [] };
  $("#im-file").addEventListener("change", async (e) => {
    const file = e.target.files[0]; $("#im-go").disabled = true;
    if (!file) { $("#im-preview").innerHTML = ""; return; }
    if (file.size > 2_000_000) { $("#im-preview").innerHTML = `<p class="field-error">That file is over 2 MB.</p>`; return; }
    parsed = csvToRows(await file.text());
    $("#im-preview").innerHTML = `${parsed.errors.length ? `<div class="plan-banner critical"><span>${parsed.errors.slice(0, 5).map(esc).join("<br>")}${parsed.errors.length > 5 ? `<br>…and ${parsed.errors.length - 5} more` : ""}</span></div>` : ""}
      ${parsed.rows.length ? `<p class="small"><strong>${parsed.rows.length}</strong> listing${parsed.rows.length === 1 ? "" : "s"} ready to import${parsed.errors.length ? " (rows with problems are skipped)" : ""}.</p>
      <div class="table-wrap csv-preview"><table><thead><tr><th>Id</th><th>Title</th><th>Location</th><th>Price</th><th>${d.category === "car" ? "Year" : "Beds"}</th></tr></thead><tbody>
        ${parsed.rows.slice(0, 50).map((x) => `<tr><td class="mono">${esc(x.id || "auto")}</td><td>${esc(x.title)}</td><td>${esc(x.location)}</td><td class="num">${pkr(x.price_pkr, { short: true })}</td><td class="num">${esc((d.category === "car" ? x.year : x.bedrooms) ?? "–")}</td></tr>`).join("")}</tbody></table></div>` : ""}`;
    $("#im-go").disabled = !parsed.rows.length;
  });
  imp.addEventListener("submit", async (e) => {
    e.preventDefault();
    const mode = $("#im-mode").value;
    if (mode === "replace" && !confirm(`Replace all of ${d.name}'s listings? Listings not in this file will be archived (hidden from buyers).`)) return;
    try {
      const out = await busy($("#im-go"), "Importing…", () => post("/listings", { mode, rows: parsed.rows }));
      toast(`${out.created} added · ${out.updated} updated${out.archived ? ` · ${out.archived} archived` : ""}`); route(true);
    } catch (ex) { toast(ex.message, { error: true }); }
  });
}

// ───────── start ─────────
themeToggle();
if (session?.session) boot(); else showLogin();
