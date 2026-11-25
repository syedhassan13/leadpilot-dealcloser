import { request, health, ApiError } from "./api.js";
import { $, $$, esc, toast, reveal, headerMenu, liveStatus, formTimer, busy, themeToggle } from "./ui.js";

themeToggle();

headerMenu();
reveal();
liveStatus($("#status"), health);

// ───────── response-time scale: logarithmic positions from seconds ─────────
const MAX_S = 172800; // 2 days
const pos = (s) => Math.log10(Math.max(1, s)) / Math.log10(MAX_S);
$$("[data-s]").forEach((el) => {
  const p = pos(Number(el.dataset.s)) * 100;
  if (el.classList.contains("scale-window")) el.style.width = p + "%";
  else el.style.left = p + "%";
});

// ───────── flight recorder: replay of a recorded conversation ─────────
function nextSaturday() {
  const d = new Date(); d.setDate(d.getDate() + ((6 - d.getDay() + 7) % 7 || 7));
  const iso = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`; // local date, not UTC
  return { label: d.toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" }), iso };
}
const sat = nextSaturday();
const SCENARIOS = {
  car: { lead: 2741, events: [
    { t: 0, who: "buyer", text: "Hi, looking for an automatic car under 55 lakh. Need it within 3 weeks." },
    { t: 1.2, who: "tool", text: "save_lead_details(budget=5,500,000, timeline=3 weeks)", g: { budget: "55 lakh", timeline: "3 weeks", score: "hot" } },
    { t: 1.9, who: "tool", text: "search_inventory(car, max=55 lakh, automatic)" },
    { t: 4.1, who: "agent", text: "Three automatics fit your budget:<br>· <b>C203</b> Honda City 1.5 Aspire 2023 · 48 lakh<br>· <b>C207</b> Toyota Yaris ATIV X 2023 · 52 lakh<br>· <b>C204</b> Suzuki Alto VXL AGS 2024 · 31 lakh<br>Want to test drive one this week?", g: { next: "Book test drive" } },
    { t: 31, who: "buyer", text: "The City, on Saturday?" },
    { t: 32.2, who: "tool", text: `check_availability(C203, ${sat.iso})` },
    { t: 34.0, who: "agent", text: `${sat.label} is open at <b>10:00, 12:00, 14:00 and 16:00</b>. Which time suits you?` },
    { t: 52, who: "buyer", text: "12 please" },
    { t: 54.6, who: "ask", text: "Asks before booking" },
    { t: 54.7, who: "agent", text: `Shall I confirm a test drive of the <b>Honda City (C203)</b> on <b>${sat.label} at 12:00</b>?` },
    { t: 63, who: "buyer", text: "Yes, confirm" },
    { t: 64.1, who: "tool", text: `book_visit(C203, ${sat.iso}, 12:00) · confirmed` },
    { t: 66.3, who: "agent", text: `Booked. Test drive <b>#2</b> at the AutoHub I-8 showroom, ${sat.label}, 12:00. Bring your CNIC and driving licence.`, g: { next: `Test drive · ${sat.label}` } },
  ] },
  property: { lead: 2758, events: [
    { t: 0, who: "buyer", text: "Salam, need a 3 bed house in Islamabad. Budget around 1.5 crore, moving in 2 months." },
    { t: 1.4, who: "tool", text: "save_lead_details(budget=15,000,000, timeline=2 months)", g: { budget: "1.5 crore", timeline: "2 months", score: "hot" } },
    { t: 2.0, who: "tool", text: "search_inventory(property, max=1.5 crore, bedrooms≥3)" },
    { t: 4.6, who: "agent", text: "Two homes match:<br>· <b>P105</b> 7 Marla house, B-17 Multi Gardens · 1.25 crore<br>· <b>P101</b> 5 Marla house, Bahria Town Ph 8 · 1.65 crore (slightly over)<br>Would you like to visit P105 this week?", g: { next: "Book site visit" } },
    { t: 40, who: "buyer", text: "Does Skyline do bank financing?" },
    { t: 41.1, who: "tool", text: "search_faq(\"bank home finance\")" },
    { t: 43.5, who: "agent", text: "Yes. Ready houses can go through <b>Meezan Easy Home</b> or <b>HBL HomeLoan</b>: usually 15–30% down, 3–6 weeks to approve. A 1% token holds a house for 15 days." },
    { t: 70, who: "buyer", text: `Ok, P105 on ${sat.label} at 10` },
    { t: 71.2, who: "tool", text: `check_availability(P105, ${sat.iso})` },
    { t: 72.9, who: "ask", text: "Asks before booking" },
    { t: 73.0, who: "agent", text: `Shall I confirm a site visit to <b>P105</b> on <b>${sat.label} at 10:00</b>?` },
    { t: 80, who: "buyer", text: "haan, theek hai" },
    { t: 81.3, who: "tool", text: `book_visit(P105, ${sat.iso}, 10:00) · confirmed` },
    { t: 83.4, who: "agent", text: `Confirmed: site visit <b>#3</b>, ${sat.label} at 10:00. Our agent will share the address on WhatsApp 2 hours before.`, g: { next: `Site visit · ${sat.label}` } },
  ] },
};
const NEEDLE = { cold: -70, warm: 0, hot: 62 };
const reduce = matchMedia("(prefers-reduced-motion: reduce)").matches;
const log = $("#rec-log");
let timers = [], current = "car", started = false;

