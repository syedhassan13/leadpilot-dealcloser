"""Process-wide runtime objects, created once at startup (see main.lifespan)."""
from dataclasses import dataclass
from typing import Any


@dataclass
class Runtime:
    graph: Any = None      # compiled LangGraph (guard -> closer -> memory)
    saver: Any = None      # checkpointer: short-term memory per thread
    store: Any = None      # store: long-term memory per user_id
    model: Any = None      # chat model for the follow-up agent

    @property
    def ready(self) -> bool:
        return self.graph is not None


rt = Runtime()
