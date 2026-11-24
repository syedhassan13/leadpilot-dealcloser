// LeadPilot dealer dashboard.
// Login exchanges the dealer key for a signed 8-hour session; logout revokes it on the server (audit 2.5).
// The session sits in sessionStorage (audit 2.6, reported as Medium). The key itself is never stored.
const API = window.LEADPILOT_API;
const $ = (id) => document.getElementById(id);
let session = null;
try { session = sessionStorage.getItem("lp_admin_session"); } catch (e) {}
let view = "overview";

// Everything from the server is escaped before it is put into the page (audit 5.2, 8.3).
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"); }
const cls = (s) => String(s ?? "").replace(/[^a-z]/g, "");    // safe CSS class from a status value
function pkr(n) {
  if (!n) return "–";
  if (n >= 10_000_000) return `${(n / 10_000_000).toFixed(2).replace(/\.?0+$/, "")} crore`;
  return `${(n / 100_000).toFixed(1).replace(/\.0$/, "")} lakh`;
}
function when(s) { if (!s) return "–"; const d = new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z"); return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
function toast(t) { const el = $("toast"); el.textContent = t; el.classList.add("show"); clearTimeout(el._h); el._h = setTimeout(() => el.classList.remove("show"), 2600); }

async function api(path, opts = {}) {
  const res = await fetch(API + path, { ...opts, headers: { "Content-Type": "application/json", "Authorization": `Bearer ${session || ""}` } });
  if (res.status === 401) { showLogin("Your session ended. Please log in again."); throw new Error("unauthorized"); }
  if (res.status === 429) throw new Error("Too many requests, please wait a bit.");
  if (!res.ok) throw new Error("Request failed");
  return res.json();
}

// ── login / logout ──
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("login-err").textContent = "";
  try {
    const r = await fetch(API + "/admin/login", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ key: $("token").value.trim() }) });
    $("token").value = "";
    if (r.status === 429) { $("login-err").textContent = "Too many attempts. Try again in 15 minutes."; return; }
    if (!r.ok) { $("login-err").textContent = "Wrong dealer key."; return; }
    session = (await r.json()).session;
    try { sessionStorage.setItem("lp_admin_session", session); } catch (err) {}
    await start();
  } catch (err) { $("login-err").textContent = "The server is not reachable right now."; }
});
async function start() {
  const me = await api("/admin/me");
  $("biz-name").textContent = me.name;
  $("login-view").style.display = "none"; $("app-view").style.display = ""; $("logout").style.display = "";
  show(view);
}
function showLogin(msg = "") {
  session = null; try { sessionStorage.removeItem("lp_admin_session"); } catch (e) {}
  $("login-view").style.display = ""; $("app-view").style.display = "none"; $("logout").style.display = "none";
  $("biz-name").textContent = ""; $("login-err").textContent = msg;
}
$("logout").addEventListener("click", async () => {
  try { await api("/admin/logout", { method: "POST" }); } catch (e) {}
  showLogin();
});

