import json
import statistics
import time
import sys
from urllib import request

sys.path.insert(0, '/app')
import main

questions = [
    'What is the price of an annual ATM transit pass for students under 27 in Milan, and how does it compare with the standard adult annual urban pass?',
    "List the steps an international student must follow to register with Italy's National Health Service (SSN) in Milan.",
    "Confrontando i bus diretti da Malpensa a Milano Centrale tra i vettori documentati (Autostradale/Malpensa Bus Express, Terravision, Flibco, FlixBus): qual e' il prezzo di partenza piu' basso indicato per ciascun vettore?",
    "Quale documento devo portare per accedere alla Biblioteca Bocconi, e qual e' la capienza massima dell'edificio?",
    'Provide a structured table of the dining areas available on the Bocconi campus, indicating the location of each and its meal/opening pattern.',
    'For the Bocconi MSc graduate Exchange Program selection score, how are academic GPA, credits, and Bachelor degree grade weighted? Show the weights and explain.',
    'What is the application deadline for the Bocconi Double Degree program with MIT (Massachusetts Institute of Technology)?',
    'What is the maximum amount of the Bocconi Merit Award tuition waiver for graduate (Master of Science) students, and what is the format of the award (e.g. tuition waiver only, tuition waiver plus stipend, etc.)?',
    'What are the placement results published in the 2026 BESS graduate survey?',
    'How many different paid Bocconi Sport Membership tiers are listed for the 2025/2026 season, and which is the cheapest one available to UB and SDA Bocconi students?',
]


def post_ask(question: str) -> tuple[int, dict]:
    payload = json.dumps({'question': question}).encode('utf-8')
    req = request.Request(
        'http://127.0.0.1:8000/ask',
        data=payload,
        headers={'Content-Type': 'application/json'},
        method='POST',
    )
    with request.urlopen(req, timeout=30) as resp:
        status = resp.getcode()
        body = resp.read().decode('utf-8')
        data = json.loads(body)
        return status, data


rows = []
for q in questions:
    t0 = time.perf_counter()
    status, data = post_ask(q)
    ms = (time.perf_counter() - t0) * 1000
    answer = data.get('answer') or ''
    rows.append(
        {
            'q': q,
            'status': status,
            'ms': ms,
            'answer_len': len(answer),
            'sources_count': len(data.get('sources') or []),
            'verticale': data.get('verticale'),
            'abstain': ('cannot answer' in answer.lower()) or ('i cannot' in answer.lower()) or ("don't know" in answer.lower()),
            'top_source': (data.get('sources') or [None])[0],
        }
    )

diag = []
for q in questions:
    v = main._classify_verticale(q)
    rq = main._rewrite_query(q, v)
    qn = main._norm_text(rq)
    qt = set(main._tokenize(qn))
    docs = main.DOCS_BY_VERTICALE.get(v, [])

    t0 = time.perf_counter()
    kw = sorted(docs, key=lambda d: main._keyword_score(d, qt, qn), reverse=True)
    kw = [d for d in kw[:40] if main._keyword_score(d, qt, qn) > 0]
    kw_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    sem = sorted(docs, key=lambda d: main._semantic_lite_score(d, qt, qn), reverse=True)
    sem = [d for d in sem[:40] if main._semantic_lite_score(d, qt, qn) > 0]
    sem_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    _ = main._rrf_fuse([kw, sem], k=max(main.TOP_K * 3, 10))
    fuse_ms = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    final = main._retrieve_docs(q, v, k=main.TOP_K)
    ret_ms = (time.perf_counter() - t0) * 1000

    diag.append(
        {
            'q': q,
            'v': v,
            'kw_ms': kw_ms,
            'sem_ms': sem_ms,
            'fuse_ms': fuse_ms,
            'ret_ms': ret_ms,
            'kw_top': kw[0].path if kw else None,
            'sem_top': sem[0].path if sem else None,
            'hybrid_top': final[0].path if final else None,
            'coverage': main._retrieval_coverage_score(q, final),
            'overlap': main._retrieval_overlap_score(q, final),
        }
    )

all_docs = [d for vv in main.DOCS_BY_VERTICALE.values() for d in vv]
all_chars = sorted(len(d.content) for d in all_docs)
chunking = {
    'max_chars_per_doc': main.MAX_CHARS_PER_DOC,
    'docs_total': len(all_docs),
    'avg_chars': statistics.mean(len(d.content) for d in all_docs),
    'p95_chars': all_chars[max(0, int(len(all_chars) * 0.95) - 1)],
}

summary = {
    'e2e': {
        'n': len(rows),
        'http_200_rate': sum(1 for r in rows if r['status'] == 200) / len(rows),
        'lat_ms_avg': statistics.mean(r['ms'] for r in rows),
        'lat_ms_p50': statistics.median(r['ms'] for r in rows),
        'lat_ms_p95': sorted([r['ms'] for r in rows])[max(0, int(len(rows) * 0.95) - 1)],
        'lat_ms_max': max(r['ms'] for r in rows),
        'within_30s_rate': sum(1 for r in rows if r['ms'] < 30000) / len(rows),
        'avg_sources': statistics.mean(r['sources_count'] for r in rows),
    },
    'retrieval': {
        'avg_ret_ms': statistics.mean(d['ret_ms'] for d in diag),
        'avg_coverage': statistics.mean(d['coverage'] for d in diag),
        'avg_overlap': statistics.mean(d['overlap'] for d in diag),
    },
    'hybrid': {
        'avg_kw_ms': statistics.mean(d['kw_ms'] for d in diag),
        'avg_sem_ms': statistics.mean(d['sem_ms'] for d in diag),
        'avg_fuse_ms': statistics.mean(d['fuse_ms'] for d in diag),
        'top1_kw_eq_hybrid_rate': sum(1 for d in diag if d['kw_top'] == d['hybrid_top']) / len(diag),
        'top1_sem_eq_hybrid_rate': sum(1 for d in diag if d['sem_top'] == d['hybrid_top']) / len(diag),
    },
    'chunking': chunking,
}

print(json.dumps({'summary': summary, 'e2e_rows': rows, 'diag_rows': diag}, indent=2))
