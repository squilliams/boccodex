"""Bocconi AI Buddy - backend entry point.

Implements a lightweight RAG pipeline over the bundled dataset and
exposes a single POST /ask endpoint. See AGENTS.md for full specs.
"""

import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
import urllib.request
import zipfile
import io
import threading
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, ConfigDict, Field
from openai import APIError, OpenAI, RateLimitError

app = FastAPI(title="Bocconi AI Buddy")

# CORS: allow the deployed frontend (and localhost during dev) to call /ask.
# Set FRONTEND_URL on Railway to your frontend service's public URL,
# e.g. https://buddy-frontend-yourname.up.railway.app
_allowed = [
    o.strip()
    for o in (os.environ.get("FRONTEND_URL") or "*").split(",")
    if o.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_allowed or ["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


Verticale = Literal[
    "relocation",
    "life_on_campus",
    "study_abroad",
    "career_readiness",
]


class AskRequest(BaseModel):
    question: str = Field(...)


class AskResponse(BaseModel):
    answer: str
    sources: list[str]
    verticale: Verticale


Rarity = Literal["common", "uncommon", "rare", "ultra-rare"]
DatasetSnapshot = Literal["2026-05-02", "live"]
CardDecisionAction = Literal["created", "already_owned", "skipped"]
CardImageSource = Literal["dataset", "unsplash", "placeholder"]


class BocCard(BaseModel):
    id: str
    vertical: Verticale
    title: str
    body: str
    longBody: str | None = None
    factTag: str
    imageQuery: str
    imageUrl: str | None = None
    imageAlt: str | None = None
    imageSource: CardImageSource | None = None
    rarity: Rarity
    isStarter: bool
    sourceLabel: str
    datasetSnapshot: DatasetSnapshot
    unlockedAt: str | None = None
    tags: list[str] = Field(default_factory=list)


class CardDecisionRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    question: str = Field(...)
    answer: str = Field(...)
    sources: list[str] = Field(default_factory=list)
    verticale: Verticale
    cards: list[BocCard] = Field(default_factory=list)
    collected_card_ids: list[str] = Field(default_factory=list, alias="collectedCardIds")


class CardDecisionResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True)

    action: CardDecisionAction
    card: BocCard | None = None
    existing_card_id: str | None = Field(default=None, alias="existingCardId")
    message: str


VERTICALI: tuple[Verticale, ...] = (
    "relocation",
    "life_on_campus",
    "study_abroad",
    "career_readiness",
)

DATA_DIR = Path(__file__).parent / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
MAX_CHARS_PER_DOC = 1800
TOP_K = 6
MODEL = os.environ.get("OPENAI_MODEL", "gpt-4.1-mini")
REQUEST_TIMEOUT = float(os.environ.get("OPENAI_TIMEOUT_SECONDS", "20"))
SEARCH_AGENT_ENABLED = os.environ.get("SEARCH_AGENT_ENABLED", "1") == "1"
SEARCH_AGENT_TIMEOUT_SECONDS = float(os.environ.get("SEARCH_AGENT_TIMEOUT_SECONDS", "2.0"))
MIN_SCOPE_OVERLAP = int(os.environ.get("MIN_SCOPE_OVERLAP", "2"))
SEARCH_AGENT_SCRIPT = Path(__file__).parent / "scripts" / "trusted_sources_agent.py"
MIN_SCOPE_COVERAGE = float(os.environ.get("MIN_SCOPE_COVERAGE", "0.18"))
API_CACHE_TTL_SECONDS = int(os.environ.get("API_CACHE_TTL_SECONDS", "1800"))
DISABLE_API_CACHE = os.environ.get("DISABLE_API_CACHE", "0") == "1"
EMBEDDING_RERANK_ENABLED = os.environ.get("EMBEDDING_RERANK_ENABLED", "1") == "1"
EMBEDDING_MODEL = os.environ.get("EMBEDDING_MODEL", "text-embedding-3-small")
EMBEDDING_RERANK_CANDIDATES = int(os.environ.get("EMBEDDING_RERANK_CANDIDATES", "18"))
EMBEDDING_WEIGHT = float(os.environ.get("EMBEDDING_WEIGHT", "4.0"))
LEXICAL_RERANK_WEIGHT = float(os.environ.get("LEXICAL_RERANK_WEIGHT", "1.5"))
OUT_OF_DOMAIN_HINTS = {
    "weather",
    "umbrella",
    "rain",
    "temperature",
    "forecast",
    "stock",
    "bitcoin",
    "sports score",
}
COUNTRY_ISO3: dict[str, str] = {
    "japan": "JPN",
    "giappone": "JPN",
    "china": "CHN",
    "cina": "CHN",
    "hong kong": "HKG",
    "singapore": "SGP",
    "south korea": "KOR",
    "corea del sud": "KOR",
    "india": "IND",
    "thailand": "THA",
    "indonesia": "IDN",
    "pakistan": "PAK",
    "israel": "ISR",
    "russia": "RUS",
    "ukraine": "UKR",
    "iran": "IRN",
    "myanmar": "MMR",
    "syria": "SYR",
    "yemen": "YEM",
    "afghanistan": "AFG",
}


_API_CACHE_LOCK = threading.Lock()
_API_CACHE: dict[str, tuple[float, tuple[str, list[str]] | None]] = {}
_EMBEDDING_LOCK = threading.Lock()
_DOC_EMBED_CACHE: dict[str, list[float]] = {}
_QUERY_EMBED_CACHE: dict[str, list[float]] = {}

client: OpenAI | None = None
if os.environ.get("OPENAI_API_KEY"):
    client = OpenAI(api_key=os.environ["OPENAI_API_KEY"])


@dataclass(frozen=True)
class Document:
    path: str
    verticale: Verticale
    title: str
    language: str
    source_url: str
    content: str
    searchable_text: str
    token_set: frozenset[str]
    title_text: str


DOCS_BY_VERTICALE: dict[Verticale, list[Document]] = {v: [] for v in VERTICALI}
TOKEN_RE = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_'-]{1,}")


VERTICALE_KEYWORDS: dict[Verticale, set[str]] = {
    "relocation": {
        "housing",
        "rent",
        "residence",
        "milan",
        "atm",
        "transport",
        "commute",
        "accommodation",
        "milano",
        "affitto",
        "alloggio",
        "trasporti",
        "casa",
        "dorm",
    },
    "life_on_campus": {
        "campus",
        "association",
        "event",
        "library",
        "wellbeing",
        "inclusion",
        "sports",
        "club",
        "student life",
        "mensa",
        "biblioteca",
        "sport",
        "counseling",
    },
    "study_abroad": {
        "exchange",
        "double degree",
        "international mobility",
        "erasmus",
        "visa",
        "abroad",
        "partner school",
        "country",
        "permesso",
        "study abroad",
    },
    "career_readiness": {
        "career",
        "internship",
        "cv",
        "job",
        "placement",
        "salary",
        "employer",
        "recruiting",
        "interview",
        "stage",
        "lavoro",
        "tirocinio",
        "scholarship",
        "merit award",
        "tuition waiver",
        "financial aid",
        "placement",
        "graduate survey",
    },
}

CAREER_QUERY_EXPANSIONS = {
    "sustainability": [
        "sustainability",
        "esg",
        "impact",
        "impact investing",
        "energy",
        "climate",
        "sustainable finance",
        "csr",
        "net zero",
        "circular economy",
    ],
    "career": [
        "career services",
        "internship",
        "curricular internship",
        "placement",
        "recruiting",
        "employers",
        "bocconi jobs",
    ],
}

GENERIC_STOPWORDS = {
    "the",
    "a",
    "an",
    "and",
    "or",
    "to",
    "for",
    "of",
    "in",
    "on",
    "at",
    "with",
    "about",
    "how",
    "what",
    "which",
    "should",
    "could",
    "would",
    "can",
    "is",
    "are",
    "i",
    "me",
    "my",
    "we",
    "our",
    "you",
    "your",
}

VERTICALE_REWRITE_HINTS: dict[Verticale, dict[str, list[str]]] = {
    "relocation": {
        "housing": ["housing", "residence", "accommodation", "rent", "alloggio"],
        "documents": ["visa", "permit of stay", "codice fiscale", "immigration documentation"],
        "transport": ["atm", "metro", "tram", "bus", "transport", "gtfs"],
        "airports": ["linate", "malpensa", "centrale", "commute", "public transport"],
    },
    "life_on_campus": {
        "associations": ["student associations", "student reps", "campus life", "community"],
        "services": ["library", "support services", "wellbeing", "inclusion", "counseling"],
        "events": ["events", "workshops", "seminars", "welcome days"],
        "city_events": ["yesmilano", "whats on", "all events", "weekend in milano"],
    },
    "study_abroad": {
        "exchange": ["exchange program", "selection criteria", "apply accept withdraw"],
        "departure": ["before departure", "health insurance", "academic recognition", "destinations"],
        "language": ["language requirement", "certificate", "eligibility"],
    },
    "career_readiness": {
        "sustainability": ["transformative sustainability", "esg", "impact", "sustainable finance"],
        "internship": ["career services", "curricular internship", "placement", "employers"],
        "jobs": ["career fairs", "bocconi jobs", "recruiting", "job market"],
    },
}


def _norm_text(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def _tokenize(text: str) -> list[str]:
    return [m.group(0).lower() for m in TOKEN_RE.finditer(text)]


def _strip_frontmatter(raw: str) -> str:
    if raw.startswith("---"):
        end = raw.find("\n---", 3)
        if end != -1:
            return raw[end + 4 :].strip()
    return raw


def _load_manifest() -> list[dict]:
    if not MANIFEST_PATH.exists():
        return []
    try:
        return json.loads(MANIFEST_PATH.read_text(encoding="utf-8")).get("files", [])
    except Exception:
        return []


def _build_local_index() -> None:
    manifest_items = _load_manifest()
    if not manifest_items:
        # Fallback if manifest is unavailable.
        for verticale in VERTICALI:
            for fp in (DATA_DIR / verticale).glob("*.md"):
                raw = fp.read_text(encoding="utf-8", errors="ignore")
                body = _strip_frontmatter(raw)
                snippet = body[:MAX_CHARS_PER_DOC]
                doc = Document(
                    path=str(fp.relative_to(Path(__file__).parent)),
                    verticale=verticale,
                    title=fp.stem,
                    language="unknown",
                    source_url="",
                    content=snippet,
                    searchable_text=_norm_text(body[:4000]),
                    token_set=frozenset(_tokenize(body[:4000])),
                    title_text=_norm_text(fp.stem),
                )
                DOCS_BY_VERTICALE[verticale].append(doc)
        return

    for item in manifest_items:
        verticale = item.get("verticale")
        rel_path = item.get("path")
        if verticale not in VERTICALI or not isinstance(rel_path, str):
            continue
        fp = DATA_DIR / rel_path
        if not fp.exists():
            continue
        raw = fp.read_text(encoding="utf-8", errors="ignore")
        body = _strip_frontmatter(raw)
        snippet = body[:MAX_CHARS_PER_DOC]
        doc = Document(
            path=f"data/{rel_path}",
            verticale=verticale,
            title=item.get("title") or fp.stem,
            language=item.get("language") or "unknown",
            source_url=item.get("source_url") or "",
            content=snippet,
            searchable_text=_norm_text(body[:4500]),
            token_set=frozenset(_tokenize(body[:4500])),
            title_text=_norm_text(item.get("title") or fp.stem),
        )
        DOCS_BY_VERTICALE[verticale].append(doc)


def _classify_verticale(question: str) -> Verticale:
    q = _norm_text(question)
    scores: dict[Verticale, int] = {v: 0 for v in VERTICALI}

    for verticale, words in VERTICALE_KEYWORDS.items():
        for w in words:
            if w in q:
                scores[verticale] += 2

    for token in _tokenize(q):
        for verticale, words in VERTICALE_KEYWORDS.items():
            if token in words:
                scores[verticale] += 1

    # Safe default: relocation questions are common and usually practical.
    return max(scores, key=scores.get) if any(scores.values()) else "relocation"


def _expand_query(question: str, verticale: Verticale) -> str:
    q = _norm_text(question)
    if verticale != "career_readiness":
        return question
    expanded_terms: list[str] = []
    if any(t in q for t in ("sustainability", "esg", "impact", "climate", "green")):
        expanded_terms.extend(CAREER_QUERY_EXPANSIONS["sustainability"])
    if any(t in q for t in ("career", "job", "internship", "work", "consulting")):
        expanded_terms.extend(CAREER_QUERY_EXPANSIONS["career"])
    if not expanded_terms:
        return question
    return f"{question} {' '.join(sorted(set(expanded_terms)))}"


def _simplify_query(question: str) -> str:
    # Keep informative tokens and strip filler wording.
    tokens = _tokenize(question)
    kept = [t for t in tokens if len(t) >= 3 and t not in GENERIC_STOPWORDS]
    return " ".join(kept[:16]) if kept else question


def _rewrite_query(question: str, verticale: Verticale) -> str:
    """Cheap deterministic query rewriting for better retrieval.

    Goal: keep user intent while producing a denser search query.
    """
    base = _simplify_query(question)
    q_norm = _norm_text(question)
    hints = VERTICALE_REWRITE_HINTS.get(verticale, {})

    extra_terms: list[str] = []
    for _, terms in hints.items():
        # If any term in a hint bucket appears, append the full bucket.
        if any(t in q_norm for t in terms):
            extra_terms.extend(terms)

    # Reuse existing career-specific expansion logic.
    expanded = _expand_query(question, verticale)
    if expanded != question:
        extra_terms.extend(_tokenize(expanded))

    dedup = []
    seen = set()
    for t in _tokenize(base) + extra_terms:
        key = t.strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        dedup.append(key)
        if len(dedup) >= 28:
            break

    return " ".join(dedup) if dedup else question


def _keyword_score(doc: Document, q_tokens: set[str], q_norm: str) -> float:
    # BM25-style lite: strong reward for exact term matches and title hits.
    overlap = len(q_tokens & doc.token_set)
    if overlap == 0:
        return 0.0
    title_overlap = sum(1 for t in q_tokens if t in doc.title_text)
    phrase_bonus = 2.0 if q_norm and q_norm[:40] in doc.searchable_text else 0.0
    return overlap * 2.5 + title_overlap * 3.0 + phrase_bonus


def _semantic_lite_score(doc: Document, q_tokens: set[str], q_norm: str) -> float:
    # Semantic-lite lane: softer matching via token coverage and phrase windows.
    if not q_tokens:
        return 0.0
    coverage = len(q_tokens & doc.token_set) / max(1, len(q_tokens))
    short_phrases = [p.strip() for p in re.split(r"[,:;?.!]", q_norm) if len(p.strip()) > 6]
    phrase_hits = sum(1 for p in short_phrases[:3] if p in doc.searchable_text)
    vertical_hint_hits = sum(
        1 for w in VERTICALE_KEYWORDS.get(doc.verticale, set()) if w in q_norm and w in doc.searchable_text
    )
    return coverage * 10.0 + phrase_hits * 2.0 + vertical_hint_hits * 0.5


def _rrf_fuse(rankings: list[list[Document]], k: int) -> list[Document]:
    # Reciprocal rank fusion for robust hybrid retrieval.
    rrf_scores: dict[str, float] = {}
    docs_by_path: dict[str, Document] = {}
    rrf_k = 60.0
    for ranking in rankings:
        for rank, doc in enumerate(ranking, start=1):
            docs_by_path[doc.path] = doc
            rrf_scores[doc.path] = rrf_scores.get(doc.path, 0.0) + 1.0 / (rrf_k + rank)
    ordered_paths = sorted(rrf_scores.keys(), key=lambda p: rrf_scores[p], reverse=True)
    return [docs_by_path[p] for p in ordered_paths[:k]]


def _study_abroad_intent_adjustment(question: str, doc: Document) -> float:
    q = _norm_text(question)
    title = doc.title_text
    path = _norm_text(doc.path)
    text = f"{title} {path}"
    score = 0.0

    exchange_intent = any(t in q for t in ("exchange", "erasmus", "partner school"))
    free_mover_intent = "free mover" in q
    double_degree_intent = "double degree" in q

    if exchange_intent and "exchange" in text:
        score += 3.0
    if exchange_intent and "free-mover" in text and not free_mover_intent:
        score -= 3.0
    if exchange_intent and "double-degree" in text and not double_degree_intent:
        score -= 2.5
    if any(t in q for t in ("before departure", "before leaving", "departure", "start mobility")):
        if any(t in text for t in ("start-mobility", "apply-accept-or-withdraw", "requirements")):
            score += 2.0
    return score


def _career_intent_adjustment(question: str, doc: Document) -> float:
    q = _norm_text(question)
    text = f"{doc.title_text} {_norm_text(doc.path)}"
    score = 0.0
    sustainability_intent = any(t in q for t in ("sustainability", "esg", "climate", "impact", "green"))
    if not sustainability_intent:
        return score

    if any(t in text for t in ("transformative sustainability", "sustainability and energy", "career-services")):
        score += 3.0
    if any(t in text for t in ("mafinrisk", "quantitative finance")) and "sustainable finance" not in text:
        score -= 3.0
    return score


def _relocation_intent_adjustment(question: str, doc: Document) -> float:
    q = _norm_text(question)
    text = f"{doc.title_text} {_norm_text(doc.path)}"
    score = 0.0

    scholarship_intent = any(
        t in q
        for t in ("scholarship", "merit award", "tuition waiver", "waiver", "fees", "financial aid", "funding")
    )
    if not scholarship_intent:
        return score

    if any(t in text for t in ("funding", "scholarship", "tuition", "fees", "merit")):
        score += 2.5
    if "housing" in text and "tuition" not in text and "scholarship" not in text:
        score -= 3.0

    fiscal_intent = any(t in q for t in ("codice fiscale", "fiscal code", "tax code"))
    if fiscal_intent:
        if any(t in text for t in ("immigration", "fiscal", "codice fiscale", "permit", "visa", "required documents")):
            score += 3.0
        if "housing" in text and "immigration" not in text:
            score -= 2.5
    return score


def _intent_adjustment(question: str, verticale: Verticale, doc: Document) -> float:
    q = _norm_text(question)
    doc_key = _norm_text(doc.path + " " + doc.title)
    if verticale == "relocation" and any(t in q for t in ("required documents", "documents", "first steps", "moving to milan")):
        if "external-relocation-required-documents-checklist" in doc_key:
            return 5.0
    if verticale == "relocation":
        return _relocation_intent_adjustment(question, doc)
    if verticale == "life_on_campus" and any(t in q for t in ("integrate", "first-year", "first month", "associations", "campus services")):
        if "external-campus-integration-first-month-pack" in doc_key:
            return 5.0
    if verticale == "study_abroad":
        return _study_abroad_intent_adjustment(question, doc)
    if verticale == "career_readiness":
        return _career_intent_adjustment(question, doc)
    return 0.0


def _intent_doc_allowed(question: str, verticale: Verticale, doc: Document) -> bool:
    if verticale != "study_abroad":
        return True
    q = _norm_text(question)
    text = f"{doc.title_text} {_norm_text(doc.path)}"
    exchange_intent = any(t in q for t in ("exchange", "erasmus", "partner school"))
    free_mover_intent = "free mover" in q
    double_degree_intent = "double degree" in q
    if exchange_intent and not free_mover_intent and "free-mover" in text:
        return False
    if exchange_intent and not double_degree_intent and "double-degree" in text:
        return False
    return True


def _cosine(a: list[float], b: list[float]) -> float:
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


def _embed_text(text: str) -> list[float] | None:
    if client is None:
        return None
    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=text[:4000],
            timeout=min(REQUEST_TIMEOUT, 12),
        )
        vec = resp.data[0].embedding
        return vec if isinstance(vec, list) else None
    except Exception:
        return None


def _get_query_embedding(query: str) -> list[float] | None:
    with _EMBEDDING_LOCK:
        cached = _QUERY_EMBED_CACHE.get(query)
    if cached is not None:
        return cached
    vec = _embed_text(query)
    if vec is not None:
        with _EMBEDDING_LOCK:
            _QUERY_EMBED_CACHE[query] = vec
    return vec


def _get_doc_embedding(doc: Document) -> list[float] | None:
    with _EMBEDDING_LOCK:
        cached = _DOC_EMBED_CACHE.get(doc.path)
    if cached is not None:
        return cached
    vec = _embed_text(f"{doc.title}\n{doc.content}")
    if vec is not None:
        with _EMBEDDING_LOCK:
            _DOC_EMBED_CACHE[doc.path] = vec
    return vec


def _get_doc_embeddings_bulk(docs: list[Document]) -> dict[str, list[float]]:
    out: dict[str, list[float]] = {}
    missing: list[Document] = []

    with _EMBEDDING_LOCK:
        for d in docs:
            cached = _DOC_EMBED_CACHE.get(d.path)
            if cached is not None:
                out[d.path] = cached
            else:
                missing.append(d)

    if not missing or client is None:
        return out

    try:
        resp = client.embeddings.create(
            model=EMBEDDING_MODEL,
            input=[f"{d.title}\n{d.content}"[:4000] for d in missing],
            timeout=min(REQUEST_TIMEOUT, 12),
        )
    except Exception:
        # Graceful fallback: keep any cached vectors and let the pipeline continue.
        return out

    with _EMBEDDING_LOCK:
        for d, item in zip(missing, resp.data):
            vec = item.embedding if isinstance(item.embedding, list) else None
            if vec is None:
                continue
            _DOC_EMBED_CACHE[d.path] = vec
            out[d.path] = vec

    return out


def _retrieve_docs(question: str, verticale: Verticale, k: int = TOP_K) -> list[Document]:
    rewritten_query = _rewrite_query(question, verticale)
    q_norm = _norm_text(rewritten_query)
    q_tokens = set(_tokenize(q_norm))
    if not q_tokens:
        return []

    docs = DOCS_BY_VERTICALE.get(verticale, [])
    keyword_ranked = sorted(
        docs,
        key=lambda d: _keyword_score(d, q_tokens, q_norm),
        reverse=True,
    )
    semantic_ranked = sorted(
        docs,
        key=lambda d: _semantic_lite_score(d, q_tokens, q_norm),
        reverse=True,
    )
    # Trim each lane before fusion to keep it fast and avoid noise.
    keyword_ranked = [d for d in keyword_ranked[:40] if _keyword_score(d, q_tokens, q_norm) > 0]
    semantic_ranked = [d for d in semantic_ranked[:40] if _semantic_lite_score(d, q_tokens, q_norm) > 0]
    fused = _rrf_fuse([keyword_ranked, semantic_ranked], k=max(k * 3, 10))

    # Lightweight re-rank to prioritize intent-specific matches.
    q_title_terms = {t for t in q_tokens if len(t) >= 4}
    rescored: list[tuple[float, Document]] = []
    for d in fused:
        title_match = len(q_title_terms & set(_tokenize(d.title_text)))
        # Source-quality nudge for known trusted domains.
        trust_bonus = (
            1.2
            if any(
                domain in d.source_url
                for domain in ("unibocconi.it", "yesmilano.it", "dati.comune.milano.it", "amat-mi.it")
            )
            else 0.0
        )
        score = (
            _keyword_score(d, q_tokens, q_norm)
            + _semantic_lite_score(d, q_tokens, q_norm)
            + title_match * 2.0
            + trust_bonus
            + _intent_adjustment(question, verticale, d)
        )
        rescored.append((score, d))
    rescored.sort(key=lambda x: x[0], reverse=True)
    ordered_docs = [d for _, d in rescored]
    filtered = [d for d in ordered_docs if _intent_doc_allowed(question, verticale, d)]
    if not filtered:
        filtered = ordered_docs

    # Optional embedding rerank on top lexical/intent candidates only (fast path).
    if EMBEDDING_RERANK_ENABLED and client is not None and filtered:
        top_n = max(k, min(len(filtered), EMBEDDING_RERANK_CANDIDATES))
        candidates = filtered[:top_n]
        qvec = _get_query_embedding(rewritten_query)
        if qvec is not None:
            emb_scores: dict[str, float] = {}
            doc_vecs = _get_doc_embeddings_bulk(candidates)
            for d in candidates:
                dvec = doc_vecs.get(d.path)
                if dvec is not None:
                    emb_scores[d.path] = _cosine(qvec, dvec)
            if emb_scores:
                blend: list[tuple[float, Document]] = []
                base_rank = {d.path: i for i, d in enumerate(filtered)}
                for d in candidates:
                    rank = base_rank.get(d.path, 999)
                    lexical_component = 1.0 / (1.0 + float(rank))
                    semantic_component = emb_scores.get(d.path, 0.0)
                    score = lexical_component * LEXICAL_RERANK_WEIGHT + semantic_component * EMBEDDING_WEIGHT
                    blend.append((score, d))
                blend.sort(key=lambda x: x[0], reverse=True)
                top = [d for _, d in blend[:k]]
                # Fill if needed from remaining filtered list.
                used = {d.path for d in top}
                for d in filtered:
                    if len(top) >= k:
                        break
                    if d.path not in used:
                        top.append(d)
                filtered = top + [d for d in filtered if d.path not in {x.path for x in top}]

    # Diversity pass (MMR-lite): keep high relevance but reduce near-duplicate pages.
    selected: list[Document] = []
    selected_paths: set[str] = set()
    for d in filtered:
        if d.path in selected_paths:
            continue
        d_tokens = set(_tokenize(d.title_text + " " + _norm_text(d.path)))
        too_similar = False
        for s in selected:
            s_tokens = set(_tokenize(s.title_text + " " + _norm_text(s.path)))
            jacc = len(d_tokens & s_tokens) / max(1, len(d_tokens | s_tokens))
            if jacc > 0.72:
                too_similar = True
                break
        if not too_similar:
            selected.append(d)
            selected_paths.add(d.path)
        if len(selected) >= k:
            break

    if len(selected) < k:
        for d in filtered:
            if d.path in selected_paths:
                continue
            selected.append(d)
            selected_paths.add(d.path)
            if len(selected) >= k:
                break

    return selected[:k]


def _clean_context_text(text: str) -> str:
    # Reduce markdown/navigation noise before passing context to the generator.
    t = text
    t = re.sub(r"!\[[^\]]*\]\([^)]+\)", " ", t)  # images
    t = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", t)  # links
    t = re.sub(r"#+\s*", " ", t)  # headings
    t = re.sub(r"\*\*+", "", t)
    t = re.sub(r"_+", "", t)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def _query_focused_context(doc: Document, question: str, max_sentences: int = 5) -> str:
    q_tokens = {t for t in _tokenize(_norm_text(question)) if len(t) >= 3}
    if not q_tokens:
        return _clean_context_text(doc.content[:800])

    bad_noise = (
        "learning how to search",
        "get to know the library",
        "website navigation",
        "breadcrumb",
        "cookie policy",
    )
    scored: list[tuple[float, str]] = []
    raw = _clean_context_text(doc.content)
    for s in re.split(r"(?<=[.!?])\s+", raw):
        sent = s.strip()
        if len(sent) < 35 or len(sent) > 280:
            continue
        ns = _norm_text(sent)
        if any(b in ns for b in bad_noise):
            continue
        overlap = len(q_tokens & set(_tokenize(ns)))
        if overlap <= 0:
            continue
        score = float(overlap)
        if any(k in ns for k in ("must", "required", "deadline", "apply", "request", "documents", "eligibility")):
            score += 0.4
        scored.append((score, sent))

    if not scored:
        return _clean_context_text(doc.content[:800])
    scored.sort(key=lambda x: x[0], reverse=True)

    picked: list[str] = []
    seen = set()
    for _, sent in scored:
        key = _norm_text(sent)
        if key in seen:
            continue
        seen.add(key)
        picked.append(sent)
        if len(picked) >= max_sentences:
            break
    return " ".join(picked)


def _retrieval_overlap_score(question: str, docs: list[Document]) -> int:
    if not docs:
        return 0
    q_tokens = set(_tokenize(question))
    if not q_tokens:
        return 0
    best = 0
    for doc in docs[:3]:
        overlap = len(q_tokens & doc.token_set)
        if overlap > best:
            best = overlap
    return best


def _retrieval_coverage_score(question: str, docs: list[Document]) -> float:
    if not docs:
        return 0.0
    q_tokens = {t for t in _tokenize(question) if len(t) >= 4}
    if not q_tokens:
        return 0.0
    union: set[str] = set()
    for d in docs[:4]:
        union |= d.token_set
    return len(q_tokens & union) / max(1, len(q_tokens))


def _is_out_of_domain_question(question: str) -> bool:
    q = _norm_text(question)
    return any(hint in q for hint in OUT_OF_DOMAIN_HINTS)


def _is_weather_question(question: str) -> bool:
    q = _norm_text(question)
    weather_terms = ("weather", "umbrella", "rain", "forecast", "temperature")
    return any(t in q for t in weather_terms)


def _is_high_risk_live_fact_question(question: str) -> bool:
    q = _norm_text(question)
    exact_markers = ("exact", "live", "right now", "at ")
    weather_markers = ("weather", "forecast", "temperature", "umbrella")
    return any(m in q for m in exact_markers) and any(w in q for w in weather_markers)


def _has_unavailability_guard(question: str) -> bool:
    q = _norm_text(question)
    return any(
        p in q
        for p in (
            "if unavailable in data",
            "if unavailable",
            "if not available",
            "if you cannot find",
            "if not in the data",
        )
    )


def _is_exact_deadline_question(question: str) -> bool:
    q = _norm_text(question)
    return "deadline" in q and any(m in q for m in ("exact", "this cycle", "current cycle"))


def _supports_entity_in_docs(question: str, docs: list[Document]) -> bool:
    q = _norm_text(question)
    probe_terms: list[str] = []
    if "mit" in q or "massachusetts institute of technology" in q:
        probe_terms.extend(["mit", "massachusetts institute of technology"])
    if "deadline" in q:
        probe_terms.append("deadline")
    if not probe_terms:
        return True
    hay = " ".join((_norm_text(d.title + " " + d.path + " " + d.content[:700])) for d in docs[:6])
    return all(t in hay for t in probe_terms)


def _is_transport_question(question: str) -> bool:
    q = _norm_text(question)
    q_tokens = set(_tokenize(q))
    transport_tokens = {"metro", "tram", "bus", "atm", "transport", "commute", "ticket", "gtfs", "subway"}
    transport_phrases = (
        "public transport",
        "urban pass",
        "monthly pass",
        "annual pass",
        "line ",
        "linea ",
        "malpensa bus",
    )
    transport_hit = bool(q_tokens & transport_tokens) or any(p in q for p in transport_phrases)
    # Keep transport API path for transport-dominant questions only.
    broader_relocation_terms = ("housing", "residence", "rent", "documents", "visa", "permit", "alloggio")
    broader_hit = any(t in q for t in broader_relocation_terms)
    # Guard: do not hijack academic/program/funding questions.
    academic_terms = (
        "exchange",
        "double degree",
        "study abroad",
        "deadline",
        "application",
        "scholarship",
        "merit award",
        "tuition waiver",
        "placement",
        "survey",
        "bess",
        "mit",
        "bachelor",
        "master of science",
        "msc",
        "gpa",
        "credits",
    )
    academic_hit = any(t in q for t in academic_terms)
    return transport_hit and not broader_hit and not academic_hit


def _is_events_question(question: str) -> bool:
    q = _norm_text(question)
    terms = (
        "events",
        "eventi",
        "things to do",
        "what to do",
        "weekend",
        "concert",
        "exhibition",
        "mostra",
        "festival",
        "campus life",
    )
    return any(t in q for t in terms)


def _cache_get(key: str) -> tuple[str, list[str]] | None | str:
    if DISABLE_API_CACHE:
        return "__MISS__"
    now = time.time()
    with _API_CACHE_LOCK:
        hit = _API_CACHE.get(key)
    if hit is None:
        return "__MISS__"
    ts, value = hit
    if now - ts > API_CACHE_TTL_SECONDS:
        with _API_CACHE_LOCK:
            _API_CACHE.pop(key, None)
        return "__MISS__"
    return value


def _cache_set(key: str, value: tuple[str, list[str]] | None) -> None:
    if DISABLE_API_CACHE:
        return
    with _API_CACHE_LOCK:
        _API_CACHE[key] = (time.time(), value)


def _fetch_milan_weather_answer(question: str) -> tuple[str, list[str]] | None:
    cache_key = "weather:milan:tomorrow"
    cached = _cache_get(cache_key)
    if cached != "__MISS__":
        return cached
    # Public API: Open-Meteo (no API key required)
    base = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": "45.4642",
        "longitude": "9.19",
        "daily": "temperature_2m_max,temperature_2m_min,precipitation_probability_max",
        "timezone": "Europe/Rome",
        "forecast_days": "3",
    }
    url = f"{base}?{urllib.parse.urlencode(params)}"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BocconiBuddy/1.0"})
        with urllib.request.urlopen(req, timeout=3.5) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        daily = payload.get("daily", {})
        dates = daily.get("time", [])
        tmax = daily.get("temperature_2m_max", [])
        tmin = daily.get("temperature_2m_min", [])
        pprob = daily.get("precipitation_probability_max", [])
        if len(dates) < 2:
            return None
        # "Tomorrow" = index 1
        date_t = dates[1]
        tmax_t = tmax[1] if len(tmax) > 1 else "n/a"
        tmin_t = tmin[1] if len(tmin) > 1 else "n/a"
        pprob_t = pprob[1] if len(pprob) > 1 else 0
        umbrella = "Yes, bring an umbrella." if float(pprob_t) >= 40 else "Umbrella is probably not necessary."
        answer = (
            f"For Milan on {date_t}, forecast temperature is about {tmin_t}°C to {tmax_t}°C "
            f"with maximum precipitation probability around {pprob_t}%. {umbrella}"
        )
        value = (answer, [url])
        _cache_set(cache_key, value)
        return value
    except Exception:
        _cache_set(cache_key, None)
        return None


