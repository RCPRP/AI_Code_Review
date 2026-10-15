"""Context engine (Lesson 5): AST chunking, embeddings, and a ChromaDB retriever.

Embeddings (REVIEW_EMBEDDINGS): "gemini" when GOOGLE_API_KEY is set, otherwise "local", an offline
hashed bag-of-identifiers embedding that needs no key and no model download. "chroma" uses ChromaDB's
built-in MiniLM model (downloads ~80MB on first use).
"""

import ast
import hashlib
import math
import os
import re
from dataclasses import dataclass

from .config import CONFIG, log


@dataclass(frozen=True)
class Chunk:
    type: str        # function | class | module | doc
    name: str
    file_path: str
    content: str
    start_line: int

    @property
    def id(self) -> str:
        return f"{self.file_path}::{self.type}::{self.name}::{self.start_line}"

    def render(self) -> str:
        return f"# {self.type}: {self.name} ({self.file_path}:{self.start_line})\n{self.content}"


def chunk_file(path: str, code: str) -> list[Chunk]:
    """Python files: one chunk per top-level function/class plus one for module-level code (imports, constants).
    Markdown: one chunk per section. Unparseable files fall back to a single chunk."""
    if path.endswith(".md"):
        sections = re.split(r"\n(?=#+ )", code)
        return [Chunk("doc", s.splitlines()[0].lstrip("# ").strip() or path, path, s.strip(), 1) for s in sections if s.strip()]
    if not path.endswith(".py"):
        return [Chunk("module", path, path, code, 1)] if code.strip() else []
    try:
        tree = ast.parse(code)
    except SyntaxError:
        log.warning("could not parse %s, indexing as one chunk", path)
        return [Chunk("module", path, path, code, 1)]

    lines = code.splitlines()
    chunks, covered = [], set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            start = min([d.lineno for d in node.decorator_list] + [node.lineno])
            end = node.end_lineno or node.lineno
            covered.update(range(start, end + 1))
            kind = "class" if isinstance(node, ast.ClassDef) else "function"
            chunks.append(Chunk(kind, node.name, path, "\n".join(lines[start - 1:end]), start))
    module_level = "\n".join(l for i, l in enumerate(lines, 1) if i not in covered).strip()
    if module_level:
        chunks.insert(0, Chunk("module", path, path, module_level, 1))
    return chunks


def chunk_repository(repo: dict[str, str]) -> list[Chunk]:
    return [c for path in sorted(repo) for c in chunk_file(path, repo[path])]


class _GeminiEF:
    """Adapter so ChromaDB can call Gemini embeddings."""

    def __init__(self):
        from langchain_google_genai import GoogleGenerativeAIEmbeddings
        kwargs = {"vertexai": True, "project": CONFIG.gcp_project, "location": CONFIG.gcp_location} if CONFIG.gcp_project else {}
        self._emb = GoogleGenerativeAIEmbeddings(model=CONFIG.embedding_model, **kwargs)

    def __call__(self, input):  # noqa: A002 - ChromaDB's interface
        return self._emb.embed_documents(list(input))

    def embed_query(self, input):  # noqa: A002
        return [self._emb.embed_query(q) for q in input]

    @staticmethod
    def name() -> str:
        return "gemini"

    def is_legacy(self) -> bool:
        return False


class _LocalHashEF:
    """Offline embedding: identifiers are split on snake_case/camelCase and hashed into a fixed-size vector.
    Good at "which code shares names with this diff", which is most of what review retrieval needs."""

    DIM = 2048

    @staticmethod
    def _tokens(text: str) -> list[str]:
        out = []
        for word in re.findall(r"[A-Za-z_][A-Za-z0-9_]*", text):
            parts = [p for p in re.split(r"_|(?<=[a-z])(?=[A-Z])", word) if len(p) > 1]
            out.append(word.lower())
            out += [p.lower() for p in parts if p.lower() != word.lower()]
        return out

    def _embed(self, text: str) -> list[float]:
        vec = [0.0] * self.DIM
        counts: dict[str, int] = {}
        for t in self._tokens(text):
            counts[t] = counts.get(t, 0) + 1
        for t, n in counts.items():
            h = int.from_bytes(hashlib.blake2b(t.encode(), digest_size=8).digest(), "big")
            vec[h % self.DIM] += (1 if (h >> 63) & 1 else -1) * (1 + math.log(n))
        norm = math.sqrt(sum(v * v for v in vec)) or 1.0
        return [v / norm for v in vec]

    def __call__(self, input):  # noqa: A002 - ChromaDB's interface
        return [self._embed(t) for t in input]

    def embed_query(self, input):  # noqa: A002
        return self(input)

    @staticmethod
    def name() -> str:
        return "local-hash"

    def is_legacy(self) -> bool:
        return False


