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
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
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


def _intent_adjustment(question: str, verticale: Verticale, doc: Document) -> float:
    q = _norm_text(question)
    doc_key = _norm_text(doc.path + " " + doc.title)
    if verticale == "relocation" and any(t in q for t in ("required documents", "documents", "first steps", "moving to milan")):
        if "external-relocation-required-documents-checklist" in doc_key:
            return 5.0
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


def _retrieve_docs(question: str, verticale: Verticale, k: int = TOP_K) -> list[Document]:
    rewritten_query = _expand_query(question, verticale)
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
    filtered = [d for _, d in rescored if _intent_doc_allowed(question, verticale, d)]
    if filtered:
        return filtered[:k]
    return [d for _, d in rescored[:k]]


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


def _is_transport_question(question: str) -> bool:
    q = _norm_text(question)
    terms = ("metro", "tram", "bus", "atm", "transport", "commute", "line ", "ticket", "pass", "gtfs")
    transport_hit = any(t in q for t in terms)
    # Keep transport API path for transport-dominant questions only.
    broader_relocation_terms = ("housing", "residence", "rent", "documents", "visa", "permit", "alloggio")
    broader_hit = any(t in q for t in broader_relocation_terms)
    return transport_hit and not broader_hit


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
        context_blocks.append(
            (
                f"[DOC {i}] path={d.path}\n"
                f"title={d.title}\n"
                f"verticale={d.verticale}\n"
                f"content:\n{d.content}"
            )
        )

    system = (
        "You are Bocconi AI Buddy. Answer ONLY using the provided context. "
        "If context is insufficient, say you cannot answer right now and ask for a clearer question. "
        "Be concise, factual, and avoid hallucinations. "
        "Keep original Bocconi terms and names. "
        "Reply in the same language as the user question when possible. "
        "Do not include website navigation artifacts or markdown boilerplate."
    )
    answer_shape = (
        "Answer format:\n"
        "1) Short answer (1-2 sentences)\n"
        "2) Recommended path at Bocconi (3-5 bullet points)\n"
        "3) Concrete next actions this month (2-4 bullet points)\n"
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
    q_tokens = set(_tokenize(question))
    if not q_tokens:
        q_tokens = set(_tokenize(_norm_text(question)))

    candidates: list[tuple[int, str]] = []
    for doc in docs[:4]:
        for raw_sentence in re.split(r"(?<=[.!?])\s+", doc.content):
            sentence = raw_sentence.strip()
            if len(sentence) < 50 or len(sentence) > 280:
                continue
            norm_sentence = _norm_text(sentence)
            overlap = sum(1 for t in q_tokens if t in norm_sentence)
            if overlap > 0:
                candidates.append((overlap, sentence))

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

    return " ".join(picked) if picked else "I cannot answer right now."


_build_local_index()


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/ask", response_model=AskResponse)
def ask(request: AskRequest) -> AskResponse:
    question = (request.question or "").strip()
    if not question:
        return AskResponse(
            answer="I cannot answer right now.",
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

    if _is_travel_safety_question(question):
        safety = _fetch_viaggiaresicuri_answer(question)
        if safety is not None:
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

    return AskResponse(answer=answer, sources=sources, verticale=verticale)
