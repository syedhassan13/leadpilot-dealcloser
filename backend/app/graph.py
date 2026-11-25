r"""The LangGraph StateGraph (Lab 05) plus persistence (Lab 00 checkpointer, Lab 06 store).

    START -> guard --(safe)--> closer (create_agent: model <-> tools) -> memory -> END
               \--(blocked)--------------------------------------------^

Short-term memory: checkpointer keyed by thread "<len(user_id)>:<user_id>:<thread_id>" (Postgres or SQLite).
Long-term memory:  store namespace ("leads", user_id) (Postgres or SQLite).
"""
import sqlite3

from langchain.messages import AIMessage, HumanMessage
from langgraph.graph import END, START, MessagesState, StateGraph
from langgraph.runtime import Runtime

from app import config, guard
from app.agent import build_closer
from app.db import get_lead, now, upsert_lead
from app import tenants
from app.tools import LeadContext


class LeadState(MessagesState):
    blocked: bool


def thread_key(user_id: str, thread_id: str) -> str:
    """Unambiguous per-user thread key: user "a:b"+thread "c" can never collide with user "a"+thread "b:c"."""
    return f"{len(user_id)}:{user_id}:{thread_id}"


def guard_node(state: LeadState) -> dict:
    """Guard agent: deterministic safety check on the newest customer message."""
    last = next((m for m in reversed(state["messages"]) if isinstance(m, HumanMessage)), None)
    refusal = guard.check(last.text if last else "")
    if refusal:
        return {"messages": [AIMessage(refusal, name="guard")], "blocked": True}
    return {"blocked": False}


def route_after_guard(state: LeadState) -> str:
    return "memory" if state.get("blocked") else "closer"


def memory_node(state: LeadState, runtime: Runtime[LeadContext]) -> dict:
    """Memory agent: records activity on the lead and in long-term memory (what threads this user had)."""
    ctx = runtime.context
    lead = get_lead(ctx.user_id)
    # A lead belongs to the dealer whose website it first chatted on (dealer-scoped widget).
    first_dealer = ctx.dealer if ctx.dealer and not (lead or {}).get("business_id") else None
    upsert_lead(ctx.user_id, last_thread_id=ctx.thread_id, last_customer_at=now(), last_agent_at=now(),
                business_id=first_dealer, category=tenants.category_of(first_dealer) if first_dealer else None)
    if runtime.store is not None:
        ns = ("leads", ctx.user_id)
        old = runtime.store.get(ns, "activity")
        threads = (old.value.get("threads", []) if old else [])
        if ctx.thread_id not in threads:
            threads = (threads + [ctx.thread_id])[-20:]
        runtime.store.put(ns, "activity", {"threads": threads, "last_seen": now().isoformat()})
    return {}


def build_graph(checkpointer, store):
    g = StateGraph(LeadState, context_schema=LeadContext)
    g.add_node("guard", guard_node)
    g.add_node("closer", build_closer())
    g.add_node("memory", memory_node)
    g.add_edge(START, "guard")
    g.add_conditional_edges("guard", route_after_guard, ["closer", "memory"])
    g.add_edge("closer", "memory")
    g.add_edge("memory", END)
    return g.compile(checkpointer=checkpointer, store=store)


def build_persistence():
    """Postgres (Supabase/Neon) when DATABASE_URL is set, so memory survives restarts; SQLite locally."""
    if config.DATABASE_URL:
        from langgraph.checkpoint.postgres import PostgresSaver
        from langgraph.store.postgres import PostgresStore
        from psycopg.rows import dict_row
        from psycopg_pool import ConnectionPool

        pool = ConnectionPool(conninfo=config.DATABASE_URL, max_size=6, open=True,
                              kwargs={"autocommit": True, "prepare_threshold": None, "row_factory": dict_row})
        saver, store = PostgresSaver(pool), PostgresStore(pool)
    else:
        from langgraph.checkpoint.sqlite import SqliteSaver
        from langgraph.store.sqlite import SqliteStore

        path = config.SQLITE_PATH.replace(".db", "_memory.db")
        saver = SqliteSaver(sqlite3.connect(path, check_same_thread=False))
        store = SqliteStore(sqlite3.connect(path, check_same_thread=False, isolation_level=None))
    saver.setup()
    store.setup()
    return saver, store
