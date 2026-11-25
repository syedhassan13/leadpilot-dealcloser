/* LeadPilot chat widget: <leadpilot-chat key="wk_…"></leadpilot-chat>
   Embeddable on any website with one script tag. Shadow DOM keeps its styles isolated from the host page.
   Attributes: key (the dealer's public widget key: name, branding and billing come from LeadPilot),
   api (optional API base), open (start open), dealer (legacy: a showcase dealer id, open mode only).
   Methods: open(), close(), ask(text), prefill(text), adoptLead({user_id, token, thread_id, name, first_message, reply, response_ms}) */
(() => {
  if (customElements.get("leadpilot-chat")) return;

  const SCRIPT_URL = document.currentScript ? document.currentScript.src : location.href;
  const ASSET_BASE = new URL(".", SCRIPT_URL).href;
  const DEFAULT_API = ["localhost", "127.0.0.1"].includes(new URL(SCRIPT_URL).hostname)
    ? new URL(SCRIPT_URL).origin : "https://leadpilot-api-hhys.onrender.com";
  // Nothing dealer-specific lives here: the widget asks the API who its key belongs to.
  const QUICK = {
    property: ["3 bed house under 2 crore", "Apartments with installments?", "What documents do I need?"],
    car: ["Automatic cars under 50 lakh", "Can I test drive on Saturday?", "Do you offer car financing?"],
    all: ["I want a 3 bed house, budget 2 crore", "Cars under 50 lakh?", "Do you offer financing?"],
  };
  const GENERIC = { name: "LeadPilot", what: "property or car", category: "all", brand_color: null };
  const configs = {};   // widget key -> promise of /widget/config
  const TOOL_LABEL = {
    search_inventory: "Searched live inventory", get_listing: "Opened listing details",
    check_availability: "Checked real availability", book_visit: "Booked the visit",
    my_bookings: "Checked your bookings", cancel_booking: "Cancelled the booking",
    save_lead_details: "Saved your preferences", search_faq: "Looked up company policy",
  };
  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k)); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) {} },
    del(k) { try { localStorage.removeItem(k); } catch (e) {} },
  };
  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  const md = (s) => esc(s).replace(/\*\*(.+?)\*\*/g, "<strong>$1</strong>")
    .replace(/^\s*[-•]\s+(.*)$/gm, '<span class="li">$1</span>').replace(/\n{3,}/g, "\n\n")
    .replace(/<\/span>\n+(?=<span class="li">)/g, "</span>").replace(/<\/span>\n/g, "</span>").replace(/\n/g, "<br>");
  const rid = (p) => `${p}-${Math.random().toString(36).slice(2, 10)}`;

  const TEMPLATE = `
    <link rel="stylesheet" href="${ASSET_BASE}leadpilot-chat.css">
    <button class="launcher" part="launcher" aria-expanded="false" aria-controls="panel">
      <span class="launcher-dot" aria-hidden="true"></span><span class="launcher-text">Ask about a listing</span>
      <span class="launcher-sub">Replies in seconds</span>
    </button>
    <section class="panel" id="panel" role="dialog" aria-modal="false" aria-label="Chat with the sales assistant" hidden>
      <header class="head">
        <div class="who"><span class="avatar" aria-hidden="true"><svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="10.5" fill="none" stroke="currentColor" stroke-opacity=".4" stroke-width="1.4"/><path class="arrow" d="M10.2 21.8 22.4 9.6M16.4 9.6h6v6" stroke="#ff4f00" stroke-width="2.6" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
          <div><strong class="dealer-name"></strong><span class="status"><i class="live"></i><span class="status-text">AI assistant · online</span></span></div></div>
        <div class="head-actions">
          <button class="icon menu-toggle" aria-label="Conversation options" aria-haspopup="menu" aria-expanded="false"><svg viewBox="0 0 20 20"><circle cx="4" cy="10" r="1.6"/><circle cx="10" cy="10" r="1.6"/><circle cx="16" cy="10" r="1.6"/></svg></button>
          <button class="icon close" aria-label="Close chat"><svg viewBox="0 0 20 20"><path d="M5 5l10 10M15 5 5 15" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg></button>
        </div>
        <div class="menu" role="menu" hidden>
          <button role="menuitem" data-act="new">Start a new conversation</button>
          <button role="menuitem" data-act="bookings">Show my bookings</button>
          <button role="menuitem" data-act="delete" class="danger">Delete my data</button>
          <a role="menuitem" class="privacy" target="_blank" rel="noopener">Privacy policy ↗</a>
        </div>
      </header>
      <div class="log" role="log" aria-live="polite" aria-relevant="additions"></div>
      <div class="quick" aria-label="Suggested questions"></div>
      <form class="composer">
        <label class="sr-only" for="msg">Message</label>
        <textarea id="msg" rows="1" maxlength="2000" placeholder="Ask about price, availability or a visit…" autocomplete="off"></textarea>
        <button class="send" type="submit" aria-label="Send message"><svg viewBox="0 0 20 20"><path d="M3 10h12M11 5l5 5-5 5" stroke="currentColor" stroke-width="2" fill="none" stroke-linecap="round" stroke-linejoin="round"/></svg></button>
      </form>
      <p class="foot">AI replies can be wrong. A human confirms prices at the visit. <a class="privacy" target="_blank" rel="noopener">Privacy</a></p>
    </section>`;

  class LeadPilotChat extends HTMLElement {
    static get observedAttributes() { return ["key", "dealer"]; }

    constructor() {
      super();
      this.root = this.attachShadow({ mode: "open" });
      this.root.innerHTML = TEMPLATE;
      this.busy = false;
      this.loaded = false;
      const q = (s) => this.root.querySelector(s);
      this.el = { launcher: q(".launcher"), panel: q(".panel"), log: q(".log"), quick: q(".quick"), form: q(".composer"),
        input: q("textarea"), send: q(".send"), menu: q(".menu"), menuToggle: q(".menu-toggle"), name: q(".dealer-name") };
      this.root.querySelectorAll(".privacy").forEach((a) => { a.href = new URL("/privacy.html", this.siteBase()).href; });
    }

    siteBase() { return new URL("../../", ASSET_BASE).href; }
    get api() { return (this.getAttribute("api") || DEFAULT_API).replace(/\/$/, ""); }
    get key() { return (this.getAttribute("key") || "").trim(); }
    get dealer() { return this.key ? "" : (this.getAttribute("dealer") || "").trim(); }
    get info() { return this.cfg || GENERIC; }
    get threadKey() { return `leadpilot:thread:${this.key || this.dealer || "all"}`; }
    get me() { return store.get("leadpilot:customer"); }
    set me(v) { v ? store.set("leadpilot:customer", v) : store.del("leadpilot:customer"); }
    get threadId() {
      let t = store.get(this.threadKey);
      if (!t) { t = rid("t"); store.set(this.threadKey, t); }
      return t;
    }

    connectedCallback() {
      const e = this.el;
      e.launcher.addEventListener("click", () => (e.panel.hidden ? this.open() : this.close()));
      this.root.querySelector(".close").addEventListener("click", () => this.close());
      e.form.addEventListener("submit", (ev) => { ev.preventDefault(); this.sendText(e.input.value); });
      e.input.addEventListener("keydown", (ev) => {
        if (ev.key === "Enter" && !ev.shiftKey) { ev.preventDefault(); this.sendText(e.input.value); }
      });
      e.input.addEventListener("input", () => this.autosize());
      e.quick.addEventListener("click", (ev) => { const b = ev.target.closest("button"); if (b) this.sendText(b.textContent); });
      e.menuToggle.addEventListener("click", () => this.toggleMenu());
      e.menu.addEventListener("click", (ev) => { const b = ev.target.closest("[data-act]"); if (b) { this.toggleMenu(false); this.action(b.dataset.act); } });
      e.log.addEventListener("click", (ev) => { const b = ev.target.closest("[data-retry]"); if (b) { b.closest(".msg").remove(); this.sendText(b.dataset.retry); } });
      this.root.addEventListener("keydown", (ev) => { if (ev.key === "Escape") { if (!e.menu.hidden) this.toggleMenu(false); else this.close(); } });
      this.applyDealer();
      if (this.hasAttribute("open")) this.open();
      const sync = () => { const th = document.documentElement.dataset.theme; th ? this.setAttribute("data-theme", th) : this.removeAttribute("data-theme"); };
      sync(); new MutationObserver(sync).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
    }

    attributeChangedCallback() {
      if (!this.el) return;
      this.applyDealer();
      if (this.loaded) { this.loaded = false; if (!this.el.panel.hidden) this.loadHistory(); }
    }

    /** Load the dealer behind the key (name, what it sells, brand colour), then redraw. */
    async applyDealer() {
      const key = this.key;
      this.cfg = null;
      this.paint();
      if (!key) return;
      if (!configs[key]) {
        configs[key] = fetch(`${this.api}/widget/config`, { headers: { "X-Widget-Key": key } })
          .then((r) => (r.ok ? r.json() : null)).catch(() => null);
      }
      const cfg = await configs[key];
      if (!cfg) delete configs[key];        // try again next time (network blip)
      if (key === this.key) { this.cfg = cfg; this.paint(); }
    }

    paint() {
      const i = this.info;
      const named = Boolean(this.cfg) || Boolean(this.dealer && this.getAttribute("dealer-name"));
      const name = this.cfg ? i.name : (this.getAttribute("dealer-name") || i.name);
      this.el.name.textContent = name;
      this.root.querySelector(".launcher-text").textContent = named ? `Chat with ${name}` : "Ask about a listing";
      const cat = this.cfg ? i.category : (this.getAttribute("category") || "all");
      this.el.quick.innerHTML = (QUICK[cat] || QUICK.all).map((t) => `<button type="button">${esc(t)}</button>`).join("");
      const color = /^#[0-9a-f]{6}$/i.test(i.brand_color || "") ? i.brand_color : "";
      color ? this.style.setProperty("--signal", color) : this.style.removeProperty("--signal");
      this.toggleAttribute("data-paused", Boolean(this.cfg && this.cfg.active === false));
    }

    open() {
      const e = this.el;
      e.panel.hidden = false; e.launcher.setAttribute("aria-expanded", "true"); this.classList.add("is-open");
      requestAnimationFrame(() => e.panel.classList.add("show"));
      if (!this.loaded) this.loadHistory();
      setTimeout(() => e.input.focus(), 60);
      this.dispatchEvent(new CustomEvent("leadpilot:open", { bubbles: true }));
    }

    close() {
      const e = this.el;
      e.panel.classList.remove("show"); e.launcher.setAttribute("aria-expanded", "false"); this.classList.remove("is-open");
      setTimeout(() => { e.panel.hidden = true; }, 180);
      e.launcher.focus();
    }

    prefill(text) { this.open(); this.el.input.value = text; this.autosize(); this.el.input.focus(); }
    ask(text) { this.open(); setTimeout(() => this.sendText(text), 120); }

    /** Continue a conversation that the website's call-back form started on the server. */
    adoptLead(d) {
      this.me = { user_id: d.user_id, token: d.token, name: d.name };
      store.set(this.threadKey, d.thread_id);
      this.el.log.innerHTML = "";
      this.loaded = true;
      this.open();
      this.add("user", d.first_message);
      this.add("assistant", d.reply, { meta: `Replied in ${(d.response_ms / 1000).toFixed(1)} s` });
    }

    toggleMenu(force) {
      const show = force ?? this.el.menu.hidden;
      this.el.menu.hidden = !show; this.el.menuToggle.setAttribute("aria-expanded", String(show));
      if (show) this.el.menu.querySelector("button").focus();
    }

    autosize() { const t = this.el.input; t.style.height = "auto"; t.style.height = Math.min(t.scrollHeight, 140) + "px"; }

    async http(path, { method = "GET", body } = {}) {
      const headers = {};
      if (body !== undefined) headers["Content-Type"] = "application/json";
      const me = this.me;
      if (me && me.token) headers["X-User-Token"] = me.token;
      if (this.key) headers["X-Widget-Key"] = this.key;
      const ctrl = new AbortController(); const timer = setTimeout(() => ctrl.abort(), 60000);
      try {
        const res = await fetch(this.api + path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, signal: ctrl.signal });
        if (!res.ok) { const err = new Error(String(res.status)); err.status = res.status; throw err; }
        return res.json();
      } catch (e) {
        if (!e.status) e.status = 0;
        throw e;
      } finally { clearTimeout(timer); }
    }

    async ensureCustomer() {
      if (this.me && this.me.token) return this.me;
      const s = await this.http("/customer/session", { method: "POST" });
      this.me = { user_id: s.user_id, token: s.token };
      return this.me;
    }

    greet() {
      this.el.quick.hidden = false;
      const name = this.me && this.me.name ? ` ${this.me.name.split(" ")[0]}` : "";
      const who = this.el.name.textContent || this.info.name;
      const what = this.cfg ? this.info.what : ({ property: "property", car: "car" }[this.getAttribute("category")] || this.info.what);
      this.add("assistant", `Hi${name}! I'm the ${who} assistant. Tell me what ${what} you're looking for, your budget and when you plan to buy, and I'll find matching options and book a visit.`, { greeting: true });
    }

    async loadHistory() {
      this.loaded = true;
      this.el.log.innerHTML = "";
      const me = this.me;
      if (!me) { this.greet(); return; }
      this.add("system", "Loading your conversation…", { id: "loading" });
      try {
        const r = await this.http(`/customer/${encodeURIComponent(me.user_id)}/threads/${encodeURIComponent(this.threadId)}`);
        this.el.log.innerHTML = "";
        if (!r.messages.length) this.greet();
        r.messages.forEach((m) => this.add(m.role, m.text, { followup: m.kind === "followup" }));
      } catch (e) {
        this.el.log.innerHTML = "";
        if (e.status === 401) { this.me = null; }
        this.greet();
      }
    }

    add(role, text, opts = {}) {
      const row = document.createElement("div");
      row.className = `msg ${role}${opts.followup ? " followup" : ""}`;
      if (opts.id) row.dataset.id = opts.id;
      let html = "";
      if (opts.followup) html += `<span class="kicker">Follow-up from ${esc(this.el.name.textContent)}</span>`;
      if (opts.tools && opts.tools.length) {
        html += `<ul class="activity">${opts.tools.map((t) => `<li><span aria-hidden="true">✓</span>${esc(TOOL_LABEL[t.name] || t.name)}</li>`).join("")}</ul>`;
      }
      html += `<div class="bubble">${role === "user" ? esc(text) : md(text)}</div>`;
      if (opts.meta) html += `<span class="meta">${esc(opts.meta)}</span>`;
      if (opts.retry) html += `<button class="retry" data-retry="${esc(opts.retry)}">Try again</button>`;
      row.innerHTML = html;
      this.el.log.appendChild(row);
      if (role === "user") this.el.quick.hidden = true;   // suggestions only help before the first message
      this.el.log.scrollTop = this.el.log.scrollHeight;
      return row;
    }

    typing(on) {
      this.root.querySelector(".msg.typing")?.remove();
      if (on) {
        const t = document.createElement("div");
        t.className = "msg assistant typing"; t.setAttribute("aria-label", "The assistant is typing");
        t.innerHTML = `<div class="bubble"><i></i><i></i><i></i></div>`;
        this.el.log.appendChild(t); this.el.log.scrollTop = this.el.log.scrollHeight;
      }
    }

    async sendText(raw) {
      const text = (raw || "").trim();
      if (!text || this.busy) return;
      this.busy = true; this.el.send.disabled = true;
      this.el.input.value = ""; this.autosize();
      this.root.querySelector('.msg.system[data-id="loading"]')?.remove();
      this.add("user", text);
      this.typing(true);
      const t0 = performance.now();
      try {
        const me = await this.ensureCustomer();
        const body = { user_id: me.user_id, thread_id: this.threadId, message: text };
        if (this.dealer) body.dealer = this.dealer;
        const r = await this.http("/chat", { method: "POST", body });
        this.typing(false);
        this.add("assistant", r.reply, { tools: r.tool_calls, meta: `Replied in ${((performance.now() - t0) / 1000).toFixed(1)} s` });
        this.dispatchEvent(new CustomEvent("leadpilot:reply", { bubbles: true, detail: { tools: r.tool_calls.map((t) => t.name) } }));
      } catch (e) {
        this.typing(false);
        if (e.status === 401) { this.me = null; }
        const msg = e.status === 429 ? "You're sending messages quickly. Please wait a moment." :
          e.status === 0 ? "I couldn't reach the server. Check your connection." : "Something went wrong sending that.";
        this.add("error", msg, { retry: text });
      } finally {
        this.busy = false; this.el.send.disabled = false; this.el.input.focus();
      }
    }

    async action(act) {
      if (act === "new") {
        store.set(this.threadKey, rid("t"));
        this.el.log.innerHTML = ""; this.greet();
        this.add("system", "New conversation. I still remember your preferences.");
      } else if (act === "bookings") {
        if (!this.me) { this.add("system", "You don't have any bookings yet."); return; }
        try {
          const list = await this.http(`/customer/${encodeURIComponent(this.me.user_id)}/bookings`);
          this.add("assistant", list.length ? "Your bookings:\n" + list.map((b) =>
            `- **${b.title}** (${b.listing_id}): ${b.kind} on ${b.date} at ${b.time} · ${b.status}`).join("\n")
            : "You don't have any bookings yet. Want me to find you a slot?");
        } catch (e) { this.add("error", "Couldn't load your bookings. Please try again."); }
      } else if (act === "delete") {
        if (!this.me) { this.add("system", "We don't hold any data for you."); return; }
        if (!confirm("Delete all your data? This removes your chats, saved details and bookings and can't be undone.")) return;
        try {
          await this.http(`/customer/${encodeURIComponent(this.me.user_id)}`, { method: "DELETE" });
          this.me = null;
          try { Object.keys(localStorage).filter((k) => k.startsWith("leadpilot:thread:")).forEach((k) => store.del(k)); } catch (err) {}
          this.el.log.innerHTML = ""; this.add("system", "All your data has been deleted.");
        } catch (e) { this.add("error", "Couldn't delete right now. Please try again."); }
      }
    }
  }

  customElements.define("leadpilot-chat", LeadPilotChat);
})();
