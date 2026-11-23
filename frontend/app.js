// LeadPilot customer page: listings, instant lead form, and the chat widget.
const API = window.LEADPILOT_API;
const $ = (id) => document.getElementById(id);
let interest = "property";
let busy = false;

// ── storage helpers (the page still works if storage is blocked) ──
const store = {
  get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
  set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
  del(k) { try { localStorage.removeItem(k); } catch (e) {} },
};
let me = store.get("lp_user");          // {user_id, name}
let threadId = store.get("lp_thread");

function randomId(prefix) { return `${prefix}-${Math.random().toString(36).slice(2, 10)}`; }
function ensureUser() {
  if (!me) { me = { user_id: randomId("guest"), name: "Guest" }; store.set("lp_user", me); }
  if (!threadId) { threadId = randomId("t"); store.set("lp_thread", threadId); }
}

function toast(text) {
  const t = $("toast"); t.textContent = text; t.classList.add("show");
  clearTimeout(t._h); t._h = setTimeout(() => t.classList.remove("show"), 2600);
}

async function api(path, opts = {}) {
  const res = await fetch(API + path, { headers: { "Content-Type": "application/json" }, ...opts });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res.text();
}

// ── formatting ──
function pkr(n) {
  if (n >= 10_000_000) return `PKR ${(n / 10_000_000).toFixed(2).replace(/\.?0+$/, "")} crore`;
  return `PKR ${(n / 100_000).toFixed(1).replace(/\.0$/, "")} lakh`;
}
function esc(s) { return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c])); }
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
    $("listing-grid").innerHTML = `<div class="empty">Couldn't reach the API at ${esc(API)}. Is it running?</div>`;
  }
}
function renderListings(cat) {
  const items = allListings.filter((l) => !cat || l.category === cat);
  $("listing-grid").innerHTML = items.map((l) => {
    const icon = l.category === "car" ? "🚗" : (l.bedrooms ? "🏠" : "📐");
    const spec = l.bedrooms ? `${l.bedrooms} bed · ` : (l.year ? `${l.year} model · ` : "");
    const cta = l.category === "car" ? "Book a test drive" : "Book a site visit";
    return `<article class="card listing">
      <div class="art">${icon}</div>
      <div class="body">
        <span class="id">${esc(l.id)} · ${esc(l.business)}</span>
        <h4>${esc(l.title)}</h4>
        <span class="loc">📍 ${esc(l.location)}</span>
        <span class="details">${spec}${esc(l.details)}</span>
        <span class="price">${pkr(l.price_pkr)}</span>
        <button class="btn small primary" onclick="askAbout('${l.id}', '${cta}')">${cta}</button>
      </div></article>`;
  }).join("") || `<div class="empty">No listings.</div>`;
}
$("tabs").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  document.querySelectorAll("#tabs button").forEach((x) => x.classList.toggle("on", x === b));
  renderListings(b.dataset.c);
});
function askAbout(id, cta) {
  openChat();
  $("chat-input").value = `${cta} for ${id}. What slots are free this week?`;
  $("chat-input").focus();
}

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
    const r = await api("/leads", { method: "POST", body: JSON.stringify({
      name: $("f-name").value, phone: $("f-phone").value, interest, message: $("f-msg").value }) });
    me = { user_id: r.user_id, name: $("f-name").value.trim() }; threadId = r.thread_id;
    store.set("lp_user", me); store.set("lp_thread", threadId);
    openChat(false);
    $("msgs").innerHTML = "";
    addMsg("user", r.first_message);
    addMsg("assistant", r.reply, [`replied in ${(r.response_ms / 1000).toFixed(1)}s`]);
    updateWho();
    $("lead-form").reset();
  } catch (err) {
    toast("Sorry, something went wrong. Please try again.");
  } finally { btn.disabled = false; btn.textContent = "Get my instant reply"; }
});

// ── chat ──
function updateWho() {
  $("chat-who").innerHTML = `<span class="dot"></span>Online · ${esc(me?.name || "Guest")} · chat ${esc(threadId || "")}`;
}
function addMsg(role, text, chips = [], kind = "") {
  const div = document.createElement("div");
  div.className = `msg ${role} ${kind === "followup" ? "followup" : ""}`;
  div.innerHTML = md(text);
  if (kind === "followup") chips = ["follow-up", ...chips];
  if (chips.length) div.innerHTML += `<div class="meta">${chips.map((c) =>
    `<span class="chip ${c === "follow-up" ? "warn" : (c.startsWith("replied") || c.startsWith("tokens") ? "muted" : "")}">${esc(c)}</span>`).join("")}</div>`;
  $("msgs").appendChild(div);
  $("msgs").scrollTop = $("msgs").scrollHeight;
}
async function loadThread() {
  ensureUser(); updateWho();
  $("msgs").innerHTML = "";
  try {
    const r = await api(`/customer/${encodeURIComponent(me.user_id)}/threads/${encodeURIComponent(threadId)}`);
    if (!r.messages.length) {
      addMsg("assistant", `Hi${me.name && me.name !== "Guest" ? " " + me.name : ""}! I'm LeadPilot. Are you looking for a property or a car? Tell me your budget and when you plan to buy.`);
    }
    r.messages.forEach((m) => addMsg(m.role, m.text, [], m.kind));
  } catch (e) { addMsg("assistant", "I can't reach the server right now. Please try again in a moment."); }
}
function openChat(load = true) {
  $("chat").classList.add("open"); $("chat-fab").style.display = "none";
  if (load) loadThread();
  setTimeout(() => $("chat-input").focus(), 50);
}
function closeChat() { $("chat").classList.remove("open"); $("chat-fab").style.display = ""; }
async function send(text) {
  if (busy || !text.trim()) return;
  ensureUser(); busy = true; $("send-btn").disabled = true;
  addMsg("user", text);
  const typing = document.createElement("div"); typing.className = "typing"; typing.textContent = "LeadPilot is typing…";
  $("msgs").appendChild(typing); $("msgs").scrollTop = $("msgs").scrollHeight;
  const t0 = performance.now();
  try {
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
function switchUser() {
  const name = prompt("Demo: switch to which customer name? (each name is a separate user)", "");
  if (!name) return;
  me = { user_id: "demo-" + name.trim().toLowerCase().replace(/[^a-z0-9]+/g, "-"), name: name.trim() };
  threadId = randomId("t"); store.set("lp_user", me); store.set("lp_thread", threadId);
  loadThread(); toast(`You are now ${name}. Their data is completely separate.`);
}
async function showBookings() {
  ensureUser(); openChat(false);
  try {
    const list = await api(`/customer/${encodeURIComponent(me.user_id)}/bookings`);
    addMsg("assistant", list.length ? "Your bookings:\n" + list.map((b) =>
      `• #${b.id} ${b.kind} – ${b.title} (${b.listing_id}) on ${b.date} at ${b.time} [${b.status}]`).join("\n")
      : "You have no bookings yet. Want me to find you a slot?");
  } catch (e) { toast("Couldn't load bookings."); }
}
$("nav-bookings").addEventListener("click", (e) => { e.preventDefault(); showBookings(); });

loadListings();
