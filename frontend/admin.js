// LeadPilot dealer dashboard.
const API = window.LEADPILOT_API;
const $ = (id) => document.getElementById(id);
let token = null;
try { token = sessionStorage.getItem("lp_admin"); } catch (e) {}
let view = "overview";

function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>"); }
function pkr(n) {
  if (!n) return "–";
  if (n >= 10_000_000) return `${(n / 10_000_000).toFixed(2).replace(/\.?0+$/, "")} crore`;
  return `${(n / 100_000).toFixed(1).replace(/\.0$/, "")} lakh`;
}
function when(s) { if (!s) return "–"; const d = new Date(s.endsWith("Z") || s.includes("+") ? s : s + "Z"); return d.toLocaleString([], { dateStyle: "medium", timeStyle: "short" }); }
function toast(t) { const el = $("toast"); el.textContent = t; el.classList.add("show"); clearTimeout(el._h); el._h = setTimeout(() => el.classList.remove("show"), 2600); }

async function api(path, opts = {}) {
  const res = await fetch(API + path, { ...opts, headers: { "Content-Type": "application/json", "X-Admin-Token": token || "" } });
  if (res.status === 401) { logout(); throw new Error("unauthorized"); }
  if (!res.ok) throw new Error(await res.text());
  return res.json();
}

// ── login ──
$("login-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  token = $("token").value.trim();
  try {
    await start();
  } catch (err) { $("login-err").textContent = "Wrong token, or the API is not reachable."; }
});
async function start() {
  const me = await api("/admin/me");
  try { sessionStorage.setItem("lp_admin", token); } catch (e) {}
  $("biz-name").textContent = me.name;
  $("login-view").style.display = "none"; $("app-view").style.display = ""; $("logout").style.display = "";
  show(view);
}
function logout() {
  token = null; try { sessionStorage.removeItem("lp_admin"); } catch (e) {}
  $("login-view").style.display = ""; $("app-view").style.display = "none"; $("logout").style.display = "none"; $("biz-name").textContent = "";
}

