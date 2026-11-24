// UI helpers shared by every page. Everything from the server is escaped before it touches the DOM.

export const $ = (sel, root = document) => root.querySelector(sel);
export const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

export function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Minimal, safe markdown for agent replies: escape first, then **bold** and bullet lines. */
export function md(s) {
  return esc(s)
    .replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^\s*[-•]\s+(.*)$/gm, '<span class="li">$1</span>')
    .replace(/\n{3,}/g, "\n\n")
    .replace(/<\/span>\n+(?=<span class="li">)/g, "</span>")
    .replace(/<\/span>\n/g, "</span>")
    .replace(/\n/g, "<br>");
}

export const cls = (s) => String(s ?? "").replace(/[^a-z]/g, "");

export function pkr(n, { short = false } = {}) {
  if (n == null || n === "") return "–";
  const v = Number(n);
  if (v >= 10_000_000) return `${short ? "" : "PKR "}${(v / 10_000_000).toFixed(2).replace(/\.?0+$/, "")} crore`;
  return `${short ? "" : "PKR "}${(v / 100_000).toFixed(1).replace(/\.0$/, "")} lakh`;
}

/** Today's date (or a given date) as YYYY-MM-DD in local time, not UTC. */
export function localISO(d = new Date()) {
  return `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
}

export function parseDate(s) {
  if (!s) return null;
  if (/^\d{4}-\d{2}-\d{2}$/.test(s)) return new Date(s + "T00:00:00");
  return new Date(/Z|[+-]\d\d:?\d\d$/.test(s) ? s : s + "Z");
}

export function when(s) {
  const d = parseDate(s);
  if (!d || isNaN(d)) return "–";
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "just now";
  if (diff < 3600) return `${Math.floor(diff / 60)} min ago`;
  if (diff < 86400) return `${Math.floor(diff / 3600)} h ago`;
  if (diff < 7 * 86400) return `${Math.floor(diff / 86400)} d ago`;
  return d.toLocaleDateString([], { day: "numeric", month: "short", year: "numeric" });
}

export function dayLabel(iso) {
  const d = parseDate(iso);
  if (!d) return "–";
  const today = new Date(); today.setHours(0, 0, 0, 0);
  const diff = Math.round((d - today) / 86400000);
  if (diff === 0) return "Today";
  if (diff === 1) return "Tomorrow";
  if (diff === -1) return "Yesterday";
  return d.toLocaleDateString([], { weekday: "short", day: "numeric", month: "short" });
}

let toaster;
export function toast(message, { error = false, ms = 3200 } = {}) {
  if (!toaster) {
    toaster = document.createElement("div");
    toaster.className = "toaster"; toaster.setAttribute("role", "status"); toaster.setAttribute("aria-live", "polite");
    document.body.appendChild(toaster);
  }
  const t = document.createElement("div");
  t.className = "toast" + (error ? " error" : "");
  t.textContent = message;
  toaster.appendChild(t);
  setTimeout(() => t.remove(), ms);
}

/** Fade elements in as they scroll into view (respects reduced motion via CSS). */
export function reveal(root = document) {
  const els = $$(".reveal", root);
  if (!("IntersectionObserver" in window)) { els.forEach((e) => e.classList.add("in")); return; }
  const io = new IntersectionObserver((entries) => entries.forEach((en) => {
    if (en.isIntersecting) { en.target.classList.add("in"); io.unobserve(en.target); }
  }), { rootMargin: "0px 0px -8% 0px", threshold: 0.08 });
  els.forEach((e) => io.observe(e));
}

/** Mobile menu toggle for the shared header. */
export function headerMenu() {
  const btn = $(".menu-btn"), nav = $(".site-header .nav");
  if (!btn || !nav) return;
  btn.addEventListener("click", () => {
    const open = nav.classList.toggle("open");
    btn.setAttribute("aria-expanded", String(open));
  });
  nav.addEventListener("click", (e) => { if (e.target.closest("a")) { nav.classList.remove("open"); btn.setAttribute("aria-expanded", "false"); } });
}

/** Live status pill: pings /health (also wakes the free-tier server early). */
export async function liveStatus(el, health) {
  if (!el) return;
  const t0 = performance.now();
  try {
    await health();
    const ms = Math.round(performance.now() - t0);
    el.innerHTML = `<span class="dot live"></span>Agent online · ${ms < 1500 ? ms + " ms" : "warming up"}`;
  } catch (e) {
    el.innerHTML = `<span class="dot down"></span>Agent unreachable`;
  }
}

export function formTimer(form) {
  const started = Date.now();
  return () => Date.now() - started;
}

/** Disable a button while an async action runs, with an accessible busy state. */
export async function busy(btn, label, fn) {
  const old = btn.innerHTML;
  btn.disabled = true; btn.setAttribute("aria-busy", "true"); btn.textContent = label;
  try { return await fn(); } finally { btn.disabled = false; btn.removeAttribute("aria-busy"); btn.innerHTML = old; }
}

/** Light / dark toggle. Remembers the choice per visitor; defaults to the system setting. */
export function themeToggle() {
  const root = document.documentElement;
  const current = () => root.dataset.theme || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
  const label = (b) => { const next = current() === "dark" ? "light" : "dark"; b.setAttribute("aria-label", `Switch to ${next} theme`); b.title = `Switch to ${next} theme`; };
  $$("[data-theme-toggle]").forEach((b) => {
    label(b);
    b.addEventListener("click", () => {
      root.dataset.theme = current() === "dark" ? "light" : "dark";
      try { localStorage.setItem("lp_theme", root.dataset.theme); } catch (e) {}
      $$("[data-theme-toggle]").forEach(label);
    });
  });
}
