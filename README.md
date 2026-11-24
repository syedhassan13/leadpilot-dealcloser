# LeadPilot

**The AI lead closer for property and car dealers.** LeadPilot replies to every buyer in seconds, qualifies budget
and timeline, shows real stock, books the site visit or test drive (only after the buyer says yes), and follows up
for days with messages a dealer approves.

| | |
|---|---|
| Product site | https://leadpilot-hassans-team1.vercel.app |
| Live showroom (what buyers see) | https://leadpilot-hassans-team1.vercel.app/showroom |
| Dealer console | https://leadpilot-hassans-team1.vercel.app/console |
| Agent API | https://leadpilot-api-hhys.onrender.com ([/health](https://leadpilot-api-hhys.onrender.com/health)) |
| Demo video | https://www.youtube.com/watch?v=6IJ6BwGFaU8 |

![LeadPilot walkthrough: landing page, showroom booking, dealer console](docs/demo.gif)

Built with LangGraph and Claude Haiku 5.5 for the DealCloser track (AI-4016 Agentic AI, FAST Islamabad, group G19).

---

## The product

**For dealers.** Most inquiries wait hours for a reply, and the buyer moves on. LeadPilot answers in seconds, 24/7,
and turns conversations into booked visits. Dealers work from a console that shows:
- every lead, scored hot, warm or cold
- the full conversation behind each lead
- upcoming visits
- follow-ups waiting for approval

**For buyers.** A chat on the dealer's website that actually knows the stock, checks real availability, books a
slot and remembers them next time. No forms to wait on.

| Surface | What it does |
|---|---|
| **Landing page** (`/`) | Explains the product to dealers: a replay of a real conversation with live gauges, "Try it live" with the real agent, the response-time evidence, architecture, security, pricing, FAQ, and a pilot request form that saves to the database |
| **Showroom** (`/showroom`) | A demo dealer website with the widget installed: dealer switcher, search and filters, listing details with **real** open slots, booking through the agent, and a call-back form with an instant reply |
| **Chat widget** (`<leadpilot-chat>`) | A self-contained web component any dealer site can embed with one script tag. It can be locked to one dealer's stock. It shows what the agent did ("Checked real availability"), handles errors with retry, and offers "my bookings" and "delete my data" |
| **Dealer console** (`/console`) | Overview (KPIs, 14-day chart, funnel, hot leads needing attention, upcoming visits), drag-and-drop pipeline, searchable leads, lead detail (transcripts, memory, bookings, follow-ups, tool audit), bookings agenda, follow-up approval queue, install snippet, and a pilot-request inbox for the LeadPilot team |

| Showroom · real availability | Widget · asks before booking, then books |
|---|---|
| ![Listing dialog with live slots](docs/showroom-slots.png) | ![Chat widget booking a test drive](docs/widget.png) |

| Console · overview | Console · lead detail |
|---|---|
| ![Console overview](docs/console-overview.png) | ![Lead detail with transcript and memory](docs/console-lead.png) |

| Landing page | Pipeline | Follow-up approvals | Mobile |
|---|---|---|---|
| ![Landing](docs/landing.png) | ![Pipeline](docs/console-pipeline.png) | ![Follow-ups](docs/console-followups.png) | ![Mobile](docs/mobile-landing.png) |

## Architecture