function setGauge(id, value) {
  const el = $(id); if (!el || el.textContent === value) return;
  el.textContent = value; el.classList.add("flash"); setTimeout(() => el.classList.remove("flash"), 900);
}
function clock(t) { const s = Math.floor(t); $("#g-clock").textContent = `T+${String(Math.floor(s / 60)).padStart(2, "0")}:${String(s % 60).padStart(2, "0")}`; }
function resetGauges() {
  $("#needle").style.transform = `rotate(${NEEDLE.cold}deg)`;
  ["#g-budget", "#g-timeline"].forEach((id) => { $(id).textContent = "—"; });
  $("#g-score").textContent = "COLD"; $("#g-next").textContent = "Qualify"; clock(0);
}
function render(ev) {
  const li = document.createElement("li");
  li.className = ev.who;
  const ts = `<span class="t">T+${String(Math.floor(ev.t / 60)).padStart(2, "0")}:${String(Math.floor(ev.t % 60)).padStart(2, "0")}</span>`;
  if (ev.who === "buyer" || ev.who === "agent") li.innerHTML = ts + `<span class="txt">${ev.who === "buyer" ? esc(ev.text) : ev.text}</span>`;
  else li.textContent = ev.text;
  log.appendChild(li);
  while (log.children.length > 12) log.firstElementChild.remove();
  clock(ev.t);
  if (ev.g) {
    if (ev.g.score) { $("#needle").style.transform = `rotate(${NEEDLE[ev.g.score]}deg)`; setGauge("#g-score", ev.g.score.toUpperCase()); }
    if (ev.g.budget) setGauge("#g-budget", ev.g.budget);
    if (ev.g.timeline) setGauge("#g-timeline", ev.g.timeline);
    if (ev.g.next) setGauge("#g-next", ev.g.next);
  }
}
function play(name) {
  timers.forEach(clearTimeout); timers = [];
  current = name; log.innerHTML = ""; resetGauges();
  const sc = SCENARIOS[name];
  $("#rec-lead").textContent = sc.lead;
  if (reduce) { sc.events.forEach(render); return; }
  // Compress buyer think-time so the replay stays watchable; agent latencies keep their real proportions.
  let at = 400, prev = 0;
  sc.events.forEach((ev) => {
    const gap = ev.t - prev; prev = ev.t;
    at += ev.who === "buyer" ? 1700 : Math.min(gap * 520, 1600) + 260;
    timers.push(setTimeout(() => render(ev), at));
  });
}
$$(".rec-tabs [role=tab]").forEach((tab) => tab.addEventListener("click", () => {
  $$(".rec-tabs [role=tab]").forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
  play(tab.dataset.scenario);
}));
$("#rec-replay").addEventListener("click", () => play(current));
new IntersectionObserver((entries) => {
  if (entries[0].isIntersecting && !started) { started = true; play("car"); }
}, { threshold: 0.3 }).observe($(".recorder"));

// ───────── try it live: the real agent, in the real widget ─────────
// The landing chat is billed to the platform demo dealer (category "all"); its public key comes from the API.
request("/dealers", { timeout: 60000 }).then((list) => {
  const demo = list.find((d) => d.category === "all" && d.widget_key);
  if (demo) $("#chat").setAttribute("key", demo.widget_key);
}).catch(() => {});
$("#try-live").addEventListener("click", async () => {
  await customElements.whenDefined("leadpilot-chat");
  $("#chat").open();
});

// ───────── pilot request ─────────
const form = $("#pilot-form");
const elapsed = formTimer(form);
$$("[data-plan]").forEach((a) => a.addEventListener("click", () => {
  $("#p-msg").value = `Interested in the ${a.dataset.plan} plan.`;
}));
form.addEventListener("submit", async (e) => {
  e.preventDefault();
  const err = $("#pilot-error"); err.textContent = "";
  $$(".input", form).forEach((i) => i.removeAttribute("aria-invalid"));
  const bad = $$("input[required], .input[pattern], input[type=email]", form).filter((i) => !i.checkValidity());
  if (bad.length) {
    bad.forEach((i) => i.setAttribute("aria-invalid", "true"));
    err.textContent = "Please fill in your name, dealership and a valid phone number.";
    bad[0].focus(); return;
  }
  const data = Object.fromEntries(new FormData(form));
  const body = { name: data.name, company: data.company, phone: data.phone, email: data.email || null,
    dealer_type: data.dealer_type, monthly_leads: data.monthly_leads || null, message: data.message || "",
    website: data.website || "", elapsed_ms: elapsed() };
  const btn = $("button[type=submit]", form);
  try {
    await busy(btn, "Sending…", () => request("/pilot-requests", { method: "POST", body }));
    const ok = $("#pilot-success"); ok.hidden = false; ok.focus();
  } catch (ex) {
    err.textContent = ex instanceof ApiError ? ex.message : "Something went wrong. Please try again.";
  }
});
