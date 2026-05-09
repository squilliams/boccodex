"""Trusted Sources Agent: search and ingest approved external sources.

Examples:
  python backend/scripts/trusted_sources_agent.py search --query relocation
  python backend/scripts/trusted_sources_agent.py ingest --source yesmilano-living
  python backend/scripts/trusted_sources_agent.py ingest --all
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import re
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

import rebuild_manifest

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
REGISTRY_PATH = DATA_DIR / "trusted_sources.json"
TIMEOUT_SECONDS = 30
VERTICALI = {"relocation", "life_on_campus", "study_abroad", "career_readiness"}


def _load_registry() -> list[dict[str, Any]]:
    payload = json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
    return payload.get("sources", [])


def _slug(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9]+", "-", value.lower()).strip("-")


def _fetch_text(url: str) -> str:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; BocconiTrustedSourcesAgent/1.0)",
            "Accept-Language": "en,it;q=0.9",
        },
    )
    with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
        return resp.read().decode("utf-8", errors="ignore")


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
    return "\n".join([ln for ln in lines if ln])


def _token_estimate(text: str) -> int:
    return max(1, len(text) // 4)


def _render_frontmatter(
    *,
    verticale: str,
    language: str,
    source_url: str,
    title: str,
    token_estimate: int,
) -> str:
    return (
        "---\n"
        f"verticale: {verticale}\n"
        f"language: {language}\n"
        f"source_url: {source_url}\n"
        f"last_updated: '{dt.date.today().isoformat()}'\n"
        f"title: {title}\n"
        f"token_estimate: {token_estimate}\n"
        "---\n\n"
    )


def _source_to_markdown(entry: dict[str, Any]) -> tuple[str, str, str]:
    source_type = entry.get("type", "html")
    source_url = entry["url"]
    configured_title = entry.get("title", entry["id"])
    tags = entry.get("tags", [])

    if source_type == "html":
        raw_html = _fetch_text(source_url)
        title = _extract_title(raw_html) or configured_title
        body = _html_to_text(raw_html)
        if len(body) < 120:
            body = (
                f"This source appears to block scraping from this environment.\n\n"
                f"Official URL: {source_url}\n"
            )
        return title, source_url, body

    if source_type == "binary_pointer":
        title = configured_title
        body = (
            "Binary dataset source pointer.\n\n"
            f"Official URL: {source_url}\n\n"
            "Use this as a grounded reference and ingest binary content with a dedicated parser.\n"
        )
        return title, source_url, body

    if source_type == "json":
        raw_json = _fetch_text(source_url)
        parsed = json.loads(raw_json)
        pretty = json.dumps(parsed, ensure_ascii=False, indent=2)
        body = f"```json\n{pretty[:12000]}\n```\n"
        return configured_title, source_url, body

    tags_line = ", ".join(tags) if tags else "none"
    body = (
        "Source pointer only.\n\n"
        f"Official URL: {source_url}\n"
        f"Tags: {tags_line}\n"
    )
    return configured_title, source_url, body


def _target_file(entry: dict[str, Any]) -> Path:
    verticale = entry["verticale"]
    source_id = _slug(entry["id"])
    netloc = urllib.parse.urlsplit(entry["url"]).netloc
    filename = f"external-{source_id}-{_slug(netloc)}.md"
    return DATA_DIR / verticale / filename


def cmd_search(args: argparse.Namespace) -> int:
    query = (args.query or "").strip().lower()
    rows = _load_registry()
    matches = []
    for row in rows:
        hay = " ".join(
            [
                row.get("id", ""),
                row.get("title", ""),
                row.get("verticale", ""),
                row.get("url", ""),
                " ".join(row.get("tags", [])),
            ]
        ).lower()
        if query in hay:
            matches.append(row)

    if not matches:
        print("No trusted sources matched.")
        return 0

    for m in matches:
        print(f"- {m['id']} | {m['verticale']} | {m['url']}")
    return 0


def cmd_ingest(args: argparse.Namespace) -> int:
    rows = _load_registry()
    selected: list[dict[str, Any]] = []
    if args.all:
        selected = rows
    elif args.source:
        selected = [r for r in rows if r.get("id") == args.source]
    else:
        raise ValueError("Use --all or --source <id>")

    if not selected:
        print("No sources selected.")
        return 1

    for entry in selected:
        verticale = entry.get("verticale")
        if verticale not in VERTICALI:
            print(f"Skipping invalid verticale source: {entry.get('id')}")
            continue

        out = _target_file(entry)
        out.parent.mkdir(parents=True, exist_ok=True)
        try:
            title, source_url, body = _source_to_markdown(entry)
        except Exception as exc:
            title = f"Source pointer: {entry.get('title', entry['id'])}"
            source_url = entry["url"]
            body = (
                "Could not fetch live content from this environment.\n\n"
                f"Official URL: {source_url}\n"
                f"Fetch error: {exc}\n"
            )

        token_est = _token_estimate(body)
        fm = _render_frontmatter(
            verticale=verticale,
            language=entry.get("language", "en"),
            source_url=source_url,
            title=title,
            token_estimate=token_est,
        )
        out.write_text(fm + body.strip() + "\n", encoding="utf-8")
        print(f"Wrote {out.relative_to(ROOT)}")

    rebuild_manifest.main()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Trusted sources search/ingest agent")
    sub = parser.add_subparsers(dest="command", required=True)

    p_search = sub.add_parser("search", help="Search curated trusted sources")
    p_search.add_argument("--query", required=True, help="Keyword query")
    p_search.set_defaults(func=cmd_search)

    p_ingest = sub.add_parser("ingest", help="Ingest one or all trusted sources to markdown")
    p_ingest.add_argument("--source", help="Single source id (e.g. yesmilano-living)")
    p_ingest.add_argument("--all", action="store_true", help="Ingest all sources")
    p_ingest.set_defaults(func=cmd_ingest)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