$("side").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.v); });
async function show(v) {
  view = v;
  document.querySelectorAll("#side button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("main").innerHTML = `<div class="empty">Loading…</div>`;
  try { await ({ overview, leads: leadsView, bookings: bookingsView, followups: followupsView })[v](); }
  catch (e) { if (e.message !== "unauthorized") $("main").innerHTML = `<div class="empty">${esc(e.message)}</div>`; }
}

// ── one delegated click handler: no inline onclick with data in it ──
document.addEventListener("click", (e) => {
  const el = e.target.closest("[data-action]"); if (!el) return;
  const { action, id, status } = el.dataset;
  if (action === "open-lead") openLead(id);
  else if (action === "close-drawer") closeDrawer();
  else if (action === "booking-status") setBooking(Number(id), status);
  else if (action === "run-followups") runFollowups(status === "demo");
  else if (action === "approve") approve(Number(id));
  else if (action === "skip") skip(Number(id));
  else if (action === "delete-lead") deleteLead(id);
});
document.addEventListener("change", (e) => {
  const el = e.target.closest("[data-action='lead-status']"); if (el) setLeadStatus(el.dataset.id, el.value);
});
$("drawer-bg").addEventListener("click", closeDrawer);

// ── overview ──
async function overview() {
  const [s, leads] = await Promise.all([api("/admin/stats"), api("/admin/leads?limit=50")]);
  setBadge(s.pending_followups);
  const stat = (label, value, small = "") => `<div class="card stat"><span>${esc(label)}</span><b>${esc(value)}</b><small>${esc(small)}</small></div>`;
  $("main").innerHTML = `
    <h2>Overview</h2><p class="sub">How fast you reply, how leads score, and what it costs.</p>
    <div class="stats">
      ${stat("Leads", s.leads, `${s.hot} hot · ${s.warm} warm · ${s.cold} cold`)}
      ${stat("Bookings", s.bookings, `${s.conversion_pct}% of leads booked`)}
      ${stat("First reply", s.avg_first_response_s != null ? s.avg_first_response_s + " s" : "–", "average, web form leads")}
      ${stat("Reply time", s.median_reply_s != null ? s.median_reply_s + " s" : "–", "average per message")}
      ${stat("Messages", s.messages, `${s.blocked} blocked by safety guard`)}
      ${stat("AI cost", "$" + Number(s.cost_usd).toFixed(4), `${(s.tokens_in / 1000).toFixed(1)}k in / ${(s.tokens_out / 1000).toFixed(1)}k out tokens`)}
      ${stat("Follow-ups waiting", s.pending_followups, "need your approval")}
    </div>
    <div class="card"><h4 style="margin:0;padding:14px">🔥 Hottest leads</h4>${leadTable(leads.filter((l) => l.score !== "cold").slice(0, 8))}</div>`;
}
function setBadge(n) { const b = $("fu-badge"); b.textContent = n; b.style.display = n ? "" : "none"; }

// ── leads ──
function leadTable(list) {
  if (!list.length) return `<div class="empty">No leads yet. Chat on the customer site to create one.</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>Lead</th><th>Wants</th><th>Budget</th><th>Timeline</th><th>Score</th><th>Status</th><th>Created</th></tr></thead><tbody>
    ${list.map((l) => `<tr class="click" data-action="open-lead" data-id="${esc(l.user_id)}">
      <td><b>${esc(l.name || "Unknown")}</b><br><span class="hint">${esc(l.phone || l.user_id)}</span></td>
      <td>${l.category === "car" ? "🚗 Car" : l.category === "property" ? "🏠 Property" : "–"}<br><span class="hint">${esc((l.preferences || "").slice(0, 40))}</span></td>
      <td>${pkr(l.budget_pkr)}</td><td>${esc(l.timeline || "–")}</td>
      <td><span class="score ${cls(l.score)}">${esc(l.score)}</span></td>
      <td><span class="status ${cls(l.status)}">${esc(l.status)}</span></td>
      <td class="hint">${when(l.created_at)}</td></tr>`).join("")}</tbody></table></div>`;
}
async function leadsView() {
  const leads = await api("/admin/leads?limit=200");
  $("main").innerHTML = `<h2>Leads</h2><p class="sub">Every inquiry, scored automatically. Click a lead to see its memory and conversations. Showing the latest ${leads.length}.</p>
    <div class="card">${leadTable(leads)}</div>`;
}
async function openLead(uid) {
  const d = await api(`/admin/leads/${encodeURIComponent(uid)}`);
  const l = d.lead;
  const mem = Object.entries(d.memory).map(([k, v]) => `<span>${esc(k)}</span><div>${esc(v)}</div>`).join("") || `<span>–</span><div>nothing saved yet</div>`;
  const convos = Object.entries(d.conversations).map(([t, msgs]) => `
    <div class="card panel"><h4>Conversation ${esc(t)}</h4><div class="convo">
      ${msgs.map((m) => `<div class="msg ${m.role === "user" ? "user" : "assistant"} ${m.kind === "followup" ? "followup" : ""}">${md(m.text)}</div>`).join("") || '<div class="hint">empty</div>'}
    </div></div>`).join("");
  const tools = d.tool_audit.map((a) => `<tr><td class="hint">${when(a.created_at)}</td><td>${esc(a.tool)}</td><td class="hint">${esc(JSON.stringify(a.args))}</td></tr>`).join("");
  $("drawer").innerHTML = `
    <div class="row"><h2 style="margin:0">${esc(l.name || "Unknown lead")}</h2><span class="score ${cls(l.score)}">${esc(l.score)}</span>
      <span class="spacer"></span><button class="btn small" data-action="close-drawer">✕</button></div>
    <p class="hint">${[l.phone, l.user_id, "source: " + (l.source || "chat")].filter(Boolean).map(esc).join(" · ")}</p>
    <div class="row" style="margin-bottom:14px"><label style="margin:0">Status</label>
      <select style="width:auto" data-action="lead-status" data-id="${esc(l.user_id)}">
        ${["new", "qualified", "booked", "lost"].map((s) => `<option ${s === l.status ? "selected" : ""}>${s}</option>`).join("")}
      </select><span class="spacer"></span>
      <button class="btn small" data-action="delete-lead" data-id="${esc(l.user_id)}" title="Customer asked to delete their data">🗑 Delete lead data</button></div>
    <div class="card panel"><h4>🧠 Long-term memory (user_id)</h4><div class="kv">${mem}</div></div>
    <div class="card panel"><h4>📅 Bookings</h4><div class="kv">${d.bookings.map((b) => `<span>#${esc(b.id)} ${esc(b.date)} ${esc(b.time)}</span><div>${esc(b.title)} (${esc(b.listing_id)}) <span class="status ${cls(b.status)}">${esc(b.status)}</span></div>`).join("") || "<span>–</span><div>none</div>"}</div></div>
    ${convos}
    <div class="card panel"><h4>🔧 Tool audit log</h4><div class="table-wrap"><table><tbody>${tools || '<tr><td class="hint">no tool calls</td></tr>'}</tbody></table></div></div>`;
  $("drawer").classList.add("open"); $("drawer-bg").classList.add("open");
}
function closeDrawer() { $("drawer").classList.remove("open"); $("drawer-bg").classList.remove("open"); }
async function setLeadStatus(uid, status) {
  await api(`/admin/leads/${encodeURIComponent(uid)}/status`, { method: "POST", body: JSON.stringify({ status }) });
  toast("Lead updated"); if (view === "leads" || view === "overview") show(view);
}
async function deleteLead(uid) {
  if (!confirm("Permanently delete this lead's data (chats, memory, bookings)? Use this when a customer asks.")) return;
  await api(`/admin/leads/${encodeURIComponent(uid)}`, { method: "DELETE" });
  closeDrawer(); toast("Lead data deleted"); show(view);
}

// ── bookings ──
async function bookingsView() {
  const list = await api("/admin/bookings?limit=200");
  $("main").innerHTML = `<h2>Bookings</h2><p class="sub">Site visits and test drives booked by the assistant.</p>
    <div class="card">${list.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>When</th><th>Listing</th><th>Customer</th><th>Type</th><th>Status</th><th></th></tr></thead><tbody>
    ${list.map((b) => `<tr><td>${esc(b.id)}</td><td><b>${esc(b.date)}</b> ${esc(b.time)}</td><td>${esc(b.title)}<br><span class="hint">${esc(b.listing_id)}</span></td>
      <td>${esc(b.name || "–")}<br><span class="hint">${esc(b.phone || "")}</span></td><td>${esc(b.kind)}</td>
      <td><span class="status ${cls(b.status)}">${esc(b.status)}</span></td>
      <td class="row">${b.status === "confirmed" ? `<button class="btn small" data-action="booking-status" data-id="${esc(b.id)}" data-status="completed">Done</button><button class="btn small" data-action="booking-status" data-id="${esc(b.id)}" data-status="cancelled">Cancel</button>` : ""}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">No bookings yet.</div>`}</div>`;
}
async function setBooking(id, status) {
  if (status === "cancelled" && !confirm(`Cancel booking #${id}?`)) return;
  await api(`/admin/bookings/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
  toast(`Booking #${id} ${status}`); show("bookings");
}

// ── follow-ups (human in the loop) ──
async function followupsView() {
  const list = await api("/admin/followups?limit=200");
  const drafts = list.filter((f) => f.status === "draft");
  setBadge(drafts.length);
  $("main").innerHTML = `<h2>Follow-ups</h2>
    <p class="sub">The follow-up agent drafts a message for leads who went quiet (day 1, 3 and 7), up to 20 per run. Nothing is sent until you approve it.</p>
    <div class="row" style="margin-bottom:16px">
      <button class="btn primary" data-action="run-followups">Run follow-up agent</button>
      <button class="btn" data-action="run-followups" data-status="demo" title="Skip the 1/3/7 day wait, for demos">Run now, ignore wait (demo)</button>
    </div>
    ${drafts.map((f) => `<div class="card fu"><div class="row"><b>${esc(f.name || f.user_id)}</b><span class="score ${cls(f.score || "cold")}">${esc(f.score || "cold")}</span>
      <span class="hint">follow-up ${esc(f.step)} of 3 · ${when(f.created_at)}</span></div>
      <textarea id="fu-${esc(f.id)}">${esc(f.message)}</textarea>
      <div class="row"><button class="btn primary small" data-action="approve" data-id="${esc(f.id)}">✓ Approve & send</button>
      <button class="btn small" data-action="skip" data-id="${esc(f.id)}">Skip</button></div></div>`).join("") || `<div class="card empty">No drafts waiting.</div>`}
    <h3>History</h3>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Lead</th><th>Message</th><th>Status</th><th>Sent</th></tr></thead><tbody>
      ${list.filter((f) => f.status !== "draft").map((f) => `<tr><td>${esc(f.name || f.user_id)}</td><td>${esc(f.message)}</td>
        <td><span class="status ${cls(f.status)}">${esc(f.status)}</span></td><td class="hint">${when(f.sent_at)}</td></tr>`).join("") || '<tr><td colspan="4" class="hint">none yet</td></tr>'}
    </tbody></table></div></div>`;
}
async function runFollowups(ignoreWait) {
  toast("Follow-up agent is working…");
  try {
    const r = await api(`/admin/followups/run?ignore_wait=${ignoreWait}`, { method: "POST" });
    toast(`${r.drafted} follow-up(s) drafted`); show("followups");
  } catch (e) { toast(e.message); }
}
async function approve(id) {
  await api(`/admin/followups/${id}/approve`, { method: "POST", body: JSON.stringify({ message: $(`fu-${id}`).value }) });
  toast("Sent to the customer's chat"); show("followups");
}
async function skip(id) { await api(`/admin/followups/${id}/skip`, { method: "POST" }); toast("Skipped"); show("followups"); }

if (session) start().catch(() => showLogin());
