"""Download reference utility interconnection PDFs listed in specs/SOURCES.md."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from urllib.parse import unquote, urlparse

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
SPECS_DIR = REPO_ROOT / "specs"
SOURCES_MD = SPECS_DIR / "SOURCES.md"

URL_RE = re.compile(r"https?://\S+")

KNOWN_FILENAMES: dict[str, str] = {
    "BB34AE143811453B8F5923C8E7C234BC.ashx": "PerformanceRequirementsLargeLoadsConnectedLIPASystem.pdf",
}


def parse_source_urls(sources_md: Path = SOURCES_MD) -> list[str]:
    text = sources_md.read_text(encoding="utf-8")
    urls: list[str] = []
    for raw in URL_RE.findall(text):
        url = raw.rstrip(".,;>")
        while url.endswith(")") and url.count(")") > url.count("("):
            url = url[:-1]
        urls.append(url)
    return urls


def target_filename_for_url(url: str) -> str:
    parsed = urlparse(url)
    raw_name = unquote(Path(parsed.path).name)
    if raw_name in KNOWN_FILENAMES:
        return KNOWN_FILENAMES[raw_name]
    if not raw_name.lower().endswith(".pdf"):
        # Handle arXiv PDF links like https://arxiv.org/pdf/2601.12686
        raw_name = f"{raw_name}.pdf" if raw_name else "document.pdf"
    return raw_name


def main() -> int:
    SPECS_DIR.mkdir(parents=True, exist_ok=True)
    urls = parse_source_urls()
    if not urls:
        print("No URLs found in specs/SOURCES.md")
        return 1

    existing = {p.name for p in SPECS_DIR.glob("*.pdf")}
    headers = {"User-Agent": "GridSafe-SpecFetcher/1.0"}

    for url in urls:
        fname = target_filename_for_url(url)
        stem = Path(fname).stem
        # Skip if exact filename or arXiv versioned equivalent (e.g., 2601.12686v1.pdf) already exists
        matches = [e for e in existing if e == fname or e.startswith(f"{stem}v")]
        if matches:
            print(f"[SKIP] {matches[0]} already present in specs/")
            continue

        dest = SPECS_DIR / fname
        print(f"[GET]  {url} -> {dest.relative_to(REPO_ROOT)}")
        with httpx.Client(follow_redirects=True, timeout=60.0, headers=headers) as client:
            resp = client.get(url)
            resp.raise_for_status()
            dest.write_bytes(resp.content)
        print(f"[OK]   saved {len(resp.content):,} bytes")

    return 0


if __name__ == "__main__":
    sys.exit(main())
