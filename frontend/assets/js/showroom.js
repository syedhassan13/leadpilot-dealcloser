import { request, ApiError } from "./api.js";
import { $, $$, esc, pkr, toast, formTimer, busy, dayLabel, themeToggle } from "./ui.js";

themeToggle();

const DEALERS = {
  skyline: { name: "Skyline Realty", category: "property", eyebrow: "Real estate · Islamabad & Rawalpindi",
    tagline: "Houses, apartments and plots in DHA, Bahria Town, Gulberg Greens, B-17, E-11 and F-11.",
    budgets: [["", "Any budget"], [10_000_000, "1 crore"], [20_000_000, "2 crore"], [50_000_000, "5 crore"], [100_000_000, "10 crore"]],
    typeOf: (l) => /plot/i.test(l.title) ? "Plot" : /apartment/i.test(l.title) ? "Apartment" : "House" },
  autohub: { name: "AutoHub Motors", category: "car", eyebrow: "Cars · G-9 & I-8 showrooms, Islamabad",
    tagline: "Inspected new and used cars with a 200-point report and a 6-month engine warranty.",
    budgets: [["", "Any budget"], [3_500_000, "35 lakh"], [5_000_000, "50 lakh"], [7_500_000, "75 lakh"], [10_000_000, "1 crore"]],
    typeOf: (l) => l.title.split(" ")[0] },
};

// Line-art glyphs, drawn in ink so they read as product drawings, not stock photos.
const GLYPH = {
  House: `<svg viewBox="0 0 120 90" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 84h92M22 84V42l38-28 38 28v42"/><path d="M48 84V58h24v26"/><path d="M30 50h12v10H30zM78 50h12v10H78z"/><path d="M82 26V12h8v20" /><path d="M60 14v-6" stroke="#ff4f00"/></svg>`,
  Apartment: `<svg viewBox="0 0 120 90" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 84h100M30 84V10h44v74M74 84V34h22v50"/><path d="M38 20h8v8h-8zM56 20h8v8h-8zM38 38h8v8h-8zM56 38h8v8h-8zM38 56h8v8h-8zM56 56h8v8h-8zM80 44h10v8H80zM80 60h10v8H80z"/><path d="M46 84V72h12v12" stroke="#ff4f00"/></svg>`,
  Plot: `<svg viewBox="0 0 120 90" fill="none" stroke="currentColor" stroke-width="2"><path d="M14 70 40 40h66L80 70z" stroke-dasharray="5 4"/><path d="M14 70h66M80 70l26-30"/><path d="M60 52v-34" stroke="#ff4f00"/><path d="M60 18h18l-5 6 5 6H60" stroke="#ff4f00"/></svg>`,
  car: `<svg viewBox="0 0 120 90" fill="none" stroke="currentColor" stroke-width="2"><path d="M10 62h100v-12c0-4-3-7-7-8l-16-4-12-14H38L26 38l-10 3c-4 1-6 5-6 9z"/><path d="M38 24h12v14H30zM56 24h14l10 14H56z"/><circle cx="32" cy="64" r="9" fill="var(--surface)"/><circle cx="88" cy="64" r="9" fill="var(--surface)"/><path d="M104 50h6" stroke="#ff4f00"/></svg>`,
};
const glyph = (l) => l.category === "car" ? GLYPH.car : GLYPH[DEALERS.skyline.typeOf(l)];

const state = { dealer: "skyline", listings: [], type: "", current: null, slot: null };
const chat = $("#chat");
const ready = customElements.whenDefined("leadpilot-chat");