def _fetch_gtfs_transport_answer(question: str) -> tuple[str, list[str]] | None:
    q = _norm_text(question)
    cache_key = f"gtfs:milan:{q}"
    cached = _cache_get(cache_key)
    if cached != "__MISS__":
        return cached

    gtfs_urls = [
        "https://dati.comune.milano.it/gtfs.zip",
        "https://dati.comune.milano.it/dataset/ae3f3db9-de61-45b7-94e7-9395c0e3ef53/resource/6251f156-4c74-4a0b-904e-01bcb701a686/download/gtfs.zip",
    ]
    line_candidate = None
    for tok in _tokenize(q):
        if len(tok) <= 4 and any(c.isdigit() for c in tok):
            line_candidate = tok.upper()
            break

    zip_bytes = None
    used_url = None
    for u in gtfs_urls:
        try:
            req = urllib.request.Request(u, headers={"User-Agent": "BocconiBuddy/1.0"})
            with urllib.request.urlopen(req, timeout=4.5) as resp:
                zip_bytes = resp.read()
            used_url = u
            break
        except Exception:
            continue
    if zip_bytes is None:
        _cache_set(cache_key, None)
        return None

    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    routes_raw = zf.read("routes.txt").decode("utf-8", errors="ignore").splitlines()
    stops_raw = zf.read("stops.txt").decode("utf-8", errors="ignore").splitlines()
    routes_count = max(0, len(routes_raw) - 1)
    stops_count = max(0, len(stops_raw) - 1)

    matched_line = None
    if line_candidate:
        header = routes_raw[0].split(",")
        try:
            idx_short = header.index("route_short_name")
            idx_long = header.index("route_long_name")
        except ValueError:
            idx_short = -1
            idx_long = -1
        if idx_short >= 0:
            for row in routes_raw[1:600]:
                parts = row.split(",")
                if len(parts) <= idx_short:
                    continue
                short = parts[idx_short].strip().strip('"')
                if short.upper() == line_candidate:
                    long_name = parts[idx_long].strip().strip('"') if idx_long >= 0 and len(parts) > idx_long else ""
                    matched_line = f"{short} ({long_name})" if long_name else short
                    break

    answer = (
        f"Milan GTFS public dataset currently includes about {routes_count} routes and {stops_count} stops. "
        + (f"I found line {matched_line} in the feed. " if matched_line else "")
        + "For route planning and up-to-date schedules, check ATM official pages and the GTFS dataset."
    )
    sources = [
        used_url or gtfs_urls[0],
        "https://dati.comune.milano.it/dataset/ds929-orari-del-trasporto-pubblico-locale-nel-comune-di-milano-in-formato-gtfs",
        "https://www.atm.it/en/ViaggiaConNoi/Pages/default.aspx",
    ]
    value = (answer, sources)
    _cache_set(cache_key, value)
    return value


