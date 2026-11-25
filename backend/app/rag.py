"""Lab 04 (RAG): the company knowledge base (financing, visit rules, documents) in a vector store.

Embeddings are a small local "hashing trick" embedder: no extra API key, no model download,
and good enough for a handful of policy documents.
"""
import hashlib
import math
import re
from pathlib import Path

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings
from langchain_core.vectorstores import InMemoryVectorStore
from langchain_text_splitters import MarkdownHeaderTextSplitter

KNOWLEDGE = Path(__file__).resolve().parents[1] / "knowledge"
DIMS = 1024
STOP = {"the", "a", "an", "and", "or", "of", "to", "in", "is", "are", "for", "on", "do", "you", "i",
        "we", "my", "your", "what", "how", "can", "with", "it", "be", "me", "at", "by", "our"}


class LocalEmbeddings(Embeddings):
    """Words + character trigrams hashed into a fixed-size, normalised vector."""

    def _vec(self, text: str) -> list[float]:
        v = [0.0] * DIMS
        words = [w for w in re.findall(r"[a-z0-9]+", text.lower()) if w not in STOP]
        feats = [(w, 1.0) for w in words] + [(f"#{w[i:i + 3]}", 0.4) for w in words for i in range(max(1, len(w) - 2))]
        for f, weight in feats:
            h = int.from_bytes(hashlib.md5(f.encode()).digest()[:4], "little")
            v[h % DIMS] += weight
        n = math.sqrt(sum(x * x for x in v)) or 1.0
        return [x / n for x in v]

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> list[float]:
        return self._vec(text)


def _load_chunks() -> list[Document]:
    splitter = MarkdownHeaderTextSplitter([("#", "doc"), ("##", "section")], strip_headers=False)
    chunks = []
    for path in sorted(KNOWLEDGE.glob("*.md")):
        for chunk in splitter.split_text(path.read_text(encoding="utf-8")):
            chunk.metadata["source"] = path.name
            chunks.append(chunk)
    return chunks


_store: InMemoryVectorStore | None = None


def knowledge_store() -> InMemoryVectorStore:
    global _store
    if _store is None:
        _store = InMemoryVectorStore.from_documents(_load_chunks(), LocalEmbeddings())
    return _store


def search_knowledge(query: str, k: int = 2) -> list[Document]:
    return knowledge_store().similarity_search(query, k=k)


# Each dealer can paste its own policies/FAQ in the owner console. Indexed on first use, re-indexed when edited.
_dealer_stores: dict[str, tuple[str, InMemoryVectorStore]] = {}


def search_dealer_policies(dealer_id: str, text: str, query: str, k: int = 2) -> list[Document]:
    digest = hashlib.sha256(text.encode()).hexdigest()
    cached = _dealer_stores.get(dealer_id)
    if not cached or cached[0] != digest:
        parts = [p.strip() for p in re.split(r"\n\s*\n|\n(?=#)", text) if p.strip()]
        docs = [Document(p, metadata={"source": f"{dealer_id}-policies"}) for p in parts[:200]]
        cached = (digest, InMemoryVectorStore.from_documents(docs, LocalEmbeddings()))
        _dealer_stores[dealer_id] = cached
    return cached[1].similarity_search(query, k=k)
