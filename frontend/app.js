// LeadPilot customer page: listings, instant lead form, and the chat widget.
const API = window.LEADPILOT_API;
const $ = (id) => document.getElementById(id);
let interest = "property";
let busy = false;
const formOpenedAt = Date.now();

// ── storage helpers (the page still works if storage is blocked) ──
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} },
};
// me = {user_id, token, name}. The server issues the id and a signed token; only that token opens this
// customer's chats, bookings and profile. Note: kept in localStorage (audit 2.6, reported as Medium).
let me = store.get("lp_user");
let threadId = store.get("lp_thread");
if (me && !me.token) { me = null; store.del("lp_user"); }

function randomId(prefix) { return `${prefix}-${Math.random().toString(36).slice(2, 10)}`; }

function toast(text) {
  const t = $("toast"); t.textContent = text; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, opts = {}) {
  const headers = { "Content-Type": "application/json" };
  if (me && me.token) headers["X-User-Token"] = me.token;
  const res = await fetch(API + path, { ...opts, headers });
  if (!res.ok) throw new Error(String(res.status));
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

async function ensureUser(name) {
  if (!me) {
    const s = await api("/customer/session", { method: "POST" });
    me = { user_id: s.user_id, token: s.token, name: name || "Guest" };
    store.set("lp_user", me);
  }
  if (!threadId) { threadId = randomId("t"); store.set("lp_thread", threadId); }
}

// ── formatting: everything is escaped before it touches the page (audit 5.2, 8.3) ──
function pkr(n) {
  if (n >= 10_000_000) return `PKR ${(n / 10_000_000).toFixed(2).replace(/\.?0+$/, "")} crore`;
  return `PKR ${(n / 100_000).toFixed(1).replace(/\.0$/, "")} lakh`;
}
function esc(s) { return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
function md(s) { return esc(s).replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/^\s*[-•]\s+/gm, "• "); }

// ── listings ──
let allListings = [];
async function loadListings() {
  try {
    allListings = await api("/listings");
    $("stat-props").textContent = allListings.filter((l) => l.category === "property").length;
    $("stat-cars").textContent = allListings.filter((l) => l.category === "car").length;
    renderListings("");
  } catch (e) {
    $("listing-grid").innerHTML = `<div class="empty">Listings are unavailable right now. Please try again shortly.</div>`;
  }
}
function renderListings(cat) {
  const items = allListings.filter((l) => !cat || l.category === cat);
  $("listing-grid").innerHTML = items.map((l) => {
    const icon = l.category === "car" ? "🚗" : (l.bedrooms ? "🏠" : "📐");
    const spec = l.bedrooms ? `${esc(l.bedrooms)} bed · ` : (l.year ? `${esc(l.year)} model · ` : "");
    const cta = l.category === "car" ? "Book a test drive" : "Book a site visit";
    return `<article class="card listing">
      <div class="art">${icon}</div>
      <div class="body">
        <span class="id">${esc(l.id)} · ${esc(l.business)}</span>
        <h4>${esc(l.title)}</h4>
        <span class="loc">📍 ${esc(l.location)}</span>
        <span class="details">${spec}${esc(l.details)}</span>
        <span class="price">${pkr(l.price_pkr)}</span>
        <button class="btn small primary" data-ask="${esc(l.id)}" data-cta="${esc(cta)}">${cta}</button>
      </div></article>`;
  }).join("") || `<div class="empty">No listings.</div>`;
}
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  renderListings(b.dataset.c);
});
$("listing-grid").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-ask]"); if (!b) return;
  openChat();
  $("chat-input").value = `${b.dataset.cta} for ${b.dataset.ask}. What slots are free this week?`;
  $("chat-input").focus();
});

// ── lead form: the instant reply ──
$("interest-seg").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  interest = b.dataset.v;
  document.querySelectorAll("#interest-seg button").forEach((x) => x.classList.toggle("on", x === b));
});
$("lead-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  const btn = $("lead-submit"); btn.disabled = true; btn.textContent = "Connecting you…";
  try {
    const r = await fetch(API + "/leads", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: $("f-name").value, phone: $("f-phone").value, interest, message: $("f-msg").value,
        website: $("f-website").value, elapsed_ms: Date.now() - formOpenedAt }) });
    if (r.status === 429) throw new Error("rate");
    if (!r.ok) throw new Error("bad");
    const d = await r.json();
    me = { user_id: d.user_id, token: d.token, name: $("f-name").value.trim() }; threadId = d.thread_id;
    store.set("lp_user", me); store.set("lp_thread", threadId);
    openChat(false);
    $("msgs").innerHTML = "";
    addMsg("user", d.first_message);
    addMsg("assistant", d.reply, [`replied in ${(d.response_ms / 1000).toFixed(1)}s`]);
    updateWho();
    $("lead-form").reset();
  } catch (err) {
    toast(err.message === "rate" ? "Too many requests. Please wait a few minutes." : "Please check your details and try again.");
  } finally { btn.disabled = false; btn.textContent = "Get my instant reply"; }
});