def _html_to_text_light(raw_html: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def _fetch_milano_events_answer(question: str) -> tuple[str, list[str]] | None:
    q = _norm_text(question)
    cache_key = f"events:milano:{q}"
    cached = _cache_get(cache_key)
    if cached != "__MISS__":
        return cached

    urls = [
        "https://www.yesmilano.it/en/whats-on/all-events",
        "https://studyandwork.yesmilano.it/en/events-study",
    ]
    for url in urls:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "BocconiBuddy/1.0"})
            with urllib.request.urlopen(req, timeout=4.0) as resp:
                raw = resp.read().decode("utf-8", errors="ignore")
            text = _html_to_text_light(raw)
            # Typical rendered fragments include "Until ... From ... <title>"
            matches = re.findall(r"Until\s+([0-9]{1,2}\s+\w+)\s+From\s+([0-9]{1,2}\s+\w+)\s+([^\\.]{10,120})", text)
            picks = []
            for m in matches[:4]:
                title = re.sub(r"\s+", " ", m[2]).strip(" -")
                picks.append(f"- {title} (from {m[1]} until {m[0]})")
            if not picks:
                answer = (
                    "Milano has an active public events calendar for students and visitors. "
                    "Use YesMilano's official events pages to filter by date/category (today, weekend, next week)."
                )
            else:
                answer = "Here are current public events in Milano from YesMilano:\n" + "\n".join(picks[:3])
            value = (answer, urls)
            _cache_set(cache_key, value)
            return value
        except Exception:
            continue
    _cache_set(cache_key, None)
    return None