// ───────── dealer switching (kept in the URL so links are shareable) ─────────
function pickDealer(id, push = true) {
  state.dealer = DEALERS[id] ? id : "skyline";
  const d = DEALERS[state.dealer];
  $$(".dealer-switch [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b.dataset.dealer === state.dealer)));
  $("#dealer-title").textContent = d.name;
  $("#dealer-eyebrow").textContent = d.eyebrow;
  $("#dealer-tagline").textContent = d.tagline;
  document.title = `${d.name} · Showroom demo · LeadPilot`;
  $("#f-max").innerHTML = d.budgets.map(([v, l]) => `<option value="${v}">${l}</option>`).join("");
  $("#c-msg").placeholder = d.category === "car" ? "e.g. automatic sedan under 50 lakh, buying this month" : "e.g. 3 bed house, budget 2 crore, buying in 2 months";
  state.type = "";
  chat.setAttribute("dealer", state.dealer);
  if (push) history.replaceState(null, "", `?dealer=${state.dealer}`);
  load();
}
$$(".dealer-switch [role=tab]").forEach((b) => b.addEventListener("click", () => pickDealer(b.dataset.dealer)));

// ───────── listings ─────────
function skeleton() {
  $("#grid").innerHTML = Array.from({ length: 6 }, () => `<article class="card skeleton-card" aria-hidden="true"><div class="card-art"></div>
    <div class="card-body"><span class="skeleton">&nbsp;</span><span class="skeleton">&nbsp;</span><span class="skeleton">&nbsp;</span></div></article>`).join("");
}
async function load() {
  skeleton(); $("#grid").setAttribute("aria-busy", "true"); $("#result-count").textContent = "";
  try {
    state.listings = await request(`/listings?dealer=${state.dealer}`);
    renderStats(); renderChips(); render();
  } catch (e) {
    $("#grid").innerHTML = `<div class="error-state" role="alert">${esc(e.message)} <button class="btn btn-sm" id="retry">Try again</button></div>`;
    $("#retry").addEventListener("click", load);
  } finally { $("#grid").setAttribute("aria-busy", "false"); }
}
function renderStats() {
  const prices = state.listings.map((l) => l.price_pkr);
  const dd = $$("#store-stats dd");
  dd[0].textContent = state.listings.length; dd[1].textContent = prices.length ? pkr(Math.min(...prices), { short: true }) : "–";
  dd.forEach((d) => d.classList.remove("skeleton"));
}
function renderChips() {
  const d = DEALERS[state.dealer];
  const types = [...new Set(state.listings.map(d.typeOf))].sort();
  $("#type-chips").innerHTML = [["", "All"], ...types.map((t) => [t, t])]
    .map(([v, l]) => `<button type="button" data-type="${esc(v)}" aria-pressed="${v === state.type}">${esc(l)}</button>`).join("");
}
$("#type-chips").addEventListener("click", (e) => {
  const b = e.target.closest("button"); if (!b) return;
  state.type = b.dataset.type; $$("#type-chips button").forEach((x) => x.setAttribute("aria-pressed", String(x === b))); render();
});
["input", "change"].forEach((ev) => $("#filters").addEventListener(ev, render));
$("#filters").addEventListener("submit", (e) => e.preventDefault());

function filtered() {
  const d = DEALERS[state.dealer];
  const q = $("#f-q").value.trim().toLowerCase();
  const max = Number($("#f-max").value) || Infinity;
  const dir = $("#f-sort").value === "desc" ? -1 : 1;
  return state.listings
    .filter((l) => l.price_pkr <= max)
    .filter((l) => !state.type || d.typeOf(l) === state.type)
    .filter((l) => !q || `${l.id} ${l.title} ${l.location} ${l.details}`.toLowerCase().includes(q))
    .sort((a, b) => (a.price_pkr - b.price_pkr) * dir);
}
function render() {
  const list = filtered();
  $("#result-count").textContent = `${list.length} of ${state.listings.length} listings`;
  if (!list.length) {
    $("#grid").innerHTML = `<div class="empty-state"><p class="h3">Nothing matches those filters.</p><p>Try a higher budget, or ask the assistant to find something close.</p>
      <button class="btn btn-sm" id="clear-filters">Clear filters</button></div>`;
    $("#clear-filters").addEventListener("click", () => { $("#filters").reset(); state.type = ""; renderChips(); render(); });
    return;
  }
  $("#grid").innerHTML = list.map((l) => {
    const spec = l.bedrooms ? `<span>${esc(l.bedrooms)} bed</span>` : l.year ? `<span>${esc(l.year)} model</span>` : `<span>Residential</span>`;
    const verb = l.category === "car" ? "test drive" : "visit";
    return `<article class="card">
      <div class="card-art" aria-hidden="true">${glyph(l)}<span class="card-id">${esc(l.id)}</span></div>
      <div class="card-body">
        <h3>${esc(l.title)}</h3>
        <span class="loc">${esc(l.location)}</span>
        <div class="specs">${spec}<span>${esc(DEALERS[state.dealer].typeOf(l))}</span></div>
        <span class="price">${pkr(l.price_pkr)}</span>
        <div class="card-actions">
          <button class="btn btn-ink btn-sm" data-open="${esc(l.id)}" aria-label="View ${esc(l.title)} and book a ${verb}">View &amp; book</button>
          <button class="btn btn-sm" data-ask="${esc(l.id)}" aria-label="Ask about ${esc(l.title)}">Ask</button>
        </div>
      </div></article>`;
  }).join("");
}
$("#grid").addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open]"), ask = e.target.closest("[data-ask]");
  if (open) openDetail(open.dataset.open);
  if (ask) { await ready; chat.prefill(`About ${ask.dataset.ask}: `); }
});