def embedding_backend() -> str:
    choice = os.getenv("REVIEW_EMBEDDINGS", "").lower()
    if choice:
        return choice
    google = os.getenv("GOOGLE_API_KEY") or os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_CLOUD_PROJECT")
    return "gemini" if google else "local"


def embedding_function():
    backend = embedding_backend()
    if backend == "gemini":
        return _GeminiEF()
    if backend == "chroma":
        from chromadb.utils.embedding_functions import DefaultEmbeddingFunction
        return DefaultEmbeddingFunction()
    return _LocalHashEF()


def embed_chunks(chunks: list[Chunk]) -> list[dict]:
    """Returns [{"chunk", "embedding"}], like the lesson. ContextRetriever can also embed for you."""
    ef = embedding_function()
    vectors = ef([c.render() for c in chunks])
    return [{"chunk": c, "embedding": list(map(float, v))} for c, v in zip(chunks, vectors)]


class ContextRetriever:
    """In-memory ChromaDB collection over code chunks. Use one per repository."""

    def __init__(self, collection: str = "code_chunks"):
        import chromadb
        self._client = chromadb.EphemeralClient()
        self._ef = embedding_function()
        self._name = collection
        self._col = None
        self.chunks: list[Chunk] = []

    def create_index(self, embedded_chunks: list[dict]) -> None:
        if self._col is not None:
            self._client.delete_collection(self._name)
        self._col = self._client.create_collection(self._name, metadata={"hnsw:space": "cosine"})
        self.chunks = [e["chunk"] for e in embedded_chunks]
        self._col.add(
            ids=[c.id for c in self.chunks],
            documents=[c.render() for c in self.chunks],
            embeddings=[e["embedding"] for e in embedded_chunks],
            metadatas=[{"type": c.type, "name": c.name, "file_path": c.file_path} for c in self.chunks],
        )
        log.info("indexed %d chunks", len(self.chunks))

    def retrieve(self, query: str, n_results: int = 10) -> list[dict]:
        if self._col is None:
            raise RuntimeError("call create_index first")
        q = self._ef.embed_query([query]) if hasattr(self._ef, "embed_query") else self._ef([query])
        res = self._col.query(query_embeddings=[list(map(float, q[0]))], n_results=min(n_results, len(self.chunks)))
        return [{"document": d, "metadata": m, "distance": dist}
                for d, m, dist in zip(res["documents"][0], res["metadatas"][0], res["distances"][0])]

    def retrieve_for_pr(self, pr: dict, n_results: int = 15) -> str:
        """Selective context for a PR: chunks similar to the title, task and diff, plus the conventions doc."""
        query = f"{pr['title']}\n{pr.get('task_context', '')}\n{pr['diff'][: CONFIG.max_diff_chars]}"
        hits = self.retrieve(query, n_results)
        docs = [h["document"] for h in hits]
        # Conventions are always relevant to review, so pin them even if similarity ranks them low.
        for c in self.chunks:
            if c.type == "doc" and c.render() not in docs:
                docs.append(c.render())
        return "\n\n".join(docs)

    def full_context(self) -> str:
        return "\n\n".join(c.render() for c in self.chunks)


def build_retriever(repo: dict[str, str]) -> ContextRetriever:
    retriever = ContextRetriever()
    retriever.create_index(embed_chunks(chunk_repository(repo)))
    return retriever