def _is_travel_safety_question(question: str) -> bool:
    q = _norm_text(question)
    intent_terms = (
        "safe",
        "safety",
        "risk",
        "dangerous",
        "travel advisory",
        "security",
        "sicuro",
        "sicurezza",
        "rischio",
        "exchange",
        "study abroad",
    )
    return any(t in q for t in intent_terms)


def _is_multi_intent_question(question: str) -> bool:
    q = _norm_text(question)
    buckets = 0
    if any(t in q for t in ("exchange", "double degree", "study abroad", "visa", "departure", "incoming")):
        buckets += 1
    if any(t in q for t in ("safe", "safety", "risk", "dangerous", "travel advisory", "security", "sicurezza")):
        buckets += 1
    if any(t in q for t in ("housing", "accommodation", "residence", "alloggio", "dorm", "support")):
        buckets += 1
    if any(t in q for t in ("career", "internship", "placement", "job", "cv", "tirocinio")):
        buckets += 1

    # Conjunction-style phrasing often signals multiple asks in one message.
    connector_hit = any(t in q for t in (" and ", " also ", " plus ", " as well as ", "where", "what should i do first"))
    return buckets >= 2 or (buckets >= 1 and connector_hit and q.count("?") <= 1)


def _extract_country_iso3(question: str) -> str | None:
    q = _norm_text(question)
    for name, iso3 in COUNTRY_ISO3.items():
        if name in q:
            return iso3
    return None


