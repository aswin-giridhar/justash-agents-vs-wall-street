"""Document index and retrieval over the frozen corpus.

Retrieval is metadata filtering followed by BM25 over text chunks. Filters are
applied *before* scoring, so restricting to a company or document type is exact
rather than a ranking hint - asking for Hays documents can never return a Deere one.

Deliberately no embeddings. Financial retrieval here is literal: "adjusted gross
margin", "net fees", "Production & Precision Ag" are the exact strings that appear
in filings. Lexical matching finds them, costs no tokens, and is byte-for-byte
reproducible between runs, which matters for a system that must be re-run under
judging conditions.
"""

from __future__ import annotations

import argparse
import json
import math
import re
from collections import Counter
from dataclasses import asdict, dataclass
from functools import lru_cache
from pathlib import Path

from avws.config import CACHE_DIR, CORPUS_DIR, CORPUS_DIRS

INDEX_PATH = CACHE_DIR / "corpus_index.json"
CHUNK_CHARS = 1200
K1 = 1.5
B = 0.75
_TOKEN = re.compile(r"[a-z0-9][a-z0-9.,%$&-]*")
_FRONTMATTER_LINE = re.compile(r'^(\w+):\s*(?:"(.*)"|(.*))\s*$')


@dataclass(frozen=True)
class Doc:
    path: str
    company: str
    ticker: str
    published_at: str
    document_type: str
    period: str
    source_url: str | None

    @property
    def full_path(self) -> Path:
        return CORPUS_DIR / self.path


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Read the leading --- block. Hand-rolled to avoid a PyYAML dependency; the
    block is a flat set of quoted scalars in every document in this corpus."""
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    out: dict[str, str] = {}
    for line in text[3:end].splitlines():
        match = _FRONTMATTER_LINE.match(line.strip())
        if match:
            key, quoted, bare = match.groups()
            value = quoted if quoted is not None else (bare or "")
            out[key] = "" if value == "null" else value
    return out


def build_index(force: bool = False) -> list[Doc]:
    if INDEX_PATH.exists() and not force:
        return load_index()

    docs: list[Doc] = []
    for ticker, subdir in CORPUS_DIRS.items():
        for path in sorted((CORPUS_DIR / subdir).rglob("*.md")):
            if path.name == "INDEX.md":
                continue
            head = path.read_text(encoding="utf-8", errors="replace")[:2000]
            meta = _parse_frontmatter(head)
            docs.append(
                Doc(
                    path=str(path.relative_to(CORPUS_DIR)).replace("\\", "/"),
                    company=meta.get("company", ""),
                    ticker=ticker,
                    published_at=meta.get("published_at", ""),
                    document_type=meta.get("document_type", ""),
                    period=meta.get("period", ""),
                    source_url=meta.get("source_url") or None,
                )
            )

    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_PATH.write_text(
        json.dumps([asdict(d) for d in docs], indent=1), encoding="utf-8"
    )
    return docs


@lru_cache(maxsize=1)
def load_index() -> tuple[Doc, ...]:
    if not INDEX_PATH.exists():
        return tuple(build_index(force=True))
    raw = json.loads(INDEX_PATH.read_text(encoding="utf-8"))
    return tuple(Doc(**r) for r in raw)


def filter_docs(
    ticker: str | None = None,
    doc_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    period: str | None = None,
) -> list[Doc]:
    docs = list(load_index())
    if ticker:
        docs = [d for d in docs if d.ticker == ticker]
    if doc_type:
        want = doc_type.upper()
        docs = [d for d in docs if d.document_type.upper() == want]
    if since:
        docs = [d for d in docs if d.published_at >= since]
    if until:
        docs = [d for d in docs if d.published_at <= until]
    if period:
        docs = [d for d in docs if d.period == period]
    return docs


def _chunk(text: str) -> list[str]:
    """Split on blank lines, packing paragraphs up to CHUNK_CHARS. Keeps financial
    tables intact where possible, since a table split mid-row loses its meaning."""
    chunks: list[str] = []
    current: list[str] = []
    size = 0
    for para in text.split("\n\n"):
        if size + len(para) > CHUNK_CHARS and current:
            chunks.append("\n\n".join(current))
            current, size = [], 0
        current.append(para)
        size += len(para)
    if current:
        chunks.append("\n\n".join(current))
    return chunks


def tokenize(text: str) -> list[str]:
    return _TOKEN.findall(text.lower())


@lru_cache(maxsize=8)
def _chunks_for(ticker: str) -> tuple[tuple[Doc, str, tuple[str, ...]], ...]:
    """Load, chunk and tokenise one company's corpus. Cached per process: about
    25 MB and a second on first call, free thereafter."""
    out = []
    for doc in load_index():
        if doc.ticker != ticker:
            continue
        text = doc.full_path.read_text(encoding="utf-8", errors="replace")
        for chunk in _chunk(text):
            out.append((doc, chunk, tuple(tokenize(chunk))))
    return tuple(out)


def search(
    query: str,
    ticker: str | None = None,
    doc_type: str | None = None,
    since: str | None = None,
    until: str | None = None,
    k: int = 8,
) -> list[tuple[Doc, float, str]]:
    """Return (doc, bm25 score, best chunk) ranked, after exact metadata filtering."""
    allowed = {d.path for d in filter_docs(ticker, doc_type, since, until)}
    tickers = [ticker] if ticker else list(CORPUS_DIRS)

    pool = [
        (doc, chunk, tokens)
        for t in tickers
        for (doc, chunk, tokens) in _chunks_for(t)
        if doc.path in allowed
    ]
    if not pool:
        return []

    query_terms = set(tokenize(query))
    n = len(pool)
    avgdl = sum(len(tokens) for _, _, tokens in pool) / n

    doc_freq: Counter[str] = Counter()
    for _, _, tokens in pool:
        for term in set(tokens) & query_terms:
            doc_freq[term] += 1

    idf = {
        term: math.log(1 + (n - df + 0.5) / (df + 0.5))
        for term, df in doc_freq.items()
    }

    scored: list[tuple[Doc, float, str]] = []
    for doc, chunk, tokens in pool:
        counts = Counter(tokens)
        dl = len(tokens)
        score = 0.0
        for term in query_terms:
            tf = counts.get(term, 0)
            if not tf:
                continue
            score += idf[term] * (tf * (K1 + 1)) / (tf + K1 * (1 - B + B * dl / avgdl))
        if score > 0:
            scored.append((doc, score, chunk))

    scored.sort(key=lambda x: (-x[1], x[0].path))
    return scored[:k]


def _cli() -> None:
    parser = argparse.ArgumentParser(description="Search the frozen document corpus.")
    parser.add_argument("--query", required=True)
    parser.add_argument("--ticker", choices=sorted(CORPUS_DIRS))
    parser.add_argument("--doc-type", choices=["FILING", "CALL_TRANSCRIPT", "SLIDE"])
    parser.add_argument("--since")
    parser.add_argument("--until")
    parser.add_argument("-k", type=int, default=5)
    parser.add_argument("--chars", type=int, default=700)
    args = parser.parse_args()

    build_index()
    hits = search(args.query, args.ticker, args.doc_type, args.since, args.until, args.k)
    if not hits:
        print("no matches")
        return
    for rank, (doc, score, chunk) in enumerate(hits, 1):
        print(f"\n{'=' * 78}\n[{rank}] {score:6.2f}  {doc.published_at}  "
              f"{doc.document_type:<16} {doc.period}\n      {doc.path}")
        print("-" * 78)
        print(chunk[: args.chars])


if __name__ == "__main__":
    _cli()
