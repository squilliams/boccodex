"""Ingest selected external public sources into backend/data.

Adds:
- YesMilano student-relocation pages (as markdown snapshots)
- Comune di Milano GTFS dataset summary (from gtfs.zip)

This script updates backend/data/manifest.json so files are available
to the retrieval pipeline immediately.
"""

from __future__ import annotations

import csv
import datetime as dt
import html
import io
import json
import re
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
RELOCATION_DIR = DATA_DIR / "relocation"
MANIFEST_PATH = DATA_DIR / "manifest.json"
TIMEOUT_SECONDS = 30

YESMILANO_URLS = [
    "https://www.yesmilano.it/en/living-in-milano",
    "https://www.yesmilano.it/en/study/cost-living-milano",
    "https://www.yesmilano.it/en/traveller-information/getting-around",
    "https://www.yesmilano.it/en/neighborhoods",
]
YESMILANO_FALLBACK_BASES = [
    "https://studyandwork.yesmilano.it",
    "https://business.yesmilano.it",
]

GTFS_DATASET_URL = (
    "https://dati.comune.milano.it/dataset/"
    "ds929-orari-del-trasporto-pubblico-locale-nel-comune-di-milano-in-formato-gtfs"
)
GTFS_ZIP_URL = "https://dati.comune.milano.it/gtfs.zip"


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; BocconiBuddyIngest/1.0)",
            "Accept-Language": "en,it;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        raw = resp.read()
    return raw.decode("utf-8", errors="ignore")


def _fetch_yesmilano_with_fallback(url: str) -> tuple[str, str]:
    candidates = [url]
    parsed = urllib.parse.urlsplit(url)
    for base in YESMILANO_FALLBACK_BASES:
        b = urllib.parse.urlsplit(base)
        alt = urllib.parse.urlunsplit((b.scheme, b.netloc, parsed.path, "", ""))
        if alt not in candidates:
            candidates.append(alt)

    last_err: Exception | None = None
    for candidate in candidates:
        try:
            return candidate, _fetch_text(candidate)
        except Exception as err:  # noqa: PERF203
            last_err = err
            continue
    if last_err is not None:
        raise last_err
    raise RuntimeError("Failed to fetch YesMilano page")


def _fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={"User-Agent": "Mozilla/5.0 (compatible; BocconiBuddyIngest/1.0)"},
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read()


def _slug_from_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    joined = f"{parts.netloc}{parts.path}".strip("/")
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", joined.lower()).strip("-")
    return f"{slug}.md"