```mermaid
flowchart LR
  subgraph Web["Website · Vercel (static, strict CSP)"]
    L["Landing /"]
    SR["Showroom /showroom"]
    CO["Console /console"]
    WG["&lt;leadpilot-chat&gt;<br/>web component"]
    SR --- WG
    L --- WG
  end
  subgraph API["Agent API · FastAPI on Render (Docker)"]
    direction TB
    R1["public router<br/>POST /chat · GET /health<br/>/listings · /leads · /pilot-requests"]
    R2["customer router<br/>signed X-User-Token"]
    R3["admin router<br/>dealer session"]
    subgraph G["LangGraph StateGraph"]
      direction LR
      S((START)) --> GU["Guard<br/>rules, 0 tokens"]
      GU -- safe --> CL["Closer<br/>Claude Haiku 5.5<br/>+ 8 tools"]
      GU -- blocked --> ME
      CL --> ME["Memory"]
      ME --> E((END))
    end
    FU["Follow-up agent<br/>max 20 drafts / run"]
    R1 --> G
    R3 --> FU
  end
  subgraph DB["Supabase Postgres (RLS on every table)"]
    CK[("checkpoints<br/>short-term, per thread")]
    ST[("store<br/>long-term, per user_id")]
    BT[("listings · leads · bookings<br/>follow-ups · audit · rate limits")]
  end
  WG --> R1
  WG --> R2
  CO --> R3
  G <--> CK
  G <--> ST
  CL <--> BT
  GH["GitHub Actions<br/>CI · daily follow-ups · keep-alive"] --> API
```

| Agent / node | Job |
|---|---|
| **Guard** | Rule-based check before any model call: prompt injection, fake "SYSTEM NOTE"s, tool-name injection, data snooping, off-topic requests. Blocked messages cost **0 tokens**. |
| **Closer** | `create_agent` with Claude Haiku 5.5 (low effort, 700-token replies) and 8 tools. Qualifies, shortlists, checks slots, books after confirmation and answers policy questions. When the widget is on one dealer's site, it only sees that dealer's stock. |
| **Memory** | Records activity, assigns the lead to the dealer they chatted with, and tracks the buyer's threads in long-term memory. |
| **Follow-up** | Drafts personal nudges for quiet leads (day 1, 3, 7), skipping anyone whose only messages were blocked. A dealer approves each one before it reaches the buyer's chat. |

| Tool | Returns |
|---|---|
| `search_inventory` | Up to 5 matching listings (category, price, area, keyword, bedrooms), dealer-scoped |
| `get_listing` | Full listing details |
| `check_availability` | Free slots for a date (Mon–Sat, 10/12/14/16, up to 60 days ahead) |
| `book_visit` | A booking, **only** if the buyer's latest message confirms; the slot is re-checked server-side |
| `my_bookings` · `cancel_booking` | The buyer's own bookings; cancelling needs confirmation |
| `save_lead_details` | Saves name, budget, timeline, preferences and financing; scores the lead |
| `search_faq` | Retrieval over financing, documents, visit and cancellation policies |

### Memory design

| | Short-term | Long-term |
|---|---|---|
| **What** | The messages of one conversation | Name, phone, interest, budget, timeline, preferences, financing, last booking, list of threads |
| **Key** | `"<len(user_id)>:<user_id>:<thread_id>"` (unambiguous) | namespace `("leads", user_id)` |
| **Where** | LangGraph `PostgresSaver` in Supabase | LangGraph `PostgresStore` in Supabase |
| **Use** | Last 16 messages go to the model | Injected into every prompt, including whether the buyer is returning |
| **Never stored** | – | Card numbers (redacted), CNIC (masked), passwords, OTPs |

**Isolation:**
- **Tools** take the buyer from server-side context, never from the model.
- **Website buyers** hold a server-signed token, required for their chats, bookings, profile and data deletion.
- **Dealers** sign in for an 8-hour, revocable session scoped to their own business.
- **The database** is only reachable through the API: RLS is on and the `anon` role is revoked.

## Engineering

**Stack decisions.**
- **Backend:** FastAPI, LangGraph and SQLAlchemy Core on Postgres. The same code runs on SQLite locally.
- **Frontend: no framework, by design.** It is three pages and one embeddable widget written as plain ES modules: no build step, no `node_modules`, a small supply-chain surface, and fast first paint. The widget is a standard Web Component with Shadow DOM, so it embeds on any dealer site without style clashes.
- **Next.js was considered and rejected.** It would add a build pipeline and a large dependency tree without solving a problem this product has.