$("side").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) show(b.dataset.v); });
async function show(v) {
  view = v;
  document.querySelectorAll("#side button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $("main").innerHTML = `<div class="empty">Loading…</div>`;
  try { await ({ overview, leads: leadsView, bookings: bookingsView, followups: followupsView })[v](); }
  catch (e) { if (e.message !== "unauthorized") $("main").innerHTML = `<div class="empty">Couldn't load: ${esc(e.message)}</div>`; }
}

// ── overview ──
async function overview() {
  const [s, leads] = await Promise.all([api("/admin/stats"), api("/admin/leads")]);
  setBadge(s.pending_followups);
  const stat = (label, value, small = "") => `<div class="card stat"><span>${label}</span><b>${value}</b><small>${small}</small></div>`;
  $("main").innerHTML = `
    <h2>Overview</h2><p class="sub">How fast you reply, how leads score, and what it costs.</p>
    <div class="stats">
      ${stat("Leads", s.leads, `${s.hot} hot · ${s.warm} warm · ${s.cold} cold`)}
      ${stat("Bookings", s.bookings, `${s.conversion_pct}% of leads booked`)}
      ${stat("First reply", s.avg_first_response_s != null ? s.avg_first_response_s + " s" : "–", "average, web form leads")}
      ${stat("Reply time", s.median_reply_s != null ? s.median_reply_s + " s" : "–", "median per message")}
      ${stat("Messages", s.messages, `${s.blocked} blocked by safety guard`)}
      ${stat("AI cost", "$" + s.cost_usd.toFixed(4), `${(s.tokens_in / 1000).toFixed(1)}k in / ${(s.tokens_out / 1000).toFixed(1)}k out tokens`)}
      ${stat("Follow-ups waiting", s.pending_followups, "need your approval")}
    </div>
    <div class="card"><h4 style="margin:0;padding:14px">🔥 Hottest leads</h4>${leadTable(leads.filter((l) => l.score !== "cold").slice(0, 8))}</div>`;
}
function setBadge(n) { const b = $("fu-badge"); b.textContent = n; b.style.display = n ? "" : "none"; }

// ── leads ──
function leadTable(list) {
  if (!list.length) return `<div class="empty">No leads yet. Chat on the customer site to create one.</div>`;
  return `<div class="table-wrap"><table><thead><tr><th>Lead</th><th>Wants</th><th>Budget</th><th>Timeline</th><th>Score</th><th>Status</th><th>Created</th></tr></thead><tbody>
    ${list.map((l) => `<tr class="click" onclick="openLead('${esc(l.user_id)}')">
      <td><b>${esc(l.name || "Unknown")}</b><br><span class="hint">${esc(l.phone || l.user_id)}</span></td>
      <td>${l.category === "car" ? "🚗 Car" : l.category === "property" ? "🏠 Property" : "–"}<br><span class="hint">${esc((l.preferences || "").slice(0, 40))}</span></td>
      <td>${pkr(l.budget_pkr)}</td><td>${esc(l.timeline || "–")}</td>
      <td><span class="score ${l.score}">${l.score}</span></td>
      <td><span class="status ${l.status}">${l.status}</span></td>
      <td class="hint">${when(l.created_at)}</td></tr>`).join("")}</tbody></table></div>`;
}
async function leadsView() {
  const leads = await api("/admin/leads");
  $("main").innerHTML = `<h2>Leads</h2><p class="sub">Every inquiry, scored automatically. Click a lead to see its memory and conversations.</p>
    <div class="card">${leadTable(leads)}</div>`;
}
async function openLead(uid) {
  const d = await api(`/admin/leads/${encodeURIComponent(uid)}`);
  const l = d.lead;
  const mem = Object.entries(d.memory).map(([k, v]) => `<span>${esc(k)}</span><div>${esc(v)}</div>`).join("") || `<span>–</span><div>nothing saved yet</div>`;
  const convos = Object.entries(d.conversations).map(([t, msgs]) => `
    <div class="card panel"><h4>Conversation ${esc(t)}</h4><div class="convo">
      ${msgs.map((m) => `<div class="msg ${m.role} ${m.kind === "followup" ? "followup" : ""}">${md(m.text)}</div>`).join("") || '<div class="hint">empty</div>'}
    </div></div>`).join("");
  const tools = d.tool_audit.map((a) => `<tr><td class="hint">${when(a.created_at)}</td><td>${esc(a.tool)}</td><td class="hint">${esc(JSON.stringify(a.args))}</td></tr>`).join("");
  $("drawer").innerHTML = `
    <div class="row"><h2 style="margin:0">${esc(l.name || "Unknown lead")}</h2><span class="score ${l.score}">${l.score}</span>
      <span class="spacer"></span><button class="btn small" onclick="closeDrawer()">✕</button></div>
    <p class="hint">${[l.phone, l.user_id, "source: " + (l.source || "chat")].filter(Boolean).map(esc).join(" · ")}</p>
    <div class="row" style="margin-bottom:14px"><label style="margin:0">Status</label>
      <select style="width:auto" onchange="setLeadStatus('${esc(l.user_id)}', this.value)">
        ${["new", "qualified", "booked", "lost"].map((s) => `<option ${s === l.status ? "selected" : ""}>${s}</option>`).join("")}
      </select></div>
    <div class="card panel"><h4>🧠 Long-term memory (user_id)</h4><div class="kv">${mem}</div></div>
    <div class="card panel"><h4>📅 Bookings</h4><div class="kv">${d.bookings.map((b) => `<span>#${b.id} ${esc(b.date)} ${esc(b.time)}</span><div>${esc(b.title)} (${esc(b.listing_id)}) <span class="status ${b.status}">${b.status}</span></div>`).join("") || "<span>–</span><div>none</div>"}</div></div>
    ${convos}
    <div class="card panel"><h4>🔧 Tool audit log</h4><div class="table-wrap"><table><tbody>${tools || '<tr><td class="hint">no tool calls</td></tr>'}</tbody></table></div></div>`;
  $("drawer").classList.add("open"); $("drawer-bg").classList.add("open");
}
function closeDrawer() { $("drawer").classList.remove("open"); $("drawer-bg").classList.remove("open"); }
async function setLeadStatus(uid, status) {
  await api(`/admin/leads/${encodeURIComponent(uid)}/status`, { method: "POST", body: JSON.stringify({ status }) });
  toast("Lead updated"); if (view === "leads" || view === "overview") show(view);
}

// ── bookings ──
async function bookingsView() {
  const list = await api("/admin/bookings");
  $("main").innerHTML = `<h2>Bookings</h2><p class="sub">Site visits and test drives booked by the assistant.</p>
    <div class="card">${list.length ? `<div class="table-wrap"><table><thead><tr><th>#</th><th>When</th><th>Listing</th><th>Customer</th><th>Type</th><th>Status</th><th></th></tr></thead><tbody>
    ${list.map((b) => `<tr><td>${b.id}</td><td><b>${esc(b.date)}</b> ${esc(b.time)}</td><td>${esc(b.title)}<br><span class="hint">${esc(b.listing_id)}</span></td>
      <td>${esc(b.name || "–")}<br><span class="hint">${esc(b.phone || "")}</span></td><td>${esc(b.kind)}</td>
      <td><span class="status ${b.status}">${b.status}</span></td>
      <td class="row">${b.status === "confirmed" ? `<button class="btn small" onclick="setBooking(${b.id},'completed')">Done</button><button class="btn small" onclick="setBooking(${b.id},'cancelled')">Cancel</button>` : ""}</td></tr>`).join("")}
    </tbody></table></div>` : `<div class="empty">No bookings yet.</div>`}</div>`;
}
async function setBooking(id, status) {
  if (status === "cancelled" && !confirm(`Cancel booking #${id}?`)) return;
  await api(`/admin/bookings/${id}/status`, { method: "POST", body: JSON.stringify({ status }) });
  toast(`Booking #${id} ${status}`); show("bookings");
}

// ── follow-ups (human in the loop) ──
async function followupsView() {
  const list = await api("/admin/followups");
  const drafts = list.filter((f) => f.status === "draft");
  setBadge(drafts.length);
  $("main").innerHTML = `<h2>Follow-ups</h2>
    <p class="sub">The follow-up agent drafts a message for leads who went quiet (day 1, 3 and 7). Nothing is sent until you approve it.</p>
    <div class="row" style="margin-bottom:16px">
      <button class="btn primary" onclick="runFollowups(false)">Run follow-up agent</button>
      <button class="btn" onclick="runFollowups(true)" title="Skip the 1/3/7 day wait, for demos">Run now, ignore wait (demo)</button>
    </div>
    ${drafts.map((f) => `<div class="card fu"><div class="row"><b>${esc(f.name || f.user_id)}</b><span class="score ${f.score || "cold"}">${f.score || "cold"}</span>
      <span class="hint">follow-up ${f.step} of 3 · ${when(f.created_at)}</span></div>
      <textarea id="fu-${f.id}">${esc(f.message)}</textarea>
      <div class="row"><button class="btn primary small" onclick="approve(${f.id})">✓ Approve & send</button>
      <button class="btn small" onclick="skip(${f.id})">Skip</button></div></div>`).join("") || `<div class="card empty">No drafts waiting.</div>`}
    <h3>History</h3>
    <div class="card"><div class="table-wrap"><table><thead><tr><th>Lead</th><th>Message</th><th>Status</th><th>Sent</th></tr></thead><tbody>
      ${list.filter((f) => f.status !== "draft").map((f) => `<tr><td>${esc(f.name || f.user_id)}</td><td>${esc(f.message)}</td>
        <td><span class="status ${f.status}">${f.status}</span></td><td class="hint">${when(f.sent_at)}</td></tr>`).join("") || '<tr><td colspan="4" class="hint">none yet</td></tr>'}
    </tbody></table></div></div>`;
}
async function runFollowups(ignoreWait) {
  toast("Follow-up agent is working…");
  const r = await api(`/admin/followups/run?ignore_wait=${ignoreWait}`, { method: "POST" });
  toast(`${r.drafted} follow-up(s) drafted`); show("followups");
}
async function approve(id) {
  await api(`/admin/followups/${id}/approve`, { method: "POST", body: JSON.stringify({ message: $(`fu-${id}`).value }) });
  toast("Sent to the customer's chat"); show("followups");
}
async function skip(id) { await api(`/admin/followups/${id}/skip`, { method: "POST" }); toast("Skipped"); show("followups"); }

if (token) start().catch(() => logout());
