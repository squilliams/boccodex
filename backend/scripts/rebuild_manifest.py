"""Rebuild backend/data/manifest.json from markdown files.

Usage:
  python backend/scripts/rebuild_manifest.py
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
MANIFEST_PATH = DATA_DIR / "manifest.json"
VERTICALI = ("relocation", "life_on_campus", "study_abroad", "career_readiness")

FRONTMATTER_RE = re.compile(r"^---\n(.*?)\n---\n", flags=re.DOTALL)
KV_RE = re.compile(r"^([A-Za-z0-9_]+):\s*(.*)$")


def _parse_frontmatter(text: str) -> dict[str, str]:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return {}
    raw = m.group(1)
    out: dict[str, str] = {}
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        km = KV_RE.match(line)
        if not km:
            continue
        key, value = km.group(1), km.group(2).strip()
        if (value.startswith("'") and value.endswith("'")) or (
            value.startswith('"') and value.endswith('"')
        ):
            value = value[1:-1]
        out[key] = value
    return out


def _strip_frontmatter(text: str) -> str:
    m = FRONTMATTER_RE.match(text)
    if not m:
        return text
    return text[m.end() :]


def _token_estimate(body: str) -> int:
    return max(1, len(body) // 4)


def _entry_from_md(fp: Path) -> dict[str, Any]:
    raw = fp.read_text(encoding="utf-8", errors="ignore")
    fm = _parse_frontmatter(raw)
    body = _strip_frontmatter(raw).strip()

    rel = fp.relative_to(DATA_DIR).as_posix()
    verticale = fm.get("verticale") or fp.parent.name
    if verticale not in VERTICALI:
        verticale = "relocation"

    language = fm.get("language", "en")
    title = fm.get("title", fp.stem)
    source_url = fm.get("source_url", "")
    last_updated = fm.get("last_updated", "")

    token_est = fm.get("token_estimate")
    try:
        token_est_int = int(token_est) if token_est is not None else _token_estimate(body)
    except ValueError:
        token_est_int = _token_estimate(body)

    return {
        "path": rel,
        "verticale": verticale,
        "language": language,
        "title": title,
        "source_url": source_url,
        "last_updated": last_updated,
        "token_estimate": token_est_int,
        "size_bytes": fp.stat().st_size,
    }


def _build_manifest(entries: list[dict[str, Any]]) -> dict[str, Any]:
    by_verticale: dict[str, dict[str, Any]] = {}
    total_tokens = 0
    for item in entries:
        total_tokens += int(item.get("token_estimate") or 0)
        v = item.get("verticale", "unknown")
        lang = item.get("language", "unknown")
        if v not in by_verticale:
            by_verticale[v] = {"files": 0, "token_estimate": 0, "languages": {}}
        by_verticale[v]["files"] += 1
        by_verticale[v]["token_estimate"] += int(item.get("token_estimate") or 0)
        by_verticale[v]["languages"][lang] = by_verticale[v]["languages"].get(lang, 0) + 1

    return {
        "generated_at": str(__import__("datetime").date.today()),
        "total_files": len(entries),
        "total_token_estimate": total_tokens,
        "by_verticale": by_verticale,
        "files": entries,
    }


def main() -> None:
    files = sorted(DATA_DIR.glob("*/*.md"))
    entries = [_entry_from_md(fp) for fp in files]
    manifest = _build_manifest(entries)
    MANIFEST_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Manifest rebuilt: {MANIFEST_PATH}")
    print(f"Files indexed: {manifest['total_files']}")


if __name__ == "__main__":
    main()
