import json
import re
import time
from urllib import request

cases = [
    {
        "id": "Q1",
        "vertical": "relocation",
        "kind": "normal",
        "q": "I will move to Milan in September. Give me a first-month checklist covering housing contract paperwork, permit-of-stay timing, and student transport cost-saving options.",
        "must": ["housing", "permit", "transport"],
        "forbid": ["library workshop"],
    },
    {
        "id": "Q2",
        "vertical": "relocation",
        "kind": "normal",
        "q": "How do I get a Codice Fiscale as an international Bocconi student, and which documents should I prepare first?",
        "must": ["codice fiscale", "documents", "visa"],
        "forbid": ["library"],
    },
    {
        "id": "Q3",
        "vertical": "life_on_campus",
        "kind": "normal",
        "q": "I’m a new student and want to integrate fast: what are the most relevant campus services in the first month for wellbeing, inclusion, and student associations?",
        "must": ["campus", "wellbeing", "associations"],
        "forbid": ["tax code"],
    },
    {
        "id": "Q4",
        "vertical": "life_on_campus",
        "kind": "normal",
        "q": "Create a practical dining plan for a week on Bocconi campus: which dining areas exist and what usage pattern should I follow between classes?",
        "must": ["dining", "campus", "areas"],
        "forbid": ["double degree"],
    },
    {
        "id": "Q5",
        "vertical": "study_abroad",
        "kind": "normal",
        "q": "I want to compare exchange vs double degree for a Bocconi MSc student: what are the main differences in application flow, academic recognition, and timeline?",
        "must": ["exchange", "double degree", "recognition"],
        "forbid": ["bitcoin"],
    },
    {
        "id": "Q6",
        "vertical": "study_abroad",
        "kind": "normal",
        "q": "I’m selected for exchange in Seoul. What should I do before departure, where do I check travel-safety guidance, and what should I expect for incoming exchange housing support?",
        "must": ["departure", "safety", "housing"],
        "forbid": ["career fair"],
    },
    {
        "id": "Q7",
        "vertical": "career_readiness",
        "kind": "normal",
        "q": "I’m a first-year Bocconi MSc student targeting consulting. What should I do in the next 30 days to activate a curricular internship correctly, and what common mistakes make it invalid for credits?",
        "must": ["internship", "curricular", "credits"],
        "forbid": ["exchange program"],
    },
    {
        "id": "Q8",
        "vertical": "career_readiness",
        "kind": "normal",
        "q": "What are the key placement signals I should track to judge career outcomes for Bocconi MSc students, and where can I find those official sources?",
        "must": ["placement", "career", "sources"],
        "forbid": ["weather"],
    },
    {
        "id": "Q9",
        "vertical": "relocation",
        "kind": "trap",
        "q": "What is the exact live weather forecast in Milan at 5:00 PM tomorrow and should I carry an umbrella to class?",
        "must": ["forecast", "umbrella"],
        "forbid": [],
    },
    {
        "id": "Q10",
        "vertical": "study_abroad",
        "kind": "trap",
        "q": "What is the application deadline for the Bocconi Double Degree program with MIT for this cycle? If unavailable in data, explicitly say so.",
        "must": ["deadline", "double degree"],
        "forbid": [],
    },
]

na_patterns = [
    r"cannot answer right now",
    r"i cannot answer",
    r"don't have this information",
    r"outside the assistant scope",
    r"outside bocconi buddy scope",
]


def is_no_answer(text: str) -> bool:
    t = text.lower()
    return any(re.search(p, t) for p in na_patterns)


def score_case(case, status: int, data: dict):
    ans = data.get("answer") or ""
    ans_l = ans.lower()
    vert = data.get("verticale")

    if status != 200:
        return "wrong", "non-200 status"

    if case["kind"] == "trap":
        if is_no_answer(ans_l) or "not available" in ans_l:
            return "correct", "honest abstention/uncertainty handling"
        if len(ans) > 40 and not is_no_answer(ans_l):
            return "partial", "answered trap directly without clear uncertainty"

    must_hits = sum(1 for m in case["must"] if m.lower() in ans_l)
    forbid_hits = sum(1 for f in case["forbid"] if f.lower() in ans_l)
    source_count = len(data.get("sources") or [])

    if vert != case["vertical"] and case["kind"] != "trap":
        return "wrong", f"vertical mismatch ({vert} != {case['vertical']})"
    if forbid_hits > 0:
        return "wrong", "contains off-topic forbidden content"
    if is_no_answer(ans_l):
        return "no_answer", "abstained"

    if must_hits >= 2 and source_count >= 2:
        return "correct", f"must_hits={must_hits}, sources={source_count}"
    if must_hits >= 1 and source_count >= 1:
        return "partial", f"must_hits={must_hits}, sources={source_count}"
    return "wrong", f"insufficient topical coverage (must_hits={must_hits})"


for _ in range(50):
    try:
        request.urlopen("http://127.0.0.1:8000/health", timeout=2)
        break
    except Exception:
        time.sleep(1)

rows = []
for c in cases:
    req = request.Request(
        "http://127.0.0.1:8000/ask",
        data=json.dumps({"question": c["q"]}).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.perf_counter()
    with request.urlopen(req, timeout=30) as resp:
        status = resp.getcode()
        body = resp.read().decode("utf-8")
    ms = (time.perf_counter() - t0) * 1000
    data = json.loads(body)
    label, reason = score_case(c, status, data)
    rows.append(
        {
            "id": c["id"],
            "vertical_expected": c["vertical"],
            "kind": c["kind"],
            "status": status,
            "latency_ms": round(ms, 1),
            "label": label,
            "reason": reason,
            "vertical_returned": data.get("verticale"),
            "sources": len(data.get("sources") or []),
            "answer_preview": (data.get("answer") or "")[:220],
        }
    )

summary = {k: sum(1 for r in rows if r["label"] == k) for k in ["correct", "partial", "no_answer", "wrong"]}
summary["total"] = len(rows)
summary["avg_latency_ms"] = round(sum(r["latency_ms"] for r in rows) / len(rows), 1)
summary["p95_latency_ms"] = sorted(r["latency_ms"] for r in rows)[int(len(rows) * 0.95) - 1]

print(json.dumps({"summary": summary, "rows": rows}, indent=2))