**Structure.**
```
backend/app/
  main.py            app factory, security headers, request logging, startup
  api/public.py      graded /chat + /health, catalogue, slots, leads, pilot requests
  api/customer.py    buyer endpoints (signed token)
  api/admin.py       console endpoints (dealer session), stats, pipeline, follow-ups
  services.py        conversation turns, transcripts, data erasure
  graph.py agent.py tools.py guard.py followups.py rag.py security.py db.py seed.py config.py runtime.py
backend/tests/       25 unit tests (no network, no AI calls)
frontend/
  index.html  showroom/  console/  privacy.html  terms.html  404.html  vercel.json
  assets/css/        base.css (design system) · landing · showroom · console
  assets/js/         api.js · ui.js · config.js · landing.js · showroom.js · console.js
  assets/widget/     leadpilot-chat.js + .css (embeddable web component)
tests.json bench.py security_tests.py
```

**Quality gates.**
| Check | Scope | Result (live) |
|---|---|---|
| `pytest` (CI on every push) | guard rules, scoring, slots, tokens, sessions, isolation, API contract | 25/25 |
| `bench.py` + `tests.json` | 10 track tests + 5 attack tests against the deployed API | **15/15** |
| `security_tests.py` | 31 checks: auth, isolation, rate limits, headers, exposed files | **31/31** |
| Browser end-to-end | 42 checks across landing, widget, showroom booking, console, mobile layouts, with the CSP enforced | **42/42**, no JS errors or CSP violations |
| `pip-audit` (CI) | all pinned dependencies (`requirements.lock`) | no known vulnerabilities |

**Security.** In addition to the guard and isolation above:
- **Abuse limits:** per-user, per-IP and daily limits stored in Postgres (they survive restarts), plus honeypot and fill-time bot checks on forms.
- **Strict Content-Security-Policy** on the site (scripts from our own origin only), with frame denial, HSTS, `nosniff` and a permissions policy.
- **Closed surfaces:** `/docs` is off in production, CORS is limited to our origin, and failed logins and admin actions are logged.
- **Pre-launch audit:** an 85-point audit was run and its findings fixed (see [SECURITY.md](SECURITY.md)).

**Reliability.**
- `/chat` never returns a 500; it always answers in the graded shape.
- Tool errors become messages, and model and tool call limits stop runaway loops.
- Every response carries a request id and produces one log line.
- The console ignores stale responses and cleans up its listeners on navigation.
- The free Render service is kept warm by a GitHub Action.

**Design.**
- **The idea:** a *flight deck for sales*, playing on Lead**Pilot**.
- **Palette:** warm paper and ink with one signal colour, International Orange.
- **Type:** Instrument Serif headlines, Instrument Sans for the interface, JetBrains Mono for live data.
- **Details:** hairline rules and square corners.
- **Themes:** light and dark, with a saved toggle.
- **Contrast:** checked against WCAG. Ink text sits on orange buttons, and a darker orange is used for orange text.
- **Accessibility:** visible focus rings, reduced-motion support, ARIA states and keyboard access (`/` to search the console).
- **Every view** has loading, empty and error-with-retry states.

## Agent API (graded)

```bash
curl -X POST "$API/chat" -H "Content-Type: application/json" \
  -d '{"user_id":"ali","thread_id":"t1","message":"I want a 3 bed house, budget 2 crore"}'
# {"reply":"...","tool_calls":[{"name":"save_lead_details","args":{...}},{"name":"search_inventory","args":{...}}],"tokens":{"in":5867,"out":464}}
curl "$API/health"   # ok
```
An optional `"dealer": "skyline" | "autohub"` field scopes the conversation to one dealer, and the response shape is unchanged.
Ids issued by the website (`lp_…`) also need their `X-User-Token`. Any other id is open, as the assignment requires
(a documented, accepted risk).

## Embed the widget on a dealer website

```html
<script src="https://leadpilot-hassans-team1.vercel.app/assets/widget/leadpilot-chat.js" defer></script>
<leadpilot-chat dealer="autohub"></leadpilot-chat>
```
The console's **Install & settings** page shows the exact snippet for each dealer.

For a dealer on their own domain, add it to the API's `ALLOWED_ORIGINS`.

## Benchmark (live deployment)