// ── chat ──
function updateWho() {
  $("chat-who").innerHTML = `<span class="dot"></span>Online · ${esc(me?.name || "Guest")}`;
}
function addMsg(role, text, chips = [], kind = "") {
  const div = document.createElement("div");
  div.className = `msg ${role} ${kind === "followup" ? "followup" : ""}`;
  div.innerHTML = md(text);
  if (kind === "followup") chips = ["follow-up", ...chips];
  if (chips.length) div.innerHTML += `<div class="meta">${chips.map((c) =>
    `<span class="chip ${c === "follow-up" ? "warn" : (c.startsWith("replied") ? "muted" : "")}">${esc(c)}</span>`).join("")}</div>`;
  $("msgs").appendChild(div);
  $("msgs").scrollTop = $("msgs").scrollHeight;
}
async function loadThread() {
  $("msgs").innerHTML = "";
  try {
    await ensureUser(); updateWho();
    const r = await api(`/customer/${encodeURIComponent(me.user_id)}/threads/${encodeURIComponent(threadId)}`);
    if (!r.messages.length) {
      addMsg("assistant", `Hi${me.name && me.name !== "Guest" ? " " + me.name : ""}! I'm LeadPilot. Are you looking for a property or a car? Tell me your budget and when you plan to buy.`);
    }
    r.messages.forEach((m) => addMsg(m.role, m.text, [], m.kind));
  } catch (e) {
    if (e.message === "401") { me = null; store.del("lp_user"); return loadThread(); }
    addMsg("assistant", "I can't reach the server right now. Please try again in a moment.");
  }
}
function openChat(load = true) {
  $("chat").classList.add("open"); $("chat-fab").style.display = "none";
  if (load) loadThread();
  setTimeout(() => $("chat-input").focus(), 50);
}
function closeChat() { $("chat").classList.remove("open"); $("chat-fab").style.display = ""; }
async function send(text) {
  if (busy || !text.trim()) return;
  busy = true; $("send-btn").disabled = true;
  addMsg("user", text);
  const typing = document.createElement("div"); typing.className = "typing"; typing.textContent = "LeadPilot is typing…";
  $("msgs").appendChild(typing); $("msgs").scrollTop = $("msgs").scrollHeight;
  const t0 = performance.now();
  try {
    await ensureUser();
    const r = await api("/chat", { method: "POST", body: JSON.stringify({ user_id: me.user_id, thread_id: threadId, message: text }) });
    typing.remove();
    const chips = r.tool_calls.map((c) => `🔧 ${c.name}`);
    chips.push(`replied in ${((performance.now() - t0) / 1000).toFixed(1)}s`);
    addMsg("assistant", r.reply, chips);
  } catch (e) {
    typing.remove(); addMsg("assistant", "Sorry, I couldn't send that. Please try again.");
  } finally { busy = false; $("send-btn").disabled = false; $("chat-input").focus(); }
}
$("composer").addEventListener("submit", (e) => { e.preventDefault(); const v = $("chat-input").value; $("chat-input").value = ""; send(v); });
$("suggest").addEventListener("click", (e) => { const b = e.target.closest("button"); if (b) send(b.textContent); });

function newThread() {
  threadId = randomId("t"); store.set("lp_thread", threadId);
  loadThread(); toast("New conversation started. I still remember you.");
}
async function switchUser() {
  const name = prompt("Demo: start as a different customer. Their name?", "");
  if (!name) return;
  me = null; threadId = null; store.del("lp_user"); store.del("lp_thread");
  await ensureUser(name.trim().slice(0, 40));
  loadThread(); toast(`You are now ${name}. Their data is completely separate.`);
}
async function showBookings() {
  openChat(false);
  try {
    await ensureUser();
    const list = await api(`/customer/${encodeURIComponent(me.user_id)}/bookings`);
    addMsg("assistant", list.length ? "Your bookings:\n" + list.map((b) =>
      `• #${b.id} ${b.kind} – ${b.title} (${b.listing_id}) on ${b.date} at ${b.time} [${b.status}]`).join("\n")
      : "You have no bookings yet. Want me to find you a slot?");
  } catch (e) { toast("Couldn't load bookings."); }
}
async function deleteMyData() {
  if (!me) { toast("We don't hold any data for you."); return; }
  if (!confirm("Delete all your data (chats, bookings, saved details)? This can't be undone.")) return;
  try {
    await api(`/customer/${encodeURIComponent(me.user_id)}`, { method: "DELETE" });
    me = null; threadId = null; store.del("lp_user"); store.del("lp_thread");
    $("msgs").innerHTML = ""; addMsg("assistant", "All your data has been deleted. You can start fresh any time.");
    toast("Your data was deleted.");
  } catch (e) { toast("Couldn't delete right now. Please try again."); }
}

// ── buttons (no inline handlers) ──
document.querySelectorAll("[data-open-chat]").forEach((b) => b.addEventListener("click", () => openChat()));
$("chat-close").addEventListener("click", closeChat);
$("btn-new").addEventListener("click", newThread);
$("btn-bookings").addEventListener("click", showBookings);
$("btn-switch").addEventListener("click", switchUser);
$("btn-delete").addEventListener("click", deleteMyData);
$("nav-bookings").addEventListener("click", (e) => { e.preventDefault(); showBookings(); });
$("footer-delete").addEventListener("click", (e) => { e.preventDefault(); openChat(false); deleteMyData(); });

loadListings();
