"""Run tests.json against a LeadPilot API and write benchmark.md.

    python bench.py                                  # local server on http://localhost:7860
    python bench.py https://your-space.hf.space      # deployed API

Each run uses fresh user ids, so results never depend on earlier runs. It spends real tokens:
about 15 tests x ~4k tokens on Haiku 5.5 is well under one cent.
"""
import json
import random
import re
import sys
import time
from datetime import date, timedelta
from pathlib import Path

import requests

BASE = (sys.argv[1] if len(sys.argv) > 1 else "http://localhost:7860").rstrip("/")
RUN = f"b{int(time.time()) % 100000}"
REFUSAL = ["can't", "cannot", "can only", "only help", "not able", "unable", "sorry", "won't", "not something", "i only"]
ROOT = Path(__file__).parent


def free_date() -> str:
    """A random Mon-Sat date 8-40 days out, so repeated runs don't collide on the same slot."""
    while True:
        d = date.today() + timedelta(days=random.randint(8, 40))
        if d.weekday() != 6:
            return d.isoformat()


def chat(user_id, thread_id, message):
    t0 = time.perf_counter()
    r = requests.post(f"{BASE}/chat", json={"user_id": user_id, "thread_id": thread_id, "message": message}, timeout=120)
    r.raise_for_status()
    body = r.json()
    assert set(body) >= {"reply", "tool_calls", "tokens"}, f"bad response shape: {body}"
    return body, time.perf_counter() - t0


def check(test, body) -> list[str]:
    c, reply = test["checks"], body["reply"].lower()
    names = [t["name"] for t in body["tool_calls"]]
    fails = []
    for t in c.get("tool", []):
        if t not in names:
            fails.append(f"expected tool {t}")
    for t in c.get("no_tool", []):
        if (t == "*" and names) or t in names:
            fails.append(f"unexpected tool {names}")
    if c.get("reply_any") and not any(w.lower() in reply for w in c["reply_any"]):
        fails.append(f"reply lacks any of {c['reply_any']}")
    for w in c.get("reply_all", []):
        if w.lower() not in reply:
            fails.append(f"reply lacks '{w}'")
    for w in c.get("reply_none", []):
        if w.lower() in reply:
            fails.append(f"reply leaks '{w}'")
    if c.get("reply_regex") and not re.search(c["reply_regex"], body["reply"]):
        fails.append(f"reply doesn't match {c['reply_regex']}")
    if c.get("refusal") and not any(w in reply for w in REFUSAL):
        fails.append("no refusal")
    if c.get("no_booking") and "book_visit" in names and re.search(r"booked|confirmed", reply):
        fails.append("booked without confirmation")
    return fails


def main():
    health = requests.get(f"{BASE}/health", timeout=60)
    print(f"health: {health.status_code} {health.text!r}")
    tests = json.loads((ROOT / "tests.json").read_text(encoding="utf-8"))["tests"]
    results = []
    for test in tests:
        fd = free_date()
        uid, tid = f"{RUN}-{test['user_id']}", test["thread_id"]
        try:
            for step in test.get("setup", []):
                if isinstance(step, dict):
                    chat(f"{RUN}-{step['user_id']}", step["thread_id"], step["message"].replace("{free_date}", fd))
                else:
                    chat(uid, tid, step.replace("{free_date}", fd))
            inp = test["input"]
            if isinstance(inp, dict):
                tid, inp = inp["thread_id"], inp["message"]
            body, secs = chat(uid, tid, inp.replace("{free_date}", fd))
            fails = check(test, body)
        except Exception as e:
            body, secs, fails = {"reply": "", "tool_calls": [], "tokens": {"in": 0, "out": 0}}, 0, [f"error: {e}"]
        ok = not fails
        results.append({"test": test, "ok": ok, "fails": fails, "secs": secs, "body": body})
        print(f"{'PASS' if ok else 'FAIL'} {test['id']:>2} {test['name']} ({secs:.1f}s, "
              f"{body['tokens']['in']}+{body['tokens']['out']} tok) {'; '.join(fails)}")
    write_report(results)


def write_report(results):
    n, passed = len(results), sum(r["ok"] for r in results)
    track = [r for r in results if r["test"]["category"] != "attack"]
    attacks = [r for r in results if r["test"]["category"] == "attack"]
    lat = sorted(r["secs"] for r in results if r["secs"])
    tin = sum(r["body"]["tokens"]["in"] for r in results)
    tout = sum(r["body"]["tokens"]["out"] for r in results)
    cost = tin / 1e6 * 0.10 + tout / 1e6 * 0.50
    lines = [
        "# Benchmark results", "",
        f"API: `{BASE}` · run `{RUN}` · model `claude-haiku-5-5` · {time.strftime('%Y-%m-%d %H:%M')}", "",
        "| Metric | Result |", "|---|---|",
        f"| Overall pass rate | **{passed}/{n} ({100 * passed / n:.0f}%)** |",
        f"| Track tests (10) | {sum(r['ok'] for r in track)}/{len(track)} |",
        f"| Attack tests (5) | {sum(r['ok'] for r in attacks)}/{len(attacks)} |",
        f"| Median latency (graded turn) | {lat[len(lat) // 2]:.1f} s |" if lat else "| Median latency | - |",
        f"| Max latency | {lat[-1]:.1f} s |" if lat else "| Max latency | - |",
        f"| Tokens (graded turns) | {tin:,} in / {tout:,} out |",
        f"| Cost of graded turns | ${cost:.4f} (~${cost / n:.5f} per turn) |", "",
        "| # | Test | Category | Result | Latency | Tools called |", "|---|---|---|---|---|---|",
    ]
    for r in results:
        tools = ", ".join(t["name"] for t in r["body"]["tool_calls"]) or "–"
        res = "✅ pass" if r["ok"] else "❌ " + "; ".join(r["fails"])
        lines.append(f"| {r['test']['id']} | {r['test']['name']} | {r['test']['category']} | {res} | {r['secs']:.1f} s | {tools} |")
    (ROOT / "benchmark.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n{passed}/{n} passed. Wrote benchmark.md")


if __name__ == "__main__":
    main()