| Metric | Result |
|---|---|
| Overall | **15/15 (100%)** · 10/10 track · 5/5 attacks (blocked by the guard, 0 tokens) |
| Median / max latency | 6.3 s / 10.4 s (from Pakistan to the Singapore API, including tool calls) |
| Tokens for 15 graded turns | 46,231 in / 2,115 out · **≈ $0.0004 per message** |

Per-test details are in [benchmark.md](benchmark.md). To re-run: `python bench.py https://leadpilot-api-hhys.onrender.com`.

## Run locally

```bash
python -m venv .venv && .venv\Scripts\activate          # macOS/Linux: source .venv/bin/activate
pip install -r backend/requirements.txt -c backend/requirements.lock
copy .env.example .env                                   # set ANTHROPIC_API_KEY; leave DATABASE_URL empty for SQLite
cd backend && uvicorn app.main:app --port 7860
```
- **Website:** http://localhost:7860, with the showroom at `/showroom` and the console at `/console`.
- **API docs:** `/docs`, in development only.
- **Demo dealer keys:** `skyline-demo`, `autohub-demo` and `admin-demo` work only when `ADMIN_TOKENS` is not set.

Tests: `cd backend && pytest` · `python bench.py` · `python security_tests.py <api> <dealer key>`.

## Deploy

1. **Supabase.** Create a project and copy the *Session pooler* URI. Tables and row-level security are created automatically on first start.
2. **Render.** Use **New**, then **Blueprint**, and pick this repo (`render.yaml`). Set:
   - `ANTHROPIC_API_KEY`
   - `DATABASE_URL`
   - `ADMIN_TOKENS` (long random keys)
   - `ALLOWED_ORIGINS` (your site's URL)

   `SESSION_SECRET` and `CRON_SECRET` are generated for you. The server refuses to start in production without `SESSION_SECRET`.
3. **Vercel.** Import the repo with Root Directory `frontend`. `vercel.json` adds the security headers and redirects. If the API URL changes, update it in `frontend/assets/js/config.js` and `vercel.json`.
4. **GitHub secrets.** Add `API_URL` and `CRON_SECRET`. They power daily follow-up drafts and a keep-alive ping every 10 minutes. CI runs on every push.

## Known limitations

- **Demo data.** The showroom listings, prices and both businesses are fictional. Pricing plans are shown, but there is no billing integration yet.
- **Dealer onboarding.** Dealer accounts are keys issued by the LeadPilot team (`ADMIN_TOKENS`). There is no self-serve signup. The landing page collects **pilot requests** instead, and they appear in the console.
- **Follow-up delivery.** Approved follow-ups arrive in the buyer's chat thread. WhatsApp, SMS and email delivery are not built.
- **Free-tier hosting.** Render sleeps after 15 idle minutes. A keep-alive ping runs every 10 minutes, but a cold start can still take about 30–60 s.
- **The widget on other sites.** It uses the host page's fonts if they load, otherwise system fonts. It needs the API's `ALLOWED_ORIGINS` updated for each new domain.
- **Language.** It is tested in English. It understands everyday Roman Urdu confirmations but has not been tested for full Urdu conversations.
- **Open `/chat`.** The open `/chat` endpoint is required by the assignment's grader. See the API section above.

## Workshop concepts used

| Lab | Where |
|---|---|
| 00–01 Agents & tools | `create_agent`, `@tool`, `ToolRuntime`, `context_schema` (`agent.py`, `tools.py`) |
| 02 Middleware | `@dynamic_prompt` (memory-aware prompt), `@wrap_model_call` (history trim), `@wrap_tool_call` (audit + error shield), `PIIMiddleware`, call limits |
| 03 Safety | Guard node; confirmation gate on bookings; human approval of follow-ups |
| 04 RAG | Policy knowledge base + `search_faq` (`rag.py`) |
| 05 Multi-agent | `StateGraph`: guard → closer → memory, plus the follow-up agent |
| 06 Long-term memory | `PostgresStore`, `runtime.store`, namespaces per buyer |

To report a security issue, see [SECURITY.md](SECURITY.md). No secrets are committed: `.env` is git-ignored and every key lives in platform secrets.