// ───────── listing detail with real availability ─────────
const dialog = $("#detail");
async function openDetail(id) {
  const l = state.listings.find((x) => x.id === id); if (!l) return;
  state.current = l; state.slot = null;
  $("#d-art").innerHTML = glyph(l);
  $("#d-id").textContent = `${l.id} · ${l.business}`;
  $("#d-title").textContent = l.title;
  $("#d-loc").textContent = l.location;
  $("#d-price").textContent = pkr(l.price_pkr);
  $("#d-details").textContent = l.details;
  const specs = [["Type", DEALERS[state.dealer].typeOf(l)], l.bedrooms ? ["Bedrooms", l.bedrooms] : null, l.year ? ["Model year", l.year] : null,
    ["Viewing", l.category === "car" ? "Test drive, 45 min" : "Site visit, 45 min"]].filter(Boolean);
  $("#d-specs").innerHTML = specs.map(([k, v]) => `<div><dt>${esc(k)}</dt><dd>${esc(v)}</dd></div>`).join("");
  updateBook();
  $("#d-slots").innerHTML = Array.from({ length: 6 }, () => `<div class="slot-day"><h4 class="skeleton">&nbsp;</h4><ul><li class="skeleton">&nbsp;</li><li class="skeleton">&nbsp;</li></ul></div>`).join("");
  dialog.showModal();
  try {
    const days = await request(`/listings/${encodeURIComponent(l.id)}/slots?days=6`);
    $("#d-slots").innerHTML = days.map((d) => `<div class="slot-day"><h4>${esc(d.weekday)}<span>${esc(dayLabel(d.date))}</span></h4><ul>
      ${d.all.map((t) => `<li><button type="button" class="slot" data-date="${esc(d.date)}" data-time="${esc(t)}" aria-pressed="false"
        ${d.free.includes(t) ? "" : "disabled aria-label='" + esc(t) + " taken'"}>${esc(t)}</button></li>`).join("")}</ul></div>`).join("");
  } catch (e) {
    $("#d-slots").innerHTML = `<p class="error-state">${esc(e.message)}</p>`;
  }
}
$("#d-slots").addEventListener("click", (e) => {
  const b = e.target.closest(".slot:not(:disabled)"); if (!b) return;
  $$(".slot", dialog).forEach((s) => s.setAttribute("aria-pressed", String(s === b)));
  state.slot = { date: b.dataset.date, time: b.dataset.time }; updateBook();
});
function updateBook() {
  const btn = $("#d-book");
  btn.disabled = !state.slot;
  btn.textContent = state.slot ? `Book ${dayLabel(state.slot.date)} at ${state.slot.time}` : "Pick a slot to book";
}
$("#d-book").addEventListener("click", async () => {
  const l = state.current, s = state.slot; if (!l || !s) return;
  dialog.close(); await ready;
  chat.ask(`I'd like to book a ${l.category === "car" ? "test drive" : "site visit"} for ${l.id} (${l.title}) on ${s.date} at ${s.time}.`);
});
$("#d-ask").addEventListener("click", async () => {
  const l = state.current; dialog.close(); await ready;
  chat.prefill(`About ${l.id} (${l.title}): `);
});
dialog.addEventListener("click", (e) => { if (e.target === dialog) dialog.close(); });

// ───────── call-back form: instant reply, continued in the widget ─────────
const form = $("#callback-form");
const elapsed = formTimer(form);
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#cb-error"); err.textContent = "";
  const bad = $$("input[required], input[pattern]", form).filter((i) => !i.checkValidity());
  $$(".input", form).forEach((i) => i.removeAttribute("aria-invalid"));
  if (bad.length) { bad.forEach((i) => i.setAttribute("aria-invalid", "true")); err.textContent = "Please add your name and a valid phone number."; bad[0].focus(); return; }
  const data = Object.fromEntries(new FormData(form));
  const btn = $("button[type=submit]", form);
  try {
    const r = await busy(btn, "Connecting you…", () => request("/leads", { method: "POST", timeout: 90000, body: {
      name: data.name, phone: data.phone, interest: DEALERS[state.dealer].category, message: data.message || "",
      website: data.website || "", elapsed_ms: elapsed() } }));
    await ready;
    chat.adoptLead({ ...r, name: data.name });
    form.reset();
    toast(`Replied in ${(r.response_ms / 1000).toFixed(1)} s`);
  } catch (ex) {
    err.textContent = ex instanceof ApiError ? ex.message : "Something went wrong. Please try again.";
  }
});

pickDealer(new URLSearchParams(location.search).get("dealer") || "skyline", false);