def _fetch_viaggiaresicuri_answer(question: str) -> tuple[str, list[str]] | None:
    iso3 = _extract_country_iso3(question)
    if not iso3:
        return None
    cache_key = f"viaggiaresicuri:{iso3}"
    cached = _cache_get(cache_key)
    if cached != "__MISS__":
        return cached
    url = f"https://www.viaggiaresicuri.it/schede_paese/{iso3}.json"
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "BocconiBuddy/1.0"})
        with urllib.request.urlopen(req, timeout=4.0) as resp:
            payload = json.loads(resp.read().decode("utf-8", errors="ignore"))
        def _collect_node_texts(node: dict) -> list[str]:
            out: list[str] = []
            testo = node.get("testo")
            if isinstance(testo, str):
                clean = re.sub(r"<[^>]+>", " ", testo)
                clean = re.sub(r"\s+", " ", clean).strip()
                if clean:
                    out.append(clean)
            for child in node.get("nodi", []) or []:
                if isinstance(child, dict):
                    out.extend(_collect_node_texts(child))
            return out

        country = iso3
        sicurezza = payload.get("infoSicurezza") if isinstance(payload, dict) else None
        texts: list[str] = []
        if isinstance(sicurezza, dict):
            for n in sicurezza.get("nodi", []) or []:
                if isinstance(n, dict):
                    texts.extend(_collect_node_texts(n))
        status = " ".join(texts[:3]).strip() if texts else "No structured safety field returned."
        if len(status) > 700:
            status = status[:700] + "..."
        update_date = payload.get("updateDate") if isinstance(payload, dict) else None
        update_suffix = f" (updated: {update_date})" if isinstance(update_date, str) and update_date else ""
        answer = (
            f"According to Italy's ViaggiareSicuri advisory for {country}, "
            f"the latest published safety guidance is: {status}{update_suffix}. "
            "Always check the full official advisory before departure."
        )
        value = (answer, [url])
        _cache_set(cache_key, value)
        return value
    except Exception:
        _cache_set(cache_key, None)
        return None


def _invoke_search_agent(question: str, verticale: Verticale, limit: int = 3) -> list[tuple[str, str, str]]:
    """Run the local trusted-sources search agent for out-of-scope questions."""
    if not SEARCH_AGENT_ENABLED or not SEARCH_AGENT_SCRIPT.exists():
        return []

    queries: list[str] = []
    query_terms = _tokenize(question)[:8]
    if query_terms:
        queries.append(" ".join(query_terms))
    # Fallback intent query by verticale to avoid empty match sets.
    queries.append(f"{verticale} milan bocconi official")
    if verticale == "relocation":
        queries.append("milan transport housing yesmilano gtfs")
    if verticale == "career_readiness":
        queries.append("career internship sustainability almalaurea")

    for query in queries:
        cmd = [sys.executable, str(SEARCH_AGENT_SCRIPT), "search", "--query", query]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=SEARCH_AGENT_TIMEOUT_SECONDS,
                check=False,
            )
        except Exception:
            continue
        if proc.returncode != 0 or not proc.stdout.strip():
            continue
        hits: list[tuple[str, str, str]] = []
        for line in proc.stdout.splitlines():
            if not line.startswith("- "):
                continue
            parts = [p.strip() for p in line[2:].split("|")]
            if len(parts) != 3:
                continue
            hits.append((parts[0], parts[1], parts[2]))
            if len(hits) >= limit:
                break
        if hits:
            return hits
    return []


def _call_llm_with_retry(messages: list[dict[str, str]]) -> str | None:
    if client is None:
        return None

    backoffs = [1, 2, 4]
    last_err: Exception | None = None
    for i, wait_s in enumerate(backoffs, start=1):
        try:
            resp = client.chat.completions.create(
                model=MODEL,
                messages=messages,
                timeout=REQUEST_TIMEOUT,
                temperature=0.2,
            )
            return (resp.choices[0].message.content or "").strip()
        except (RateLimitError, APIError) as err:
            last_err = err
            if i < len(backoffs):
                time.sleep(wait_s)
        except Exception as err:
            last_err = err
            break
    _ = last_err
    return None


def _build_prompt(question: str, verticale: Verticale, docs: list[Document]) -> list[dict[str, str]]:
    context_blocks: list[str] = []
    for i, d in enumerate(docs, start=1):
        focused = _query_focused_context(d, question, max_sentences=5)
        context_blocks.append(
            (
                f"[DOC {i}] path={d.path}\n"
                f"title={d.title}\n"
                f"verticale={d.verticale}\n"
                f"content:\n{focused}"
            )
        )

    system = (
        "You are Bocconi AI Buddy. Answer ONLY using the provided context. "
        "If context is insufficient, say you cannot answer right now and ask for a clearer question. "
        "Be concise, factual, and avoid hallucinations. "
        "Keep original Bocconi terms and names. "
        "Reply in the same language as the user question when possible. "
        "Do not include website navigation artifacts or markdown boilerplate. "
        "Do not include unrelated topics, even if they appear in context."
    )
    answer_shape = (
        "Answer format:\n"
        "1) Short answer (1-2 sentences)\n"
        "2) Recommended path at Bocconi (3-5 bullet points)\n"
        "3) Concrete next actions this month (2-4 bullet points)\n"
        "Each bullet must be directly relevant to the user's exact question.\n"
        "If multiple constraints are asked, cover each constraint explicitly.\n"
        "If the question is about sustainability careers, include relevant courses/programs, internship guidance, and career-service touchpoints when present in context."
    )
    user = (
        f"Question: {question}\n"
        f"Detected verticale: {verticale}\n\n"
        "Context documents:\n"
        + "\n\n".join(context_blocks)
        + "\n\n"
        + answer_shape
        + "\n\nProvide a direct helpful answer grounded in these documents."
    )
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _extractive_fallback_answer(question: str, docs: list[Document]) -> str:
    q_norm = _norm_text(question)
    q_tokens = {t for t in _tokenize(q_norm) if len(t) >= 3}
    if not q_tokens:
        return "I cannot answer right now."

    navigation_artifacts = (
        "get to know the library",
        "library website",
        "learning how to search",
        "website navigation",
        "click",
        "breadcrumb",
    )

    # Prioritize docs with strongest query overlap first.
    ranked_docs: list[tuple[int, Document]] = []
    for d in docs[:6]:
        doc_overlap = len(q_tokens & d.token_set)
        if doc_overlap > 0:
            ranked_docs.append((doc_overlap, d))
    ranked_docs.sort(key=lambda x: x[0], reverse=True)

    candidates: list[tuple[float, str]] = []
    for _, doc in ranked_docs[:4]:
        for raw_sentence in re.split(r"(?<=[.!?])\s+", doc.content):
            sentence = re.sub(r"\s+", " ", raw_sentence).strip()
            if len(sentence) < 35 or len(sentence) > 260:
                continue
            norm_sentence = _norm_text(sentence)
            if any(a in norm_sentence for a in navigation_artifacts):
                continue
            overlap = len(q_tokens & set(_tokenize(norm_sentence)))
            if overlap <= 0:
                continue
            # Keep only clearly relevant snippets.
            density = overlap / max(1, len(q_tokens))
            if overlap == 1 and density < 0.20:
                continue
            # Prefer practical/actionable language for procedural questions.
            action_bonus = 0.3 if any(w in norm_sentence for w in ("must", "required", "request", "apply", "need", "documents")) else 0.0
            score = overlap + density + action_bonus
            candidates.append((score, sentence))

    if not candidates:
        return "I cannot answer right now."

    candidates.sort(key=lambda x: x[0], reverse=True)
    picked: list[str] = []
    seen = set()
    for _, sentence in candidates:
        key = _norm_text(sentence)
        if key in seen:
            continue
        seen.add(key)
        picked.append(sentence)
        if len(picked) == 3:
            break

    if not picked:
        return "I cannot answer right now."
    if len(picked) == 1:
        return picked[0]
    return "Based on Bocconi information, here is the most relevant guidance:\n- " + "\n- ".join(picked)


def _is_procedural_question(question: str) -> bool:
    q = _norm_text(question)
    procedural_starters = (
        "how do i",
        "how can i",
        "what should i do",
        "steps to",
        "how to",
        "what do i need",
        "which documents do i need",
    )
    intent_terms = (
        "codice fiscale",
        "fiscal code",
        "visa",
        "permit of stay",
        "permesso",
        "ssn",
        "health service",
        "housing application",
        "exchange",
        "double degree",
        "internship",
        "career services",
    )
    return any(s in q for s in procedural_starters) or any(t in q for t in intent_terms)