def _extract_title(raw_html: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", raw_html, flags=re.I | re.S)
    if not m:
        return "Untitled"
    return html.unescape(re.sub(r"\s+", " ", m.group(1))).strip()


def _html_to_text(raw_html: str) -> str:
    text = re.sub(r"(?is)<script.*?>.*?</script>", " ", raw_html)
    text = re.sub(r"(?is)<style.*?>.*?</style>", " ", text)
    text = re.sub(r"(?i)<br\\s*/?>", "\n", text)
    text = re.sub(r"(?i)</p>", "\n\n", text)
    text = re.sub(r"(?i)</h[1-6]>", "\n\n", text)
    text = re.sub(r"(?i)</li>", "\n", text)
    text = re.sub(r"(?is)<[^>]+>", " ", text)
    text = html.unescape(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    return "\n".join(lines)


def _build_frontmatter(
    *,
    title: str,
    source_url: str,
    language: str = "en",
    verticale: str = "relocation",
    token_estimate: int,
) -> str:
    today = dt.date.today().isoformat()
    return (
        "---\n"
        f"verticale: {verticale}\n"
        f"language: {language}\n"
        f"source_url: {source_url}\n"
        f"last_updated: '{today}'\n"
        f"title: {title}\n"
        f"token_estimate: {token_estimate}\n"
        "---\n\n"
    )


def _estimate_tokens(text: str) -> int:
    return max(1, len(text) // 4)


def _upsert_manifest_entries(entries: list[dict[str, Any]]) -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    files = manifest.get("files", [])
    by_path = {item.get("path"): item for item in files if isinstance(item, dict)}

    for entry in entries:
        by_path[entry["path"]] = entry

    new_files = sorted(by_path.values(), key=lambda x: x["path"])
    manifest["files"] = new_files
    manifest["total_files"] = len(new_files)
    manifest["total_token_estimate"] = sum(
        int(item.get("token_estimate") or 0) for item in new_files
    )
    manifest["generated_at"] = dt.date.today().isoformat()

    by_verticale: dict[str, dict[str, Any]] = {}
    for item in new_files:
        verticale = item.get("verticale", "unknown")
        lang = item.get("language", "unknown")
        if verticale not in by_verticale:
            by_verticale[verticale] = {
                "files": 0,
                "token_estimate": 0,
                "languages": {},
            }
        by_verticale[verticale]["files"] += 1
        by_verticale[verticale]["token_estimate"] += int(item.get("token_estimate") or 0)
        by_verticale[verticale]["languages"][lang] = (
            by_verticale[verticale]["languages"].get(lang, 0) + 1
        )
    manifest["by_verticale"] = by_verticale
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_yesmilano_pages() -> list[dict[str, Any]]:
    manifest_entries: list[dict[str, Any]] = []
    for url in YESMILANO_URLS:
        source_url = url
        try:
            source_url, raw_html = _fetch_yesmilano_with_fallback(url)
            title = _extract_title(raw_html)
            text = _html_to_text(raw_html)
        except Exception:
            # Some YesMilano pages block bot traffic; keep a grounded pointer file
            # so retrieval can still cite official sources and topics.
            title = f"YesMilano source pointer: {url}"
            text = (
                "This source is part of the relocation knowledge base.\n\n"
                f"Official URL: {url}\n\n"
                "Relevant themes:\n"
                "- student life in Milan\n"
                "- accommodation and rents\n"
                "- cost of living\n"
                "- public transport and getting around\n"
                "- neighborhoods and local orientation\n"
            )
        token_est = _estimate_tokens(text)
        fm = _build_frontmatter(
            title=title,
            source_url=source_url,
            language="en",
            verticale="relocation",
            token_estimate=token_est,
        )
        filename = _slug_from_url(url)
        out_path = RELOCATION_DIR / filename
        out_path.write_text(fm + text + "\n", encoding="utf-8")

        rel = out_path.relative_to(DATA_DIR).as_posix()
        manifest_entries.append(
            {
                "path": rel,
                "verticale": "relocation",
                "language": "en",
                "title": title,
                "source_url": source_url,
                "last_updated": dt.date.today().isoformat(),
                "token_estimate": token_est,
                "size_bytes": out_path.stat().st_size,
            }
        )
    return manifest_entries


def _summarize_gtfs(zip_bytes: bytes) -> str:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    files = {name: zf.read(name) for name in zf.namelist()}

    summary_lines: list[str] = ["# Milano GTFS Snapshot", ""]
    summary_lines.append(f"- Source zip: {GTFS_ZIP_URL}")
    summary_lines.append(f"- Contains {len(files)} files")

    for name in sorted(files):
        if not name.endswith(".txt"):
            continue
        content = files[name].decode("utf-8", errors="ignore")
        rows = list(csv.reader(io.StringIO(content)))
        row_count = max(0, len(rows) - 1)
        cols = rows[0] if rows else []
        summary_lines.append(f"- `{name}`: {row_count} rows, {len(cols)} columns")

    def _peek(name: str, max_rows: int = 10) -> list[list[str]]:
        if name not in files:
            return []
        content = files[name].decode("utf-8", errors="ignore")
        rows = list(csv.reader(io.StringIO(content)))
        return rows[: max_rows + 1]

    for table_name in ["agency.txt", "routes.txt", "trips.txt", "stops.txt"]:
        rows = _peek(table_name, max_rows=8)
        if not rows:
            continue
        summary_lines.append("")
        summary_lines.append(f"## {table_name}")
        summary_lines.append("")
        headers = rows[0]
        summary_lines.append("| " + " | ".join(headers) + " |")
        summary_lines.append("| " + " | ".join(["---"] * len(headers)) + " |")
        for r in rows[1:]:
            clean = [c.replace("|", "/").strip()[:120] for c in r]
            clean += [""] * (len(headers) - len(clean))
            clean = clean[: len(headers)]
            summary_lines.append("| " + " | ".join(clean) + " |")

    return "\n".join(summary_lines).strip() + "\n"


def _write_gtfs_snapshot() -> dict[str, Any]:
    try:
        zip_bytes = _fetch_bytes(GTFS_ZIP_URL)
        body = _summarize_gtfs(zip_bytes)
    except Exception:
        body = (
            "# Milano GTFS Dataset Pointer\n\n"
            "Official dataset page:\n"
            f"- {GTFS_DATASET_URL}\n\n"
            "Direct GTFS zip:\n"
            f"- {GTFS_ZIP_URL}\n\n"
            "Notes:\n"
            "- Comune di Milano transport dataset (AMAT/ATM), typically updated every 2-4 weeks.\n"
            "- Useful for relocation answers about routes, stops, and mobility planning.\n"
            "- GTFS standard reference: https://developers.google.com/transit/gtfs/reference\n"
        )
    token_est = _estimate_tokens(body)
    title = "Milano public transport GTFS dataset snapshot"
    fm = _build_frontmatter(
        title=title,
        source_url=GTFS_DATASET_URL,
        language="it",
        verticale="relocation",
        token_estimate=token_est,
    )
    out_path = RELOCATION_DIR / "dati-comune-milano-it-gtfs-dataset-snapshot.md"
    out_path.write_text(fm + body, encoding="utf-8")
    rel = out_path.relative_to(DATA_DIR).as_posix()
    return {
        "path": rel,
        "verticale": "relocation",
        "language": "it",
        "title": title,
        "source_url": GTFS_DATASET_URL,
        "last_updated": dt.date.today().isoformat(),
        "token_estimate": token_est,
        "size_bytes": out_path.stat().st_size,
    }


def main() -> None:
    RELOCATION_DIR.mkdir(parents=True, exist_ok=True)
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"Manifest not found: {MANIFEST_PATH}")

    entries = _write_yesmilano_pages()
    entries.append(_write_gtfs_snapshot())
    _upsert_manifest_entries(entries)
    print(f"Added/updated {len(entries)} external-source files.")


if __name__ == "__main__":
    main()