def _should_use_procedural_override(question: str) -> bool:
    q = _norm_text(question)
    # Keep deterministic mode for operational "how-to" tasks, but avoid
    # overriding complex comparative/multi-constraint prompts where LLM synthesis is better.
    complex_markers = (
        "compare",
        "difference",
        "vs",
        "versus",
        "pros and cons",
        "timeline and",
        "covering",
        "checklist covering",
        "main differences",
        "what are the differences",
    )
    if any(m in q for m in complex_markers):
        return False
    if _has_unavailability_guard(question) and _is_exact_deadline_question(question):
        return False
    # Multi-intent questions should go through full RAG synthesis.
    if _is_multi_intent_question(question):
        return False
    return _is_procedural_question(question)


def _contains_concrete_date(text: str) -> bool:
    t = _norm_text(text)
    # Supports common date shapes: YYYY-MM-DD, DD/MM/YYYY, and month-name forms.
    if re.search(r"\b20\d{2}-\d{2}-\d{2}\b", t):
        return True
    if re.search(r"\b\d{1,2}/\d{1,2}/20\d{2}\b", t):
        return True
    if re.search(
        r"\b\d{1,2}\s+(january|february|march|april|may|june|july|august|september|october|november|december)\b",
        t,
    ):
        return True
    return False


def _build_procedural_answer(question: str, docs: list[Document], verticale: Verticale) -> str | None:
    q_tokens = {t for t in _tokenize(_norm_text(question)) if len(t) >= 3}
    if not q_tokens or not docs:
        return None

    action_terms = (
        "must",
        "required",
        "request",
        "apply",
        "need",
        "submit",
        "register",
        "book",
        "contact",
        "complete",
        "confirm",
        "follow",
    )
    noise_terms = (
        "library",
        "learning how to search",
        "website navigation",
        "breadcrumb",
        "click here",
    )
    qn = _norm_text(question)
    required_terms: set[str] = set()
    if any(t in qn for t in ("codice fiscale", "fiscal code", "tax code")):
        required_terms |= {"fiscal", "codice", "tax", "immigration", "permit", "visa"}
    if "exchange" in qn or "study abroad" in qn:
        required_terms |= {"exchange", "application", "deadline", "gpa", "credits", "mobility"}
    if any(t in qn for t in ("internship", "tirocinio")):
        required_terms |= {"internship", "career services", "activation", "activate", "documents", "training"}
    internship_curricular_focus = "internship" in qn and any(t in qn for t in ("curricular", "credits", "credit"))
    internship_invalidity_focus = "internship" in qn and any(t in qn for t in ("invalid", "mistake", "invalid for credits"))
    msc_focus = any(t in qn for t in ("msc", "master of science", "graduate student", "first-year msc", "first year msc"))
    if any(t in qn for t in ("visa", "permit of stay", "permesso")):
        required_terms |= {"visa", "permit", "immigration", "documents", "required"}
    if any(t in qn for t in ("housing", "accommodation", "residence", "alloggio")):
        required_terms |= {"housing", "residence", "application", "requirements", "documents"}

    def _clean_sentence(s: str) -> str:
        s = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1", s)
        s = re.sub(r"#+\s*", "", s)
        s = re.sub(r"\*\*+", "", s)
        s = re.sub(r"_+", "", s)
        s = re.sub(r"ATTENTION PLEASE!?", "", s, flags=re.IGNORECASE)
        s = re.sub(r"Welcome activities.*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"useful tips.*$", "", s, flags=re.IGNORECASE)
        s = re.sub(r"\s+", " ", s).strip(" -:;")
        # Drop dangling tails that are usually nav/promo leftovers.
        if s.lower().endswith(("through the", "on the", "to the", "from the")):
            s = ""
        # Keep only complete-looking statements.
        if s and not re.search(r"[.!?]$", s):
            # Accept imperative-like bullets, reject obviously truncated fragments.
            if len(s.split()) > 18:
                s = ""
        return s

    working_docs = list(docs[:6])
    if internship_curricular_focus:
        filtered_docs = []
        for d in working_docs:
            path_l = _norm_text(d.path)
            if any(x in path_l for x in ("extracurricular", "after-graduation", "switch-curricular-extracurricular")):
                continue
            filtered_docs.append(d)
        if filtered_docs:
            working_docs = filtered_docs

    candidates: list[tuple[float, str]] = []
    for doc in working_docs[:5]:
        for raw in re.split(r"(?<=[.!?])\s+", doc.content):
            s = _clean_sentence(raw)
            if len(s) < 40 or len(s) > 240:
                continue
            ns = _norm_text(s)
            if any(n in ns for n in noise_terms):
                continue
            if required_terms and not any(t in ns for t in required_terms):
                continue
            if internship_curricular_focus and "extracurricular" in ns:
                continue
            if internship_curricular_focus and "after graduation" in ns:
                continue
            if internship_curricular_focus and "after the awarding of the degree" in ns:
                continue
            if internship_curricular_focus and "lombardy" in ns and "curricular" in ns:
                continue
            if msc_focus and "bsc students" in ns:
                continue
            if msc_focus and "law students" in ns:
                continue
            overlap = len(q_tokens & set(_tokenize(ns)))
            if overlap <= 0:
                continue
            action_bonus = 0.8 if any(a in ns for a in action_terms) else 0.0
            curricular_bonus = 0.6 if internship_curricular_focus and any(
                t in ns for t in ("curricular", "credits", "activation", "before start", "signature", "required")
            ) else 0.0
            invalidity_bonus = 0.6 if internship_invalidity_focus and any(
                t in ns for t in ("invalid", "not valid", "must", "required", "before start", "signature", "recognition")
            ) else 0.0
            score = overlap + action_bonus + curricular_bonus + invalidity_bonus
            if score < 1.8:
                continue
            candidates.append((score, s))

    if not candidates:
        return None

    candidates.sort(key=lambda x: x[0], reverse=True)
    picked: list[str] = []
    seen = set()
    for _, s in candidates:
        key = _norm_text(s)
        if key in seen:
            continue
        seen.add(key)
        picked.append(s)
        if len(picked) >= 4:
            break

    # Ensure at least 3 practical bullets when possible.
    if len(picked) < 3:
        for doc in working_docs[:3]:
            for raw in re.split(r"(?<=[.!?])\s+", doc.content):
                s = _clean_sentence(raw)
                if len(s) < 35 or len(s) > 220:
                    continue
                ns = _norm_text(s)
                if any(n in ns for n in noise_terms):
                    continue
                if required_terms and not any(t in ns for t in required_terms):
                    continue
                if internship_curricular_focus and "extracurricular" in ns:
                    continue
                if internship_curricular_focus and "after graduation" in ns:
                    continue
                if internship_curricular_focus and "after the awarding of the degree" in ns:
                    continue
                if internship_curricular_focus and "lombardy" in ns and "curricular" in ns:
                    continue
                if msc_focus and "bsc students" in ns:
                    continue
                if msc_focus and "law students" in ns:
                    continue
                if not any(a in ns for a in action_terms):
                    continue
                key = _norm_text(s)
                if key in seen:
                    continue
                seen.add(key)
                picked.append(s)
                if len(picked) >= 3:
                    break
            if len(picked) >= 3:
                break

    if not picked:
        return None

    intro = "Here is the recommended process based on Bocconi information:"
    if any(t in _norm_text(question) for t in ("codice fiscale", "fiscal code", "tax code")):
        intro = "To get your Codice Fiscale, follow this practical sequence:"
    return intro + "\n- " + "\n- ".join(picked)


def _answer_fails_quality_gate(question: str, answer: str) -> bool:
    q = _norm_text(question)
    a = _norm_text(answer)
    if not a or len(a) < 25:
        return True

    # Common UX artifacts / noisy fragments we don't want to show.
    bad_fragments = (
        "no structured safety field returned",
        "website navigation",
        "learning how to search",
        "breadcrumb",
        "click here",
    )
    if any(b in a for b in bad_fragments):
        return True

    # If the question is clearly about one admin topic, suppress obvious cross-topic leakage.
    if any(t in q for t in ("codice fiscale", "fiscal code", "tax code")):
        if "library" in a and "library" not in q:
            return True
        if "sport membership" in a and "sport" not in q:
            return True

    # Minimal lexical grounding check: answer should share enough content words with question.
    q_tokens = {t for t in _tokenize(q) if len(t) >= 4 and t not in GENERIC_STOPWORDS}
    a_tokens = {t for t in _tokenize(a) if len(t) >= 4 and t not in GENERIC_STOPWORDS}
    if q_tokens:
        overlap = len(q_tokens & a_tokens) / max(1, len(q_tokens))
        if overlap < 0.18:
            return True
    return False


def _filter_sources_for_procedural_question(question: str, docs: list[Document], sources: list[str]) -> list[str]:
    qn = _norm_text(question)
    required_terms: set[str] = set()
    if any(t in qn for t in ("codice fiscale", "fiscal code", "tax code")):
        required_terms |= {"fiscal", "codice", "tax", "immigration", "permit", "visa", "documents"}
    if "exchange" in qn or "study abroad" in qn:
        required_terms |= {"exchange", "application", "deadline", "mobility", "credits", "gpa"}
    if any(t in qn for t in ("internship", "tirocinio")):
        required_terms |= {"internship", "career", "activation", "documents", "training"}
    if not required_terms:
        return sources

    doc_by_path = {d.path: d for d in docs}
    kept: list[str] = []
    internship_curricular_focus = "internship" in qn and any(t in qn for t in ("curricular", "credits", "credit"))
    for s in sources:
        d = doc_by_path.get(s)
        hay = _norm_text(s if d is None else f"{d.path} {d.title} {d.content[:400]}")
        if internship_curricular_focus and any(x in hay for x in ("extracurricular", "after-graduation", "switch-curricular-extracurricular")):
            continue
        if any(t in hay for t in required_terms):
            kept.append(s)
    return kept or sources


CARD_SKIP_RE = re.compile(
    r"cannot answer|cannot fetch|don't have reliable|do not have reliable|"
    r"i don't know|outside the assistant scope|outside bocconi buddy scope|"
    r"insufficient|not enough context",
    re.IGNORECASE,
)

CARD_TITLE_STOPWORDS = {
    "what",
    "when",
    "where",
    "which",
    "about",
    "does",
    "with",
    "from",
    "into",
    "bocconi",
    "should",
    "could",
    "would",
    "there",
    "their",
    "after",
    "before",
    "student",
    "students",
}


def _canonical_card_terms(text: str) -> set[str]:
    terms = set()
    for token in _tokenize(text):
        if token in CARD_TITLE_STOPWORDS or len(token) < 3:
            continue
        if token.endswith("ies") and len(token) > 5:
            token = token[:-3] + "y"
        elif token.endswith("s") and len(token) > 4:
            token = token[:-1]
        terms.add(token)
    return terms


def _card_term_similarity(left: str | list[str], right: str | list[str]) -> float:
    left_text = " ".join(left) if isinstance(left, list) else left
    right_text = " ".join(right) if isinstance(right, list) else right
    left_terms = _canonical_card_terms(left_text)
    right_terms = _canonical_card_terms(right_text)
    if not left_terms or not right_terms:
        return 0.0
    return len(left_terms & right_terms) / len(left_terms | right_terms)


def _answer_can_unlock_card(answer: str) -> bool:
    compact = re.sub(r"\s+", " ", answer).strip()
    if len(compact) < 40:
        return False
    return CARD_SKIP_RE.search(compact) is None


def _next_card_id(verticale: Verticale, cards: list[BocCard]) -> str:
    vertical_number = VERTICALI.index(verticale) + 1
    prefix = f"V{vertical_number:02d}"
    existing_numbers = [0]
    pattern = re.compile(rf"^{prefix}-(\d+)$")
    for card in cards:
        if card.vertical != verticale:
            continue
        match = pattern.match(card.id)
        if match:
            existing_numbers.append(int(match.group(1)))
    return f"{prefix}-{max(existing_numbers) + 1:03d}"


def _make_card_title(question: str) -> str:
    words = [
        token
        for token in _tokenize(question)
        if len(token) > 2 and token not in CARD_TITLE_STOPWORDS
    ][:6]
    title = " ".join(words) if words else "Campus Discovery"
    return title.title()


def _trim_to_card_body(text: str, max_length: int = 270) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if len(compact) <= max_length:
        return compact
    clipped = compact[: max_length - 1]
    sentence_end = max(clipped.rfind("."), clipped.rfind(";"))
    if sentence_end > 120:
        return clipped[: sentence_end + 1]
    word_end = clipped.rfind(" ")
    return f"{clipped[:word_end if word_end > 80 else len(clipped)]}..."


def _extract_card_fact(answer: str) -> str:
    match = re.search(
        r"(~?€\s?[\d,.]+|[\d,.]+\s?(?:%|ECTS|countries|partners|months|weeks|hours|days))",
        answer,
        re.IGNORECASE,
    )
    return match.group(0) if match else "2026 snapshot"


def _short_card_source(source: str) -> str:
    clean = source.split("/")[-1].removesuffix(".md")
    return re.sub(r"[-_]+", " ", clean or source)[:42]


CARD_IMAGE_MARKDOWN_RE = re.compile(r"!\[([^\]]*)\]\((https?://[^)\s]+)\)", re.IGNORECASE)
CARD_IMAGE_URL_RE = re.compile(
    r"https?://[^\s)\"']+\.(?:jpg|jpeg|png|webp)(?:\?[^\s)\"']+)?",
    re.IGNORECASE,
)
CARD_IMAGE_NOISE_TERMS = {
    "logo",
    "icon",
    "flag",
    "mapfiles",
    "property-pin",
    "university-pin",
    "facebook",
    "twitter",
    "linkedin",
    "youtube",
    "validatore",
    "telefono",
}
CARD_FALLBACK_IMAGES: dict[Verticale, tuple[str, str, CardImageSource]] = {
    "relocation": (
        "https://www.unibocconi.it/sites/default/files/styles/fullwidth_xxl/public/media/images/piazza_gae_aulenti_0.jpg.webp?itok=m2__eciS",
        "Piazza Gae Aulenti in Milan",
        "dataset",
    ),
    "life_on_campus": (
        "https://www.unibocconi.it/sites/default/files/styles/link_card/public/media/images/studenti_4.jpg.webp?itok=gdH9KVpy",
        "Bocconi students gathered on campus",
        "dataset",
    ),
    "study_abroad": (
        "https://www.unibocconi.it/sites/default/files/styles/fullwidth_xxl/public/media/images/_mg_7245_0_0.jpg.webp?itok=bsdBRWTs",
        "Bocconi international mobility students",
        "dataset",
    ),
    "career_readiness": (
        "https://www.unibocconi.it/sites/default/files/styles/highlight_slide/public/media/images/ipp-402.jpg.webp?itok=q3qk9oCr",
        "BocconiJobs career event",
        "dataset",
    ),
}


def _resolve_data_source_path(source: str) -> Path | None:
    if source.startswith(("http://", "https://")):
        return None

    raw_path = Path(source)
    candidates = []
    if raw_path.is_absolute():
        candidates.append(raw_path)
    else:
        parts = raw_path.parts
        if parts and parts[0] == "backend":
            candidates.append(Path(__file__).parent.parent / raw_path)
        elif parts and parts[0] == "data":
            candidates.append(Path(__file__).parent / raw_path)
        else:
            candidates.append(DATA_DIR / raw_path)

    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            resolved.relative_to(DATA_DIR.resolve())
        except (OSError, ValueError):
            continue
        if resolved.is_file():
            return resolved
    return None


def _is_image_url(url: str) -> bool:
    lower = urllib.parse.unquote(url.lower())
    return any(ext in lower for ext in (".jpg", ".jpeg", ".png", ".webp"))


def _card_image_score(url: str, alt: str, question: str, verticale: Verticale) -> int:
    candidate_text = _norm_text(f"{urllib.parse.unquote(url)} {alt}")
    if any(term in candidate_text for term in CARD_IMAGE_NOISE_TERMS):
        return -100

    query_terms = _canonical_card_terms(question)
    score = 0
    score += sum(2 for term in query_terms if term in candidate_text)
    if "bocconi" in candidate_text or "unibocconi" in candidate_text:
        score += 4
    if any(term in candidate_text for term in ("student", "studenti", "campus", "milano", "milan")):
        score += 2
    if any(term in candidate_text for term in ("fullwidth", "highlight", "container_100", "link_card", "image_compression")):
        score += 1
    if verticale == "relocation" and any(term in candidate_text for term in ("milan", "milano", "housing", "casa", "trasport")):
        score += 2
    if verticale == "life_on_campus" and any(term in candidate_text for term in ("campus", "student", "sport", "library", "wellbeing")):
        score += 2
    if verticale == "study_abroad" and any(term in candidate_text for term in ("international", "exchange", "abroad", "mobility")):
        score += 2
    if verticale == "career_readiness" and any(term in candidate_text for term in ("career", "job", "faculty", "alumni", "internship")):
        score += 2
    return score


def _extract_dataset_image_from_sources(
    sources: list[str],
    question: str,
    verticale: Verticale,
) -> tuple[str, str, CardImageSource] | None:
    candidates: list[tuple[int, str, str]] = []

    for source in sources[:5]:
        if source.startswith(("http://", "https://")) and _is_image_url(source):
            candidates.append((_card_image_score(source, "", question, verticale), source, "Card source image"))
            continue

        source_path = _resolve_data_source_path(source)
        if source_path is None:
            continue
        try:
            raw = source_path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue

        seen: set[str] = set()
        for match in CARD_IMAGE_MARKDOWN_RE.finditer(raw):
            alt, url = match.group(1).strip(), match.group(2).strip()
            if url in seen or not _is_image_url(url):
                continue
            seen.add(url)
            candidates.append((_card_image_score(url, alt, question, verticale), url, alt or "Bocconi source image"))

        for match in CARD_IMAGE_URL_RE.finditer(raw):
            url = match.group(0).strip()
            if url in seen or not _is_image_url(url):
                continue
            seen.add(url)
            candidates.append((_card_image_score(url, "", question, verticale), url, "Bocconi source image"))

    if not candidates:
        return None

    score, url, alt = max(candidates, key=lambda item: item[0])
    if score < 0:
        return None
    return url, alt, "dataset"


def _fallback_card_image(verticale: Verticale, title: str) -> tuple[str | None, str | None, CardImageSource]:
    fallback = CARD_FALLBACK_IMAGES.get(verticale)
    if fallback:
        url, alt, source = fallback
        return url, f"{alt} for {title}", source
    return None, None, "placeholder"


def _score_card_rarity(question: str, answer: str, sources: list[str]) -> tuple[Rarity, int]:
    """Score rarity from observable card qualities.

    Rarity is intentionally deterministic so the collection feels earned:
    live/current sources, multiple sources, concrete numbers, specific Bocconi
    terms, procedural depth, and longer grounded answers all add weight.
    """
    q = _norm_text(question)
    a = _norm_text(answer)
    source_text = _norm_text(" ".join(sources))
    score = 0

    if any(source.startswith(("http://", "https://")) for source in sources):
        score += 3
    if len(set(sources)) >= 2:
        score += 1
    if len(set(sources)) >= 4:
        score += 1
    if re.search(r"(~?€\s?[\d,.]+|[\d,.]+\s?(?:%|ects|countries|partners|months|weeks|hours|days))", answer, re.IGNORECASE):
        score += 2
    if any(
        term in f"{q} {a}"
        for term in (
            "codice fiscale",
            "permit of stay",
            "jobgate",
            "erasmus",
            "double degree",
            "almalaurea",
            "viaggiaresicuri",
            "bocconi sport",
            "ssn",
        )
    ):
        score += 1
    if answer.count("\n- ") >= 3 or sum(1 for word in ("apply", "request", "submit", "register", "deadline") if word in a) >= 2:
        score += 1
    if len(answer) >= 520:
        score += 1
    if any(term in source_text for term in ("almalaurea", "viaggiaresicuri", "comune", "yesmilano", "gtfs")):
        score += 1

    if score >= 8:
        return "ultra-rare", score
    if score >= 5:
        return "rare", score
    if score >= 2:
        return "uncommon", score
    return "common", score


def _build_unlocked_card(request: CardDecisionRequest) -> BocCard | None:
    answer = request.answer.strip()
    if not _answer_can_unlock_card(answer):
        return None

    source = request.sources[0] if request.sources else "Bocconi 2026 dataset"
    has_live_source = any(source.startswith(("http://", "https://")) for source in request.sources)
    tags = list(dict.fromkeys(_canonical_card_terms(request.question)))[:6]
    rarity, _rarity_score = _score_card_rarity(request.question, answer, request.sources)
    title = _make_card_title(request.question)
    image_url, image_alt, image_source = _extract_dataset_image_from_sources(
        request.sources,
        request.question,
        request.verticale,
    ) or _fallback_card_image(request.verticale, title)

    return BocCard(
        id=_next_card_id(request.verticale, request.cards),
        vertical=request.verticale,
        title=title,
        body=_trim_to_card_body(answer),
        longBody=answer,
        factTag=_extract_card_fact(answer),
        imageQuery=request.question,
        imageUrl=image_url,
        imageAlt=image_alt,
        imageSource=image_source,
        rarity=rarity,
        isStarter=False,
        sourceLabel=_short_card_source(source),
        datasetSnapshot="live" if has_live_source else "2026-05-02",
        unlockedAt=datetime.now(UTC).isoformat(),
        tags=tags,
    )


def _find_owned_duplicate(candidate: BocCard, cards: list[BocCard], collected_ids: list[str]) -> BocCard | None:
    collected = set(collected_ids)
    best: tuple[float, BocCard] | None = None

    for card in cards:
        if card.id not in collected or card.vertical != candidate.vertical:
            continue

        title_score = _card_term_similarity(card.title, candidate.title)
        tag_score = _card_term_similarity(card.tags, candidate.tags)
        duplicate_score = title_score * 0.75 + tag_score * 0.25
        is_duplicate = (
            title_score >= 0.72
            or (title_score >= 0.45 and tag_score >= 0.45)
            or (title_score >= 0.20 and tag_score >= 0.62)
            or duplicate_score >= 0.56
        )
        if not is_duplicate:
            continue
        if best is None or duplicate_score > best[0]:
            best = (duplicate_score, card)

    return best[1] if best else None


_build_local_index()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/cards/decision", response_model=CardDecisionResponse)
def decide_card(request: CardDecisionRequest) -> CardDecisionResponse:
    candidate = _build_unlocked_card(request)
    if candidate is None:
        return CardDecisionResponse(
            action="skipped",
            message="No new card was created for this answer.",
        )

    duplicate = _find_owned_duplicate(candidate, request.cards, request.collected_card_ids)
    if duplicate is not None:
        return CardDecisionResponse(
            action="already_owned",
            card=duplicate,
            existing_card_id=duplicate.id,
            message=f"You already have this card: {duplicate.title}.",
        )

    return CardDecisionResponse(
        action="created",
        card=candidate,
        message=f"New card added to your collection: {candidate.title}.",
    )


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    question = (request.question or "").strip()
    if not question:
        return AskResponse(
            answer="I cannot answer right now.",
            sources=[],
            verticale="relocation",
        )

    if _is_high_risk_live_fact_question(question):
        return AskResponse(
            answer=(
                "I cannot answer right now with exact live weather values because forecasts change continuously. "
                "Please check a live official forecast source for the exact hour."
            ),
            sources=[],
            verticale="relocation",
        )

    if _is_weather_question(question):
        weather = _fetch_milan_weather_answer(question)
        if weather is not None:
            answer, weather_sources = weather
            return AskResponse(answer=answer, sources=weather_sources, verticale="relocation")
        return AskResponse(
            answer="I cannot fetch live weather data right now.",
            sources=[],
            verticale="relocation",
        )

    if _is_transport_question(question):
        gtfs = _fetch_gtfs_transport_answer(question)
        if gtfs is not None:
            answer, gtfs_sources = gtfs
            return AskResponse(answer=answer, sources=gtfs_sources, verticale="relocation")
        return AskResponse(
            answer=(
                "I cannot fetch live GTFS data right now, but you can use Milano's official transport sources: "
                "the GTFS dataset page, direct GTFS zip, and ATM journey/ticket pages."
            ),
            sources=[
                "https://dati.comune.milano.it/dataset/ds929-orari-del-trasporto-pubblico-locale-nel-comune-di-milano-in-formato-gtfs",
                "https://dati.comune.milano.it/gtfs.zip",
                "https://www.atm.it/en/ViaggiaConNoi/Pages/default.aspx",
            ],
            verticale="relocation",
        )

    if _is_events_question(question):
        events = _fetch_milano_events_answer(question)
        if events is not None:
            answer, event_sources = events
            return AskResponse(answer=answer, sources=event_sources, verticale="life_on_campus")
        return AskResponse(
            answer=(
                "I cannot fetch the live events feed right now. "
                "For current public events in Milano, use YesMilano official calendars "
                "(all events, weekend, and student community pages)."
            ),
            sources=[
                "https://www.yesmilano.it/en/whats-on/all-events",
                "https://studyandwork.yesmilano.it/en/events-study",
                "https://www.yesmilano.it/welcomestudents",
            ],
            verticale="life_on_campus",
        )

    safety: tuple[str, list[str]] | None = None
    is_multi_intent = _is_multi_intent_question(question)
    if _is_travel_safety_question(question):
        safety = _fetch_viaggiaresicuri_answer(question)
        # Preserve the fast path only for clearly single-intent safety asks.
        if safety is not None and not is_multi_intent:
            answer, safety_sources = safety
            return AskResponse(answer=answer, sources=safety_sources, verticale="study_abroad")

    verticale = _classify_verticale(question)
    docs = _retrieve_docs(question, verticale, k=TOP_K)
    sources = [d.path for d in docs]
    overlap_score = _retrieval_overlap_score(question, docs)
    coverage_score = _retrieval_coverage_score(question, docs)

    if not docs:
        search_hits = _invoke_search_agent(question, verticale=verticale)
        if search_hits:
            source_ids = [f"trusted_source:{sid}" for sid, _, _ in search_hits]
            answer = (
                "I cannot answer from the current knowledge base yet. "
                "Trusted sources to add for this topic: "
                + "; ".join(url for _, _, url in search_hits)
            )
            return AskResponse(answer=answer, sources=source_ids, verticale=verticale)
        return AskResponse(
            answer="I cannot answer right now.",
            sources=[],
            verticale=verticale,
        )

    if _is_out_of_domain_question(question):
        search_hits = _invoke_search_agent(question, verticale=verticale)
        if search_hits:
            source_ids = [f"trusted_source:{sid}" for sid, _, _ in search_hits]
            answer = (
                "This question is outside Bocconi Buddy scope. "
                "Trusted sources you can consult: "
                + "; ".join(url for _, _, url in search_hits)
            )
            return AskResponse(answer=answer, sources=source_ids, verticale=verticale)
        return AskResponse(
            answer="I cannot answer this question because it is outside the assistant scope.",
            sources=[],
            verticale=verticale,
        )

    if overlap_score < MIN_SCOPE_OVERLAP or coverage_score < MIN_SCOPE_COVERAGE:
        search_hits = _invoke_search_agent(question, verticale=verticale)
        if search_hits:
            source_ids = [f"trusted_source:{sid}" for sid, _, _ in search_hits]
            answer = (
                "This question appears outside the currently indexed data. "
                "Trusted sources to cover it: "
                + "; ".join(url for _, _, url in search_hits)
            )
            return AskResponse(answer=answer, sources=source_ids, verticale=verticale)

    messages = _build_prompt(question, verticale, docs)
    answer = _call_llm_with_retry(messages)
    if not answer:
        answer = _extractive_fallback_answer(question, docs)

    if _has_unavailability_guard(question) and _is_exact_deadline_question(question):
        if (not _supports_entity_in_docs(question, docs)) or (not _contains_concrete_date(answer)):
            answer = (
                "I cannot answer right now because the exact deadline is not available in the indexed data."
            )
            sources = [d.path for d in docs[:3]]

    procedural_answer = None
    if _should_use_procedural_override(question):
        procedural_answer = _build_procedural_answer(question, docs, verticale)

    # Prefer deterministic procedural rendering when available to avoid noisy mixed fragments.
    if procedural_answer:
        answer = procedural_answer
        sources = _filter_sources_for_procedural_question(question, docs, sources)

    if safety is not None and is_multi_intent:
        safety_answer, safety_sources = safety
        safety_note = safety_answer.strip()
        # Keep this concise so the main RAG answer remains primary.
        if len(safety_note) > 260:
            safety_note = safety_note[:260].rstrip() + "..."
        answer = f"{answer}\n\nTravel safety note: {safety_note}"
        sources = list(dict.fromkeys(sources + safety_sources))

    if _answer_fails_quality_gate(question, answer):
        recovered = _build_procedural_answer(question, docs, verticale)
        if not recovered:
            recovered = _extractive_fallback_answer(question, docs)
        answer = recovered

    return AskResponse(answer=answer, sources=sources, verticale=verticale)
